"""add fenced cross-role runtime and maintenance tenures

Revision ID: 20260727_0014
Revises: 20260727_0013
Create Date: 2026-07-27
"""

from alembic import op
import sqlalchemy as sa


revision = "20260727_0014"
down_revision = "20260727_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runtime_tenures",
        sa.Column("resource_key", sa.String(length=64), primary_key=True),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("owner_id", sa.String(length=36), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("pid", sa.Integer(), nullable=False),
        sa.Column(
            "process_start_identity",
            sa.String(length=256),
            nullable=False,
        ),
        sa.Column("acquired_at", sa.DateTime(), nullable=False),
        sa.Column("renewed_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("released_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "("
            "resource_key = 'runtime:app' AND role = 'app'"
            ") OR ("
            "resource_key = 'runtime:daemon' AND role = 'daemon'"
            ") OR ("
            "resource_key = 'runtime:mcp' AND role = 'mcp'"
            ") OR ("
            "resource_key = 'sensitive-migration:global' "
            "AND role = 'maintenance'"
            ")",
            name="ck_runtime_tenures_resource_role",
        ),
        sa.CheckConstraint(
            "state IN ('held','released')",
            name="ck_runtime_tenures_state",
        ),
        sa.CheckConstraint(
            "generation > 0",
            name="ck_runtime_tenures_generation_positive",
        ),
        sa.CheckConstraint(
            "length(owner_id) = 36",
            name="ck_runtime_tenures_owner_id",
        ),
        sa.CheckConstraint(
            "pid > 0",
            name="ck_runtime_tenures_pid_positive",
        ),
        sa.CheckConstraint(
            "length(process_start_identity) BETWEEN 1 AND 256",
            name="ck_runtime_tenures_process_identity",
        ),
        sa.CheckConstraint(
            "acquired_at <= renewed_at AND renewed_at <= expires_at",
            name="ck_runtime_tenures_timestamp_order",
        ),
        sa.CheckConstraint(
            "(state = 'held' AND released_at IS NULL "
            "AND renewed_at < expires_at) OR "
            "(state = 'released' AND released_at IS NOT NULL "
            "AND released_at = expires_at)",
            name="ck_runtime_tenures_lifecycle",
        ),
    )
    op.create_index(
        "ix_runtime_tenures_role",
        "runtime_tenures",
        ["role"],
    )
    op.create_index(
        "ix_runtime_tenures_state",
        "runtime_tenures",
        ["state"],
    )
    op.create_index(
        "ix_runtime_tenures_expires_at",
        "runtime_tenures",
        ["expires_at"],
    )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name != "sqlite":
        raise RuntimeError("runtime_tenure_downgrade_blocked")
    try:
        connection.execute(
            sa.text(
                "UPDATE runtime_tenures "
                "SET resource_key=resource_key"
            )
        )
        held = connection.scalar(
            sa.text(
                "SELECT count(*) FROM runtime_tenures "
                "WHERE state != 'released'"
            )
        )
    except Exception:
        raise RuntimeError("runtime_tenure_downgrade_blocked") from None
    if held:
        raise RuntimeError("runtime_tenure_downgrade_blocked")
    op.drop_index(
        "ix_runtime_tenures_expires_at",
        table_name="runtime_tenures",
    )
    op.drop_index(
        "ix_runtime_tenures_state",
        table_name="runtime_tenures",
    )
    op.drop_index(
        "ix_runtime_tenures_role",
        table_name="runtime_tenures",
    )
    op.drop_table("runtime_tenures")
