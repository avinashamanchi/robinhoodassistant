from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml


_STATIC = Path("src/trading_assistant/app/static")


def test_release_static_gate_passes_for_the_committed_runtime_sources():
    completed = subprocess.run(
        [sys.executable, "scripts/check_release_safety.py"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed.stdout.strip() == "release static checks: PASS"


def test_operator_cockpit_exposes_broker_truth_contract():
    html = (_STATIC / "index.html").read_text(encoding="utf-8")
    script = (_STATIC / "js" / "index.js").read_text(encoding="utf-8")
    required_ids = (
        "proof-broker",
        "proof-market",
        "proof-data",
        "proof-daemon",
        "proof-reconciliation",
        "account-status",
        "account-equity",
        "account-buying-power",
        "account-cash",
        "account-exposure",
    )

    for element_id in required_ids:
        assert html.count(f'id="{element_id}"') == 1

    assert (
        "account: {requestSequence: 0, controller: null, timeoutId: null}"
        in script
    )
    assert 'api("/account"' in script
    assert "async function refreshAccount()" in script
    assert "renderPositions(payload.positions)" in script
    refresh_all = script.split("async function refreshAll()", 1)[1].split(
        "async function initialize()",
        1,
    )[0]
    assert "refreshPositions()" not in refresh_all


def test_private_runtime_artifacts_are_gitignored():
    rules = Path(".gitignore").read_text(encoding="utf-8")

    assert "*.db.*.pre-migration.bak" in rules
    assert "*.db.submission.lock*" in rules


def _static_fixture(tmp_path: Path) -> Path:
    root = tmp_path / "fixture"
    static = root / "src" / "trading_assistant" / "app" / "static"
    static.mkdir(parents=True)
    app = static.parent
    (app / "policy.py").write_text(
        "ROUTE_POLICIES = (\n"
        "    RoutePolicy('GET', '/covered', AuthLevel.PUBLIC, 'read'),\n"
        ")\n",
        encoding="utf-8",
    )
    (app / "main.py").write_text(
        "@app.get('/covered')\n"
        "def covered():\n"
        "    return None\n",
        encoding="utf-8",
    )
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


@pytest.mark.parametrize(
    ("relative_path", "source", "expected"),
    [
        (
            "src/trading_assistant/app/main.py",
            "@app.get('/covered')\n"
            "def covered():\n"
            "    return None\n\n"
            "@app.post('/uncovered')\n"
            "def uncovered():\n"
            "    return None\n",
            "route missing from ROUTE_POLICIES: POST /uncovered",
        ),
        (
            "src/trading_assistant/app/main.py",
            "@app.get('/covered')\n"
            "def covered():\n"
            "    return None\n\n"
            "@app.api_route('/uncovered', methods=['PUT', 'DELETE'])\n"
            "def uncovered():\n"
            "    return None\n",
            "route missing from ROUTE_POLICIES: DELETE /uncovered",
        ),
        (
            "src/trading_assistant/unsafe_backend.py",
            "AnthropicBackend('key', 'model', 1)\n",
            "raw LLM backend construction outside factory: "
            "src/trading_assistant/unsafe_backend.py:1",
        ),
        (
            "src/trading_assistant/unsafe_backend.py",
            "GeminiBackend('key', 'model')\n",
            "raw LLM backend construction outside factory: "
            "src/trading_assistant/unsafe_backend.py:1",
        ),
        (
            "src/trading_assistant/unsafe_backend.py",
            "GroqBackend('key', 'model')\n",
            "raw LLM backend construction outside factory: "
            "src/trading_assistant/unsafe_backend.py:1",
        ),
        (
            "src/trading_assistant/unsafe_limiter.py",
            "from trading_assistant.app.ratelimit import RateLimiter\n",
            "deleted RateLimiter import: "
            "src/trading_assistant/unsafe_limiter.py:1",
        ),
    ],
)
def test_release_static_gate_rejects_policy_omission_fixtures(
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
