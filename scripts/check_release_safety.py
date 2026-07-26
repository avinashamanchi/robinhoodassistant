#!/usr/bin/env python3
"""Fail closed when release-critical source defaults or paths drift."""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parent.parent
RUNTIME = ROOT / "src" / "trading_assistant"
STATIC = RUNTIME / "app" / "static"


def _fail(message: str) -> None:
    raise RuntimeError(message)


def _check_config() -> None:
    config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    expected = {
        "trading.mode": config["trading"]["mode"] == "paper",
        "trading.broker": config["trading"]["broker"] == "alpaca",
        "features.auto_execute_preapproved_rules": (
            config["features"]["auto_execute_preapproved_rules"] is False
        ),
        "execution.prefer_bracket_orders": (
            config["execution"]["prefer_bracket_orders"] is False
        ),
        "llm.fallback_provider": config["llm"]["fallback_provider"] is None,
    }
    failed = [name for name, safe in expected.items() if not safe]
    if failed:
        _fail("unsafe config defaults: " + ", ".join(failed))


def _check_no_runtime_create_all() -> None:
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in RUNTIME.rglob("*.py")
        if "Base.metadata.create_all(" in path.read_text(encoding="utf-8")
    ]
    if offenders:
        _fail("runtime create_all calls: " + ", ".join(offenders))


def _check_submission_paths() -> None:
    allowed = {
        "src/trading_assistant/orders/submission.py",
        "src/trading_assistant/broker/alpaca.py",
        "src/trading_assistant/broker/mock.py",
        "src/trading_assistant/backtest/engine.py",
        "src/trading_assistant/backtest/sim_broker.py",
        # A BrokerClient decorator. Its delegate is invoked only by the real
        # OrderSubmissionService so the drill can simulate response loss.
        "src/trading_assistant/ops/safety_drill.py",
    }
    offenders: list[str] = []
    for path in RUNTIME.rglob("*.py"):
        relative = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in {"submit_order", "submit_bracket"}:
                continue
            allowed_call = relative in allowed
            if relative == "src/trading_assistant/ops/safety_drill.py":
                delegate = node.func.value
                allowed_call = (
                    node.func.attr == "submit_order"
                    and isinstance(delegate, ast.Attribute)
                    and delegate.attr == "_broker"
                    and isinstance(delegate.value, ast.Name)
                    and delegate.value.id == "self"
                )
            if not allowed_call:
                offenders.append(f"{relative}:{node.lineno}")
    if offenders:
        _fail("broker submission outside approved paths: " + ", ".join(offenders))


def _check_browser_sources() -> None:
    forbidden = {
        "localStorage": re.compile(r"\blocalStorage\b"),
        "X-API-Key": re.compile(r"\bX-API-Key\b", re.IGNORECASE),
    }
    offenders: list[str] = []
    for path in STATIC.rglob("*"):
        if path.suffix not in {".html", ".js"}:
            continue
        text = path.read_text(encoding="utf-8")
        for name, pattern in forbidden.items():
            if pattern.search(text):
                offenders.append(f"{path.relative_to(ROOT).as_posix()}:{name}")
        event = r"on(?:click|change|submit|load|error|input|key(?:down|up))"
        inline_pattern = (
            re.compile(rf"\s{event}\s*=", re.IGNORECASE)
            if path.suffix == ".html"
            else re.compile(
                rf"(?:\.\s*{event}\s*=|<[^>]+\s{event}\s*=)",
                re.IGNORECASE,
            )
        )
        if inline_pattern.search(text):
            offenders.append(
                f"{path.relative_to(ROOT).as_posix()}:inline event handler"
            )
    if offenders:
        _fail("unsafe browser source: " + ", ".join(offenders))


def _check_no_unofficial_robinhood_dependency() -> None:
    paths = [
        ROOT / "pyproject.toml",
        ROOT / "uv.lock",
        *RUNTIME.rglob("*.py"),
    ]
    pattern = re.compile(r"robin[_-]?stocks|robinhood[_-]?api", re.IGNORECASE)
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in paths
        if pattern.search(path.read_text(encoding="utf-8"))
    ]
    if offenders:
        _fail("unofficial Robinhood dependency/import: " + ", ".join(offenders))


def _check_no_tracked_secret_files() -> None:
    tracked = subprocess.check_output(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
    ).decode().split("\0")
    secret_names = {".env", ".env.local", "id_rsa", "id_ed25519"}
    offenders = [
        name
        for name in tracked
        if name and (Path(name).name in secret_names or Path(name).suffix in {".pem", ".p12"})
    ]
    if offenders:
        _fail("tracked secret-bearing file: " + ", ".join(offenders))


def main() -> int:
    checks = (
        _check_config,
        _check_no_runtime_create_all,
        _check_submission_paths,
        _check_browser_sources,
        _check_no_unofficial_robinhood_dependency,
        _check_no_tracked_secret_files,
    )
    try:
        for check in checks:
            check()
    except (KeyError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"release static checks: FAIL ({exc})", file=sys.stderr)
        return 1
    print("release static checks: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
