from __future__ import annotations

from dataclasses import FrozenInstanceError
import hashlib
import importlib.util
import os
import re
import resource
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


_STATIC = Path("src/trading_assistant/app/static")
_TRUSTED_ANCESTRY_ANCHORS: dict[Path, str] = {}


def test_release_static_gate_passes_for_the_committed_runtime_sources():
    inherited_output_ceiling = 64 * 1024 * 1024

    def limit_output_files() -> None:
        resource.setrlimit(
            resource.RLIMIT_FSIZE,
            (inherited_output_ceiling, inherited_output_ceiling),
        )

    completed = subprocess.run(
        [sys.executable, "scripts/check_release_safety.py"],
        check=False,
        capture_output=True,
        text=True,
        preexec_fn=limit_output_files,
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
    root = _trust_fixture(tmp_path)
    app = root / "src" / "trading_assistant" / "app"
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
        "from fastapi import FastAPI\n\n"
        "def create_app():\n"
        "    app = FastAPI()\n"
        "    @app.get('/covered')\n"
        "    def covered():\n"
        "        return None\n"
        "    return app\n",
        encoding="utf-8",
    )
    return root


def _write_static_mutation(
    root: Path,
    relative_path: str,
    source: str,
) -> None:
    target = root / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    if relative_path == "src/trading_assistant/app/main.py":
        indented = "".join(
            f"    {line}" if line.strip() else line
            for line in source.splitlines(keepends=True)
        )
        source = (
            "from fastapi import FastAPI\n\n"
            "def create_app():\n"
            "    app = FastAPI()\n"
            f"{indented}"
            "    return app\n"
        )
    elif relative_path.startswith(
        "src/trading_assistant/app/routers/"
    ):
        source = (
            "from fastapi import APIRouter\n"
            "router = APIRouter()\n"
            f"{source}"
        )
        (root / "src/trading_assistant/app/main.py").write_text(
            "from fastapi import FastAPI\n"
            "from .routers.unsafe import router\n\n"
            "def create_app():\n"
            "    app = FastAPI()\n"
            "    app.include_router(router)\n"
            "    @app.get('/covered')\n"
            "    def covered():\n"
            "        return None\n"
            "    return app\n",
            encoding="utf-8",
        )
    target.write_text(source, encoding="utf-8")


def _legacy_expected_code(expected: str) -> str:
    mappings = (
        ("runtime create_all", "RUNTIME_SCHEMA_MUTATION"),
        ("broker submission", "BROKER_SUBMISSION_PATH_UNAPPROVED"),
        ("sensitive field write bypass", "PLAINTEXT_SENSITIVE_WRITE"),
        ("inline event handler", "UNSAFE_BROWSER_SOURCE"),
        ("raw broker escape", "RAW_BROKER_ESCAPE"),
        ("plaintext operational backup", "PLAINTEXT_BACKUP_SURFACE"),
        ("route ", "ROUTE_REGISTRATION_UNPROVEN"),
        ("route mount", "ROUTE_REGISTRATION_UNPROVEN"),
        ("imperative HTTP", "ROUTE_REGISTRATION_UNPROVEN"),
        ("websocket route", "ROUTE_REGISTRATION_UNPROVEN"),
        ("unresolved route", "ROUTE_REGISTRATION_UNPROVEN"),
        ("conflicting route", "ROUTE_REGISTRATION_UNPROVEN"),
        ("raw LLM", "LLM_CONSTRUCTION_UNPROVEN"),
        ("direct LLM", "LLM_CONSTRUCTION_UNPROVEN"),
        ("unproven wildcard", "LLM_CONSTRUCTION_UNPROVEN"),
        ("deleted RateLimiter", "DELETED_RATE_LIMITER_REFERENCE"),
    )
    for marker, code in mappings:
        if marker in expected:
            return code
    raise AssertionError(f"legacy expectation has no stable code: {expected}")


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
            "src/trading_assistant/sensitive_escape.py",
            "from trading_assistant.db.models import AuditEvent\n"
            "event = AuditEvent(reason='plain')\n",
            "sensitive field write bypass",
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
    _write_static_mutation(root, relative_path, source)

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert _legacy_expected_code(expected) in completed.stderr


@pytest.mark.parametrize(
    ("relative_path", "source", "expected"),
    [
        (
            "src/trading_assistant/sensitive_cte_escape.py",
            "from sqlalchemy import text\n"
            "session.execute(text('WITH x AS (SELECT 1) UPDATE "
            "audit_events SET reason=:reason'))\n",
            "sensitive field write bypass",
        ),
        (
            "src/trading_assistant/sensitive_dynamic_escape.py",
            "table = 'audit_events'\n"
            "session.execute(f'UPDATE {table} SET reason=:reason')\n",
            "sensitive field write bypass",
        ),
        (
            "src/trading_assistant/sensitive_driver_escape.py",
            "connection.exec_driver_sql("
            "'UPDATE audit_events SET detail_json=:value')\n",
            "sensitive field write bypass",
        ),
        (
            "src/trading_assistant/sensitive_delete_escape.py",
            "from sqlalchemy import text\n"
            "session.execute(text("
            "'WITH chosen AS (SELECT 1) DELETE FROM audit_events'))\n",
            "sensitive field write bypass",
        ),
        (
            "src/trading_assistant/sensitive_untyped_escape.py",
            "from sqlalchemy import select\n"
            "from trading_assistant.db.models import AuditEvent\n"
            "row = session.scalar(select(AuditEvent))\n"
            "row.reason = 'plain'\n",
            "sensitive field write bypass",
        ),
        (
            "src/trading_assistant/raw_broker_escape.py",
            "def escape(guarded):\n"
            "    return guarded._broker\n",
            "raw broker escape",
        ),
        (
            "src/trading_assistant/raw_sdk_escape.py",
            "def escape(broker):\n"
            "    return broker._trading.cancel_order_by_id('id')\n",
            "raw broker escape",
        ),
        (
            "src/trading_assistant/ops/backup.py",
            "import sqlite3\n"
            "def backup_database(source, destination):\n"
            "    target = destination / 'trading-assistant-copy.sqlite3'\n"
            "    with sqlite3.connect(target) as backup:\n"
            "        return target\n",
            "plaintext operational backup entrypoint",
        ),
        (
            "README.md",
            "Retain trading-assistant-*.sqlite3 as the nightly backup.\n",
            "plaintext operational backup entrypoint",
        ),
    ],
)
def test_release_static_gate_rejects_task6_review_bypasses(
    tmp_path,
    relative_path,
    source,
    expected,
):
    root = _static_fixture(tmp_path)
    _write_static_mutation(root, relative_path, source)

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert _legacy_expected_code(expected) in completed.stderr


@pytest.mark.parametrize(
    ("relative_path", "source", "expected"),
    [
        (
            "src/trading_assistant/app/main.py",
            "app.mount('/plugin', plugin)\n",
            "non-allowlisted route mount: "
            "src/trading_assistant/app/main.py:1",
        ),
        (
            "src/trading_assistant/app/main.py",
            "app.add_api_route('/imperative', endpoint, methods=['GET'])\n",
            "imperative HTTP route registration: "
            "src/trading_assistant/app/main.py:1",
        ),
        (
            "src/trading_assistant/app/routers/unsafe.py",
            "@router.websocket('/socket')\n"
            "async def socket(websocket):\n"
            "    return None\n",
            "websocket route registration: "
            "src/trading_assistant/app/routers/unsafe.py:2",
        ),
        (
            "src/trading_assistant/app/routers/unsafe.py",
            "router.add_websocket_route('/socket', endpoint)\n",
            "websocket route registration: "
            "src/trading_assistant/app/routers/unsafe.py:1",
        ),
        (
            "src/trading_assistant/unsafe_llm.py",
            "from trading_assistant.llm.factory import _make_backend\n"
            "_make_backend(provider, config, secrets)\n",
            "raw LLM factory helper reference outside factory: "
            "src/trading_assistant/unsafe_llm.py:1",
        ),
        (
            "src/trading_assistant/unsafe_llm.py",
            "backend = build_llm_backend(config, secrets)\n"
            "backend.delegate.create()\n",
            "direct LLM delegate access outside wrapper: "
            "src/trading_assistant/unsafe_llm.py:2",
        ),
        (
            "src/trading_assistant/llm/factory.py",
            "def _make_backend(provider, config, secrets):\n"
            "    return object()\n",
            "raw LLM constructor helper exposed by factory: "
            "src/trading_assistant/llm/factory.py:1",
        ),
    ],
)
def test_release_static_gate_rejects_final_review_escape_paths(
    tmp_path,
    relative_path,
    source,
    expected,
):
    root = _static_fixture(tmp_path)
    _write_static_mutation(root, relative_path, source)

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
    assert _legacy_expected_code(expected) in completed.stderr


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
    _write_static_mutation(root, relative_path, source)

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
    assert _legacy_expected_code(expected) in completed.stderr


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
    _write_static_mutation(root, relative_path, source)

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
    assert _legacy_expected_code(expected) in completed.stderr


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
    _write_static_mutation(root, relative_path, source)

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
    assert _legacy_expected_code(expected) in completed.stderr


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
    assert _legacy_expected_code(expected) in completed.stderr


@pytest.mark.parametrize(
    ("relative_path", "source", "expected"),
    [
        (
            "src/trading_assistant/llm/unsafe_backend.py",
            "from . import groq_backend as provider\n"
            "vars(provider)['GroqBackend']()\n",
            "raw LLM backend module import outside factory: "
            "src/trading_assistant/llm/unsafe_backend.py:1",
        ),
        (
            "src/trading_assistant/llm/nested/unsafe_backend.py",
            "from .. import gemini_backend as backend_alias\n"
            "vars(backend_alias)['GeminiBackend']()\n",
            "raw LLM backend module import outside factory: "
            "src/trading_assistant/llm/nested/unsafe_backend.py:1",
        ),
    ],
)
def test_release_static_gate_rejects_sibling_relative_provider_imports(
    tmp_path,
    relative_path,
    source,
    expected,
):
    root = _static_fixture(tmp_path)
    _write_static_mutation(root, relative_path, source)

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
    assert _legacy_expected_code(expected) in completed.stderr


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
    _write_static_mutation(
        root,
        "src/trading_assistant/app/main.py",
        f"@app.get('{route_path}')\n"
        "def covered():\n"
        "    return None\n",
    )

    completed = _run_trust_gate(root)

    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_release_static_gate_rejects_conflicting_route_aliases(tmp_path):
    root = _static_fixture(tmp_path)
    _write_static_mutation(
        root,
        "src/trading_assistant/app/main.py",
        "expose = app.post\n"
        "expose = app.get\n\n"
        "@expose('/covered')\n"
        "def covered():\n"
        "    return None\n",
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
    assert "ROUTE_REGISTRATION_UNPROVEN" in completed.stderr


def test_release_static_gate_ignores_backend_and_rate_limiter_text(tmp_path):
    root = _static_fixture(tmp_path)
    target = root / "src" / "trading_assistant" / "safe_text.py"
    target.write_text(
        "# GroqBackend RateLimiter\n"
        "message = 'GeminiBackend and RateLimiter are text'\n",
        encoding="utf-8",
    )

    completed = _run_trust_gate(root)

    assert completed.returncode == 0, completed.stdout + completed.stderr


# ── Task 11 trust-boundary release gate ────────────────────────────────

_READ_TOOLS = (
    "get_market_data",
    "get_account_summary",
    "get_open_orders",
    "get_order_status",
    "list_rules",
)
_DRAFT_TOOLS = (
    "draft_order_candidate",
    "draft_rule_candidate",
)


def _write_fixture_file(root: Path, relative: str, source: str) -> Path:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8")
    return target


def _agent_fixture_source() -> str:
    specs = ",\n".join(
        f"    {{'name': {name!r}, 'input_schema': {{'type': 'object'}}}}"
        for name in (*_READ_TOOLS, *_DRAFT_TOOLS)
    )
    dispatch = ",\n".join(
        (
            f"            {name!r}: lambda: s.{name}()"
            if name in _READ_TOOLS
            else (
                "            'draft_order_candidate': "
                "lambda: self._draft('order', tool_input)"
                if name == "draft_order_candidate"
                else
                "            'draft_rule_candidate': "
                "lambda: self._draft('rule', tool_input)"
            )
        )
        for name in (*_READ_TOOLS, *_DRAFT_TOOLS)
    )
    return (
        "READ_ONLY_TOOL_SPECS = (\n"
        f"{specs},\n"
        ")\n\n"
        "class ToolRouter:\n"
        "    def dispatch(self, name, tool_input):\n"
        "        s = self.service\n"
        "        table = {\n"
        f"{dispatch},\n"
        "        }\n"
        "        return table[name]()\n\n"
        "    def _draft(self, kind, tool_input):\n"
        "        if kind == 'order':\n"
        "            return self.candidate_drafts.draft_order(tool_input)\n"
        "        return self.candidate_drafts.draft_rule(tool_input)\n\n"
        "class Agent:\n"
        "    def chat(self):\n"
        "        self.backend.create(tools=READ_ONLY_TOOL_SPECS)\n"
        "        return self.router.dispatch('get_account_summary', {})\n"
    )


def _trust_fixture(tmp_path: Path, *, git: bool = True) -> Path:
    """Build one self-contained clean root for Task 11 static analysis."""
    root = tmp_path / "trust-root"
    _write_fixture_file(
        root,
        "config.yaml",
        yaml.safe_dump(
            {
                "server": {
                    "bind_host": "127.0.0.1",
                    "port": 8020,
                    "origin": "https://localhost:8020",
                    "allowed_hosts": ["localhost", "127.0.0.1", "::1"],
                    "tls_ca_path": ".local/tls/rootCA.pem",
                    "tls_cert_path": ".local/tls/localhost.pem",
                    "tls_key_path": ".local/tls/localhost-key.pem",
                    "secure_cookies": True,
                },
                "provider_origins": {
                    "alpaca_trading": "https://paper-api.alpaca.markets",
                    "alpaca_data": "https://data.alpaca.markets",
                    "alpaca_stream": "wss://stream.data.alpaca.markets",
                    "anthropic": "https://api.anthropic.com",
                    "gemini": "https://generativelanguage.googleapis.com",
                    "groq": "https://api.groq.com",
                    "telegram": "https://api.telegram.org",
                    "coingecko": "https://api.coingecko.com",
                },
                "integrations": {
                    "webhooks_enabled": False,
                    "composio_enabled": False,
                },
                "trading": {"mode": "paper", "broker": "alpaca"},
                "features": {
                    "auto_execute_preapproved_rules": False,
                    "telegram_notifications": False,
                },
                "execution": {"prefer_bracket_orders": False},
                "llm": {
                    "provider": "anthropic",
                    "fallback_provider": None,
                },
            },
            sort_keys=False,
        ),
    )
    _write_fixture_file(root, "pyproject.toml", "")
    _write_fixture_file(root, "uv.lock", "")
    _write_fixture_file(
        root,
        ".env.example",
        "# Migration-only names; normal runtime uses macOS Keychain.\n"
        "ANTHROPIC_API_KEY=\nALPACA_API_KEY=\n",
    )
    _write_fixture_file(
        root,
        "src/trading_assistant/app/main.py",
        "from fastapi import FastAPI\n"
        "from .routers.safe import router as safe_router\n\n"
        "def create_app():\n"
        "    app = FastAPI()\n"
        "    app.include_router(safe_router)\n"
        "    return app\n",
    )
    _write_fixture_file(
        root,
        "src/trading_assistant/app/routers/safe.py",
        "from fastapi import APIRouter\n"
        "router = APIRouter(prefix='/v1')\n\n"
        "@router.get('/covered')\n"
        "def covered():\n"
        "    return None\n",
    )
    _write_fixture_file(
        root,
        "src/trading_assistant/app/policy.py",
        "ROUTE_POLICIES = (\n"
        "    RoutePolicy('GET', '/v1/covered', AuthLevel.PUBLIC, 'read'),\n"
        ")\n",
    )
    _write_fixture_file(
        root,
        "src/trading_assistant/app/agent.py",
        _agent_fixture_source(),
    )
    _write_fixture_file(
        root,
        "src/trading_assistant/config.py",
        "from typing import Literal\n\n"
        "class IntegrationsConfig:\n"
        "    webhooks_enabled: Literal[False] = False\n"
        "    composio_enabled: Literal[False] = False\n",
    )
    _write_fixture_file(
        root,
        "src/trading_assistant/security/secrets.py",
        "_SIMPLE_SECRET_FIELDS = (\n"
        "    'anthropic_api_key',\n"
        "    'alpaca_api_key',\n"
        "    'alpaca_secret_key',\n"
        "    'database_url',\n"
        ")\n\n"
        "class EnvironmentSecretProvider:\n"
        "    def __init__(self, *, environ, encryption):\n"
        "        self.environ = environ\n",
    )
    _write_fixture_file(
        root,
        "src/trading_assistant/security/sensitive_fields.py",
        "SENSITIVE_FIELDS = {\n"
        "    'audit_events': {'reason', 'detail_json'},\n"
        "}\n\n"
        "def sensitive_store(session, factory):\n"
        "    return object()\n",
    )
    _write_fixture_file(
        root,
        "src/trading_assistant/db/models.py",
        "class AuditEvent:\n"
        "    __tablename__ = 'audit_events'\n",
    )
    _write_fixture_file(
        root,
        "src/trading_assistant/security/outbound.py",
        "OUTBOUND_ORIGIN_MANIFEST = (\n"
        "    OutboundOriginRule('alpaca_trading', 'alpaca.trading', "
        "'https://paper-api.alpaca.markets', frozenset({'app', 'daemon', "
        "'mcp', 'paper-drill', 'preflight', 'safety-drill'})),\n"
        "    OutboundOriginRule('alpaca_data', 'alpaca.historical', "
        "'https://data.alpaca.markets', frozenset({'app', 'daemon', "
        "'mcp', 'paper-drill', 'preflight', 'safety-drill', "
        "'validate-analyst'})),\n"
        "    OutboundOriginRule('alpaca_stream', 'alpaca.stream', "
        "'wss://stream.data.alpaca.markets', frozenset({'daemon'}), "
        "'daemon.use_websocket'),\n"
        "    OutboundOriginRule('anthropic', 'llm.anthropic', "
        "'https://api.anthropic.com', frozenset({'app', 'daemon', "
        "'validate-analyst'}), 'llm.provider=anthropic'),\n"
        "    OutboundOriginRule('gemini', 'llm.gemini', "
        "'https://generativelanguage.googleapis.com', "
        "frozenset({'app', 'daemon', 'validate-analyst'}), "
        "'llm.provider=gemini'),\n"
        "    OutboundOriginRule('groq', 'llm.groq', "
        "'https://api.groq.com', frozenset({'app', 'daemon', "
        "'validate-analyst'}), 'llm.provider=groq'),\n"
        "    OutboundOriginRule('telegram', 'notifier.telegram', "
        "'https://api.telegram.org', frozenset({'app', 'daemon', "
        "'preflight'}), 'features.telegram_notifications'),\n"
        "    OutboundOriginRule('coingecko', 'marketdata.coingecko', "
        "'https://api.coingecko.com', frozenset({'app', 'daemon'}), "
        "'crypto_risk'),\n"
        ")\n",
    )
    static = root / "src/trading_assistant/app/static"
    static.mkdir(parents=True, exist_ok=True)
    for surface in (
        "README.md",
        "docs/RUNBOOK.md",
        "docs/ops/README.md",
        "scripts/launchd/README.md",
    ):
        _write_fixture_file(
            root,
            surface,
            "Composio is disabled. There is no webhook receiver. "
            "Backups use whole-database-v1.sqlite3.aesgcm.\n",
        )
    if git:
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "add", "--all"], cwd=root, check=True)
        anchor = subprocess.run(
            [
                "git",
                "-c",
                "user.name=Release Test",
                "-c",
                "user.email=release-test@example.invalid",
                "commit",
                "-qm",
                "fixture baseline",
            ],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
        assert anchor.returncode == 0
        _TRUSTED_ANCESTRY_ANCHORS[root.resolve()] = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    return root


def _run_trust_gate(
    root: Path,
    *,
    trusted_ancestry_anchor: str | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        resolved_root = root.resolve()
    except (OSError, RuntimeError):
        resolved_root = None
    anchor = trusted_ancestry_anchor or (
        _TRUSTED_ANCESTRY_ANCHORS.get(resolved_root, "0" * 40)
        if resolved_root is not None
        else "0" * 40
    )
    return subprocess.run(
        [
            sys.executable,
            "scripts/check_release_safety.py",
            "--root",
            str(root),
        ],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "TRADING_ASSISTANT_TRUSTED_ANCESTRY_ANCHOR": anchor,
        },
    )


def test_release_violation_is_strict_immutable_and_value_free():
    spec = importlib.util.spec_from_file_location(
        "release_gate_contract",
        Path("scripts/check_release_safety.py"),
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    finding = module.ReleaseViolation(
        "QUERY_SECRET",
        "src/trading_assistant/provider.py",
        17,
    )
    assert (
        finding.code,
        finding.path,
        finding.line,
    ) == (
        "QUERY_SECRET",
        "src/trading_assistant/provider.py",
        17,
    )
    with pytest.raises(FrozenInstanceError):
        finding.line = 18
    with pytest.raises((TypeError, ValueError)):
        module.ReleaseViolation(
            "QUERY_SECRET",
            "https://provider.invalid/path?credential=fixture",
            1,
        )


@pytest.mark.parametrize(
    ("relative_path", "source", "code"),
    [
        (
            "src/trading_assistant/app/main.py",
            "from fastapi import FastAPI\n"
            "def create_app():\n"
            "    app = FastAPI()\n"
            "    expose = getattr(app, 'post')\n"
            "    @expose('/webhook-orders')\n"
            "    def inbound():\n"
            "        return None\n"
            "    return app\n",
            "WEBHOOK_ROUTE_PRESENT",
        ),
        (
            "src/trading_assistant/app/main.py",
            "from fastapi import FastAPI\n"
            "def register(app, path):\n"
            "    app.add_api_route(path, endpoint, methods=['GET'])\n"
            "def create_app():\n"
            "    app = FastAPI()\n"
            "    register(app, '/covered')\n"
            "    register(app, '/hooks-second-call')\n"
            "    return app\n",
            "WEBHOOK_ROUTE_PRESENT",
        ),
        (
            "src/trading_assistant/app/main.py",
            "from fastapi import FastAPI\n"
            "from .routers.safe import router\n"
            "def create_app():\n"
            "    app = FastAPI()\n"
            "    app.include_router(router, prefix='/hooks')\n"
            "    return app\n",
            "WEBHOOK_ROUTE_PRESENT",
        ),
        (
            "src/trading_assistant/app/main.py",
            "from fastapi import FastAPI\n"
            "def create_app():\n"
            "    app = FastAPI()\n"
            "    app.add_api_route('/hooks-events', endpoint, methods=['POST'])\n"
            "    return app\n",
            "WEBHOOK_ROUTE_PRESENT",
        ),
        (
            "src/trading_assistant/app/main.py",
            "from fastapi import FastAPI\n"
            "def create_app():\n"
            "    app = FastAPI()\n"
            "    app.mount('/webhook-assets', child_app)\n"
            "    return app\n",
            "WEBHOOK_ROUTE_PRESENT",
        ),
        (
            "src/trading_assistant/app/main.py",
            "from fastapi import FastAPI\n"
            "def create_app(path):\n"
            "    app = FastAPI()\n"
            "    app.add_api_route(path, endpoint, methods=['POST'])\n"
            "    return app\n",
            "ROUTE_REGISTRATION_UNPROVEN",
        ),
        (
            "src/trading_assistant/app/main.py",
            "from fastapi import FastAPI\n"
            "def create_app(method_name):\n"
            "    app = FastAPI()\n"
            "    expose = getattr(app, method_name)\n"
            "    @expose('/covered')\n"
            "    def covered():\n"
            "        return None\n"
            "    return app\n",
            "ROUTE_REGISTRATION_UNPROVEN",
        ),
        (
            "src/trading_assistant/app/main.py",
            "import importlib\n"
            "from fastapi import FastAPI\n"
            "def create_app():\n"
            "    app = FastAPI()\n"
            "    routes = importlib.import_module(route_module)\n"
            "    app.include_router(routes.router)\n"
            "    return app\n",
            "ROUTE_REGISTRATION_UNPROVEN",
        ),
        (
            "src/trading_assistant/app/main.py",
            "from fastapi import FastAPI\n"
            "from third_party_routes import register_routes\n"
            "def create_app():\n"
            "    app = FastAPI()\n"
            "    register_routes(app)\n"
            "    return app\n",
            "ROUTE_REGISTRATION_UNPROVEN",
        ),
        (
            "src/trading_assistant/app/main.py",
            "from fastapi import FastAPI\n"
            "def create_app():\n"
            "    app = FastAPI()\n"
            "    app.router.add_api_route(\n"
            "        '/hooks-router', endpoint, methods=['POST']\n"
            "    )\n"
            "    return app\n",
            "WEBHOOK_ROUTE_PRESENT",
        ),
        (
            "src/trading_assistant/app/main.py",
            "from fastapi import FastAPI\n"
            "def create_app():\n"
            "    child = FastAPI()\n"
            "    @child.get('/hooks-mounted')\n"
            "    def mounted():\n"
            "        return None\n"
            "    app = FastAPI()\n"
            "    app.mount('/static', child)\n"
            "    return app\n",
            "WEBHOOK_ROUTE_PRESENT",
        ),
        (
            "src/trading_assistant/app/main.py",
            "from fastapi import APIRouter, FastAPI\n"
            "def create_app():\n"
            "    shadow = APIRouter()\n"
            "    @shadow.get('/covered')\n"
            "    def covered():\n"
            "        return None\n"
            "    app = FastAPI()\n"
            "    app.routes.append(shadow.routes[0])\n"
            "    return app\n",
            "ROUTE_REGISTRATION_UNPROVEN",
        ),
    ],
)
def test_effective_route_graph_negative_fixtures(
    tmp_path,
    relative_path,
    source,
    code,
):
    root = _trust_fixture(tmp_path)
    _write_fixture_file(root, relative_path, source)

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert code in completed.stderr


def test_effective_route_graph_resolves_fastapi_module_alias(tmp_path):
    root = _trust_fixture(tmp_path)
    _write_fixture_file(
        root,
        "src/trading_assistant/app/main.py",
        "import fastapi as framework\n"
        "def create_app():\n"
        "    app = framework.FastAPI()\n"
        "    @app.get('/covered')\n"
        "    def covered():\n"
        "        return None\n"
        "    return app\n",
    )
    _write_fixture_file(
        root,
        "src/trading_assistant/app/policy.py",
        "ROUTE_POLICIES = (\n"
        "    RoutePolicy('GET', '/covered', AuthLevel.PUBLIC, 'read'),\n"
        ")\n",
    )

    completed = _run_trust_gate(root)

    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_effective_route_graph_resolves_imported_nested_factories(tmp_path):
    root = _trust_fixture(tmp_path)
    _write_fixture_file(
        root,
        "src/trading_assistant/app/factory.py",
        "from fastapi import FastAPI\n"
        "from .routers.outer import router\n\n"
        "def build_app():\n"
        "    app = FastAPI()\n"
        "    app.include_router(router, prefix='/api')\n"
        "    return app\n",
    )
    _write_fixture_file(
        root,
        "src/trading_assistant/app/routers/outer.py",
        "from fastapi import APIRouter\n"
        "from .inner import router as inner\n"
        "router = APIRouter(prefix='/outer')\n"
        "router.include_router(inner, prefix='/nested')\n",
    )
    _write_fixture_file(
        root,
        "src/trading_assistant/app/routers/inner.py",
        "from fastapi import APIRouter\n"
        "router = APIRouter(prefix='/hooks')\n"
        "@router.get('/events')\n"
        "def events():\n"
        "    return None\n",
    )
    _write_fixture_file(
        root,
        "src/trading_assistant/app/main.py",
        "from .factory import build_app\n"
        "app = build_app()\n",
    )

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert "WEBHOOK_ROUTE_PRESENT" in completed.stderr


def test_effective_route_graph_detects_duplicate_effective_routes(tmp_path):
    root = _trust_fixture(tmp_path)
    _write_fixture_file(
        root,
        "src/trading_assistant/app/main.py",
        "from fastapi import FastAPI\n"
        "def create_app():\n"
        "    app = FastAPI()\n"
        "    @app.get('/same')\n"
        "    def first():\n"
        "        return None\n"
        "    @app.get('/same')\n"
        "    def second():\n"
        "        return None\n"
        "    return app\n",
    )
    _write_fixture_file(
        root,
        "src/trading_assistant/app/policy.py",
        "ROUTE_POLICIES = (\n"
        "    RoutePolicy('GET', '/same', AuthLevel.PUBLIC, 'read'),\n"
        ")\n",
    )

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert "DUPLICATE_EFFECTIVE_ROUTE" in completed.stderr


def test_effective_route_graph_detects_duplicate_parameter_shapes(tmp_path):
    root = _trust_fixture(tmp_path)
    _write_fixture_file(
        root,
        "src/trading_assistant/app/main.py",
        "from fastapi import FastAPI\n"
        "def create_app():\n"
        "    app = FastAPI()\n"
        "    @app.get('/same/{first}')\n"
        "    def first():\n"
        "        return None\n"
        "    @app.get('/same/{second}')\n"
        "    def second():\n"
        "        return None\n"
        "    return app\n",
    )
    _write_fixture_file(
        root,
        "src/trading_assistant/app/policy.py",
        "ROUTE_POLICIES = (\n"
        "    RoutePolicy('GET', '/same/{first}', AuthLevel.PUBLIC, 'read'),\n"
        "    RoutePolicy('GET', '/same/{second}', AuthLevel.PUBLIC, 'read'),\n"
        ")\n",
    )

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert "DUPLICATE_EFFECTIVE_ROUTE" in completed.stderr


@pytest.mark.parametrize(
    ("source", "code"),
    [
        (
            _agent_fixture_source().replace(
                "'get_account_summary': lambda: s.get_account_summary()",
                "'get_account_summary': lambda: s.cancel_order()",
            ),
            "MUTABLE_CHAT_TOOL",
        ),
        (
            _agent_fixture_source().replace(
                "s = self.service",
                "s = self.service\n        mutate = s.approve_plan",
            ).replace(
                "'get_account_summary': lambda: s.get_account_summary()",
                "'get_account_summary': lambda: mutate()",
            ),
            "MUTABLE_CHAT_TOOL",
        ),
        (
            _agent_fixture_source().replace(
                "table = {",
                "table = dict({",
            ).replace(
                "        }\n        return table[name]()",
                "        })\n        return table[name]()",
            ),
            "CHAT_TOOL_REGISTRY_UNPROVEN",
        ),
        (
            _agent_fixture_source().replace(
                "'get_account_summary': lambda: s.get_account_summary()",
                "'get_account_summary': lambda: getattr(s, method_name)()",
            ),
            "CHAT_TOOL_REGISTRY_UNPROVEN",
        ),
        (
            _agent_fixture_source()
            + "\ndef dynamic_escape(name):\n"
            "    return __import__(name)\n",
            "CHAT_TOOL_REGISTRY_UNPROVEN",
        ),
        (
            _agent_fixture_source().replace(
                "        return table[name]()",
                "        s.cancel_order()\n"
                "        return table[name]()",
            ),
            "MUTABLE_CHAT_TOOL",
        ),
        (
            _agent_fixture_source().replace(
                "        self.backend.create(tools=READ_ONLY_TOOL_SPECS)",
                "        self.router.service.approve_plan()\n"
                "        self.backend.create(tools=READ_ONLY_TOOL_SPECS)",
            ),
            "MUTABLE_CHAT_TOOL",
        ),
        (
            _agent_fixture_source().replace(
                "s.get_account_summary()",
                "other.get_account_summary()",
            ),
            "CHAT_TOOL_REGISTRY_UNPROVEN",
        ),
        (
            _agent_fixture_source().replace(
                "self.candidate_drafts.draft_order",
                "self.service.draft_order",
            ),
            "CHAT_TOOL_REGISTRY_UNPROVEN",
        ),
    ],
)
def test_chat_tool_boundary_negative_fixtures(tmp_path, source, code):
    root = _trust_fixture(tmp_path)
    _write_fixture_file(
        root,
        "src/trading_assistant/app/agent.py",
        source,
    )

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert code in completed.stderr


@pytest.mark.parametrize(
    "source",
    [
        "from trading_assistant.db.models import AuditEvent\n"
        "event = AuditEvent(reason='fixture-sensitive-value')\n",
        "from trading_assistant.db.models import AuditEvent\n"
        "event: AuditEvent\n"
        "event.reason = 'fixture-sensitive-value'\n",
        "from sqlalchemy import update\n"
        "from trading_assistant.db.models import AuditEvent\n"
        "statement = update(AuditEvent).values(reason='fixture-sensitive-value')\n",
        "from trading_assistant.db.models import AuditEvent as Event\n"
        "rows = [{'reason': 'fixture-sensitive-value'}]\n"
        "session.bulk_update_mappings(Event, rows)\n",
        "from trading_assistant.db.models import AuditEvent\n"
        "model = AuditEvent\n"
        "session.bulk_insert_mappings(model, [{'detail_json': '{}'}])\n",
        "from trading_assistant.db.models import AuditEvent\n"
        "write_model(AuditEvent, {'reason': 'fixture-sensitive-value'})\n",
        "from trading_assistant.db.models import AuditEvent\n"
        "def persist_sensitive(model, values):\n"
        "    return None\n"
        "persist_sensitive(AuditEvent, {'reason': 'fixture-sensitive-value'})\n",
    ],
)
def test_sensitive_write_negative_fixtures_are_root_local(tmp_path, source):
    root = _trust_fixture(tmp_path)
    _write_fixture_file(
        root,
        "src/trading_assistant/plaintext_write.py",
        source,
    )

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert "PLAINTEXT_SENSITIVE_WRITE" in completed.stderr
    assert "fixture-sensitive-value" not in completed.stderr


def test_dynamic_sensitive_registry_fails_closed(tmp_path):
    root = _trust_fixture(tmp_path)
    _write_fixture_file(
        root,
        "src/trading_assistant/security/sensitive_fields.py",
        "SENSITIVE_FIELDS = build_sensitive_registry()\n",
    )

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert "SENSITIVE_REGISTRY_INVALID" in completed.stderr


@pytest.mark.parametrize(
    ("relative_path", "code"),
    [
        (
            "src/trading_assistant/app/agent.py",
            "CHAT_TOOL_REGISTRY_UNPROVEN",
        ),
        (
            "src/trading_assistant/security/sensitive_fields.py",
            "SENSITIVE_REGISTRY_INVALID",
        ),
        (
            "src/trading_assistant/security/secrets.py",
            "ENVIRONMENT_SECRETS_IN_PRODUCTION",
        ),
        (
            "src/trading_assistant/config.py",
            "COMPOSIO_ENABLED",
        ),
        (
            "docs/RUNBOOK.md",
            "COMPOSIO_ENABLED",
        ),
    ],
)
def test_missing_trust_boundary_authority_fails_closed(
    tmp_path,
    relative_path,
    code,
):
    root = _trust_fixture(tmp_path)
    (root / relative_path).unlink()

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert code in completed.stderr


def test_sensitive_registry_never_imports_the_active_checkout(tmp_path):
    root = _trust_fixture(tmp_path)
    _write_fixture_file(
        root,
        "src/trading_assistant/security/sensitive_fields.py",
        "SENSITIVE_FIELDS = {'fixture_rows': {'secret_note'}}\n",
    )
    _write_fixture_file(
        root,
        "src/trading_assistant/db/models.py",
        "class AuditEvent:\n"
        "    __tablename__ = 'audit_events'\n\n"
        "class FixtureRow:\n"
        "    __tablename__ = 'fixture_rows'\n",
    )
    _write_fixture_file(
        root,
        "src/trading_assistant/root_isolation.py",
        "from trading_assistant.db.models import AuditEvent, FixtureRow\n"
        "safe = AuditEvent(reason='not-registered-in-this-root')\n"
        "unsafe = FixtureRow(secret_note='fixture-root-only')\n",
    )

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    lines = [
        line
        for line in completed.stderr.splitlines()
        if line.startswith("PLAINTEXT_SENSITIVE_WRITE ")
    ]
    assert lines == [
        "PLAINTEXT_SENSITIVE_WRITE "
        "src/trading_assistant/root_isolation.py:3"
    ]


@pytest.mark.parametrize(
    ("relative_path", "source"),
    [
        (
            "src/trading_assistant/bootstrap.py",
            "from trading_assistant.security.secrets import "
            "EnvironmentSecretProvider as Provider\n"
            "provider = Provider(environ={}, encryption=config.encryption)\n",
        ),
        (
            "src/trading_assistant/daemon/main.py",
            "import os\n"
            "key = os.environ['ALPACA_API_KEY']\n",
        ),
        (
            "src/trading_assistant/daemon/main.py",
            "import os as operating_system\n"
            "environment = operating_system.environ\n"
            "key = environment['ALPACA_API_KEY']\n",
        ),
        (
            "src/trading_assistant/mcp_server/server.py",
            "from os import getenv as read_environment\n"
            "key = read_environment('ANTHROPIC_API_KEY')\n",
        ),
        (
            "src/trading_assistant/app/main.py",
            "import importlib\n"
            "provider = getattr(importlib.import_module(module_name), "
            "'EnvironmentSecretProvider')\n",
        ),
    ],
)
def test_production_environment_secret_sources_fail_closed(
    tmp_path,
    relative_path,
    source,
):
    root = _trust_fixture(tmp_path)
    _write_fixture_file(root, relative_path, source)

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert "ENVIRONMENT_SECRETS_IN_PRODUCTION" in completed.stderr


@pytest.mark.parametrize(
    ("relative_path", "source"),
    [
        (
            "src/trading_assistant/db/migrate.py",
            "import os\n"
            "from trading_assistant.security.secrets import (\n"
            "    EnvironmentSecretProvider, load_role_secrets,\n"
            ")\n"
            "def main(args, config):\n"
            "    if args.development_environment_secrets:\n"
            "        provider = EnvironmentSecretProvider(\n"
            "            environ=os.environ, encryption=config.encryption,\n"
            "        )\n"
            "        return load_role_secrets(\n"
            "            'migration', config=config, provider=provider,\n"
            "            allow_environment=True,\n"
            "        )\n"
            "    return load_role_secrets('migration', config=config)\n",
        ),
        (
            "src/trading_assistant/ops/safety_drill.py",
            "import os\n"
            "from trading_assistant.security.secrets import (\n"
            "    EnvironmentSecretProvider, load_role_secrets,\n"
            ")\n"
            "def main(args, config):\n"
            "    if args.development_environment_secrets:\n"
            "        return load_role_secrets(\n"
            "            'safety-drill', config=config,\n"
            "            provider=EnvironmentSecretProvider(\n"
            "                environ=os.environ, encryption=config.encryption,\n"
            "            ), allow_environment=True,\n"
            "        )\n"
            "    return load_role_secrets('safety-drill', config=config)\n",
        ),
    ],
)
def test_exact_development_environment_secret_branches_are_allowed(
    tmp_path,
    relative_path,
    source,
):
    root = _trust_fixture(tmp_path)
    _write_fixture_file(root, relative_path, source)

    completed = _run_trust_gate(root)

    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_development_environment_branch_rejects_extra_unapproved_role(tmp_path):
    root = _trust_fixture(tmp_path)
    _write_fixture_file(
        root,
        "src/trading_assistant/db/migrate.py",
        "import os\n"
        "from trading_assistant.security.secrets import (\n"
        "    EnvironmentSecretProvider, load_role_secrets,\n"
        ")\n"
        "def main(args, config):\n"
        "    if args.development_environment_secrets:\n"
        "        provider = EnvironmentSecretProvider(\n"
        "            environ=os.environ, encryption=config.encryption,\n"
        "        )\n"
        "        load_role_secrets(\n"
        "            'migration', config=config, provider=provider,\n"
        "            allow_environment=True,\n"
        "        )\n"
        "        return load_role_secrets(\n"
        "            'app', config=config, provider=provider,\n"
        "            allow_environment=True,\n"
        "        )\n",
    )

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert "ENVIRONMENT_SECRETS_IN_PRODUCTION" in completed.stderr


@pytest.mark.parametrize(
    ("relative_path", "source"),
    [
        (
            "config.yaml",
            yaml.safe_dump(
                {
                    "server": {
                        "bind_host": "127.0.0.1",
                        "port": 8020,
                        "origin": "https://localhost:8020",
                        "allowed_hosts": ["localhost"],
                        "secure_cookies": True,
                    },
                    "integrations": {
                        "webhooks_enabled": False,
                        "composio_enabled": True,
                    },
                    "trading": {"mode": "paper", "broker": "alpaca"},
                    "features": {
                        "auto_execute_preapproved_rules": False,
                    },
                    "execution": {"prefer_bracket_orders": False},
                    "llm": {"fallback_provider": None},
                }
            ),
        ),
        (
            ".env.example",
            "COMPOSIO_API_KEY=\n",
        ),
        (
            "src/trading_assistant/integrations.py",
            "from composio import App\n"
            "toolkit = App()\n",
        ),
        (
            "docs/RUNBOOK.md",
            "Enable the integration with `composio login`.\n"
            "Backups use whole-database-v1.sqlite3.aesgcm.\n",
        ),
    ],
)
def test_composio_must_remain_explicitly_disabled(
    tmp_path,
    relative_path,
    source,
):
    root = _trust_fixture(tmp_path)
    _write_fixture_file(root, relative_path, source)

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert "COMPOSIO_ENABLED" in completed.stderr


@pytest.mark.parametrize(
    ("relative_path", "source", "code"),
    [
        (
            "src/trading_assistant/unsafe_http.py",
            "import httpx\nclient = httpx.Client()\n",
            "OUTBOUND_CLIENT_UNAPPROVED",
        ),
        (
            "src/trading_assistant/unsafe_http.py",
            "import requests as transport\n"
            "response = transport.get('https://api.anthropic.com')\n",
            "OUTBOUND_CLIENT_UNAPPROVED",
        ),
        (
            "src/trading_assistant/unsafe_origin.py",
            "from trading_assistant.security.outbound import OutboundPolicy\n"
            "policy = OutboundPolicy('https://unknown.invalid')\n",
            "OUTBOUND_ORIGIN_UNAPPROVED",
        ),
        (
            "src/trading_assistant/unsafe_origin.py",
            "from trading_assistant.security.outbound import OutboundPolicy\n"
            "policy = OutboundPolicy('https://api.anthropic.com')\n",
            "OUTBOUND_ORIGIN_UNAPPROVED",
        ),
        (
            "src/trading_assistant/unsafe_origin.py",
            "from trading_assistant.security.outbound import OutboundPolicy\n"
            "origin = configured_origin\n"
            "policy = OutboundPolicy(origin)\n",
            "OUTBOUND_ORIGIN_UNAPPROVED",
        ),
        (
            "src/trading_assistant/query_auth.py",
            "client.get('/data', params={'access_key': credential})\n",
            "QUERY_SECRET",
        ),
        (
            "src/trading_assistant/query_auth.py",
            "client.get('https://api.anthropic.com/data?api_key=fixture')\n",
            "QUERY_SECRET",
        ),
        (
            "src/trading_assistant/redirects.py",
            "client.get('/data', allow_redirects=True)\n",
            "CROSS_ORIGIN_REDIRECT_ENABLED",
        ),
        (
            "src/trading_assistant/redirects.py",
            "client.get('/data', follow_redirects=configured)\n",
            "CROSS_ORIGIN_REDIRECT_ENABLED",
        ),
        (
            "src/trading_assistant/proxy.py",
            "from httpx import Client\n"
            "client = Client(trust_env=True)\n",
            "PROXY_HEADERS_TRUSTED",
        ),
        (
            "src/trading_assistant/query_auth.py",
            "client.get('/data', params=query_values)\n",
            "QUERY_SECRET",
        ),
        (
            "src/trading_assistant/query_auth.py",
            "client.get(url='https://api.anthropic.com/data?token=fixture')\n",
            "QUERY_SECRET",
        ),
        (
            "src/trading_assistant/unknown_client.py",
            "import urllib3\nclient = urllib3.PoolManager()\n",
            "OUTBOUND_CLIENT_UNAPPROVED",
        ),
        (
            "src/trading_assistant/dynamic_origin.py",
            "client = ProviderClient(base_url=configured_origin)\n",
            "OUTBOUND_ORIGIN_UNAPPROVED",
        ),
        (
            "src/trading_assistant/http_origin.py",
            "client.get('http://provider.invalid/data')\n",
            "OUTBOUND_ORIGIN_UNAPPROVED",
        ),
        (
            "src/trading_assistant/client_alias.py",
            "import httpx\n"
            "make_client = httpx.Client\n"
            "client = make_client()\n",
            "OUTBOUND_CLIENT_UNAPPROVED",
        ),
        (
            "src/trading_assistant/client_alias.py",
            "import httpx._client as transport\n"
            "client = transport.Client()\n",
            "OUTBOUND_CLIENT_UNAPPROVED",
        ),
        (
            "src/trading_assistant/query_auth.py",
            "client.get(f'https://api.anthropic.com/data?"
            "access_key={credential}')\n",
            "QUERY_SECRET",
        ),
        (
            "src/trading_assistant/backtest/coingecko.py",
            "query = {'access_key': credential}\n"
            "client.get('/data', params=query)\n",
            "QUERY_SECRET",
        ),
        (
            "src/trading_assistant/sdk_escape.py",
            "from alpaca.data.historical import "
            "StockHistoricalDataClient\n"
            "client = StockHistoricalDataClient('fixture', 'fixture')\n",
            "OUTBOUND_CLIENT_UNAPPROVED",
        ),
        (
            "src/trading_assistant/redirects.py",
            "client.follow_redirects = True\n",
            "CROSS_ORIGIN_REDIRECT_ENABLED",
        ),
        (
            "src/trading_assistant/proxy.py",
            "import uvicorn\n"
            "uvicorn.run(app, proxy_headers=False, "
            "forwarded_allow_ips='*')\n",
            "PROXY_HEADERS_TRUSTED",
        ),
        (
            "src/trading_assistant/tls.py",
            "import httpx\nclient = httpx.Client(verify=False)\n",
            "TLS_DISABLED",
        ),
    ],
)
def test_outbound_manifest_negative_fixtures(
    tmp_path,
    relative_path,
    source,
    code,
):
    root = _trust_fixture(tmp_path)
    _write_fixture_file(root, relative_path, source)

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert code in completed.stderr


def test_dynamic_outbound_manifest_fails_closed(tmp_path):
    root = _trust_fixture(tmp_path)
    _write_fixture_file(
        root,
        "src/trading_assistant/security/outbound.py",
        "OUTBOUND_ORIGIN_MANIFEST = build_manifest()\n",
    )

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert "OUTBOUND_ORIGIN_UNAPPROVED" in completed.stderr


@pytest.mark.parametrize(
        ("source", "code"),
        [
            (
                "import httpx\n"
                "client = httpx.Client(follow_redirects=True)\n",
                "CROSS_ORIGIN_REDIRECT_ENABLED",
            ),
        (
            "options['trust_env'] = configured_proxy_trust\n",
            "PROXY_HEADERS_TRUSTED",
        ),
    ],
)
def test_outbound_wrapper_options_are_statically_gated(
    tmp_path,
    source,
    code,
):
    root = _trust_fixture(tmp_path)
    path = root / "src/trading_assistant/security/outbound.py"
    path.write_text(
        path.read_text(encoding="utf-8") + source,
        encoding="utf-8",
    )

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert code in completed.stderr


@pytest.mark.parametrize(
    ("mutator", "code"),
    [
        (
            lambda raw: raw["server"].update({"secure_cookies": False}),
            "INSECURE_COOKIE",
        ),
        (
            lambda raw: raw["server"].update({"allowed_hosts": ["*"]}),
            "WILDCARD_HOST_ORIGIN",
        ),
        (
            lambda raw: raw["server"].update({"origin": "http://localhost:8020"}),
            "TLS_DISABLED",
        ),
        (
            lambda raw: raw["server"].update({"proxy_headers": True}),
            "PROXY_HEADERS_TRUSTED",
        ),
        (
            lambda raw: raw["server"].update({"tls_key_path": None}),
            "TLS_DISABLED",
        ),
        (
            lambda raw: raw["server"].update(
                {"tls_ca_path": ".local/tls/renamed-root.pem"}
            ),
            "TLS_DISABLED",
        ),
    ],
)
def test_transport_config_negative_fixtures(tmp_path, mutator, code):
    root = _trust_fixture(tmp_path)
    config_path = root / "config.yaml"
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    mutator(raw)
    config_path.write_text(
        yaml.safe_dump(raw, sort_keys=False),
        encoding="utf-8",
    )

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert code in completed.stderr


@pytest.mark.parametrize(
    ("section", "code"),
    [
        ("server", "TLS_DISABLED"),
        ("integrations", "COMPOSIO_ENABLED"),
        ("provider_origins", "OUTBOUND_ORIGIN_UNAPPROVED"),
    ],
)
def test_missing_structural_config_sections_fail_closed(
    tmp_path,
    section,
    code,
):
    root = _trust_fixture(tmp_path)
    config_path = root / "config.yaml"
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    raw.pop(section)
    config_path.write_text(
        yaml.safe_dump(raw, sort_keys=False),
        encoding="utf-8",
    )

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert code in completed.stderr


def test_integration_defaults_must_be_literal_false_types(tmp_path):
    root = _trust_fixture(tmp_path)
    _write_fixture_file(
        root,
        "src/trading_assistant/config.py",
        "class IntegrationsConfig:\n"
        "    webhooks_enabled: bool = False\n"
        "    composio_enabled: bool = False\n",
    )

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert "COMPOSIO_ENABLED" in completed.stderr
    assert "WEBHOOK_ROUTE_PRESENT" in completed.stderr


def test_operator_docs_must_explicitly_reject_a_webhook_receiver(tmp_path):
    root = _trust_fixture(tmp_path)
    _write_fixture_file(
        root,
        "docs/RUNBOOK.md",
        "Composio is disabled. The webhook receiver is enabled.\n"
        "Backups use whole-database-v1.sqlite3.aesgcm.\n",
    )

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert "WEBHOOK_ROUTE_PRESENT" in completed.stderr


@pytest.mark.parametrize(
    ("relative_path", "source"),
    [
        (
            "src/trading_assistant/app/security.py",
            "from starlette.middleware.cors import CORSMiddleware\n"
            "middleware = CORSMiddleware(app, allow_origins=['*'])\n",
        ),
        (
            "src/trading_assistant/mcp_server/composio_escape.py",
            "toolkit = getattr(integrations, 'ComposioToolSet')()\n",
        ),
        (
            "src/trading_assistant/integrations.py",
            "endpoint = 'https://provider.composio.invalid/tools'\n",
        ),
        (
            "src/trading_assistant/mcp_server/server.py",
            "toolkit = getattr(integrations, toolkit_name)()\n",
        ),
        (
            "src/trading_assistant/integrations.py",
            "import importlib\n"
            "toolkit = importlib.import_module(provider_module)\n",
        ),
    ],
)
def test_ast_transport_and_composio_escapes_fail_closed(
    tmp_path,
    relative_path,
    source,
):
    root = _trust_fixture(tmp_path)
    _write_fixture_file(root, relative_path, source)

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    expected = (
        "WILDCARD_HOST_ORIGIN"
        if "CORSMiddleware" in source
        else "COMPOSIO_ENABLED"
    )
    assert expected in completed.stderr


@pytest.mark.parametrize(
    ("relative_path", "code"),
    [
        (".env", "TRACKED_ENV_FILE"),
        ("state/runtime.sqlite3", "TRACKED_SQLITE_DATABASE"),
        ("state/runtime.sqlite3-wal", "TRACKED_SQLITE_WAL"),
        ("state/runtime.sqlite3-shm", "TRACKED_SQLITE_SHM"),
        (".local/tls/localhost-key.pem", "TRACKED_TLS_PRIVATE_KEY"),
        (".local/tls/operator.p12", "TRACKED_TLS_PRIVATE_CERTIFICATE"),
        ("backups/decrypted-backup.bak", "TRACKED_DECRYPTED_BACKUP"),
        ("logs/runtime.log", "TRACKED_RUNTIME_LOG"),
        ("exports/raw-account-export.csv", "TRACKED_RAW_EXPORT"),
    ],
)
def test_tracked_private_artifacts_have_separate_stable_codes(
    tmp_path,
    relative_path,
    code,
):
    root = _trust_fixture(tmp_path)
    _write_fixture_file(root, relative_path, "fixture-only\n")
    subprocess.run(["git", "add", "--", relative_path], cwd=root, check=True)

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert code in completed.stderr
    assert "fixture-only" not in completed.stderr


def test_git_tree_failure_is_value_free_and_fail_closed(tmp_path):
    root = _trust_fixture(tmp_path, git=False)

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert completed.stderr.splitlines() == [
        "GIT_TREE_UNPROVEN .:1",
        "release static checks: FAIL (1 violation)",
    ]


def test_clean_root_and_false_positive_decoys_pass(tmp_path):
    root = _trust_fixture(tmp_path)
    _write_fixture_file(
        root,
        "src/trading_assistant/decoys.py",
        "# /webhook execute approve cancel Composio http://not-a-call.invalid\n"
        "message = '/hooks access_key EnvironmentSecretProvider notify reset'\n",
    )
    _write_fixture_file(root, "untracked.sqlite3", "untracked fixture\n")

    completed = _run_trust_gate(root)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed.stdout.strip() == "release static checks: PASS"
    assert completed.stderr == ""


def test_findings_are_sorted_deduplicated_and_never_include_matches(tmp_path):
    root = _trust_fixture(tmp_path)
    marker = "fixture-query-marker"
    _write_fixture_file(
        root,
        "src/trading_assistant/z_query.py",
        "client.get('/x', params={'access_key': 'fixture-query-marker'})\n",
    )
    _write_fixture_file(
        root,
        "src/trading_assistant/a_client.py",
        "import httpx\n"
        "first = httpx.Client()\n"
        "second = httpx.Client()\n",
    )

    first = _run_trust_gate(root)
    second = _run_trust_gate(root)

    assert first.returncode == second.returncode == 1
    assert first.stderr == second.stderr
    finding_lines = first.stderr.splitlines()[:-1]
    assert finding_lines == sorted(set(finding_lines))
    assert marker not in first.stderr
    assert "https://" not in first.stderr
    assert "?" not in first.stderr
    assert first.stderr.splitlines()[-1] == (
        f"release static checks: FAIL ({len(finding_lines)} violations)"
    )


@pytest.mark.parametrize(
    ("relative", "mutation", "code"),
    [
        (
            "src/trading_assistant/app/agent.py",
            "\nREAD_ONLY_TOOL_SPECS = READ_ONLY_TOOL_SPECS\n",
            "CHAT_TOOL_REGISTRY_UNPROVEN",
        ),
        (
            "src/trading_assistant/app/agent.py",
            "\nif runtime_flag:\n    READ_ONLY_TOOL_SPECS = ()\n",
            "CHAT_TOOL_REGISTRY_UNPROVEN",
        ),
        (
            "src/trading_assistant/app/agent.py",
            "\nspec_alias = READ_ONLY_TOOL_SPECS\nspec_alias += ()\n",
            "CHAT_TOOL_REGISTRY_UNPROVEN",
        ),
        (
            "src/trading_assistant/security/sensitive_fields.py",
            "\nSENSITIVE_FIELDS = {'audit_events': {'reason'}}\n",
            "SENSITIVE_REGISTRY_INVALID",
        ),
        (
            "src/trading_assistant/security/sensitive_fields.py",
            "\nregistry_alias = SENSITIVE_FIELDS\nregistry_alias.update({})\n",
            "SENSITIVE_REGISTRY_INVALID",
        ),
        (
            "src/trading_assistant/security/sensitive_fields.py",
            "\nregistry_alias = SENSITIVE_FIELDS\n"
            "mutate_registry = registry_alias.update\n"
            "mutate_registry({})\n",
            "SENSITIVE_REGISTRY_INVALID",
        ),
        (
            "src/trading_assistant/security/secrets.py",
            "\n_SIMPLE_SECRET_FIELDS = ('database_url',)\n",
            "ENVIRONMENT_SECRETS_IN_PRODUCTION",
        ),
        (
            "src/trading_assistant/security/secrets.py",
            "\nsecret_alias = _SIMPLE_SECRET_FIELDS\nsecret_alias += ()\n",
            "ENVIRONMENT_SECRETS_IN_PRODUCTION",
        ),
        (
            "src/trading_assistant/security/outbound.py",
            "\nOUTBOUND_ORIGIN_MANIFEST = OUTBOUND_ORIGIN_MANIFEST\n",
            "OUTBOUND_ORIGIN_UNAPPROVED",
        ),
        (
            "src/trading_assistant/security/outbound.py",
            "\nmanifest_alias = OUTBOUND_ORIGIN_MANIFEST\nmanifest_alias += ()\n",
            "OUTBOUND_ORIGIN_UNAPPROVED",
        ),
        (
            "src/trading_assistant/config.py",
            "\nclass IntegrationsConfig:\n"
            "    webhooks_enabled: bool = True\n"
            "    composio_enabled: bool = True\n",
            "COMPOSIO_ENABLED",
        ),
        (
            "src/trading_assistant/config.py",
            "\nIntegrationAlias = IntegrationsConfig\n"
            "IntegrationAlias.composio_enabled = True\n",
            "COMPOSIO_ENABLED",
        ),
        (
            "src/trading_assistant/config.py",
            "\nIntegrationAlias = IntegrationsConfig\n"
            "setattr(IntegrationAlias, 'webhooks_enabled', True)\n",
            "WEBHOOK_ROUTE_PRESENT",
        ),
    ],
)
def test_final_authorities_reject_rebinding_conditionals_and_alias_mutation(
    tmp_path,
    relative,
    mutation,
    code,
):
    root = _trust_fixture(tmp_path)
    target = root / relative
    target.write_text(
        target.read_text(encoding="utf-8") + mutation,
        encoding="utf-8",
    )

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert code in completed.stderr


def test_route_branch_union_catches_webhook_hidden_by_safe_else(tmp_path):
    root = _trust_fixture(tmp_path)
    _write_fixture_file(
        root,
        "src/trading_assistant/app/main.py",
        "from fastapi import FastAPI\n"
        "def create_app(enabled=True):\n"
        "    if enabled:\n"
        "        app = FastAPI()\n"
        "        @app.get('/webhook-branch')\n"
        "        def hidden():\n"
        "            return None\n"
        "    else:\n"
        "        app = FastAPI()\n"
        "        @app.get('/covered')\n"
        "        def covered():\n"
        "            return None\n"
        "    return app\n",
    )

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert "WEBHOOK_ROUTE_PRESENT" in completed.stderr


@pytest.mark.parametrize(
    ("statement", "code"),
    [
        ("app.routes += app.routes[:1]", "ROUTE_REGISTRATION_UNPROVEN"),
        ("app.routes.append(app.routes[0])", "ROUTE_REGISTRATION_UNPROVEN"),
        ("app.routes.extend(app.routes[:1])", "ROUTE_REGISTRATION_UNPROVEN"),
        ("app.routes.insert(0, app.routes[0])", "ROUTE_REGISTRATION_UNPROVEN"),
        ("app.routes[:] = app.routes", "ROUTE_REGISTRATION_UNPROVEN"),
        ("app.routes[0] = app.routes[0]", "ROUTE_REGISTRATION_UNPROVEN"),
        (
            "app.__getattribute__('add_api_route')"
            "('/hooks-hidden', endpoint, methods=['GET'])",
            "WEBHOOK_ROUTE_PRESENT",
        ),
        (
            "app.__getattribute__(method_name)"
            "('/covered', endpoint, methods=['GET'])",
            "ROUTE_REGISTRATION_UNPROVEN",
        ),
    ],
)
def test_route_list_and_dunder_registration_bypasses_fail_closed(
    tmp_path,
    statement,
    code,
):
    root = _trust_fixture(tmp_path)
    _write_fixture_file(
        root,
        "src/trading_assistant/app/main.py",
        "from fastapi import FastAPI\n"
        "def endpoint():\n"
        "    return None\n"
        "def create_app():\n"
        "    app = FastAPI()\n"
        "    @app.get('/covered')\n"
        "    def covered():\n"
        "        return None\n"
        f"    {statement}\n"
        "    return app\n",
    )

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert code in completed.stderr


def test_unresolved_route_side_effect_in_one_branch_fails_closed(tmp_path):
    root = _trust_fixture(tmp_path)
    _write_fixture_file(
        root,
        "src/trading_assistant/app/main.py",
        "from fastapi import FastAPI\n"
        "def create_app(enabled=True):\n"
        "    app = FastAPI()\n"
        "    if enabled:\n"
        "        unknown_registration_helper(app)\n"
        "    return app\n",
    )

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert "ROUTE_REGISTRATION_UNPROVEN" in completed.stderr


def test_route_mutation_fixture_really_registers_effective_webhook(tmp_path):
    namespace: dict[str, object] = {}
    exec(
        "from fastapi import FastAPI\n"
        "def endpoint():\n"
        "    return None\n"
        "def create_app():\n"
        "    app = FastAPI()\n"
        "    app.__getattribute__('add_api_route')"
        "('/hooks-runtime-proof', endpoint, methods=['GET'])\n"
        "    return app\n",
        namespace,
    )

    app = namespace["create_app"]()

    assert "/hooks-runtime-proof" in {
        getattr(route, "path", None) for route in app.routes
    }


def _agent_with_recursive_helper(method_body: str) -> str:
    return _agent_fixture_source().replace(
        "            'get_account_summary': "
        "lambda: s.get_account_summary()",
        "            'get_account_summary': "
        "lambda: self._dispatch_account(s)",
    ).replace(
        "    def _draft(self, kind, tool_input):\n",
        "    def _dispatch_account(self, s):\n"
        f"{method_body}\n\n"
        "    def _draft(self, kind, tool_input):\n",
    )


def test_chat_reachable_recursive_mutation_is_reported_as_mutable(tmp_path):
    root = _trust_fixture(tmp_path)
    _write_fixture_file(
        root,
        "src/trading_assistant/app/agent.py",
        _agent_with_recursive_helper(
            "        return self._second_helper(s)"
        ).replace(
            "    def _draft(self, kind, tool_input):\n",
            "    def _second_helper(self, s):\n"
            "        return s.cancel_order()\n\n"
            "    def _draft(self, kind, tool_input):\n",
        ),
    )

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert "MUTABLE_CHAT_TOOL" in completed.stderr


def test_chat_reachable_local_read_helper_is_proven(tmp_path):
    root = _trust_fixture(tmp_path)
    _write_fixture_file(
        root,
        "src/trading_assistant/app/agent.py",
        _agent_with_recursive_helper(
            "        return s.get_account_summary()"
        ),
    )

    completed = _run_trust_gate(root)

    assert completed.returncode == 0, completed.stdout + completed.stderr


@pytest.mark.parametrize(
    "method_body",
    [
        "        return external_dispatch_helper(s)",
        "        callback = lambda: s.get_account_summary()\n"
        "        return callback()",
        "        return getattr(s, method_name)()",
    ],
)
def test_chat_reachable_unproven_helpers_fail_closed(tmp_path, method_body):
    root = _trust_fixture(tmp_path)
    _write_fixture_file(
        root,
        "src/trading_assistant/app/agent.py",
        _agent_with_recursive_helper(method_body),
    )

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert "CHAT_TOOL_REGISTRY_UNPROVEN" in completed.stderr


@pytest.mark.parametrize(
    "source",
    [
        "from trading_assistant.db.models import AuditEvent\n"
        "session.query(AuditEvent).update({'reason': 'fixture'})\n",
        "from trading_assistant.db.models import AuditEvent\n"
        "AuditEvent.__table__.update().values(reason='fixture')\n",
        "from trading_assistant.db.models import AuditEvent\n"
        "event = load_event(AuditEvent)\n"
        "event.reason = 'fixture'\n",
        "event = load_event()\n"
        "event.reason = 'fixture'\n",
        "from sqlalchemy import text\n"
        "execute = session.execute\n"
        "execute(text('UPDATE audit_events SET reason=:reason'), "
        "{'reason': 'fixture'})\n",
        "from trading_assistant.db.models import AuditEvent\n"
        "write = session.query(AuditEvent).update\n"
        "write({'detail_json': '{}'})\n",
    ],
)
def test_sensitive_write_round_one_bypasses_fail_closed(tmp_path, source):
    root = _trust_fixture(tmp_path)
    _write_fixture_file(
        root,
        "src/trading_assistant/sensitive_round_one.py",
        source,
    )

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert "PLAINTEXT_SENSITIVE_WRITE" in completed.stderr
    assert "fixture" not in completed.stderr


@pytest.mark.parametrize(
    "expression",
    [
        "os.environ.copy()",
        "os.environ.keys()",
        "os.environ.items()",
        "os.environ.values()",
        "os.environ.__getitem__('ALPACA_API_KEY')",
    ],
)
def test_environment_mapping_views_and_copies_are_secret_sources(
    tmp_path,
    expression,
):
    root = _trust_fixture(tmp_path)
    _write_fixture_file(
        root,
        "src/trading_assistant/daemon/main.py",
        "import os\n"
        f"value = {expression}\n",
    )

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert "ENVIRONMENT_SECRETS_IN_PRODUCTION" in completed.stderr


@pytest.mark.parametrize(
    "escape",
    [
        "        arbitrary_helper(provider)\n",
        "        return provider\n",
        "        escaped.append(provider)\n",
    ],
)
def test_development_environment_provider_cannot_escape_authorized_load(
    tmp_path,
    escape,
):
    root = _trust_fixture(tmp_path)
    _write_fixture_file(
        root,
        "src/trading_assistant/db/migrate.py",
        "import os\n"
        "from trading_assistant.security.secrets import (\n"
        "    EnvironmentSecretProvider, load_role_secrets,\n"
        ")\n"
        "def main(args, config):\n"
        "    if args.development_environment_secrets:\n"
        "        provider = EnvironmentSecretProvider(\n"
        "            environ=os.environ, encryption=config.encryption,\n"
        "        )\n"
        f"{escape}"
        "        return load_role_secrets(\n"
        "            'migration', config=config, provider=provider,\n"
        "            allow_environment=True,\n"
        "        )\n",
    )

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert "ENVIRONMENT_SECRETS_IN_PRODUCTION" in completed.stderr


@pytest.mark.parametrize(
    "source",
    [
        "import anthropic\nclient = anthropic.Anthropic()\n",
        "import openai\nclient = openai.OpenAI()\n",
        "from openai import OpenAI as Client\nclient = Client()\n",
        "import httpx\nclient = httpx._client.Client()\n",
        "import urllib.request\nurllib.request.urlopen('https://example.test')\n",
        "from urllib import request as transport\n"
        "transport.urlopen('https://example.test')\n",
        "import websockets as ws\nws.connect('wss://example.test')\n",
        "import socket\nsocket.create_connection(('example.test', 443))\n",
        "from websockets import connect as dial\n"
        "dial('wss://example.test')\n",
        "from socket import create_connection as dial\n"
        "dial(('example.test', 443))\n",
        "import httpx\nmaker = httpx._client.Client\nmaker()\n",
        "import anthropic as vendor\n"
        "maker = vendor.Anthropic\nmaker()\n",
        "import aiohttp.client as transport\n"
        "transport.ClientSession()\n",
    ],
)
def test_module_qualified_direct_network_clients_are_rejected(tmp_path, source):
    root = _trust_fixture(tmp_path)
    _write_fixture_file(
        root,
        "src/trading_assistant/direct_network.py",
        source,
    )

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert "OUTBOUND_CLIENT_UNAPPROVED" in completed.stderr


@pytest.mark.parametrize(
    ("source", "code"),
    [
        (
            "import httpx\n"
            "options = {'follow_redirects': False}\n"
            "options.update(runtime_options)\n"
            "client = httpx.Client(**options)\n",
            "OUTBOUND_CLIENT_UNAPPROVED",
        ),
        (
            "import httpx\nclient = httpx.Client(**runtime_options)\n",
            "OUTBOUND_CLIENT_UNAPPROVED",
        ),
        (
            "params = {}\n"
            "params['access_key'] = credential\n"
            "client.get('/data', params=params)\n",
            "QUERY_SECRET",
        ),
        (
            "params = {'symbol': 'AAPL'}\n"
            "params.update({'api_key': credential})\n"
            "client.get('/data', params=params)\n",
            "QUERY_SECRET",
        ),
    ],
)
def test_network_option_provenance_fails_closed(tmp_path, source, code):
    root = _trust_fixture(tmp_path)
    _write_fixture_file(
        root,
        "src/trading_assistant/network_options.py",
        source,
    )

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert code in completed.stderr


def test_non_network_verify_and_params_decoys_do_not_trigger(tmp_path):
    root = _trust_fixture(tmp_path)
    _write_fixture_file(
        root,
        "src/trading_assistant/non_network_options.py",
        "class UIState:\n"
        "    pass\n"
        "state = UIState()\n"
        "state.verify = False\n"
        "state.params = {'access_key': 'display-label-only'}\n",
    )

    completed = _run_trust_gate(root)

    assert completed.returncode == 0, completed.stdout + completed.stderr


@pytest.mark.parametrize(
    ("source", "code"),
    [
        (
            "response.set_cookie('session', secure=False, "
            "httponly=True, samesite='strict')\n",
            "INSECURE_COOKIE",
        ),
        (
            "response.set_cookie('session', httponly=True, "
            "samesite='strict')\n",
            "INSECURE_COOKIE",
        ),
        (
            "response.set_cookie('session', secure=True)\n",
            "INSECURE_COOKIE",
        ),
        (
            "CORSMiddleware(app, allow_origins=[], allow_origin_regex='.*')\n",
            "WILDCARD_HOST_ORIGIN",
        ),
        (
            "import ssl\ncontext = ssl.create_default_context()\n"
            "context.verify_mode = ssl.CERT_NONE\n",
            "TLS_DISABLED",
        ),
        (
            "import ssl\ncontext = ssl.create_default_context()\n"
            "context.check_hostname = False\n",
            "TLS_DISABLED",
        ),
        (
            "import ssl\ncontext = ssl.create_default_context()\n"
            "context.minimum_version = ssl.TLSVersion.TLSv1\n",
            "TLS_DISABLED",
        ),
    ],
)
def test_transport_round_one_ast_bypasses_fail_closed(
    tmp_path,
    source,
    code,
):
    root = _trust_fixture(tmp_path)
    _write_fixture_file(
        root,
        "src/trading_assistant/transport_round_one.py",
        source,
    )

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert code in completed.stderr


def test_secure_cookie_call_is_accepted(tmp_path):
    root = _trust_fixture(tmp_path)
    _write_fixture_file(
        root,
        "src/trading_assistant/cookie_safe.py",
        "response.set_cookie(\n"
        "    'session', secure=True, httponly=True, samesite='strict',\n"
        ")\n",
    )

    completed = _run_trust_gate(root)

    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_scanned_root_must_be_the_exact_git_toplevel(tmp_path):
    outer = tmp_path / "outer"
    subprocess.run(["git", "init", "-q", str(outer)], check=True)
    root = _trust_fixture(outer, git=False)
    subprocess.run(["git", "add", "--all"], cwd=outer, check=True)

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert completed.stderr.splitlines()[0] == "GIT_TREE_UNPROVEN .:1"


@pytest.mark.parametrize(
    "relative",
    [
        "config.yaml",
        "src/trading_assistant/app/main.py",
    ],
)
def test_security_sensitive_symlinks_are_rejected_before_read(
    tmp_path,
    relative,
):
    root = _trust_fixture(tmp_path)
    target = root / relative
    outside = tmp_path / ("outside-" + target.name)
    outside.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
    target.unlink()
    target.symlink_to(outside)
    subprocess.run(["git", "add", "--all"], cwd=root, check=True)

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert completed.stderr.splitlines()[0] == "GIT_TREE_UNPROVEN .:1"


def test_root_symlink_loop_returns_one_value_free_internal_error(tmp_path):
    left = tmp_path / "unsafe-left"
    right = tmp_path / "unsafe-right"
    left.symlink_to(right)
    right.symlink_to(left)

    completed = _run_trust_gate(left)

    assert completed.returncode == 1
    assert completed.stderr.splitlines() == [
        "INTERNAL_GATE_ERROR internal:1",
        "release static checks: FAIL (1 violation)",
    ]
    assert "Traceback" not in completed.stderr
    assert "unsafe-left" not in completed.stderr


def test_cli_parse_failure_is_one_value_free_internal_error():
    completed = subprocess.run(
        [
            sys.executable,
            "scripts/check_release_safety.py",
            "--unsupported-\x1b[31m\u2028option",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert completed.stderr.splitlines() == [
        "INTERNAL_GATE_ERROR internal:1",
        "release static checks: FAIL (1 violation)",
    ]


def test_unsafe_tracked_path_is_replaced_with_stable_placeholder(tmp_path):
    root = _trust_fixture(tmp_path)
    unsafe_name = "logs/\x1b[31mprivate\u2028runtime.log"
    _write_fixture_file(root, unsafe_name, "private fixture marker\n")
    subprocess.run(["git", "add", "--", unsafe_name], cwd=root, check=True)

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert "TRACKED_RUNTIME_LOG unsafe-path:1" in completed.stderr
    assert "\x1b" not in completed.stderr
    assert "\u2028" not in completed.stderr
    assert "private fixture marker" not in completed.stderr


@pytest.mark.parametrize(
    ("relative", "code"),
    [
        ("tls/server_private_key.pem", "TRACKED_TLS_PRIVATE_KEY"),
        ("tls/server.private.pem", "TRACKED_TLS_PRIVATE_KEY"),
        ("state/runtime.wal", "TRACKED_SQLITE_WAL"),
        ("state/runtime.shm", "TRACKED_SQLITE_SHM"),
        (".envrc", "TRACKED_ENV_FILE"),
        ("backups/plaintext-production.sql", "TRACKED_DECRYPTED_BACKUP"),
    ],
)
def test_broad_private_artifact_names_are_rejected(
    tmp_path,
    relative,
    code,
):
    root = _trust_fixture(tmp_path)
    _write_fixture_file(root, relative, "fixture\n")
    subprocess.run(["git", "add", "--", relative], cwd=root, check=True)

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert code in completed.stderr


@pytest.mark.parametrize(
    "relative",
    [
        "certs/server_cert.pem",
        "certs/localhost.pem",
        "certs/public-certificate.crt",
    ],
)
def test_public_certificate_names_are_not_private_artifacts(
    tmp_path,
    relative,
):
    root = _trust_fixture(tmp_path)
    _write_fixture_file(root, relative, "public certificate fixture\n")
    subprocess.run(["git", "add", "--", relative], cwd=root, check=True)

    completed = _run_trust_gate(root)

    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_executable_plan_cannot_reintroduce_marketstack(tmp_path):
    root = _trust_fixture(tmp_path)
    _write_fixture_file(
        root,
        "docs/superpowers/plans/future.md",
        "Configure MARKETSTACK_API_KEY and run the MarketStack downloader.\n",
    )

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert "OUTBOUND_ORIGIN_UNAPPROVED" in completed.stderr


def test_historical_marketstack_removal_note_is_allowed(tmp_path):
    root = _trust_fixture(tmp_path)
    _write_fixture_file(
        root,
        "docs/superpowers/specs/history.md",
        "Historical non-executable decision: MarketStack was removed; "
        "Alpaca historical data is authoritative.\n",
    )

    completed = _run_trust_gate(root)

    assert completed.returncode == 0, completed.stdout + completed.stderr


@pytest.mark.parametrize(
    "mutation",
    [
        "\nnested = SENSITIVE_FIELDS['audit_events']\n"
        "nested.add('fixture_field')\n",
        "\nouter = SENSITIVE_FIELDS\n"
        "nested = outer['audit_events']\n"
        "nested.clear()\n",
        "\nglobals()['SENSITIVE_FIELDS'] = {}\n",
    ],
)
def test_sensitive_authority_rejects_nested_alias_and_global_rebinding(
    tmp_path,
    mutation,
):
    root = _trust_fixture(tmp_path)
    path = root / "src/trading_assistant/security/sensitive_fields.py"
    path.write_text(
        path.read_text(encoding="utf-8") + mutation,
        encoding="utf-8",
    )

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert "SENSITIVE_REGISTRY_INVALID" in completed.stderr
    assert "fixture_field" not in completed.stderr


@pytest.mark.parametrize(
    "method_body",
    [
        "        self.cached_result = s.get_account_summary()\n"
        "        return self.cached_result",
        "        self.counter += 1\n"
        "        return s.get_account_summary()",
        "        del self.cached_result\n"
        "        return s.get_account_summary()",
    ],
)
def test_chat_reachable_helpers_reject_state_mutation_syntax(
    tmp_path,
    method_body,
):
    root = _trust_fixture(tmp_path)
    _write_fixture_file(
        root,
        "src/trading_assistant/app/agent.py",
        _agent_with_recursive_helper(method_body),
    )

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert "MUTABLE_CHAT_TOOL" in completed.stderr


@pytest.mark.parametrize(
    "source",
    [
        "\nimport httpx\n"
        "def unsafe_direct_request(url):\n"
        "    return httpx.get(url, follow_redirects=False)\n",
        "\nimport httpx\n"
        "def unsafe_client_request(url):\n"
        "    client = httpx.Client(\n"
        "        follow_redirects=False, trust_env=False, proxy=None,\n"
        "    )\n"
        "    return client.get(url)\n",
    ],
)
def test_outbound_wrapper_rejects_unproven_dynamic_direct_request(
    tmp_path,
    source,
):
    root = _trust_fixture(tmp_path)
    path = root / "src/trading_assistant/security/outbound.py"
    path.write_text(
        path.read_text(encoding="utf-8") + source,
        encoding="utf-8",
    )

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert "OUTBOUND_CLIENT_UNAPPROVED" in completed.stderr


def test_named_provider_kwargs_are_resolved_and_origin_checked(tmp_path):
    root = _trust_fixture(tmp_path)
    _write_fixture_file(
        root,
        "src/trading_assistant/llm/anthropic_backend.py",
        "import anthropic\n"
        "options = {\n"
        "    'base_url': 'https://unapproved.invalid',\n"
        "    'api_key': credential,\n"
        "}\n"
        "client = anthropic.Anthropic(**options)\n",
    )

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert "OUTBOUND_ORIGIN_UNAPPROVED" in completed.stderr


def test_literal_provider_kwargs_are_already_fail_closed(tmp_path):
    """Reviewer subclaim counterexample: inline mappings were already rejected."""

    root = _trust_fixture(tmp_path)
    _write_fixture_file(
        root,
        "src/trading_assistant/llm/anthropic_backend.py",
        "import anthropic\n"
        "client = anthropic.Anthropic(**{\n"
        "    'base_url': 'https://unapproved.invalid',\n"
        "    'api_key': credential,\n"
        "})\n",
    )

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert "OUTBOUND_CLIENT_UNAPPROVED" in completed.stderr


def test_uvicorn_unpacked_proxy_options_are_rejected(tmp_path):
    root = _trust_fixture(tmp_path)
    _write_fixture_file(
        root,
        "src/trading_assistant/ops/serve.py",
        "import uvicorn\n"
        "options = {\n"
        "    'proxy_headers': True,\n"
        "    'forwarded_allow_ips': '*',\n"
        "}\n"
        "uvicorn.run(app, **options)\n",
    )

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert "PROXY_HEADERS_TRUSTED" in completed.stderr


@pytest.mark.parametrize(
    "source",
    [
        "from trading_assistant.db.models import AuditEvent\n"
        "event = AuditEvent()\n"
        "alias = event\n"
        "alias.reason = 'fixture'\n",
        "from sqlalchemy import text\n"
        "execute = session.execute\n"
        "run = execute\n"
        "run(text('UPDATE audit_events SET reason=:reason'), "
        "{'reason': 'fixture'})\n",
    ],
)
def test_sensitive_write_alias_chains_fail_closed(tmp_path, source):
    root = _trust_fixture(tmp_path)
    _write_fixture_file(
        root,
        "src/trading_assistant/sensitive_alias_chain.py",
        source,
    )

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert "PLAINTEXT_SENSITIVE_WRITE" in completed.stderr
    assert "fixture" not in completed.stderr


@pytest.mark.parametrize(
    "source",
    [
        "import trading_assistant.security.secrets as secrets\n"
        "Provider = secrets.EnvironmentSecretProvider\n"
        "provider = Provider(environ={}, encryption=None)\n",
        "import os\ncopy = dict(os.environ)\n",
    ],
)
def test_environment_provider_and_mapping_aliases_fail_closed(tmp_path, source):
    root = _trust_fixture(tmp_path)
    _write_fixture_file(
        root,
        "src/trading_assistant/environment_alias.py",
        source,
    )

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert "ENVIRONMENT_SECRETS_IN_PRODUCTION" in completed.stderr


def test_route_registrar_container_indirection_fails_closed(tmp_path):
    root = _trust_fixture(tmp_path)
    _write_fixture_file(
        root,
        "src/trading_assistant/app/main.py",
        "from fastapi import FastAPI\n"
        "def endpoint():\n"
        "    return None\n"
        "def create_app():\n"
        "    app = FastAPI()\n"
        "    @app.get('/covered')\n"
        "    def covered():\n"
        "        return None\n"
        "    registrars = [app.add_api_route]\n"
        "    registrar = registrars[0]\n"
        "    registrar('/hooks-list', endpoint, methods=['GET'])\n"
        "    return app\n",
    )

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert "ROUTE_REGISTRATION_UNPROVEN" in completed.stderr


@pytest.mark.parametrize(
    ("source", "codes"),
    [
        (
            "import http.client\n"
            "connection = http.client.HTTPSConnection('example.test')\n"
            "connection.request('GET', '/')\n",
            ("OUTBOUND_CLIENT_UNAPPROVED",),
        ),
        (
            "import http.client as hc\n"
            "import ssl\n"
            "connection = hc.HTTPSConnection(\n"
            "    'example.test', context=ssl._create_unverified_context(),\n"
            ")\n",
            ("OUTBOUND_CLIENT_UNAPPROVED", "TLS_DISABLED"),
        ),
    ],
)
def test_stdlib_http_client_and_unverified_contexts_are_rejected(
    tmp_path,
    source,
    codes,
):
    root = _trust_fixture(tmp_path)
    _write_fixture_file(
        root,
        "src/trading_assistant/stdlib_network.py",
        source,
    )

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    for code in codes:
        assert code in completed.stderr


def test_shared_query_mapping_alias_mutation_is_rejected(tmp_path):
    root = _trust_fixture(tmp_path)
    _write_fixture_file(
        root,
        "src/trading_assistant/query_alias.py",
        "params = {'symbol': 'AAPL'}\n"
        "alias = params\n"
        "alias.setdefault('api_key', credential)\n"
        "client.get('/data', params=params)\n",
    )

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert "QUERY_SECRET" in completed.stderr


@pytest.mark.parametrize(
    ("source", "code"),
    [
        (
            "from fastapi import FastAPI\n"
            "from starlette.middleware.cors import CORSMiddleware\n"
            "app = FastAPI()\n"
            "app.add_middleware(CORSMiddleware, allow_origins=['*'])\n",
            "WILDCARD_HOST_ORIGIN",
        ),
        (
            "cookie = response.set_cookie\n"
            "cookie('session', secure=False, httponly=True, "
            "samesite='strict')\n",
            "INSECURE_COOKIE",
        ),
        (
            "import ssl\n"
            "factory = ssl.create_default_context\n"
            "context = factory()\n"
            "context.check_hostname = False\n",
            "TLS_DISABLED",
        ),
    ],
)
def test_transport_alias_and_middleware_calls_are_rejected(
    tmp_path,
    source,
    code,
):
    root = _trust_fixture(tmp_path)
    _write_fixture_file(
        root,
        "src/trading_assistant/transport_alias.py",
        source,
    )

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert code in completed.stderr


def test_tracked_production_sql_backup_is_rejected(tmp_path):
    root = _trust_fixture(tmp_path)
    relative = "backups/production.sql"
    _write_fixture_file(root, relative, "fixture backup marker\n")
    subprocess.run(["git", "add", "--", relative], cwd=root, check=True)

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert "TRACKED_DECRYPTED_BACKUP" in completed.stderr
    assert "fixture backup marker" not in completed.stderr


def test_non_network_verify_keyword_call_is_a_decoy(tmp_path):
    root = _trust_fixture(tmp_path)
    _write_fixture_file(
        root,
        "src/trading_assistant/non_network_compare.py",
        "result = compare(expected, actual, verify=False)\n",
    )

    completed = _run_trust_gate(root)

    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_plaintext_format_document_is_not_a_decrypted_backup(tmp_path):
    root = _trust_fixture(tmp_path)
    relative = "docs/plaintext-format.md"
    _write_fixture_file(
        root,
        relative,
        "Documentation about the word plaintext.\n",
    )
    subprocess.run(["git", "add", "--", relative], cwd=root, check=True)

    completed = _run_trust_gate(root)

    assert completed.returncode == 0, completed.stdout + completed.stderr


# ── Task 11 review round 3 regressions ────────────────────────────────


@pytest.mark.parametrize(
    "mutation",
    [
        "\nlocals()['SENSITIVE_FIELDS'] = {}\n",
        "\nexec('SENSITIVE_FIELDS = {}')\n",
        (
            "\nregistry_box = [SENSITIVE_FIELDS]\n"
            "registry_alias = registry_box[0]\n"
            "registry_alias.clear()\n"
        ),
    ],
    ids=("locals-rebind", "exec-rebind", "nested-container-alias"),
)
def test_final_sensitive_authority_rejects_dynamic_and_nested_access(
    tmp_path,
    mutation,
):
    root = _trust_fixture(tmp_path)
    target = root / "src/trading_assistant/security/sensitive_fields.py"
    target.write_text(
        target.read_text(encoding="utf-8") + mutation,
        encoding="utf-8",
    )

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert "SENSITIVE_REGISTRY_INVALID" in completed.stderr


@pytest.mark.parametrize(
    ("relative", "authority", "code"),
    [
        (
            "src/trading_assistant/app/agent.py",
            "READ_ONLY_TOOL_SPECS",
            "CHAT_TOOL_REGISTRY_UNPROVEN",
        ),
        (
            "src/trading_assistant/security/sensitive_fields.py",
            "SENSITIVE_FIELDS",
            "SENSITIVE_REGISTRY_INVALID",
        ),
        (
            "src/trading_assistant/security/secrets.py",
            "_SIMPLE_SECRET_FIELDS",
            "ENVIRONMENT_SECRETS_IN_PRODUCTION",
        ),
        (
            "src/trading_assistant/security/outbound.py",
            "OUTBOUND_ORIGIN_MANIFEST",
            "OUTBOUND_ORIGIN_UNAPPROVED",
        ),
    ],
)
def test_every_final_authority_rejects_dynamic_locals_rebinding(
    tmp_path,
    relative,
    authority,
    code,
):
    root = _trust_fixture(tmp_path)
    target = root / relative
    target.write_text(
        target.read_text(encoding="utf-8")
        + f"\nlocals()[{authority!r}] = {authority}\n",
        encoding="utf-8",
    )

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert code in completed.stderr


@pytest.mark.parametrize(
    ("needle", "replacement"),
    [
        (
            "        s = self.service\n",
            "        s = self.service\n        self.dispatch_state = {}\n",
        ),
        (
            "        s = self.service\n",
            "        s = self.service\n        del self.service\n",
        ),
    ],
    ids=("root-assignment", "root-delete"),
)
def test_chat_dispatch_root_rejects_state_mutation(
    tmp_path,
    needle,
    replacement,
):
    root = _trust_fixture(tmp_path)
    source = _agent_fixture_source()
    assert needle in source
    _write_fixture_file(
        root,
        "src/trading_assistant/app/agent.py",
        source.replace(needle, replacement, 1),
    )

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert "MUTABLE_CHAT_TOOL" in completed.stderr


def test_chat_dispatch_root_rejects_direct_mutating_effect(tmp_path):
    root = _trust_fixture(tmp_path)
    source = _agent_fixture_source()
    needle = "        s = self.service\n"
    assert needle in source
    _write_fixture_file(
        root,
        "src/trading_assistant/app/agent.py",
        source.replace(
            needle,
            needle + "        self.service.cancel_order('order-id')\n",
            1,
        ),
    )

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert "MUTABLE_CHAT_TOOL" in completed.stderr


def test_chat_dispatch_root_rejects_unknown_direct_effect(tmp_path):
    root = _trust_fixture(tmp_path)
    source = _agent_fixture_source()
    needle = "        s = self.service\n"
    assert needle in source
    _write_fixture_file(
        root,
        "src/trading_assistant/app/agent.py",
        source.replace(
            needle,
            needle + "        self.service.unmodeled_effect()\n",
            1,
        ),
    )

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert "CHAT_TOOL_REGISTRY_UNPROVEN" in completed.stderr


def test_outbound_dynamic_policy_guard_must_dominate_transport(tmp_path):
    root = _trust_fixture(tmp_path)
    target = root / "src/trading_assistant/security/outbound.py"
    target.write_text(
        target.read_text(encoding="utf-8")
        + (
            "\nclass NoRedirectSession:\n"
            "    def request(self, method, url, params=None):\n"
            "        response = super().request(\n"
            "            method, url,\n"
            "            params=_validated_query_params(params),\n"
            "        )\n"
            "        self._policy.assert_url(url)\n"
            "        return response\n"
        ),
        encoding="utf-8",
    )

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert "OUTBOUND_CLIENT_UNAPPROVED" in completed.stderr


def test_outbound_dynamic_policy_guard_rejects_url_rebinding_before_transport(
    tmp_path,
):
    root = _trust_fixture(tmp_path)
    target = root / "src/trading_assistant/security/outbound.py"
    target.write_text(
        target.read_text(encoding="utf-8")
        + (
            "\nclass NoRedirectSession:\n"
            "    def request(self, method, url, params=None):\n"
            "        self._policy.assert_url(url)\n"
            "        url = configured_url\n"
            "        return super().request(\n"
            "            method, url,\n"
            "            params=_validated_query_params(params),\n"
            "        )\n"
        ),
        encoding="utf-8",
    )

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert "OUTBOUND_CLIENT_UNAPPROVED" in completed.stderr


def test_chained_query_mapping_targets_share_mutation_provenance(tmp_path):
    root = _trust_fixture(tmp_path)
    target = root / "src/trading_assistant/security/outbound.py"
    target.write_text(
        target.read_text(encoding="utf-8")
        + (
            "\nclass NoRedirectSession:\n"
            "    def request(self, method, url):\n"
            "        params = alias = {'symbol': 'AAPL'}\n"
            "        alias.setdefault('access_key', credential)\n"
            "        self._policy.assert_url(url)\n"
            "        return super().request(method, url, params=params)\n"
        ),
        encoding="utf-8",
    )

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert "QUERY_SECRET" in completed.stderr


def test_chained_provider_options_share_mutation_provenance(tmp_path):
    root = _trust_fixture(tmp_path)
    _write_fixture_file(
        root,
        "src/trading_assistant/llm/anthropic_backend.py",
        "from anthropic import Anthropic\n"
        "options = alias = {'api_key': credential}\n"
        "alias['base_url'] = 'https://unapproved.invalid'\n"
        "client = Anthropic(**options)\n",
    )

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert "OUTBOUND_ORIGIN_UNAPPROVED" in completed.stderr


def test_environment_mapping_unpack_is_rejected(tmp_path):
    root = _trust_fixture(tmp_path)
    _write_fixture_file(
        root,
        "src/trading_assistant/environment_unpack.py",
        "import os\n"
        "copied_environment = {**os.environ}\n",
    )

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert "ENVIRONMENT_SECRETS_IN_PRODUCTION" in completed.stderr


@pytest.mark.parametrize(
    ("source", "code"),
    [
        (
            "import starlette.middleware.cors as cors\n"
            "middlewares = [cors.CORSMiddleware]\n"
            "app.add_middleware(middlewares[0], allow_origins=['*'])\n",
            "WILDCARD_HOST_ORIGIN",
        ),
        (
            "import http.client as http_client\n"
            "clients = [http_client.HTTPSConnection]\n"
            "clients[0]('example.invalid')\n",
            "OUTBOUND_CLIENT_UNAPPROVED",
        ),
        (
            "cookies = [response.set_cookie]\n"
            "cookies[0]('session', secure=False, httponly=True, "
            "samesite='strict')\n",
            "INSECURE_COOKIE",
        ),
        (
            "import ssl as tls\n"
            "factories = [tls._create_unverified_context]\n"
            "factories[0]()\n",
            "TLS_DISABLED",
        ),
    ],
    ids=("cors", "http-client", "cookie", "ssl-factory"),
)
def test_security_call_collection_indirection_fails_closed(
    tmp_path,
    source,
    code,
):
    root = _trust_fixture(tmp_path)
    _write_fixture_file(
        root,
        "src/trading_assistant/security_indirection.py",
        source,
    )

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert code in completed.stderr


def test_tracked_root_production_sql_is_rejected(tmp_path):
    root = _trust_fixture(tmp_path)
    relative = "production.sql"
    _write_fixture_file(root, relative, "fixture backup marker\n")
    subprocess.run(["git", "add", "--", relative], cwd=root, check=True)

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert "TRACKED_DECRYPTED_BACKUP" in completed.stderr
    assert "fixture backup marker" not in completed.stderr


def test_non_network_get_verify_keyword_is_a_decoy(tmp_path):
    root = _trust_fixture(tmp_path)
    _write_fixture_file(
        root,
        "src/trading_assistant/local_store.py",
        "result = local_store.get('record', verify=False)\n",
    )

    completed = _run_trust_gate(root)

    assert completed.returncode == 0, completed.stdout + completed.stderr


@pytest.mark.parametrize(
    "source",
    [
        (
            "import urllib.request\n"
            "urllib.request.build_opener().open("
            "'https://example.invalid/data')\n"
        ),
        (
            "import socket\n"
            "socket.socket().connect(('example.invalid', 443))\n"
        ),
    ],
    ids=("urllib-opener", "socket-instance"),
)
def test_chained_stdlib_network_clients_are_rejected(tmp_path, source):
    root = _trust_fixture(tmp_path)
    _write_fixture_file(
        root,
        "src/trading_assistant/stdlib_chain.py",
        source,
    )

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert "OUTBOUND_CLIENT_UNAPPROVED" in completed.stderr


def _operator_api_static_fixture(tmp_path: Path, source: str | None = None) -> Path:
    root = _trust_fixture(tmp_path)
    operator_source = source or Path(
        "src/trading_assistant/ops/operator_api.py"
    ).read_text(encoding="utf-8")
    _write_fixture_file(
        root,
        "src/trading_assistant/ops/operator_api.py",
        operator_source,
    )
    return root


def test_static_gate_accepts_only_the_proven_terminal_urllib_shape(tmp_path):
    root = _operator_api_static_fixture(tmp_path)

    completed = _run_trust_gate(root)

    assert completed.returncode == 0, completed.stdout + completed.stderr


@pytest.mark.parametrize(
    ("needle", "replacement"),
    [
        (
            '_ORIGIN = "https://localhost:8020"',
            '_ORIGIN = "https://evil.test"',
        ),
        (
            "ProxyHandler({})",
            'ProxyHandler({"https": "http://proxy.test"})',
        ),
        (
            "ssl_context_factory(cafile=str(ca_path))",
            "ssl_context_factory()",
        ),
        (
            "ssl.CERT_REQUIRED",
            "ssl.CERT_NONE",
        ),
        (
            "                return None\n\n        self._opener",
            "                return request\n\n        self._opener",
        ),
        (
            "stream.read(self._max_response_bytes + 1)",
            "stream.read()",
        ),
        (
            "\n\ndef _reject_constant",
            "\n\nextra_opener = build_opener()\n\ndef _reject_constant",
        ),
    ],
    ids=(
        "remote-origin",
        "proxy-enabled",
        "missing-local-ca",
        "insecure-tls",
        "redirect-following",
        "unbounded-read",
        "extra-urllib-opener",
    ),
)
def test_static_gate_rejects_terminal_urllib_shape_regressions(
    tmp_path,
    needle,
    replacement,
):
    source = Path("src/trading_assistant/ops/operator_api.py").read_text(
        encoding="utf-8"
    )
    assert needle in source
    root = _operator_api_static_fixture(
        tmp_path,
        source.replace(needle, replacement, 1),
    )

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert "OUTBOUND_CLIENT_UNAPPROVED" in completed.stderr


@pytest.mark.parametrize(
    "mutation",
    [
        lambda source: source.replace(
            "        try:\n"
            "            body = stream.read(self._max_response_bytes + 1)\n",
            "        if False:\n"
            "            stream.read(self._max_response_bytes + 1)\n"
            "        try:\n"
            "            body = stream.read()\n",
            1,
        ),
        lambda source: source.replace(
            "        if (\n            getattr(context,",
            "        if False and (\n            getattr(context,",
            1,
        ),
        lambda source: source.replace("is not True", "is True", 1),
        lambda source: source.replace(
            "                del args, kwargs\n                return None",
            "                del args, kwargs\n"
            "                if args:\n"
            "                    return request\n"
            "                return None",
            1,
        ),
        lambda source: source.replace(
            "        self._cookies = CookieJar()",
            "        context = other_context\n"
            "        self._cookies = CookieJar()",
            1,
        ),
        lambda source: source.replace(
            "HTTPSHandler(context=context)",
            "HTTPSHandler(context=other_context)",
            1,
        ),
        lambda source: source.replace(
            "        self._opener = opener or build_opener(",
            "        proxy_handler = ProxyHandler({})\n"
            "        self._opener = opener or build_opener(",
            1,
        ).replace("ProxyHandler({}),", "proxy_handler,", 1),
    ],
    ids=(
        "dead-bounded-read-live-unbounded-read",
        "dead-tls-guard",
        "inverted-tls-guard",
        "alternate-redirect-return",
        "context-rebinding",
        "handler-context-disconnect",
        "proxy-alias",
    ),
)
def test_static_gate_rejects_terminal_control_flow_and_dataflow_bypasses(
    tmp_path,
    mutation,
):
    source = Path("src/trading_assistant/ops/operator_api.py").read_text(
        encoding="utf-8"
    )
    mutated = mutation(source)
    assert mutated != source
    root = _operator_api_static_fixture(tmp_path, mutated)

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert "OUTBOUND_CLIENT_UNAPPROVED" in completed.stderr


@pytest.mark.parametrize(
    "mutation",
    [
        lambda source: source.replace(
            "        if not isinstance(body, bytes):\n",
            "        if (body := getattr(stream, \"read\")()) and not isinstance(\n"
            "            body, bytes\n"
            "        ):\n",
            1,
        ),
        lambda source: source.replace(
            "        class _NoRedirect(HTTPRedirectHandler):\n",
            "        def _redirect_rebinder(_class):\n"
            "            return HTTPRedirectHandler\n\n"
            "        @_redirect_rebinder\n"
            "        class _NoRedirect(HTTPRedirectHandler):\n",
            1,
        ),
        lambda source: source.replace(
            "        self._opener = opener or build_opener(\n",
            "        setattr(context, \"check_hostname\", False)\n"
            "        setattr(context, \"verify_mode\", 0)\n"
            "        self._opener = opener or build_opener(\n",
            1,
        ),
        lambda source: source.replace(
            "        if (\n            getattr(context,",
            "        context_alias = context\n"
            "        if (\n            getattr(context,",
            1,
        ).replace(
            "        self._opener = opener or build_opener(\n",
            "        setattr(context_alias, \"check_hostname\", False)\n"
            "        setattr(context_alias, \"verify_mode\", 0)\n"
            "        self._opener = opener or build_opener(\n",
            1,
        ),
    ],
    ids=(
        "indirect-unbounded-read-after-bounded-decoy",
        "active-no-redirect-module-rebinding",
        "context-mutation-between-guard-and-opener",
        "pre-guard-context-alias-post-guard-mutation",
    ),
)
def test_static_gate_rejects_terminal_active_binding_and_mutation_bypasses(
    tmp_path,
    mutation,
):
    source = Path("src/trading_assistant/ops/operator_api.py").read_text(
        encoding="utf-8"
    )
    mutated = mutation(source)
    assert mutated != source
    root = _operator_api_static_fixture(tmp_path, mutated)

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert "OUTBOUND_CLIENT_UNAPPROVED" in completed.stderr


@pytest.mark.parametrize(
    "mutation",
    [
        lambda source: source.replace(
            "        if not isinstance(body, bytes):\n",
            "        if (\n"
            "            body := getattr(locals()[\"stream\"], \"read\")()\n"
            "        ) and not isinstance(body, bytes):\n",
            1,
        ),
        lambda source: source.replace(
            "        class _NoRedirect(HTTPRedirectHandler):\n",
            "        def _redirect_rebinder(_class):\n"
            "            return HTTPRedirectHandler\n\n"
            "        @_redirect_rebinder\n"
            "        class _NoRedirect(HTTPRedirectHandler):\n",
            1,
        ),
        lambda source: source.replace(
            "        self._opener = opener or build_opener(\n",
            "        setattr(locals()[\"context\"], \"check_hostname\", False)\n"
            "        setattr(locals()[\"context\"], \"verify_mode\", 0)\n"
            "        self._opener = opener or build_opener(\n",
            1,
        ),
        lambda source: source.replace(
            'body.decode("utf-8")',
            'body.decode("utf8")',
            1,
        ),
        lambda source: source.replace(
            '            """Treat every redirect as a failed local request, never a new destination."""\n',
            '            """Refuse redirects."""\n',
            1,
        ),
        lambda source: source.replace(
            "        self._opener = opener or build_opener(\n",
            "        pass\n"
            "        self._opener = opener or build_opener(\n",
            1,
        ),
    ],
    ids=(
        "locals-indirect-unbounded-read",
        "redirect-class-decorator-rebinding",
        "locals-context-tls-downgrade",
        "reader-benign-decode-spelling",
        "redirect-benign-docstring",
        "transport-benign-pass",
    ),
)
def test_static_gate_requires_exact_terminal_security_node_shapes(tmp_path, mutation):
    source = Path("src/trading_assistant/ops/operator_api.py").read_text(
        encoding="utf-8"
    )
    mutated = mutation(source)
    assert mutated != source
    root = _operator_api_static_fixture(tmp_path, mutated)

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert "OUTBOUND_CLIENT_UNAPPROVED" in completed.stderr


@pytest.mark.parametrize(
    "mutation",
    [
        lambda source: source.replace(
            "class OperatorApiClient:\n",
            "def _replace_opener_default(client):\n"
            '    client.__init__.__kwdefaults__["opener"] = "attacker-opener"\n'
            "    return client\n\n\n"
            "@_replace_opener_default\n"
            "class OperatorApiClient:\n",
            1,
        ),
        lambda source: source.replace(
            "from ..config import load_config\n",
            "from ..config import load_config\n\nbuild_opener = str\n",
            1,
        ),
        lambda source: source.replace(
            "from ..config import load_config\n",
            "from ..config import load_config\n\nRequest = str\n",
            1,
        ),
        lambda source: source.replace(
            "        try:\n"
            "            with self._opener.open(request,",
            '        request.full_url = payload["url"]\n'
            "        try:\n"
            "            with self._opener.open(request,",
            1,
        ),
        lambda source: source.replace(
            '_ORIGIN = "https://localhost:8020"\n',
            '_ORIGIN = "https://localhost:8020"\n'
            'globals()["_ORIGIN"] = "https://evil.test"\n',
            1,
        ),
        lambda source: source.replace(
            "class OperatorApiClient:\n",
            "def _default_side_effect(\n"
            "    client,\n"
            '    _mutated=globals().__setitem__("Request", str),\n'
            "):\n"
            "    return client\n\n\n"
            "@_default_side_effect\n"
            "class OperatorApiClient:\n",
            1,
        ),
        lambda source: source.replace(
            '_GENERIC_REQUEST_MESSAGE = "Operator API transport failed"\n',
            '_GENERIC_REQUEST_MESSAGE = "Operator API transport failed"\n'
            "_ARBITRARY_EXECUTABLE_STATEMENT = 1\n",
            1,
        ),
        lambda source: source.replace(
            "import json\n",
            "import json\nimport collections\n",
            1,
        ),
    ],
    ids=(
        "class-decorator-mutates-opener-kwdefault",
        "module-build-opener-rebinding",
        "module-request-rebinding",
        "request-url-rebound-before-open",
        "dynamic-origin-namespace-mutation",
        "class-decorator-default-side-effect",
        "arbitrary-module-statement",
        "added-module-import",
    ),
)
def test_operator_api_whole_module_anchor_rejects_executable_changes(
    tmp_path,
    mutation,
):
    source = Path("src/trading_assistant/ops/operator_api.py").read_text(
        encoding="utf-8"
    )
    mutated = mutation(source)
    assert mutated != source
    root = _operator_api_static_fixture(tmp_path, mutated)

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert "OUTBOUND_CLIENT_UNAPPROVED" in completed.stderr


def test_operator_api_whole_module_anchor_accepts_formatting_and_comments(tmp_path):
    source = Path("src/trading_assistant/ops/operator_api.py").read_text(
        encoding="utf-8"
    )
    formatted = source.replace(
        "from urllib.error import HTTPError, URLError\n",
        "# Location-only edits do not change the audited executable structure.\n"
        "from urllib.error import (\n"
        "    HTTPError,\n"
        "    URLError,\n"
        ")\n",
        1,
    ).replace(
        "        request = Request(\n",
        "        # The fixed-origin request remains structurally identical.\n"
        "        request = Request(\n",
        1,
    )
    assert formatted != source
    root = _operator_api_static_fixture(tmp_path, formatted)

    completed = _run_trust_gate(root)

    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_computed_credential_query_key_is_rejected(tmp_path):
    root = _trust_fixture(tmp_path)
    _write_fixture_file(
        root,
        "src/trading_assistant/computed_query.py",
        "import urllib.request\n"
        "credential_key = 'api_' + 'key'\n"
        "target = ('https://example.invalid/data?' + credential_key "
        "+ '=fixture')\n"
        "urllib.request.urlopen(target)\n",
    )

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert "QUERY_SECRET" in completed.stderr


# ── Task 11 review round 4 regressions ────────────────────────────────


def test_final_authority_rejects_mutation_built_collection_alias(tmp_path):
    root = _trust_fixture(tmp_path)
    target = root / "src/trading_assistant/security/sensitive_fields.py"
    target.write_text(
        target.read_text(encoding="utf-8")
        + (
            "\nregistry_box = []\n"
            "registry_box.append(SENSITIVE_FIELDS)\n"
            "registry_box[0].clear()\n"
        ),
        encoding="utf-8",
    )

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert "SENSITIVE_REGISTRY_INVALID" in completed.stderr


def test_sensitive_bound_update_alias_resolves_keyword_values(tmp_path):
    root = _trust_fixture(tmp_path)
    _write_fixture_file(
        root,
        "src/trading_assistant/bound_sensitive_update.py",
        "from trading_assistant.db.models import AuditEvent\n"
        "mutate = session.query(AuditEvent).update\n"
        "mutate(values={'reason': 'round4-fixture'})\n",
    )

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert "PLAINTEXT_SENSITIVE_WRITE" in completed.stderr
    assert "round4-fixture" not in completed.stderr


def test_sensitive_nested_keyword_only_helper_propagates_model(tmp_path):
    root = _trust_fixture(tmp_path)
    _write_fixture_file(
        root,
        "src/trading_assistant/nested_sensitive_helper.py",
        "from trading_assistant.db.models import AuditEvent\n"
        "def nested(*, record):\n"
        "    record.reason = 'round4-fixture'\n"
        "def outer(record):\n"
        "    nested(record=record)\n"
        "event = session.get(AuditEvent, 1)\n"
        "outer(event)\n",
    )

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert "PLAINTEXT_SENSITIVE_WRITE" in completed.stderr
    assert "round4-fixture" not in completed.stderr


@pytest.mark.parametrize(
    ("source", "code"),
    [
        (
            "import starlette.middleware.cors as cors\n"
            "middlewares = []\n"
            "middlewares.append(cors.CORSMiddleware)\n"
            "app.add_middleware(middlewares[0], allow_origins=['*'])\n",
            "WILDCARD_HOST_ORIGIN",
        ),
        (
            "import http.client as http_client\n"
            "clients = []\n"
            "clients.insert(0, http_client.HTTPSConnection)\n"
            "clients[0]('example.invalid')\n",
            "OUTBOUND_CLIENT_UNAPPROVED",
        ),
        (
            "cookies = []\n"
            "cookies.extend([response.set_cookie])\n"
            "cookies[0]('session', secure=False, httponly=True, "
            "samesite='strict')\n",
            "INSECURE_COOKIE",
        ),
        (
            "import ssl as tls\n"
            "factories = {}\n"
            "factories.update(factory=tls._create_unverified_context)\n"
            "factories['factory']()\n",
            "TLS_DISABLED",
        ),
    ],
    ids=("cors-append", "http-insert", "cookie-extend", "ssl-update"),
)
def test_mutation_built_security_identity_collections_fail_closed(
    tmp_path,
    source,
    code,
):
    root = _trust_fixture(tmp_path)
    _write_fixture_file(
        root,
        "src/trading_assistant/mutation_built_security_identity.py",
        source,
    )

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert code in completed.stderr


def test_local_client_verify_keyword_is_not_network_transport(tmp_path):
    root = _trust_fixture(tmp_path)
    _write_fixture_file(
        root,
        "src/trading_assistant/local_client.py",
        "class Client:\n"
        "    def __init__(self, *, verify):\n"
        "        self.verify = verify\n"
        "client = Client(verify=False)\n",
    )

    completed = _run_trust_gate(root)

    assert completed.returncode == 0, completed.stdout + completed.stderr


# ── Plan 4 Task 1: current and historical release artifacts ───────────


def _commit_release_fixture(root: Path, message: str) -> str:
    subprocess.run(["git", "add", "--all"], cwd=root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Release Test",
            "-c",
            "user.email=release-test@example.invalid",
            "commit",
            "-qm",
            message,
            "--allow-empty",
        ],
        cwd=root,
        check=True,
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.mark.parametrize(
    ("relative_path", "code"),
    [
        (".local/operator-note.txt", "TRACKED_LOCAL_ARTIFACT"),
        ("certificates/localhost.pem", "TRACKED_TLS_CERTIFICATE"),
        ("state/runtime.sqlite.snapshot", "TRACKED_SQLITE_DATABASE"),
    ],
)
def test_expanded_current_artifact_rules_reject_private_surfaces(
    tmp_path,
    relative_path,
    code,
):
    root = _trust_fixture(tmp_path)
    _write_fixture_file(root, relative_path, "private fixture value\n")
    subprocess.run(
        ["git", "add", "--", relative_path],
        cwd=root,
        check=True,
    )

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert code in completed.stderr
    assert "private fixture value" not in completed.stderr


def test_current_credential_fingerprint_is_reported_without_value(tmp_path):
    root = _trust_fixture(tmp_path)
    token = "ck_" + "MixedCase9ValueWithEnoughEntropy"
    relative_path = "docs/current-provider-fixture.md"
    _write_fixture_file(root, relative_path, f"credential: {token}\n")
    subprocess.run(
        ["git", "add", "--", relative_path],
        cwd=root,
        check=True,
    )

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert "COMPOSIO_CREDENTIAL_FINGERPRINT" in completed.stderr
    assert token not in completed.stderr
    assert "credential:" not in completed.stderr


def test_deleted_historical_private_artifact_is_still_rejected(tmp_path):
    root = _trust_fixture(tmp_path)
    _commit_release_fixture(root, "baseline")
    relative_path = ".local/retired-provider-output.txt"
    _write_fixture_file(root, relative_path, "retired private value\n")
    offending_commit = _commit_release_fixture(root, "add private artifact")
    (root / relative_path).unlink()
    _commit_release_fixture(root, "remove private artifact")

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert (
        f"TRACKED_LOCAL_ARTIFACT {offending_commit} {relative_path}"
        in completed.stderr
    )
    assert "retired private value" not in completed.stderr
    assert "add private artifact" not in completed.stderr


def test_deleted_historical_credential_reports_only_rule_commit_and_path(
    tmp_path,
):
    root = _trust_fixture(tmp_path)
    _commit_release_fixture(root, "baseline")
    token = "ck_" + "MixedCase9ValueWithEnoughEntropy"
    relative_path = "docs/retired-provider-fixture.md"
    _write_fixture_file(root, relative_path, f"credential: {token}\n")
    offending_commit = _commit_release_fixture(root, "add retired credential")
    (root / relative_path).unlink()
    _commit_release_fixture(root, "remove retired credential")

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    finding = (
        f"COMPOSIO_CREDENTIAL_FINGERPRINT "
        f"{offending_commit} {relative_path}"
    )
    assert finding in completed.stderr
    assert token not in completed.stderr
    assert "credential:" not in completed.stderr
    assert "add retired credential" not in completed.stderr


def test_shallow_history_is_rejected_before_it_can_hide_a_retired_credential(
    tmp_path,
):
    origin_parent = tmp_path / "origin"
    origin_parent.mkdir()
    origin = _trust_fixture(origin_parent)
    _commit_release_fixture(origin, "baseline")
    token = "ck_" + "MixedCase9ShallowHistoryCredential"
    relative_path = "docs/retired-shallow-credential.md"
    _write_fixture_file(origin, relative_path, f"credential: {token}\n")
    _commit_release_fixture(origin, "add retired credential")
    (origin / relative_path).unlink()
    _commit_release_fixture(origin, "remove retired credential")

    shallow = tmp_path / "shallow"
    subprocess.run(
        [
            "git",
            "clone",
            "--quiet",
            "--depth=1",
            origin.resolve().as_uri(),
            str(shallow),
        ],
        check=True,
    )

    completed = _run_trust_gate(shallow)

    assert completed.returncode == 1
    assert completed.stderr.splitlines()[0] == "GIT_HISTORY_SHALLOW .:1"
    assert token not in completed.stderr


@pytest.mark.parametrize(
    ("state", "expected_code"),
    (
        ("grafts", "GIT_HISTORY_GRAFTS"),
        ("replace", "GIT_HISTORY_REPLACE_REFS"),
        ("alternates", "GIT_HISTORY_ALTERNATES"),
        ("partial-clone", "GIT_HISTORY_PARTIAL_CLONE"),
    ),
)
def test_history_indirection_and_partial_clone_state_are_rejected(
    tmp_path,
    state,
    expected_code,
):
    root = _trust_fixture(tmp_path)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if state == "grafts":
        path = root / Path(
            subprocess.run(
                ["git", "rev-parse", "--git-path", "info/grafts"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{head}\n", encoding="ascii")
    elif state == "replace":
        subprocess.run(
            ["git", "replace", head, head],
            cwd=root,
            check=True,
        )
    elif state == "alternates":
        path = root / Path(
            subprocess.run(
                [
                    "git",
                    "rev-parse",
                    "--git-path",
                    "objects/info/alternates",
                ],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("/nonexistent/verifier-alternate\n", encoding="ascii")
    else:
        subprocess.run(
            ["git", "config", "remote.origin.promisor", "true"],
            cwd=root,
            check=True,
        )

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert completed.stderr.splitlines()[0] == f"{expected_code} .:1"


def test_history_scan_requires_the_configured_trusted_anchor(tmp_path):
    root = _trust_fixture(tmp_path)

    completed = _run_trust_gate(
        root,
        trusted_ancestry_anchor="0" * 40,
    )

    assert completed.returncode == 1
    assert (
        completed.stderr.splitlines()[0]
        == "TRUSTED_ANCESTRY_UNPROVEN .:1"
    )


@pytest.mark.parametrize("surface", ("commit", "tag"))
def test_historical_ref_messages_are_scanned_without_printing_values(
    tmp_path,
    surface,
):
    root = _trust_fixture(tmp_path)
    _commit_release_fixture(root, "baseline")
    token = "ck_" + "MixedCase9RefMessageCredential"
    if surface == "commit":
        _write_fixture_file(root, "docs/message-change.md", "safe\n")
        offending_object = _commit_release_fixture(
            root,
            f"message contains {token}",
        )
        expected_path = "commit-message"
    else:
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Release Test",
                "-c",
                "user.email=release-test@example.invalid",
                "tag",
                "-a",
                "release-test",
                "-m",
                f"tag contains {token}",
            ],
            cwd=root,
            check=True,
        )
        offending_object = subprocess.run(
            ["git", "rev-parse", "refs/tags/release-test"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        expected_path = "tag-message"

    completed = _run_trust_gate(root)

    assert completed.returncode == 1
    assert (
        f"COMPOSIO_CREDENTIAL_FINGERPRINT "
        f"{offending_object} {expected_path}"
        in completed.stderr
    )
    assert token not in completed.stderr


def test_static_gate_uses_verified_git_identity_not_later_path_resolution(
    tmp_path,
):
    root = _trust_fixture(tmp_path)
    _commit_release_fixture(root, "baseline")
    git_path = Path(shutil.which("git") or "").resolve()
    assert git_path.is_file()
    fingerprint = "sha256:" + hashlib.sha256(git_path.read_bytes()).hexdigest()
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_git = fake_bin / "git"
    fake_git.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    fake_git.chmod(0o700)
    environment = {
        **os.environ,
        "PATH": str(fake_bin),
        "TRADING_ASSISTANT_VERIFIED_GIT": str(git_path),
        "TRADING_ASSISTANT_VERIFIED_GIT_FINGERPRINT": fingerprint,
        "TRADING_ASSISTANT_TRUSTED_ANCESTRY_ANCHOR": (
            _TRUSTED_ANCESTRY_ANCHORS[root.resolve()]
        ),
    }

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
        env=environment,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_static_gate_rejects_preexisting_path_spoofed_git(tmp_path):
    root = _trust_fixture(tmp_path)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_git = fake_bin / "git"
    fake_git.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_git.chmod(0o700)
    environment = {
        **os.environ,
        "PATH": str(fake_bin),
        "TRADING_ASSISTANT_TRUSTED_ANCESTRY_ANCHOR": (
            _TRUSTED_ANCESTRY_ANCHORS[root.resolve()]
        ),
    }
    environment.pop("TRADING_ASSISTANT_VERIFIED_GIT", None)
    environment.pop("TRADING_ASSISTANT_VERIFIED_GIT_FINGERPRINT", None)

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
        env=environment,
    )

    assert completed.returncode == 1
    assert completed.stderr.splitlines()[0] == "GIT_TOOL_UNPROVEN internal:1"


def test_static_gate_rejects_mismatched_verified_git_without_path_or_hash_leak(
    tmp_path,
):
    root = _trust_fixture(tmp_path)
    _commit_release_fixture(root, "baseline")
    git_path = Path(shutil.which("git") or "").resolve()
    assert git_path.is_file()
    bad_fingerprint = "sha256:" + "0" * 64
    environment = {
        **os.environ,
        "TRADING_ASSISTANT_VERIFIED_GIT": str(git_path),
        "TRADING_ASSISTANT_VERIFIED_GIT_FINGERPRINT": bad_fingerprint,
    }

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
        env=environment,
    )

    assert completed.returncode == 1
    assert completed.stderr.splitlines()[0] == "GIT_TOOL_UNPROVEN internal:1"
    assert str(git_path) not in completed.stderr
    assert bad_fingerprint not in completed.stderr


def test_static_gate_git_runner_uses_private_bounded_regular_spools(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "release_gate_bounded_process_contract",
        Path("scripts/check_release_safety.py"),
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    inspect_descriptors = (
        "import os, stat, sys\n"
        "print('stdout_regular=' + "
        "str(stat.S_ISREG(os.fstat(1).st_mode)))\n"
        "print('stdout_mode=' + "
        "oct(stat.S_IMODE(os.fstat(1).st_mode)))\n"
        "print('stderr_regular=' + "
        "str(stat.S_ISREG(os.fstat(2).st_mode)), file=sys.stderr)\n"
        "print('stderr_mode=' + "
        "oct(stat.S_IMODE(os.fstat(2).st_mode)), file=sys.stderr)\n"
    )

    completed = module._run_bounded_process(
        argv=(sys.executable, "-c", inspect_descriptors),
        cwd=tmp_path,
        env={
            "PATH": os.defpath,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
        },
        input_bytes=None,
        timeout=5.0,
        max_stdout_bytes=4096,
        max_stderr_bytes=4096,
    )

    assert completed is not None
    assert completed.returncode == 0
    assert completed.stdout == (
        b"stdout_regular=True\nstdout_mode=0o600\n"
    )
    assert completed.stderr == (
        b"stderr_regular=True\nstderr_mode=0o600\n"
    )
    marker = tmp_path / "output-limit-marker"
    output_limit_probe = (
        "import os, pathlib, signal\n"
        "signal.signal(signal.SIGXFSZ, signal.SIG_IGN)\n"
        "try:\n"
        "    for _ in range(10000):\n"
        "        os.write(1, b'x' * 1024)\n"
        "except OSError:\n"
        f"    pathlib.Path({str(marker)!r}).write_text('enforced')\n"
        "else:\n"
        f"    pathlib.Path({str(marker)!r}).write_text('missed')\n"
    )
    oversized = module._run_bounded_process(
        argv=(sys.executable, "-c", output_limit_probe),
        cwd=tmp_path,
        env={
            "PATH": os.defpath,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
        },
        input_bytes=None,
        timeout=5.0,
        max_stdout_bytes=128,
        max_stderr_bytes=128,
    )
    assert oversized is None
    assert marker.read_text(encoding="utf-8") == "enforced"


def _ci_workflow() -> tuple[str, dict[str, object]]:
    source = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    parsed = yaml.load(source, Loader=yaml.BaseLoader)
    assert isinstance(parsed, dict)
    return source, parsed


def test_ci_actions_are_commit_pinned_and_checkout_complete_history():
    _source, workflow = _ci_workflow()
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    expected_action_commits = {
        "actions/checkout": "3d3c42e5aac5ba805825da76410c181273ba90b1",
        "astral-sh/setup-uv": "08807647e7069bb48b6ef5acd8ec9567f424441b",
        "gitleaks/gitleaks-action": (
            "ff98106e4c7b2bc287b24eaf42907196329070c7"
        ),
    }

    uses_steps: list[dict[str, object]] = []
    for job in jobs.values():
        assert isinstance(job, dict)
        steps = job.get("steps", [])
        assert isinstance(steps, list)
        uses_steps.extend(
            step
            for step in steps
            if isinstance(step, dict) and "uses" in step
        )

    assert uses_steps
    for step in uses_steps:
        uses = step["uses"]
        assert isinstance(uses, str)
        assert re.fullmatch(r"[^@\s]+@[0-9a-f]{40}", uses), uses
        action, commit = uses.rsplit("@", 1)
        assert commit == expected_action_commits[action]

    checkout_steps = [
        step
        for step in uses_steps
        if str(step["uses"]).startswith("actions/checkout@")
    ]
    assert len(checkout_steps) == len(jobs)
    for step in checkout_steps:
        with_values = step.get("with")
        assert isinstance(with_values, dict)
        assert with_values.get("fetch-depth") == "0"
        assert with_values.get("persist-credentials") == "false"

    setup_uv_steps = [
        step
        for step in uses_steps
        if str(step["uses"]).startswith("astral-sh/setup-uv@")
    ]
    assert len(setup_uv_steps) == 1
    assert setup_uv_steps[0].get("with") == {
        "version": "0.11.28",
    }


def test_ci_matches_offline_release_gate_without_runtime_authority():
    source, workflow = _ci_workflow()
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    verification = jobs["verification"]
    assert isinstance(verification, dict)
    steps = verification["steps"]
    assert isinstance(steps, list)
    named_steps = {
        str(step.get("name")): step
        for step in steps
        if isinstance(step, dict)
    }
    ordered_names = [
        str(step.get("name"))
        for step in steps
        if isinstance(step, dict)
    ]
    assert len(ordered_names) == len(set(ordered_names))
    assert workflow.get("env") is None
    assert workflow.get("defaults") is None
    assert verification.get("env") is None
    assert verification.get("defaults") is None
    assert verification.get("container") is None
    assert verification.get("services") is None
    assert verification.get("if") is None
    assert verification.get("continue-on-error") is None
    assert verification.get("needs") is None
    assert verification.get("runs-on") == "ubuntu-24.04"
    assert verification.get("timeout-minutes") == "60"
    expected_order = (
        "Install locked test dependencies without project code",
        "Run the deterministic release verifier",
        "Verify and install the full locked dependency graph",
        "Audit installed dependencies",
        "Prepare isolated development-only migration secrets",
        "Apply and verify migrations on an isolated database",
        "Run the isolated mock safety drill",
    )
    assert all(name in named_steps for name in expected_order)
    assert [
        ordered_names.index(name)
        for name in expected_order
    ] == sorted(ordered_names.index(name) for name in expected_order)

    dependency_run = str(
        named_steps[
            "Install locked test dependencies without project code"
        ].get("run", "")
    )
    normalized_dependency_run = " ".join(
        dependency_run.replace("\\\n", " ").split()
    )
    assert normalized_dependency_run == (
        "uv lock --check --no-build --no-config "
        "uv sync --frozen --all-extras --dev "
        "--no-install-project --no-build --no-config "
        "--managed-python --python 3.11.15"
    )

    verifier_run = str(
        named_steps["Run the deterministic release verifier"].get("run", "")
    )
    normalized_verifier_run = " ".join(
        verifier_run.replace("\\\n", " ").split()
    )
    assert normalized_verifier_run == " ".join(
        (
            'uv_source="$(command -v uv)"',
            'node_source="$(command -v node)"',
            (
                'verifier_python="$(uv python find --managed-python '
                '--no-project --no-config --resolve-links 3.11.15)"'
            ),
            (
                "sudo install -d -o root -g root -m 0555 "
                "/opt/trading-assistant-verifier/bin"
            ),
            (
                "sudo install -o root -g root -m 0555 "
                '"$uv_source" '
                "/opt/trading-assistant-verifier/bin/uv"
            ),
            (
                "sudo install -o root -g root -m 0555 "
                '"$node_source" '
                "/opt/trading-assistant-verifier/bin/node"
            ),
            'test ! -L /opt/trading-assistant-verifier/bin/uv',
            'test ! -L /opt/trading-assistant-verifier/bin/node',
            (
                'test "$(stat -c \'%u:%g:%a\' '
                '/opt/trading-assistant-verifier/bin)" '
                '= "0:0:555"'
            ),
            (
                'test "$(stat -c \'%u:%g:%a\' '
                '/opt/trading-assistant-verifier/bin/uv)" '
                '= "0:0:555"'
            ),
            (
                'test "$(stat -c \'%u:%g:%a\' '
                '/opt/trading-assistant-verifier/bin/node)" '
                '= "0:0:555"'
            ),
            (
                'export PATH="/opt/trading-assistant-verifier/bin:'
                '/usr/bin:/bin"'
            ),
            '"$verifier_python" -I -S scripts/verify_loopback_release.py',
        )
    )

    verifier_index = ordered_names.index(
        "Run the deterministic release verifier"
    )
    pre_verifier_run_steps = {
        str(step.get("name")): " ".join(
            str(step.get("run", "")).replace("\\\n", " ").split()
        )
        for step in steps[:verifier_index]
        if isinstance(step, dict) and "run" in step
    }
    assert pre_verifier_run_steps == {
        "Set up Python 3.11": "uv python install --no-config 3.11.15",
        "Install locked test dependencies without project code": (
            normalized_dependency_run
        ),
    }
    protected_steps = (
        "Set up Python 3.11",
        "Install locked test dependencies without project code",
        "Run the deterministic release verifier",
    )
    exact_shell = "/bin/bash --noprofile --norc -e -o pipefail {0}"
    for name in protected_steps:
        step = named_steps[name]
        assert step.get("shell") == exact_shell
        assert step.get("env") is None
        assert step.get("if") is None
        assert step.get("continue-on-error") is None
        assert step.get("working-directory") is None

    prepare_run = str(
        named_steps[
            "Prepare isolated development-only migration secrets"
        ].get("run", "")
    )
    mask = 'print(f"::add-mask::{value}")'
    assert prepare_run.index(mask) < prepare_run.index("with open(")
    assert prepare_run.index("with open(") < prepare_run.index(
        "environment.write"
    )

    runs = "\n".join(
        str(step.get("run", ""))
        for step in steps
        if isinstance(step, dict)
    )

    required = (
        "uv sync --all-extras --dev",
        "uv lock --check --no-build --no-config",
        "uv python install --no-config 3.11.15",
        "uv run --with pip-audit==2.10.1 pip-audit",
        "trading_assistant.db.migrate --development-environment-secrets upgrade",
        "trading_assistant.db.migrate --development-environment-secrets status",
        "scripts/verify_loopback_release.py",
        "trading_assistant.ops.safety_drill",
        "PRAGMA wal_checkpoint(TRUNCATE)",
        "PRAGMA journal_mode=DELETE",
        "--development-environment-secrets",
        "--mock",
    )
    for fragment in required:
        assert fragment in runs

    forbidden = (
        "--armed",
        "--alpaca-paper",
        "trading_assistant.daemon",
        "ALPACA_API_KEY",
        "ALPACA_SECRET_KEY",
        "COMPOSIO_API_KEY",
        "ANTHROPIC_API_KEY",
        "security find-generic-password",
        "launchctl",
    )
    for fragment in forbidden:
        assert fragment not in source

    assert "gitleaks/gitleaks-action@" in source
    assert "uv run pytest" not in runs.replace(
        '"$verifier_python" -I -S scripts/verify_loopback_release.py',
        "",
    )
