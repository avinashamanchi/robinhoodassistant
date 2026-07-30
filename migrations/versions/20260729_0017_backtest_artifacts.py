"""persist encrypted truthful backtest artifacts

Revision ID: 20260729_0017
Revises: 20260728_0016
Create Date: 2026-07-29
"""

from alembic import op
import sqlalchemy as sa

from trading_assistant.db.migration_authority import (
    assert_migration_authority,
    migration_schema_fence,
)


revision = "20260729_0017"
down_revision = "20260728_0016"
branch_labels = None
depends_on = None


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
        op.create_table(
            "backtest_artifacts",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "run_id",
                sa.Integer(),
                sa.ForeignKey("backtest_runs.id"),
                nullable=False,
            ),
            sa.Column(
                "artifact_key",
                sa.String(length=160),
                nullable=False,
            ),
            sa.Column(
                "schema_version",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("1"),
            ),
            sa.Column("payload_json", sa.Text(), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.CheckConstraint(
                "schema_version > 0",
                name="ck_backtest_artifacts_schema_version_positive",
            ),
            sa.UniqueConstraint(
                "run_id",
                "artifact_key",
                name="uq_backtest_artifacts_run_key",
            ),
        )
        op.create_index(
            "ix_backtest_artifacts_run_id",
            "backtest_artifacts",
            ["run_id"],
            unique=False,
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
        # Alembic's version row always exists while a revision is running.
        # Touch it to acquire SQLite's writer lock without re-validating a
        # deliberately corrupt protected-domain row. A lower revision must
        # retain authority to classify that row with its own stable blocker.
        # Updating an empty artifact table does not necessarily acquire the
        # lock, which could otherwise allow DDL before the lower check.
        connection.execute(
            sa.text(
                "UPDATE alembic_version SET version_num=version_num"
            )
        )
        rows = connection.scalar(
            sa.text("SELECT count(*) FROM backtest_artifacts")
        )
    except Exception:
        # Preserve the chain-wide fail-before-DDL lock contract.
        raise RuntimeError("runtime_tenure_downgrade_blocked") from None
    if rows:
        raise RuntimeError("backtest_artifact_downgrade_blocked")
    assert_migration_authority(
        authority,
        connection,
        allowed_modes=frozenset({"maintenance"}),
    )
    with migration_schema_fence(authority, connection):
        op.drop_index(
            "ix_backtest_artifacts_run_id",
            table_name="backtest_artifacts",
        )
        op.drop_table("backtest_artifacts")
