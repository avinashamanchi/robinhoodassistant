"""add order outbox state

Revision ID: 20260724_0002
Revises: 20260724_0001
Create Date: 2026-07-24
"""

from alembic import op
import sqlalchemy as sa


revision = "20260724_0002"
down_revision = "20260724_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("approval_actor", sa.String(length=128), nullable=True))
    op.add_column(
        "orders",
        sa.Column("approval_reason", sa.Text(), server_default=sa.text("''"), nullable=False),
    )
    op.add_column("orders", sa.Column("approved_at", sa.DateTime(), nullable=True))
    op.add_column(
        "orders",
        sa.Column("submission_kind", sa.String(length=16), server_default=sa.text("'simple'"), nullable=False),
    )
    op.add_column(
        "orders",
        sa.Column("submission_payload_json", sa.Text(), server_default=sa.text("'{}'"), nullable=False),
    )
    op.add_column(
        "orders",
        sa.Column("submission_attempt", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column("orders", sa.Column("submission_started_at", sa.DateTime(), nullable=True))
    op.add_column(
        "orders",
        sa.Column("acceptance_state", sa.String(length=24), server_default=sa.text("'not_started'"), nullable=False),
    )
    op.add_column("orders", sa.Column("last_reconciled_at", sa.DateTime(), nullable=True))
    op.add_column(
        "orders",
        sa.Column("last_error_code", sa.String(length=64), server_default=sa.text("''"), nullable=False),
    )
    op.add_column(
        "orders",
        sa.Column("version", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.create_index("ix_orders_status_broker_order_id", "orders", ["status", "broker_order_id"])
    op.create_index("ix_orders_status_idempotency_key", "orders", ["status", "idempotency_key"])
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("target_type", sa.String(length=32), nullable=False),
        sa.Column("target_id", sa.String(length=64), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), server_default=sa.text("''"), nullable=False),
        sa.Column("reason", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("result_code", sa.String(length=64), server_default=sa.text("''"), nullable=False),
        sa.Column("latency_ms", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("detail_json", sa.Text(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_events_actor", "audit_events", ["actor"])
    op.create_index("ix_audit_events_action", "audit_events", ["action"])
    op.create_index("ix_audit_events_idempotency_key", "audit_events", ["idempotency_key"])
    op.create_index("ix_audit_events_request_id", "audit_events", ["request_id"])
    op.execute("UPDATE orders SET status = 'approval_recorded' WHERE status = 'approved'")


def downgrade() -> None:
    op.execute("UPDATE orders SET status = 'approved' WHERE status = 'approval_recorded'")
    op.drop_index("ix_audit_events_request_id", table_name="audit_events")
    op.drop_index("ix_audit_events_idempotency_key", table_name="audit_events")
    op.drop_index("ix_audit_events_action", table_name="audit_events")
    op.drop_index("ix_audit_events_actor", table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_index("ix_orders_status_idempotency_key", table_name="orders")
    op.drop_index("ix_orders_status_broker_order_id", table_name="orders")
    with op.batch_alter_table("orders") as batch_op:
        batch_op.drop_column("version")
        batch_op.drop_column("last_error_code")
        batch_op.drop_column("last_reconciled_at")
        batch_op.drop_column("acceptance_state")
        batch_op.drop_column("submission_started_at")
        batch_op.drop_column("submission_attempt")
        batch_op.drop_column("submission_payload_json")
        batch_op.drop_column("submission_kind")
        batch_op.drop_column("approved_at")
        batch_op.drop_column("approval_reason")
        batch_op.drop_column("approval_actor")
