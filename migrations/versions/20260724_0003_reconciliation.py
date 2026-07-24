"""add reconciliation activity cursor

Revision ID: 20260724_0003
Revises: 20260724_0002
Create Date: 2026-07-24
"""

from alembic import op
import sqlalchemy as sa


revision = "20260724_0003"
down_revision = "20260724_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reconciliation_cursors",
        sa.Column("broker", sa.String(length=64), nullable=False),
        sa.Column("stream", sa.String(length=64), nullable=False),
        sa.Column("last_activity_id", sa.String(length=128), nullable=True),
        sa.Column("last_activity_at", sa.DateTime(), nullable=True),
        sa.Column(
            "version",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("broker", "stream"),
    )


def downgrade() -> None:
    op.drop_table("reconciliation_cursors")
