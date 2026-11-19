"""Operator-only Mac power and read-only health observation; never submits orders."""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import signal
import sqlite3
import stat
import subprocess
import tempfile
import threading
import uuid

from trading_assistant.installation import require_designated
from trading_assistant.ops.operational_state import StateStore, StateError
from trading_assistant.ops.power_guard import GuardController, battery_request, parse_power

from trading_assistant.ops.incident_catalog import COMPONENTS, RECOVERY


def stamp(now):
    if now.utcoffset() is None:
        raise ValueError("timezone-aware time required")
    return now.astimezone(timezone.utc).isoformat()


def _date(value):
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=timezone.utc) if parsed.utcoffset() is None else parsed.astimezone(timezone.utc)


def record_checks(store, checks, now, *, cipher=None):
    at = stamp(now)
    checks = [dict(c) for c in checks]
    for check in checks:
        if (check.get("component") not in COMPONENTS
                or check.get("code") not in COMPONENTS[check["component"]]
                or check.get("status") not in {"pass", "warn", "fail", "unknown"}
                or set(check) != {"component", "code", "status"}):
            raise ValueError("unknown health check")
    def change(previous):
        from trading_assistant.ops.state_attestation import seal_doctor_health, verify_doctor_health
        def publish(value, *, previous_verified):
            if cipher is None:
                return value
            try:
                return seal_doctor_health(value, cipher, previous_verified=previous_verified)
            except Exception:
                # Preserve conservative failures, but never retain a stale tag.
                return {k: v for k, v in value.items() if k != "doctor_publication"}
        previous_verified = verify_doctor_health(previous, cipher)
        last = previous.get("checked_at")
        if last is not None and now <= _date(last):
            value = {**previous, "stale_observations": min(previous.get("stale_observations", 0) + 1, 1000000000)}
            return publish(value, previous_verified=True) if previous_verified else value
        incidents = previous.get("incidents", {})
        events = previous.get("events", [])
        gap = last is not None and not 0 <= (now - _date(last)).total_seconds() <= 180
        monitor_status = "fail" if gap else "unknown" if cipher is not None and not previous_verified else "pass"
        effective = checks + [{"component":"monitor", "status":monitor_status,
                               "code":"monitoring_gap" if gap else "monitoring_current"}]
        for check in effective:
            component = check["component"]
            old = incidents.get(component)
            if check["status"] in {"warn", "fail"}:
                if not old or old["state"] != "open" or old["code"] != check["code"]:
                    incident = {"id":uuid.uuid4().hex, "state":"open", "code":check["code"],
                                "opened_at":at, "last_seen_at":at}
                    incidents[component] = incident
                    events.append({"id":uuid.uuid4().hex, "incident_id":incident["id"],
                                   "component":component, "code":check["code"], "state":"open", "at":at})
                else:
                    old["last_seen_at"] = at
            elif check["status"] == "pass" and old and old["state"] == "open":
                old.update(state="resolved", resolved_at=at, last_seen_at=at)
                events.append({"id":uuid.uuid4().hex, "incident_id":old["id"],
                               "component":component, "code":check["code"], "state":"resolved", "at":at})
        value = {"schema_version":1, "checked_at":at, "checks":effective, "incidents":incidents,
                "stale_observations":previous.get("stale_observations", 0),
                "events":events[-100:], "discarded_events":previous.get("discarded_events",0)+max(0,len(events)-100)}
        if cipher is not None:
            value["legacy_history_unverified"] = bool(previous.get("legacy_history_unverified", False) or (previous and not previous_verified))
        return publish(value, previous_verified=previous_verified)
    return store.update("health", change)


def database_checks(root, now):
    components = ("database", "daemon", "safety", "reconciliation")
    unknown = [{"component":c,"status":"unknown","code":"database_unreadable"} for c in components]
    path = root / "trading_assistant.db"
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            return unknown
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)) as connection:
            connection.execute("PRAGMA query_only=ON")
            heartbeat = connection.execute("SELECT at FROM heartbeats WHERE source='daemon'").fetchone()
            safety = connection.execute("SELECT count(*) FROM circuit_breaker_state WHERE tripped=1").fetchone()[0]
            reconciliations = connection.execute("SELECT status,generation,completed_generation FROM startup_reconciliation_state").fetchall()
        age = (now - _date(heartbeat[0])).total_seconds() if heartbeat else None
        fresh = age is not None and 0 <= age <= 120
        clean = bool(reconciliations) and all(status == "current" and generation == completed for status,generation,completed in reconciliations)
        return [{"component":"database", "status":"pass", "code":"database_readable"},
                {"component":"daemon", "status":"pass" if fresh else "fail", "code":"heartbeat_fresh" if fresh else "heartbeat_stale"},
                {"component":"safety", "status":"fail" if safety else "pass", "code":"safety_latched" if safety else "safety_clear"},
                {"component":"reconciliation", "status":"pass" if clean else "fail", "code":"reconciliation_clean" if clean else "reconciliation_required"}]
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return unknown


def sample_power():
    try:
        with tempfile.TemporaryFile() as output:
            result = subprocess.run(["/usr/bin/pmset", "-g", "batt"], stdin=subprocess.DEVNULL,
                                    stdout=output, stderr=subprocess.DEVNULL, timeout=3,
                                    env={"PATH":"/usr/bin:/bin", "LC_ALL":"C"})
            output.seek(0)
            raw = output.read(4097)
        if result.returncode or len(raw) > 4096:
            return parse_power(None)
        return parse_power(raw.decode("utf-8"))
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return parse_power(None)


def job_check(*, runner=subprocess.run):
    try:
        for label in ("app","daemon","watchdog","backup","power","doctor","release-sync","alerts"):
            result = runner(["/bin/launchctl","print",f"gui/{os.getuid()}/com.trading.{label}"],
                            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                            stdin=subprocess.DEVNULL,timeout=3,check=False)
            if result.returncode == 113:
                return {"component":"jobs","status":"fail","code":"jobs_missing"}
            if result.returncode != 0:
                return {"component":"jobs","status":"unknown","code":"jobs_unknown"}
        return {"component":"jobs","status":"pass","code":"jobs_loaded"}
    except (OSError,subprocess.SubprocessError):
        return {"component":"jobs","status":"unknown","code":"jobs_unknown"}


def backup_check(root,now):
    try:
        from trading_assistant.ops.backup import list_committed_backups, read_encrypted_backup_header
        artifacts = list_committed_backups(root / ".local/encrypted-backups",artifact_label="whole-database-v1")
        dates = [_date(read_encrypted_backup_header(path)["created_at"]) for path in artifacts]
        recent = any(0 <= (now-date).total_seconds() <= 36*3600 for date in dates)
        return {"component":"backup","status":"pass" if recent else "fail", "code":"backup_recent" if recent else "backup_missing"}
    except Exception:
        return {"component":"backup","status":"unknown","code":"backup_unknown"}


def maintenance_state(root):
    try:
        fd = os.open(root / ".local/scheduled-backup.lock", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return "inactive"
    except OSError:
        return "unknown"
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o077:
            return "unknown"
        try:
            fcntl.flock(fd,fcntl.LOCK_SH | fcntl.LOCK_NB)
            return "inactive"
        except BlockingIOError:
            return "active"
    finally:
        os.close(fd)


def notification_check(store,now):
    destination = store.load("telegram")
    status,code = "warn","telegram_unpaired"
    if destination:
        if not destination.get("test_delivered_at"):
            code = "telegram_test_unconfirmed"
        else:
            receipt = store.load("telegram-status")
            try:
                fresh = 0 <= (now-_date(receipt["checked_at"])).total_seconds() <= 180
            except (KeyError,TypeError,ValueError):
                fresh = False
            if not fresh:
                status,code = "unknown","telegram_unknown"
            elif receipt.get("status") == "available":
                uncertain = any(value.get("state") == "unknown" for value in store.load("telegram-delivery").get("events",{}).values())
                status,code = ("warn","telegram_delivery_uncertain") if uncertain else ("pass","telegram_current")
            else:
                status,code = "fail","telegram_unavailable"
    return {"component":"notifications","status":status,"code":code}


def power_run(store, stop=None):
    stop = stop or threading.Event()
    controller = GuardController()
    try:
        with store.lease("power-owner"):
            while not stop.is_set():
                now = datetime.now(timezone.utc)
                result = controller.tick(sample_power(), store.load("battery-request"), now)
                store.update("power", lambda _: {**result,"checked_at":stamp(now)})
                stop.wait(15)
    finally:
        controller.close()


def doctor(root, store, now, *, cipher=None):
    with store.lease("doctor-owner"):
        previous = store.load("health")
        if previous.get("checked_at") and now <= _date(previous["checked_at"]):
            return record_checks(store, [], now, cipher=cipher)
        return _doctor(root, store, now, cipher=cipher)


def _doctor(root, store, now, *, cipher=None):
    maintenance = maintenance_state(root)
    if maintenance == "active":
        observed = store.update("maintenance",lambda old: old if old.get("active_since") else {"active_since":stamp(now)})
        elapsed = (now - _date(observed["active_since"])).total_seconds()
        stalled = not 0 <= elapsed <= 900
        return record_checks(store,[{"component":"maintenance", "status":"fail" if stalled else "pass",
                                     "code":"backup_stalled" if stalled else "backup_running"}],now,cipher=cipher)
    store.update("maintenance",lambda _: {})
    checks = database_checks(root, now)
    checks.append({"component":"maintenance","status":"warn" if maintenance == "unknown" else "pass",
                   "code":"maintenance_unknown" if maintenance == "unknown" else "maintenance_idle"})
    checks.extend([job_check(),backup_check(root,now),notification_check(store,now)])
    power = store.load("power")
    fresh = power.get("checked_at") and 0 <= (now - _date(power["checked_at"])).total_seconds() <= 45
    code = "power_unknown"
    status = "unknown"
    if fresh:
        if power.get("guard_active") and power.get("mode") == "ac":
            code,status = "ac_awake","pass"
        elif power.get("guard_active") and power.get("mode") == "temporary_battery":
            code,status = "battery_awake","warn"
        else:
            code,status = "awake_released","warn"
    checks.append({"component":"power","status":status,"code":code})
    try:
        from trading_assistant.config import load_config
        from trading_assistant.ops.watchdog import fetch_health
        from trading_assistant.security.outbound import build_local_liveness_transport
        transport = build_local_liveness_transport(load_config().server.tls_ca_path)
        live = fetch_health("https://localhost:8020/health/live", transport=transport) is not None
    except Exception:
        live = False
    checks.append({"component":"app","status":"pass" if live else "fail", "code":"app_live" if live else "app_unreachable"})
    try:
        from trading_assistant.ops.release_evidence import clean_commit, read_report, validate_report
        commit = clean_commit(root)
        validate_report(read_report(root / ".local/verification/release-results.json"), expected_commit=commit, root=root)
        verified = True
    except Exception:
        verified = False
    checks.append({"component":"release","status":"pass" if verified else "warn", "code":"release_verified" if verified else "release_unverified"})
    return record_checks(store, checks, now, cipher=cipher)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command",required=True)
    battery = commands.add_parser("battery"); battery.add_argument("--minutes",type=int,default=120)
    commands.add_parser("release-battery")
    commands.add_parser("power-run")
    commands.add_parser("doctor")
    commands.add_parser("status")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    try:
        require_designated(root)
        store = StateStore(root)
        now = datetime.now(timezone.utc)
        if args.command == "battery":
            result = store.update("battery-request", lambda _: battery_request(now,args.minutes))
        elif args.command == "release-battery":
            result = store.update("battery-request", lambda _: {})
        elif args.command == "power-run":
            stop = threading.Event()
            for sig in (signal.SIGINT,signal.SIGTERM):
                signal.signal(sig, lambda *_: stop.set())
            power_run(store,stop)
            return 0
        elif args.command == "doctor":
            try:
                from trading_assistant.config import load_config
                from trading_assistant.security.secrets import load_role_secrets
                from trading_assistant.security.crypto import build_sensitive_data_cipher
                config = load_config()
                cipher = build_sensitive_data_cipher(config.encryption, load_role_secrets("backup", config=config))
            except Exception:
                cipher = None
            result = doctor(root,store,now,cipher=cipher)
        else:
            result = {"power":store.load("power"),"health":store.load("health")}
        print(json.dumps(result,sort_keys=True))
        return 0
    except (StateError, ValueError, OSError):
        print('{"status":"unavailable","code":"operational_check_failed"}')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
