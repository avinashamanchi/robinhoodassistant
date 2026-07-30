"""add durable startup broker-reconciliation generation

Revision ID: 20260724_0008
Revises: 20260724_0007
Create Date: 2026-07-26
"""

from alembic import op
import sqlalchemy as sa


revision = "20260724_0008"
down_revision = "20260724_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "startup_reconciliation_state",
        sa.Column("broker", sa.String(length=64), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("completed_generation", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("evidence_json", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("broker"),
    )
    op.create_index(
        "ix_startup_reconciliation_state_status",
        "startup_reconciliation_state",
        ["status"],
    )


def downgrade() -> None:
    connection = op.get_bind()
    count = connection.scalar(
        sa.text("SELECT count(*) FROM startup_reconciliation_state")
    )
    if count:
        raise RuntimeError(
            "downgrade would remove durable startup reconciliation state; "
            "restore from a verified pre-upgrade backup instead"
        )
    op.drop_index(
        "ix_startup_reconciliation_state_status",
        table_name="startup_reconciliation_state",
    )
    op.drop_table("startup_reconciliation_state")
