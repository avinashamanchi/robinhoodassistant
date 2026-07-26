from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml


def test_release_static_gate_passes_for_the_committed_runtime_sources():
    completed = subprocess.run(
        [sys.executable, "scripts/check_release_safety.py"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed.stdout.strip() == "release static checks: PASS"


def _static_fixture(tmp_path: Path) -> Path:
    root = tmp_path / "fixture"
    static = root / "src" / "trading_assistant" / "app" / "static"
    static.mkdir(parents=True)
    (root / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "trading": {"mode": "paper", "broker": "alpaca"},
                "features": {
                    "auto_execute_preapproved_rules": False,
                },
                "execution": {"prefer_bracket_orders": False},
                "llm": {"fallback_provider": None},
            }
        ),
        encoding="utf-8",
    )
    (root / "pyproject.toml").write_text("", encoding="utf-8")
    (root / "uv.lock").write_text("", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return root


@pytest.mark.parametrize(
    ("relative_path", "source", "expected"),
    [
        (
            "src/trading_assistant/runtime_schema.py",
            "schema_creator = Base.metadata.create_all\nschema_creator(engine)\n",
            "runtime create_all",
        ),
        (
            "src/trading_assistant/escape.py",
            "getattr(broker, 'submit_order')(request)\n",
            "broker submission",
        ),
        (
            "src/trading_assistant/escape.py",
            "send = broker.submit_order\nsend(request)\n",
            "broker submission",
        ),
        (
            "src/trading_assistant/app/static/index.html",
            "<input onfocus=\"steal()\">",
            "inline event handler",
        ),
        (
            "src/trading_assistant/app/static/app.js",
            "const view = `<div onmouseover='steal()'>safe</div>`;",
            "inline event handler",
        ),
    ],
)
def test_release_static_gate_rejects_negative_fixtures(
    tmp_path,
    relative_path,
    source,
    expected,
):
    root = _static_fixture(tmp_path)
    target = root / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/check_release_safety.py",
            "--root",
            str(root),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    assert expected in completed.stderr
