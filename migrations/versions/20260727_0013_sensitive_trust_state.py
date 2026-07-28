"""add sensitive encryption migration and untrusted-ingest trust state

Revision ID: 20260727_0013
Revises: 20260727_0012
Create Date: 2026-07-27
"""

from alembic import op
import sqlalchemy as sa


revision = "20260727_0013"
down_revision = "20260727_0012"
branch_labels = None
depends_on = None


def _hash_check(column: str) -> str:
    return (
        f"length({column}) = 64 "
        f"AND {column} NOT GLOB '*[^0-9a-f]*'"
    )


def _acquire_downgrade_lock() -> None:
    connection = op.get_bind()
    if connection.dialect.name != "sqlite":
        raise RuntimeError("sensitive_trust_downgrade_blocked")
    try:
        # Alembic has already opened the per-migration transaction. This
        # no-op write upgrades it to SQLite's single-writer lock without
        # releasing that transaction before the checks, DDL, or version move.
        connection.execute(
            sa.text(
                "UPDATE sensitive_migration_state "
                "SET singleton_id=singleton_id"
            )
        )
    except Exception:
        raise RuntimeError("sensitive_trust_downgrade_blocked") from None


def _require_pristine_downgrade_state() -> None:
    connection = op.get_bind()
    try:
        states = connection.execute(
            sa.text(
                "SELECT singleton_id,schema_version,state,active_key_id,"
                "rows_total,rows_completed,backup_path_hash,started_at,"
                "completed_at FROM sensitive_migration_state"
            )
        ).mappings().all()
        candidate_count = connection.scalar(
            sa.text("SELECT count(*) FROM candidate_nonces")
        )
        ingest_count = connection.scalar(
            sa.text("SELECT count(*) FROM untrusted_ingest_events")
        )
    except Exception:
        raise RuntimeError("sensitive_trust_downgrade_blocked") from None

    pristine = (
        len(states) == 1
        and states[0]["singleton_id"] == 1
        and states[0]["schema_version"] == 1
        and states[0]["state"] == "required"
        and states[0]["active_key_id"] == "migration-required"
        and states[0]["rows_total"] == 0
        and states[0]["rows_completed"] == 0
        and states[0]["backup_path_hash"] is None
        and states[0]["started_at"] is None
        and states[0]["completed_at"] is None
        and candidate_count == 0
        and ingest_count == 0
    )
    if not pristine:
        raise RuntimeError("sensitive_trust_downgrade_blocked")


def upgrade() -> None:
    op.create_table(
        "sensitive_migration_state",
        sa.Column("singleton_id", sa.Integer(), primary_key=True),
        sa.Column(
            "schema_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column(
            "state",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'required'"),
        ),
        sa.Column("active_key_id", sa.String(length=64), nullable=False),
        sa.Column(
            "rows_total",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "rows_completed",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("backup_path_hash", sa.String(length=64), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "singleton_id = 1",
            name="ck_sensitive_migration_state_singleton",
        ),
        sa.CheckConstraint(
            "schema_version > 0",
            name="ck_sensitive_migration_state_schema_positive",
        ),
        sa.CheckConstraint(
            "state IN ('required','migrating','complete','rotating','failed')",
            name="ck_sensitive_migration_state_state",
        ),
        sa.CheckConstraint(
            "length(active_key_id) BETWEEN 8 AND 64 "
            "AND substr(active_key_id,1,1) GLOB '[A-Za-z0-9]' "
            "AND active_key_id NOT GLOB '*[^A-Za-z0-9._-]*'",
            name="ck_sensitive_migration_state_key_id",
        ),
        sa.CheckConstraint(
            "rows_total >= 0 AND rows_completed >= 0 "
            "AND rows_completed <= rows_total",
            name="ck_sensitive_migration_state_progress",
        ),
        sa.CheckConstraint(
            "backup_path_hash IS NULL OR "
            "(length(backup_path_hash) = 64 "
            "AND backup_path_hash NOT GLOB '*[^0-9a-f]*')",
            name="ck_sensitive_migration_state_backup_hash",
        ),
        sa.CheckConstraint(
            "completed_at IS NULL OR "
            "(started_at IS NOT NULL AND completed_at >= started_at)",
            name="ck_sensitive_migration_state_timestamp_order",
        ),
        sa.CheckConstraint(
            "(state = 'required' AND started_at IS NULL "
            "AND completed_at IS NULL AND rows_completed = 0) OR "
            "(state IN ('migrating','rotating','failed') "
            "AND started_at IS NOT NULL AND completed_at IS NULL) OR "
            "(state = 'complete' AND started_at IS NOT NULL "
            "AND completed_at IS NOT NULL "
            "AND rows_completed = rows_total "
            "AND backup_path_hash IS NOT NULL)",
            name="ck_sensitive_migration_state_lifecycle",
        ),
    )
    op.create_index(
        "ix_sensitive_migration_state_state",
        "sensitive_migration_state",
        ["state"],
    )
    op.create_index(
        "ix_sensitive_migration_state_updated_at",
        "sensitive_migration_state",
        ["updated_at"],
    )
    op.execute(
        sa.text(
            "INSERT INTO sensitive_migration_state "
            "(singleton_id,schema_version,state,active_key_id,"
            "rows_total,rows_completed,updated_at) VALUES "
            "(1,1,'required','migration-required',0,0,CURRENT_TIMESTAMP)"
        )
    )

    op.create_table(
        "candidate_nonces",
        sa.Column("nonce_hash", sa.String(length=64), primary_key=True),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("consumed_at", sa.DateTime(), nullable=True),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.CheckConstraint(
            _hash_check("nonce_hash"),
            name="ck_candidate_nonces_hash",
        ),
        sa.CheckConstraint(
            "length(actor) BETWEEN 1 AND 128",
            name="ck_candidate_nonces_actor",
        ),
        sa.CheckConstraint(
            "length(kind) BETWEEN 1 AND 32",
            name="ck_candidate_nonces_kind",
        ),
        sa.CheckConstraint(
            "length(request_id) BETWEEN 1 AND 64",
            name="ck_candidate_nonces_request_id",
        ),
    )
    op.create_index(
        "ix_candidate_nonces_actor",
        "candidate_nonces",
        ["actor"],
    )
    op.create_index(
        "ix_candidate_nonces_kind",
        "candidate_nonces",
        ["kind"],
    )
    op.create_index(
        "ix_candidate_nonces_expires_at",
        "candidate_nonces",
        ["expires_at"],
    )
    op.create_index(
        "ix_candidate_nonces_consumed_at",
        "candidate_nonces",
        ["consumed_at"],
    )
    op.create_index(
        "ix_candidate_nonces_request_id",
        "candidate_nonces",
        ["request_id"],
    )

    op.create_table(
        "untrusted_ingest_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("byte_length", sa.Integer(), nullable=False),
        sa.Column(
            "flags_json",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column(
            "state",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'received'"),
        ),
        sa.Column(
            "received_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "summary_decision_id",
            sa.Integer(),
            sa.ForeignKey("llm_decisions.id"),
            nullable=True,
        ),
        sa.CheckConstraint(
            _hash_check("source_hash"),
            name="ck_untrusted_ingest_events_source_hash",
        ),
        sa.CheckConstraint(
            _hash_check("content_hash"),
            name="ck_untrusted_ingest_events_content_hash",
        ),
        sa.CheckConstraint(
            "byte_length >= 0",
            name="ck_untrusted_ingest_events_byte_length_nonnegative",
        ),
        sa.CheckConstraint(
            "json_valid(flags_json)",
            name="ck_untrusted_ingest_events_flags_json",
        ),
        sa.CheckConstraint(
            "state IN ('received','summarized','rejected','failed')",
            name="ck_untrusted_ingest_events_state",
        ),
        sa.CheckConstraint(
            "state != 'summarized' OR summary_decision_id IS NOT NULL",
            name="ck_untrusted_ingest_events_summary",
        ),
    )
    op.create_index(
        "ix_untrusted_ingest_events_source_hash",
        "untrusted_ingest_events",
        ["source_hash"],
    )
    op.create_index(
        "ix_untrusted_ingest_events_content_hash",
        "untrusted_ingest_events",
        ["content_hash"],
    )
    op.create_index(
        "ix_untrusted_ingest_events_state",
        "untrusted_ingest_events",
        ["state"],
    )
    op.create_index(
        "ix_untrusted_ingest_events_received_at",
        "untrusted_ingest_events",
        ["received_at"],
    )
    op.create_index(
        "ix_untrusted_ingest_events_summary_decision_id",
        "untrusted_ingest_events",
        ["summary_decision_id"],
    )
    op.create_index(
        "ux_untrusted_ingest_source_content",
        "untrusted_ingest_events",
        ["source_hash", "content_hash"],
        unique=True,
    )


def downgrade() -> None:
    _acquire_downgrade_lock()
    _require_pristine_downgrade_state()

    op.drop_index(
        "ux_untrusted_ingest_source_content",
        table_name="untrusted_ingest_events",
    )
    op.drop_index(
        "ix_untrusted_ingest_events_summary_decision_id",
        table_name="untrusted_ingest_events",
    )
    op.drop_index(
        "ix_untrusted_ingest_events_received_at",
        table_name="untrusted_ingest_events",
    )
    op.drop_index(
        "ix_untrusted_ingest_events_state",
        table_name="untrusted_ingest_events",
    )
    op.drop_index(
        "ix_untrusted_ingest_events_content_hash",
        table_name="untrusted_ingest_events",
    )
    op.drop_index(
        "ix_untrusted_ingest_events_source_hash",
        table_name="untrusted_ingest_events",
    )
    op.drop_table("untrusted_ingest_events")

    op.drop_index(
        "ix_candidate_nonces_request_id",
        table_name="candidate_nonces",
    )
    op.drop_index(
        "ix_candidate_nonces_consumed_at",
        table_name="candidate_nonces",
    )
    op.drop_index(
        "ix_candidate_nonces_expires_at",
        table_name="candidate_nonces",
    )
    op.drop_index(
        "ix_candidate_nonces_kind",
        table_name="candidate_nonces",
    )
    op.drop_index(
        "ix_candidate_nonces_actor",
        table_name="candidate_nonces",
    )
    op.drop_table("candidate_nonces")

    op.drop_index(
        "ix_sensitive_migration_state_updated_at",
        table_name="sensitive_migration_state",
    )
    op.drop_index(
        "ix_sensitive_migration_state_state",
        table_name="sensitive_migration_state",
    )
    op.drop_table("sensitive_migration_state")
