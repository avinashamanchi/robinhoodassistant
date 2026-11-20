"""AC idle-sleep guard with explicitly bounded temporary battery operation."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
import os
import re
import subprocess
import time


def parse_power(text):
    unknown = {"source": "unknown", "battery_percent": None}
    if not isinstance(text, str):
        return unknown
    source = re.search(r"^Now drawing from '(AC Power|Battery Power)'", text)
    charges = re.findall(r"(?<!\d)(\d{1,3})%;", text)
    if not source or len(charges) != 1 or not 0 <= int(charges[0]) <= 100:
        return unknown
    return {"source": "ac" if source[1] == "AC Power" else "battery",
            "battery_percent": int(charges[0])}


def _aware(value):
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("timezone-aware time required")
    return value.astimezone(timezone.utc)


def battery_request(now, minutes):
    if type(minutes) is not int or not 1 <= minutes <= 120:
        raise ValueError("battery awake duration must be 1 through 120 minutes")
    now = _aware(now)
    return {"issued_at": now.isoformat(), "expires_at": (now + timedelta(minutes=minutes)).isoformat()}


def guard_decision(snapshot, request, now):
    inactive = {"active": False, "mode": "released", "remaining_seconds": 0}
    try:
        now = _aware(now)
        charge = snapshot.get("battery_percent")
        if type(charge) is not int or not 0 <= charge <= 100:
            return inactive
        if snapshot.get("source") == "ac":
            return {"active": True, "mode": "ac", "remaining_seconds": None}
        if snapshot.get("source") != "battery" or charge <= 30:
            return inactive
        issued = _aware(datetime.fromisoformat(request["issued_at"]))
        expires = _aware(datetime.fromisoformat(request["expires_at"]))
        if not timedelta(0) < expires - issued <= timedelta(minutes=120) or not issued <= now < expires:
            return inactive
        return {"active": True, "mode": "temporary_battery",
                "remaining_seconds": math.ceil((expires - now).total_seconds())}
    except (TypeError, ValueError, KeyError, AttributeError):
        return inactive


class GuardController:
    def __init__(self, *, spawn=subprocess.Popen, owner_pid=None, monotonic=time.monotonic):
        self.spawn = spawn
        self.owner_pid = owner_pid or os.getpid()
        self.child = None
        self.identity = None
        self.monotonic = monotonic
        self.battery_identity = None
        self.battery_deadline = None

    def close(self):
        if self.child is not None:
            if self.child.poll() is None:
                self.child.terminate()
                try:
                    self.child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.child.kill()
                    self.child.wait(timeout=3)
            self.child = None
            self.identity = None

    def tick(self, snapshot, request, now):
        decision = guard_decision(snapshot, request, now)
        identity = (decision["mode"], request.get("expires_at") if decision["mode"] == "temporary_battery" else None)
        if decision["mode"] == "temporary_battery":
            if self.battery_identity != identity:
                self.battery_identity = identity
                self.battery_deadline = self.monotonic() + decision["remaining_seconds"]
            remaining = math.floor(self.battery_deadline - self.monotonic())
            decision["remaining_seconds"] = min(remaining, decision["remaining_seconds"])
            if remaining <= 0:
                decision.update(active=False, mode="released", remaining_seconds=0)
        if self.child is not None and (self.child.poll() is not None or self.identity != identity or not decision["active"]):
            self.close()
        if decision["active"] and self.child is None:
            args = ["/usr/bin/caffeinate", "-i", "-w", str(self.owner_pid)]
            if decision["mode"] == "ac":
                args.append("-s")
            else:
                args.extend(["-t", str(decision["remaining_seconds"])])
            try:
                self.child = self.spawn(args, stdin=subprocess.DEVNULL,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                        close_fds=True)
                self.identity = identity
            except OSError:
                self.child = None
        return {**snapshot, **decision, "guard_active": self.child is not None and self.child.poll() is None}
