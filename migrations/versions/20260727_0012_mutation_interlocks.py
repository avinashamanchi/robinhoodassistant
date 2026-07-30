"""persist non-expiring protected-mutation interlocks

Revision ID: 20260727_0012
Revises: 20260727_0011
Create Date: 2026-07-27
"""

from alembic import op
import sqlalchemy as sa


revision = "20260727_0012"
down_revision = "20260727_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "panic_receipts",
        sa.Column(
            "lease_generation",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.create_table(
        "mutation_interlocks",
        sa.Column(
            "resource_key",
            sa.String(length=128),
            primary_key=True,
        ),
        sa.Column("owner", sa.String(length=64), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("operation", sa.String(length=32), nullable=False),
        sa.Column(
            "state",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        sa.Column(
            "outcome_code",
            sa.String(length=64),
            nullable=False,
            server_default=sa.text("''"),
        ),
        sa.Column("worker_finished_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "generation >= 0",
            name="ck_mutation_interlocks_generation_nonnegative",
        ),
        sa.CheckConstraint(
            "operation IN ("
            "'order_approve','order_reject','breaker_reset','order_cancel',"
            "'portfolio_reconcile','order_sync','panic','analysis',"
            "'plan_approve','plan_cancel','proposal_batch','backtest'"
            ")",
            name="ck_mutation_interlocks_operation",
        ),
        sa.CheckConstraint(
            "outcome_code IN ("
            "'','handler_completed','request_cancelled','handler_failed',"
            "'lease_renewal_unproven','lease_ownership_lost',"
            "'panic_settlement_unproven','lease_release_unproven',"
            "'interlock_settlement_unproven'"
            ")",
            name="ck_mutation_interlocks_outcome",
        ),
        sa.CheckConstraint(
            "("
            "state = 'active' AND outcome_code = '' "
            "AND worker_finished_at IS NULL"
            ") OR ("
            "state = 'settled' AND outcome_code = 'handler_completed' "
            "AND worker_finished_at IS NOT NULL"
            ") OR ("
            "state = 'uncertain' AND outcome_code NOT IN "
            "('', 'handler_completed')"
            ")",
            name="ck_mutation_interlocks_state_lifecycle",
        ),
    )
    op.create_index(
        "ix_mutation_interlocks_state",
        "mutation_interlocks",
        ["state"],
    )


def downgrade() -> None:
    connection = op.get_bind()
    durable_latches = connection.scalar(
        sa.text("SELECT count(*) FROM mutation_interlocks")
    )
    if durable_latches:
        raise RuntimeError(
            "downgrade would remove a durable mutation interlock; "
            "reconcile it against broker/domain truth first"
        )
    active_panic = connection.scalar(
        sa.text(
            "SELECT count(*) FROM panic_receipts "
            "WHERE state = 'started'"
        )
    )
    if active_panic:
        raise RuntimeError(
            "downgrade would remove a generation-bound panic receipt"
        )
    op.drop_index(
        "ix_mutation_interlocks_state",
        table_name="mutation_interlocks",
    )
    op.drop_table("mutation_interlocks")
    op.drop_column("panic_receipts", "lease_generation")
