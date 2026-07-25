"""deduplicate runtime heartbeats and enforce one row per source

Revision ID: 20260724_0007
Revises: 20260724_0006
Create Date: 2026-07-24
"""

from alembic import op


revision = "20260724_0007"
down_revision = "20260724_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DELETE FROM heartbeats
        WHERE id IN (
            SELECT id
            FROM (
                SELECT
                    id,
                    ROW_NUMBER() OVER (
                        PARTITION BY source
                        ORDER BY at DESC, id DESC
                    ) AS duplicate_rank
                FROM heartbeats
            )
            WHERE duplicate_rank > 1
        )
        """
    )
    op.create_index(
        "uq_heartbeats_source",
        "heartbeats",
        ["source"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "uq_heartbeats_source",
        table_name="heartbeats",
    )
