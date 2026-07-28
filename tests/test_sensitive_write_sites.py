"""Static enforcement for registered sensitive ORM write sites."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import func, select

from trading_assistant.db.models import AuditEvent
from trading_assistant.security.sensitive_fields import (
    PlaintextSensitiveField,
)
from trading_assistant.security.sensitive_write_scan import (
    scan_sensitive_writes,
)


ROOT = Path("src/trading_assistant")
ALLOWED = {
    Path("src/trading_assistant/security/sensitive_fields.py"),
    Path("src/trading_assistant/ops/encrypt_sensitive.py"),
}


def _scan_fixture(tmp_path: Path, source: str) -> list[str]:
    path = tmp_path / "fixture.py"
    path.write_text(source, encoding="utf-8")
    return scan_sensitive_writes([path])


@pytest.mark.parametrize(
    ("source", "marker"),
    [
        (
            """
from trading_assistant.db.models import AuditEvent as Event
row = Event(reason="plain")
""",
            "AuditEvent.reason",
        ),
        (
            """
from trading_assistant.db.models import AuditEvent
Event = AuditEvent
row = Event(**{"reason": "plain"})
""",
            "AuditEvent.**mapping",
        ),
        (
            """
from trading_assistant.db.models import AuditEvent
row = AuditEvent()
row.reason = "plain"
""",
            "AuditEvent.reason",
        ),
        (
            """
from trading_assistant.db.models import AuditEvent
def mutate(row: AuditEvent):
    row.reason = "plain"
""",
            "AuditEvent.reason",
        ),
        (
            """
from trading_assistant.db.models import AuditEvent
row = session.get(AuditEvent, 1)
setattr(row, "detail_json", "{}")
""",
            "AuditEvent.detail_json",
        ),
        (
            """
from sqlalchemy import insert
from trading_assistant.db.models import AuditEvent as Event
statement = insert(Event).values(reason="plain")
""",
            "AuditEvent.reason",
        ),
        (
            """
from sqlalchemy import update
from trading_assistant.db.models import RiskEvent
statement = update(RiskEvent).values(**payload)
""",
            "RiskEvent.**mapping",
        ),
        (
            """
from trading_assistant.db.models import AuditEvent
session.bulk_insert_mappings(
    AuditEvent,
    [{"actor": "a", **payload}],
)
""",
            "AuditEvent.**mapping",
        ),
        (
            """
from sqlalchemy import insert
from trading_assistant.db.models import AuditEvent
session.execute(insert(AuditEvent), [{"reason": "plain"}])
""",
            "AuditEvent.reason",
        ),
        (
            """
from sqlalchemy import text
session.execute(
    text("UPDATE audit_events SET reason = :reason WHERE id = :id")
)
""",
            "audit_events.reason",
        ),
        (
            """
session.execute(
    "INSERT INTO proposals (order_id, reasoning) VALUES (1, 'plain')"
)
""",
            "proposals.reasoning",
        ),
    ],
)
def test_static_gate_rejects_sensitive_write_bypasses(
    tmp_path: Path,
    source: str,
    marker: str,
):
    offenders = _scan_fixture(tmp_path, source)
    assert any(marker in offender for offender in offenders), offenders


def test_release_static_gate_scans_all_production_write_sites():
    paths = [
        path
        for path in sorted(ROOT.rglob("*.py"))
        if path not in ALLOWED
    ]
    assert scan_sensitive_writes(paths) == []


def test_factory_bound_runtime_guard_blocks_direct_plaintext_commit(
    session_factory,
):
    with session_factory() as session:
        session.add(
            AuditEvent(
                actor="test",
                action="static.backstop",
                target_type="test",
                target_id="1",
                request_id="runtime-backstop",
                reason="plain",
                detail_json="{}",
            )
        )
        with pytest.raises(
            PlaintextSensitiveField,
            match="^plaintext_sensitive_field$",
        ):
            session.commit()
        session.rollback()
        assert session.scalar(
            select(func.count()).select_from(AuditEvent)
        ) == 0
