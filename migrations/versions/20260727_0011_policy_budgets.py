"""persist rate and provider budget state

Revision ID: 20260727_0011
Revises: 20260726_0010
Create Date: 2026-07-27
"""

from alembic import op
import sqlalchemy as sa


revision = "20260727_0011"
down_revision = "20260726_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rate_windows",
        sa.Column("bucket_key", sa.String(length=64), primary_key=True),
        sa.Column("policy_name", sa.String(length=32), nullable=False),
        sa.Column("window_started_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column(
            "hits",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.CheckConstraint("hits >= 0", name="ck_rate_windows_hits_nonnegative"),
        sa.CheckConstraint(
            "version >= 0", name="ck_rate_windows_version_nonnegative"
        ),
    )
    op.create_index("ix_rate_windows_policy_name", "rate_windows", ["policy_name"])
    op.create_index("ix_rate_windows_expires_at", "rate_windows", ["expires_at"])

    op.create_table(
        "concurrency_leases",
        sa.Column("resource_key", sa.String(length=128), primary_key=True),
        sa.Column(
            "owner",
            sa.String(length=64),
            nullable=False,
            server_default=sa.text("''"),
        ),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column(
            "generation",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.CheckConstraint(
            "generation >= 0", name="ck_concurrency_leases_generation_nonnegative"
        ),
    )
    op.create_index(
        "ix_concurrency_leases_expires_at", "concurrency_leases", ["expires_at"]
    )

    op.create_table(
        "provider_budget_days",
        sa.Column("provider", sa.String(length=32), primary_key=True),
        sa.Column("budget_day", sa.Date(), primary_key=True),
        sa.Column(
            "calls_used",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "input_tokens_used",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "output_tokens_used",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "reconciliation_required",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "reconciliation_code",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("''"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "calls_used >= 0", name="ck_provider_budget_days_calls_nonnegative"
        ),
        sa.CheckConstraint(
            "input_tokens_used >= 0",
            name="ck_provider_budget_days_input_tokens_nonnegative",
        ),
        sa.CheckConstraint(
            "output_tokens_used >= 0",
            name="ck_provider_budget_days_output_tokens_nonnegative",
        ),
    )

    op.create_table(
        "provider_reservations",
        sa.Column("reservation_id", sa.String(length=32), primary_key=True),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("budget_day", sa.Date(), nullable=False),
        sa.Column(
            "state",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'reserved'"),
        ),
        sa.Column("input_reserved", sa.Integer(), nullable=False),
        sa.Column("output_reserved", sa.Integer(), nullable=False),
        sa.Column("input_actual", sa.Integer(), nullable=True),
        sa.Column("output_actual", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("settled_at", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "state IN ('reserved', 'started', 'settled', 'unknown', 'released')",
            name="ck_provider_reservations_state",
        ),
        sa.CheckConstraint(
            "input_reserved >= 0",
            name="ck_provider_reservations_input_reserved_nonnegative",
        ),
        sa.CheckConstraint(
            "output_reserved >= 0",
            name="ck_provider_reservations_output_reserved_nonnegative",
        ),
        sa.CheckConstraint(
            "input_actual IS NULL OR input_actual >= 0",
            name="ck_provider_reservations_input_actual_nonnegative",
        ),
        sa.CheckConstraint(
            "output_actual IS NULL OR output_actual >= 0",
            name="ck_provider_reservations_output_actual_nonnegative",
        ),
    )
    op.create_index(
        "ix_provider_reservations_provider", "provider_reservations", ["provider"]
    )
    op.create_index(
        "ix_provider_reservations_category", "provider_reservations", ["category"]
    )
    op.create_index(
        "ix_provider_reservations_request_id",
        "provider_reservations",
        ["request_id"],
    )
    op.create_index(
        "ix_provider_reservations_budget_day",
        "provider_reservations",
        ["budget_day"],
    )
    op.create_index(
        "ix_provider_reservations_state", "provider_reservations", ["state"]
    )
    op.create_index(
        "ix_provider_reservations_expires_at",
        "provider_reservations",
        ["expires_at"],
    )

    op.create_table(
        "panic_receipts",
        sa.Column("account_scope", sa.String(length=64), primary_key=True),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("response_json", sa.Text(), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "state IN ('started', 'completed', 'failed')",
            name="ck_panic_receipts_state",
        ),
    )
    op.create_index(
        "ix_panic_receipts_request_id", "panic_receipts", ["request_id"]
    )
    op.create_index("ix_panic_receipts_state", "panic_receipts", ["state"])
    op.create_index(
        "ix_panic_receipts_expires_at", "panic_receipts", ["expires_at"]
    )


def downgrade() -> None:
    connection = op.get_bind()
    inflight_reservations = connection.scalar(
        sa.text(
            "SELECT count(*) FROM provider_reservations "
            "WHERE state IN ('started', 'unknown')"
        )
    )
    started_panic_receipts = connection.scalar(
        sa.text("SELECT count(*) FROM panic_receipts WHERE state = 'started'")
    )
    if inflight_reservations or started_panic_receipts:
        raise RuntimeError(
            "downgrade would remove inflight policy state; "
            "restore from a verified pre-upgrade backup instead"
        )

    op.drop_index("ix_panic_receipts_expires_at", table_name="panic_receipts")
    op.drop_index("ix_panic_receipts_state", table_name="panic_receipts")
    op.drop_index("ix_panic_receipts_request_id", table_name="panic_receipts")
    op.drop_table("panic_receipts")

    op.drop_index(
        "ix_provider_reservations_expires_at", table_name="provider_reservations"
    )
    op.drop_index("ix_provider_reservations_state", table_name="provider_reservations")
    op.drop_index(
        "ix_provider_reservations_budget_day", table_name="provider_reservations"
    )
    op.drop_index(
        "ix_provider_reservations_request_id", table_name="provider_reservations"
    )
    op.drop_index(
        "ix_provider_reservations_category", table_name="provider_reservations"
    )
    op.drop_index(
        "ix_provider_reservations_provider", table_name="provider_reservations"
    )
    op.drop_table("provider_reservations")

    op.drop_table("provider_budget_days")

    op.drop_index(
        "ix_concurrency_leases_expires_at", table_name="concurrency_leases"
    )
    op.drop_table("concurrency_leases")

    op.drop_index("ix_rate_windows_expires_at", table_name="rate_windows")
    op.drop_index("ix_rate_windows_policy_name", table_name="rate_windows")
    op.drop_table("rate_windows")
