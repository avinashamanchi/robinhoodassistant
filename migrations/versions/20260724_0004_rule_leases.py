"""add typed rule payloads and group leases

Revision ID: 20260724_0004
Revises: 20260724_0003
Create Date: 2026-07-24
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from alembic import op
import sqlalchemy as sa


revision = "20260724_0004"
down_revision = "20260724_0003"
branch_labels = None
depends_on = None

_KINDS = {"price", "entry", "target", "stop", "trailing", "time"}
_NONTERMINAL = {"active", "processing"}


def _positive(value) -> bool:
    try:
        parsed = Decimal(str(value))
        return parsed.is_finite() and parsed > 0
    except (InvalidOperation, TypeError, ValueError):
        return False


def _valid_deadline(value) -> bool:
    if isinstance(value, datetime):
        return True
    try:
        datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    return True


def _condition(row) -> dict:
    source = json.loads(row.condition_json)
    if not isinstance(source, dict):
        raise ValueError("condition must be an object")
    if set(source) == {"price_below"} and _positive(source["price_below"]):
        return {
            "type": "price",
            "direction": "below",
            "price": source["price_below"],
        }
    if set(source) == {"price_above"} and _positive(source["price_above"]):
        return {
            "type": "price",
            "direction": "above",
            "price": source["price_above"],
        }
    if set(source) == {"trailing_stop_pct"}:
        percent = source["trailing_stop_pct"]
        if _positive(percent) and Decimal(str(percent)) <= 100:
            return {"type": "trailing", "percent": percent}
    if (
        row.kind == "time"
        and source == {}
        and row.deadline is not None
        and _valid_deadline(row.deadline)
    ):
        return {"type": "time", "deadline": str(row.deadline)}

    # Accept already-typed known payloads if a 0003 database was written by a
    # staged deployment, while applying the same exact shape checks.
    if set(source) == {"type", "direction", "price"}:
        if (
            source["type"] == "price"
            and source["direction"] in {"below", "above"}
            and _positive(source["price"])
        ):
            return source
    if set(source) == {"type", "percent"}:
        percent = source["percent"]
        if (
            source["type"] == "trailing"
            and _positive(percent)
            and Decimal(str(percent)) <= 100
        ):
            return source
    if set(source) == {"type", "deadline"}:
        if source["type"] == "time" and _valid_deadline(source["deadline"]):
            return source
    raise ValueError("unknown condition shape")


def _action(row) -> dict:
    source = json.loads(row.action_json)
    if not isinstance(source, dict):
        raise ValueError("action must be an object")
    allowed = {"side", "order_type", "qty", "notional", "limit_price"}
    if not set(source) <= allowed or source.get("side") not in {"buy", "sell"}:
        raise ValueError("unknown action shape")
    qty = source.get("qty")
    notional = source.get("notional")
    if (qty is None) == (notional is None):
        raise ValueError("action requires exactly one quantity shape")
    if qty is not None and not _positive(qty):
        raise ValueError("qty must be positive")
    if notional is not None and not _positive(notional):
        raise ValueError("notional must be positive")
    order_type = source.get("order_type", "market")
    if order_type not in {"market", "limit"}:
        raise ValueError("unknown order type")
    limit_price = source.get("limit_price")
    if (order_type == "limit") != (limit_price is not None):
        raise ValueError("invalid limit-price shape")
    if limit_price is not None and not _positive(limit_price):
        raise ValueError("limit_price must be positive")
    return {**source, "order_type": order_type}


def _converted(row) -> tuple[str, str] | None:
    try:
        if row.kind not in _KINDS:
            raise ValueError("unknown rule kind")
        condition = _condition(row)
        action = _action(row)
        expected = (
            "trailing"
            if row.kind == "trailing"
            else "time"
            if row.kind == "time"
            else "price"
        )
        if condition["type"] != expected:
            raise ValueError("condition does not match rule kind")
        return (
            json.dumps(condition, separators=(",", ":"), sort_keys=True),
            json.dumps(action, separators=(",", ":"), sort_keys=True),
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        if row.state in _NONTERMINAL:
            raise RuntimeError(
                f"unknown active rule {row.id}: migration aborted"
            ) from None
        return None


def upgrade() -> None:
    bind = op.get_bind()
    legacy_rows = bind.execute(
        sa.text(
            "SELECT id, ticker, condition_json, action_json, state, created_at, "
            "plan_id, kind, fraction, hwm, deadline, pre_approved "
            "FROM rules ORDER BY id"
        )
    ).mappings().all()

    # Validate every resumable row before SQLite's non-transactional DDL begins.
    conversions = {row.id: _converted(row) for row in legacy_rows}

    op.create_table(
        "rule_groups",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("group_key", sa.String(length=128), nullable=False),
        sa.Column(
            "state",
            sa.String(length=16),
            server_default=sa.text("'active'"),
            nullable=False,
        ),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("terminal_rule_id", sa.Integer(), nullable=True),
        sa.Column(
            "version", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "reconciliation_required",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["terminal_rule_id"], ["rules.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("group_key"),
    )
    op.create_index("ix_rule_groups_group_key", "rule_groups", ["group_key"])
    op.create_index("ix_rule_groups_state", "rule_groups", ["state"])

    with op.batch_alter_table("rules") as batch_op:
        batch_op.add_column(sa.Column("group_id", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "payload_version",
                sa.Integer(),
                server_default=sa.text("1"),
                nullable=False,
            )
        )
        batch_op.create_foreign_key(
            "fk_rules_group_id_rule_groups", "rule_groups", ["group_id"], ["id"]
        )
        batch_op.create_index("ix_rules_group_id", ["group_id"])

    with op.batch_alter_table("proposals") as batch_op:
        batch_op.add_column(
            sa.Column("source_rule_group_id", sa.Integer(), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_proposals_source_rule_group_id_rule_groups",
            "rule_groups",
            ["source_rule_group_id"],
            ["id"],
        )
        batch_op.create_index(
            "ix_proposals_source_rule_group_id", ["source_rule_group_id"]
        )

    now = datetime.now(timezone.utc)
    grouped: dict[str, list] = {}
    for row in legacy_rows:
        key = (
            f"legacy-plan-{row.plan_id}"
            if row.plan_id is not None
            else f"legacy-rule-{row.id}"
        )
        grouped.setdefault(key, []).append(row)

    rule_to_group: dict[int, int] = {}
    for group_key, rows in grouped.items():
        triggered = next((row for row in rows if row.state == "triggered"), None)
        failed = next((row for row in rows if row.state == "failed"), None)
        if triggered is not None:
            state, terminal_rule_id = "triggered", triggered.id
        elif failed is not None:
            state, terminal_rule_id = "failed", failed.id
        elif any(row.state in _NONTERMINAL for row in rows):
            state, terminal_rule_id = "active", None
        else:
            state, terminal_rule_id = "canceled", None
        created_at = min(
            (row.created_at for row in rows if row.created_at is not None),
            default=now,
        )
        result = bind.execute(
            sa.text(
                "INSERT INTO rule_groups "
                "(group_key,state,lease_owner,lease_expires_at,terminal_rule_id,"
                "version,reconciliation_required,created_at,updated_at) "
                "VALUES (:group_key,:state,NULL,NULL,:terminal_rule_id,0,0,"
                ":created_at,:updated_at)"
            ),
            {
                "group_key": group_key,
                "state": state,
                "terminal_rule_id": terminal_rule_id,
                "created_at": created_at,
                "updated_at": now,
            },
        )
        group_id = result.lastrowid
        for row in rows:
            rule_to_group[row.id] = group_id

    for row in legacy_rows:
        converted = conversions[row.id]
        values = {
            "id": row.id,
            "group_id": rule_to_group[row.id],
            "payload_version": 1 if converted is not None else 0,
            "condition_json": (
                converted[0] if converted is not None else row.condition_json
            ),
            "action_json": (
                converted[1] if converted is not None else row.action_json
            ),
        }
        bind.execute(
            sa.text(
                "UPDATE rules SET group_id=:group_id, "
                "payload_version=:payload_version, "
                "condition_json=:condition_json, action_json=:action_json "
                "WHERE id=:id"
            ),
            values,
        )
        bind.execute(
            sa.text(
                "UPDATE proposals SET source_rule_group_id=:group_id "
                "WHERE source_rule_group_id IS NULL AND order_id IN "
                "(SELECT id FROM orders WHERE idempotency_key=:client_order_id)"
            ),
            {
                "group_id": rule_to_group[row.id],
                "client_order_id": f"rule-{row.id}",
            },
        )

    bind.execute(
        sa.text(
            "UPDATE rule_groups SET reconciliation_required=1, "
            "updated_at=:updated_at WHERE EXISTS ("
            "SELECT 1 FROM proposals JOIN orders "
            "ON proposals.order_id=orders.id "
            "WHERE proposals.source_rule_group_id=rule_groups.id "
            "AND orders.status IN ('submitting','acceptance_unknown'))"
        ),
        {"updated_at": now},
    )

    with op.batch_alter_table("rules") as batch_op:
        batch_op.alter_column(
            "group_id", existing_type=sa.Integer(), nullable=False
        )


def downgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT id, kind, condition_json, action_json, payload_version "
            "FROM rules ORDER BY id"
        )
    ).mappings().all()
    for row in rows:
        if row.payload_version != 1:
            continue
        condition = json.loads(row.condition_json)
        action = json.loads(row.action_json)
        if condition.get("type") == "price":
            legacy_condition = {
                f"price_{condition['direction']}": condition["price"]
            }
        elif condition.get("type") == "trailing":
            legacy_condition = {"trailing_stop_pct": condition["percent"]}
        elif condition.get("type") == "time":
            legacy_condition = {}
        else:
            continue
        if action.get("order_type") == "market":
            action.pop("order_type", None)
        bind.execute(
            sa.text(
                "UPDATE rules SET condition_json=:condition_json, "
                "action_json=:action_json WHERE id=:id"
            ),
            {
                "id": row.id,
                "condition_json": json.dumps(legacy_condition),
                "action_json": json.dumps(action),
            },
        )

    with op.batch_alter_table("proposals") as batch_op:
        batch_op.drop_index("ix_proposals_source_rule_group_id")
        batch_op.drop_constraint(
            "fk_proposals_source_rule_group_id_rule_groups",
            type_="foreignkey",
        )
        batch_op.drop_column("source_rule_group_id")
    with op.batch_alter_table("rules") as batch_op:
        batch_op.drop_index("ix_rules_group_id")
        batch_op.drop_constraint(
            "fk_rules_group_id_rule_groups", type_="foreignkey"
        )
        batch_op.drop_column("payload_version")
        batch_op.drop_column("group_id")
    op.drop_index("ix_rule_groups_state", table_name="rule_groups")
    op.drop_index("ix_rule_groups_group_key", table_name="rule_groups")
    op.drop_table("rule_groups")
