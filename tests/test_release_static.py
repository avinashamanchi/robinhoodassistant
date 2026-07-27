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


def _static_fixture(
    tmp_path: Path,
    *,
    policy_source: str | None = None,
) -> Path:
    root = tmp_path / "fixture"
    static = root / "src" / "trading_assistant" / "app" / "static"
    static.mkdir(parents=True)
    app = static.parent
    (app / "policy.py").write_text(
        policy_source
        or (
            "ROUTE_POLICIES = (\n"
            "    RoutePolicy('GET', '/covered', AuthLevel.PUBLIC, 'read'),\n"
            ")\n"
        ),
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
            "raw LLM backend reference outside factory: "
            "src/trading_assistant/unsafe_backend.py:1",
        ),
        (
            "src/trading_assistant/unsafe_backend.py",
            "GeminiBackend('key', 'model')\n",
            "raw LLM backend reference outside factory: "
            "src/trading_assistant/unsafe_backend.py:1",
        ),
        (
            "src/trading_assistant/unsafe_backend.py",
            "GroqBackend('key', 'model')\n",
            "raw LLM backend reference outside factory: "
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


@pytest.mark.parametrize(
    ("relative_path", "source", "expected", "policy_source"),
    [
        (
            "src/trading_assistant/app/main.py",
            "expose = app.post\n\n"
            "@expose('/uncovered')\n"
            "def uncovered():\n"
            "    return None\n",
            "route missing from ROUTE_POLICIES: POST /uncovered",
            None,
        ),
        (
            "src/trading_assistant/app/routers/unsafe.py",
            "@router.route('/uncovered', methods=['DELETE'])\n"
            "def uncovered():\n"
            "    return None\n",
            "route missing from ROUTE_POLICIES: DELETE /uncovered",
            None,
        ),
        (
            "src/trading_assistant/app/main.py",
            "@expose('/uncovered')\n"
            "def uncovered():\n"
            "    return None\n",
            "unresolved route decorator: "
            "src/trading_assistant/app/main.py:2",
            None,
        ),
        (
            "src/trading_assistant/app/main.py",
            "@app.get('/covered')\n"
            "def covered():\n"
            "    return None\n",
            "route missing from ROUTE_POLICIES: GET /covered",
            "ROUTE_POLICIES = (\n"
            "    RoutePolicy('GET', 'covered', AuthLevel.PUBLIC, 'read'),\n"
            ")\n",
        ),
        (
            "src/trading_assistant/app/main.py",
            "@app.get('//covered')\n"
            "def covered():\n"
            "    return None\n",
            "route missing from ROUTE_POLICIES: GET //covered",
            None,
        ),
        (
            "src/trading_assistant/unsafe_backend.py",
            "def make(ctor=GroqBackend):\n"
            "    return ctor()\n",
            "raw LLM backend reference outside factory: "
            "src/trading_assistant/unsafe_backend.py:1",
            None,
        ),
        (
            "src/trading_assistant/unsafe_backend.py",
            "constructors = [GeminiBackend]\n"
            "constructors[0]()\n",
            "raw LLM backend reference outside factory: "
            "src/trading_assistant/unsafe_backend.py:1",
            None,
        ),
        (
            "src/trading_assistant/unsafe_backend.py",
            "from trading_assistant.llm.anthropic_backend import AnthropicBackend\n",
            "raw LLM backend module import outside factory: "
            "src/trading_assistant/unsafe_backend.py:1",
            None,
        ),
        (
            "src/trading_assistant/unsafe_limiter.py",
            "import trading_assistant.app.ratelimit as old\n"
            "old.RateLimiter()\n",
            "deleted RateLimiter module import: "
            "src/trading_assistant/unsafe_limiter.py:1",
            None,
        ),
        (
            "src/trading_assistant/unsafe_limiter.py",
            "value = RateLimiter\n",
            "deleted RateLimiter reference: "
            "src/trading_assistant/unsafe_limiter.py:1",
            None,
        ),
        (
            "src/trading_assistant/unsafe_limiter.py",
            "old.RateLimiter()\n",
            "deleted RateLimiter reference: "
            "src/trading_assistant/unsafe_limiter.py:1",
            None,
        ),
    ],
)
def test_release_static_gate_rejects_fix_round_one_bypasses(
    tmp_path,
    relative_path,
    source,
    expected,
    policy_source,
):
    root = _static_fixture(tmp_path, policy_source=policy_source)
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
            "@decorators.expose('/uncovered')\n"
            "def uncovered():\n"
            "    return None\n",
            "unresolved route decorator: "
            "src/trading_assistant/app/main.py:2",
        ),
        (
            "src/trading_assistant/unsafe_backend.py",
            "from trading_assistant.llm.groq_backend import *\n"
            "globals()['GroqBackend']()\n",
            "unproven wildcard import: "
            "src/trading_assistant/unsafe_backend.py:1",
        ),
        (
            "src/trading_assistant/unsafe_backend.py",
            "from trading_assistant.llm.groq_backend import BACKENDS\n"
            "BACKENDS['groq']()\n",
            "raw LLM backend module import outside factory: "
            "src/trading_assistant/unsafe_backend.py:1",
        ),
        (
            "src/trading_assistant/unsafe_limiter.py",
            "from trading_assistant.app import ratelimit as old\n"
            "old.RateLimiter()\n",
            "deleted RateLimiter module import: "
            "src/trading_assistant/unsafe_limiter.py:1",
        ),
    ],
)
def test_release_static_gate_rejects_fix_round_two_bypasses(
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
    ("source", "expected"),
    [
        (
            "from trading_assistant.llm import groq_backend as provider\n"
            "vars(provider)['GroqBackend']()\n",
            "raw LLM backend module import outside factory: "
            "src/trading_assistant/unsafe_backend.py:1",
        ),
        (
            "from .llm import gemini_backend as provider\n"
            "vars(provider)['GeminiBackend']()\n",
            "raw LLM backend module import outside factory: "
            "src/trading_assistant/unsafe_backend.py:1",
        ),
    ],
)
def test_release_static_gate_rejects_parent_provider_module_imports(
    tmp_path,
    source,
    expected,
):
    root = _static_fixture(tmp_path)
    target = root / "src" / "trading_assistant" / "unsafe_backend.py"
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


@pytest.mark.parametrize("route_path", ("/covered/", "//covered"))
def test_release_static_gate_accepts_exact_noncanonical_route_paths(
    tmp_path,
    route_path,
):
    policy_source = (
        "ROUTE_POLICIES = (\n"
        f"    RoutePolicy('GET', '{route_path}', AuthLevel.PUBLIC, 'read'),\n"
        ")\n"
    )
    root = _static_fixture(tmp_path, policy_source=policy_source)
    (root / "src" / "trading_assistant" / "app" / "main.py").write_text(
        f"@app.get('{route_path}')\n"
        "def covered():\n"
        "    return None\n",
        encoding="utf-8",
    )

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

    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_release_static_gate_rejects_conflicting_route_aliases(tmp_path):
    root = _static_fixture(tmp_path)
    (root / "src" / "trading_assistant" / "app" / "main.py").write_text(
        "expose = app.post\n"
        "expose = app.get\n\n"
        "@expose('/covered')\n"
        "def covered():\n"
        "    return None\n",
        encoding="utf-8",
    )

    try:
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
            timeout=1,
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(f"checker did not terminate: {exc}")

    assert completed.returncode == 1
    assert (
        "conflicting route decorator alias: "
        "src/trading_assistant/app/main.py:2"
    ) in completed.stderr


def test_release_static_gate_ignores_backend_and_rate_limiter_text(tmp_path):
    root = _static_fixture(tmp_path)
    target = root / "src" / "trading_assistant" / "safe_text.py"
    target.write_text(
        "# GroqBackend RateLimiter\n"
        "message = 'GeminiBackend and RateLimiter are text'\n",
        encoding="utf-8",
    )

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

    assert completed.returncode == 0, completed.stdout + completed.stderr
