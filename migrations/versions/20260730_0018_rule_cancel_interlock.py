"""allow guarded rule cancellation interlocks

Revision ID: 20260730_0018
Revises: 20260729_0017
Create Date: 2026-07-30
"""

from alembic import op
import sqlalchemy as sa

from trading_assistant.db.migration_authority import (
    assert_migration_authority,
    migration_schema_fence,
)


revision = "20260730_0018"
down_revision = "20260729_0017"
branch_labels = None
depends_on = None


_OLD_OPERATION_CHECK = (
    "operation IN ("
    "'order_approve','order_reject','breaker_reset','order_cancel',"
    "'portfolio_reconcile','order_sync','panic','analysis',"
    "'plan_approve','plan_cancel','proposal_batch','backtest'"
    ")"
)
_OPERATION_CHECK = (
    "operation IN ("
    "'order_approve','order_reject','breaker_reset','order_cancel',"
    "'rule_cancel',"
    "'portfolio_reconcile','order_sync','panic','analysis',"
    "'plan_approve','plan_cancel','proposal_batch','backtest'"
    ")"
)


def _replace_operation_check(check: str) -> None:
    with op.batch_alter_table("mutation_interlocks") as batch:
        batch.drop_constraint(
            "ck_mutation_interlocks_operation",
            type_="check",
        )
        batch.create_check_constraint(
            "ck_mutation_interlocks_operation",
            check,
        )


def upgrade() -> None:
    connection = op.get_bind()
    authority = op.get_context().config.attributes.get(
        "migration_authority"
    )
    assert_migration_authority(
        authority,
        connection,
        allowed_modes=frozenset({"bootstrap", "maintenance"}),
    )
    with migration_schema_fence(authority, connection):
        _replace_operation_check(_OPERATION_CHECK)


def downgrade() -> None:
    connection = op.get_bind()
    authority = op.get_context().config.attributes.get(
        "migration_authority"
    )
    assert_migration_authority(
        authority,
        connection,
        allowed_modes=frozenset({"maintenance"}),
    )
    try:
        # Preserve the chain-wide fail-before-DDL lock contract. This no-op
        # write acquires SQLite's single-writer lock before the batch table
        # rebuild, keeping a held external writer from leaking a raw error.
        connection.execute(
            sa.text(
                "UPDATE alembic_version SET version_num=version_num"
            )
        )
        rule_cancellations = connection.scalar(
            sa.text(
                "SELECT count(*) FROM mutation_interlocks "
                "WHERE operation='rule_cancel'"
            )
        )
    except Exception:
        raise RuntimeError("runtime_tenure_downgrade_blocked") from None
    if rule_cancellations:
        raise RuntimeError("rule_cancel_interlock_downgrade_blocked")
    assert_migration_authority(
        authority,
        connection,
        allowed_modes=frozenset({"maintenance"}),
    )
    with migration_schema_fence(authority, connection):
        _replace_operation_check(_OLD_OPERATION_CHECK)
