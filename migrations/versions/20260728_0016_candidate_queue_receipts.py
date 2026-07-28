"""add crash-safe signed candidate queue receipts

Revision ID: 20260728_0016
Revises: 20260727_0015
Create Date: 2026-07-28
"""

from alembic import op
import sqlalchemy as sa

from trading_assistant.db.migration_authority import (
    assert_migration_authority,
    migration_schema_fence,
)


revision = "20260728_0016"
down_revision = "20260727_0015"
branch_labels = None
depends_on = None


def _hash_check(column: str) -> str:
    return (
        f"length({column}) = 64 "
        f"AND {column} NOT GLOB '*[^0-9a-f]*'"
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
        with op.batch_alter_table("mutation_interlocks") as batch:
            batch.drop_constraint(
                "ck_mutation_interlocks_operation",
                type_="check",
            )
            batch.create_check_constraint(
                "ck_mutation_interlocks_operation",
                "operation IN ("
                "'order_approve','order_reject','breaker_reset','order_cancel',"
                "'portfolio_reconcile','order_sync','panic','analysis',"
                "'plan_approve','plan_cancel','proposal_batch','backtest',"
                "'candidate_queue'"
                ")",
            )
        op.create_table(
            "candidate_queue_receipts",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "session_binding_hash",
                sa.String(length=64),
                nullable=False,
            ),
            sa.Column("actor_hash", sa.String(length=64), nullable=False),
            sa.Column("kind", sa.String(length=8), nullable=False),
            sa.Column(
                "idempotency_key_hash",
                sa.String(length=64),
                nullable=False,
            ),
            sa.Column(
                "candidate_hash",
                sa.String(length=64),
                nullable=False,
            ),
            sa.Column(
                "reason_hash",
                sa.String(length=64),
                nullable=False,
            ),
            sa.Column(
                "nonce_hash",
                sa.String(length=64),
                nullable=False,
            ),
            sa.Column(
                "state",
                sa.String(length=24),
                nullable=False,
                server_default=sa.text("'reserved'"),
            ),
            sa.Column(
                "outcome_code",
                sa.String(length=64),
                nullable=True,
            ),
            sa.Column("target_id", sa.Integer(), nullable=True),
            sa.Column("http_status", sa.Integer(), nullable=True),
            sa.Column(
                "request_id",
                sa.String(length=64),
                nullable=False,
            ),
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
            sa.Column("completed_at", sa.DateTime(), nullable=True),
            sa.CheckConstraint(
                _hash_check("session_binding_hash"),
                name="ck_candidate_queue_receipts_session_hash",
            ),
            sa.CheckConstraint(
                _hash_check("actor_hash"),
                name="ck_candidate_queue_receipts_actor_hash",
            ),
            sa.CheckConstraint(
                "kind IN ('order','rule')",
                name="ck_candidate_queue_receipts_kind",
            ),
            sa.CheckConstraint(
                _hash_check("idempotency_key_hash"),
                name="ck_candidate_queue_receipts_idempotency_hash",
            ),
            sa.CheckConstraint(
                _hash_check("candidate_hash"),
                name="ck_candidate_queue_receipts_candidate_hash",
            ),
            sa.CheckConstraint(
                _hash_check("reason_hash"),
                name="ck_candidate_queue_receipts_reason_hash",
            ),
            sa.CheckConstraint(
                _hash_check("nonce_hash"),
                name="ck_candidate_queue_receipts_nonce_hash",
            ),
            sa.CheckConstraint(
                "state IN ('reserved','target_persisted','completed')",
                name="ck_candidate_queue_receipts_state",
            ),
            sa.CheckConstraint(
                "length(request_id) BETWEEN 1 AND 64",
                name="ck_candidate_queue_receipts_request_id",
            ),
            sa.CheckConstraint(
                "outcome_code IS NULL OR "
                "length(outcome_code) BETWEEN 1 AND 64",
                name="ck_candidate_queue_receipts_outcome",
            ),
            sa.CheckConstraint(
                "target_id IS NULL OR target_id > 0",
                name="ck_candidate_queue_receipts_target_id",
            ),
            sa.CheckConstraint(
                "http_status IS NULL OR http_status BETWEEN 100 AND 599",
                name="ck_candidate_queue_receipts_http_status",
            ),
            sa.CheckConstraint(
                "(state = 'reserved' AND target_id IS NULL "
                "AND outcome_code IS NULL AND http_status IS NULL "
                "AND completed_at IS NULL) OR "
                "(state = 'target_persisted' AND target_id IS NOT NULL "
                "AND outcome_code IS NOT NULL "
                "AND http_status IS NOT NULL AND completed_at IS NULL) OR "
                "(state = 'completed' AND outcome_code IS NOT NULL "
                "AND http_status IS NOT NULL "
                "AND completed_at IS NOT NULL)",
                name="ck_candidate_queue_receipts_lifecycle",
            ),
            sa.UniqueConstraint(
                "session_binding_hash",
                "kind",
                "idempotency_key_hash",
                name="uq_candidate_queue_receipt_identity",
            ),
            sa.UniqueConstraint(
                "nonce_hash",
                name="uq_candidate_queue_receipt_nonce",
            ),
        )
        for name, columns in (
            (
                "ix_candidate_queue_receipts_session_binding_hash",
                ["session_binding_hash"],
            ),
            ("ix_candidate_queue_receipts_actor_hash", ["actor_hash"]),
            ("ix_candidate_queue_receipts_kind", ["kind"]),
            ("ix_candidate_queue_receipts_nonce_hash", ["nonce_hash"]),
            ("ix_candidate_queue_receipts_state", ["state"]),
            ("ix_candidate_queue_receipts_request_id", ["request_id"]),
        ):
            op.create_index(
                name,
                "candidate_queue_receipts",
                columns,
            )


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
        connection.execute(
            sa.text(
                "UPDATE candidate_queue_receipts SET id=id"
            )
        )
        receipt_count = connection.scalar(
            sa.text("SELECT count(*) FROM candidate_queue_receipts")
        )
    except Exception:
        # Preserve the lower migration's fail-before-DDL lock contract.
        raise RuntimeError("runtime_tenure_downgrade_blocked") from None
    if receipt_count:
        raise RuntimeError("candidate_queue_receipt_downgrade_blocked")
    with migration_schema_fence(authority, connection):
        op.drop_table("candidate_queue_receipts")
        with op.batch_alter_table("mutation_interlocks") as batch:
            batch.drop_constraint(
                "ck_mutation_interlocks_operation",
                type_="check",
            )
            batch.create_check_constraint(
                "ck_mutation_interlocks_operation",
                "operation IN ("
                "'order_approve','order_reject','breaker_reset','order_cancel',"
                "'portfolio_reconcile','order_sync','panic','analysis',"
                "'plan_approve','plan_cancel','proposal_batch','backtest'"
                ")",
            )
