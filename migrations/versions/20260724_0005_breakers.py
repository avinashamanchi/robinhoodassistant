"""replace legacy kill switches with scoped circuit breakers

Revision ID: 20260724_0005
Revises: 20260724_0004
Create Date: 2026-07-24
"""

from alembic import op
import sqlalchemy as sa


revision = "20260724_0005"
down_revision = "20260724_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "fills",
        sa.Column(
            "reconciliation_state",
            sa.String(length=24),
            server_default=sa.text("'trusted'"),
            nullable=False,
        ),
    )
    op.execute(
        """
        UPDATE fills
        SET reconciliation_state = 'quarantined'
        """
    )
    op.execute(
        """
        DELETE FROM reconciliation_cursors
        WHERE
            stream = 'fills'
            AND EXISTS (
                SELECT 1
                FROM fills
                WHERE reconciliation_state = 'quarantined'
            )
        """
    )
    op.create_table(
        "circuit_breaker_state",
        sa.Column("scope_key", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("target", sa.String(length=32), nullable=False),
        sa.Column("tripped", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("scope_key"),
    )
    op.create_index(
        "ix_circuit_breaker_state_kind",
        "circuit_breaker_state",
        ["kind"],
    )
    op.execute(
        """
        INSERT INTO circuit_breaker_state
            (
                scope_key, kind, target, tripped, reason, actor, generation,
                updated_at
            )
        SELECT
            CASE
                WHEN asset_class = 'operator_global' THEN 'operator_global'
                ELSE 'loss:' || asset_class
            END,
            CASE
                WHEN asset_class = 'operator_global' THEN 'operator_global'
                ELSE 'loss'
            END,
            CASE
                WHEN asset_class = 'operator_global' THEN ''
                ELSE asset_class
            END,
            tripped,
            reason,
            'migration:0005',
            1,
            updated_at
        FROM killswitch_state
        """
    )
    op.execute(
        """
        INSERT INTO circuit_breaker_state
            (
                scope_key, kind, target, tripped, reason, actor, generation,
                updated_at
            )
        SELECT
            'broker_drift',
            'broker_drift',
            '',
            1,
            'legacy fill from pre-0005 lacks authoritative broker provenance',
            'migration:0005',
            1,
            CURRENT_TIMESTAMP
        WHERE EXISTS (
            SELECT 1
            FROM fills
            WHERE reconciliation_state = 'quarantined'
        )
        """
    )
    op.execute(
        """
        UPDATE orders
        SET
            acceptance_state = 'fill_reconcile_required',
            last_error_code = 'legacy_unidentified_fill',
            updated_at = CURRENT_TIMESTAMP,
            version = version + 1
        WHERE id IN (
            SELECT order_id
            FROM fills
            WHERE
                reconciliation_state = 'quarantined'
                AND order_id IS NOT NULL
        )
        """
    )
    op.drop_index(
        op.f("ix_killswitch_state_asset_class"),
        table_name="killswitch_state",
    )
    op.drop_table("killswitch_state")
    op.create_table(
        "account_risk_state",
        sa.Column("asset_class", sa.String(length=16), nullable=False),
        sa.Column("high_water_mark", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("last_equity", sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("asset_class"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    fill_count = bind.scalar(sa.text("SELECT count(*) FROM fills"))
    if fill_count:
        raise RuntimeError(
            "cannot safely downgrade migration 0005 with a non-empty fill "
            "trust ledger; restore the verified pre-upgrade backup instead"
        )

    op.create_table(
        "killswitch_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("asset_class", sa.String(length=16), nullable=False),
        sa.Column("tripped", sa.Boolean(), nullable=False),
        sa.Column("tripped_at", sa.DateTime(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_killswitch_state_asset_class"),
        "killswitch_state",
        ["asset_class"],
        unique=True,
    )
    op.execute(
        """
        INSERT INTO killswitch_state
            (asset_class, tripped, tripped_at, reason, updated_at)
        SELECT
            CASE
                WHEN kind = 'operator_global' THEN 'operator_global'
                ELSE target
            END,
            tripped,
            CASE WHEN tripped THEN updated_at ELSE NULL END,
            reason,
            updated_at
        FROM circuit_breaker_state
        WHERE kind IN ('loss', 'operator_global')
        """
    )
    op.drop_table("account_risk_state")
    op.drop_index(
        "ix_circuit_breaker_state_kind",
        table_name="circuit_breaker_state",
    )
    op.drop_table("circuit_breaker_state")
    with op.batch_alter_table("fills") as batch_op:
        batch_op.drop_column("reconciliation_state")
