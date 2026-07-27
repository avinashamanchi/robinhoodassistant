"""add fill-activated and progressive plan-rule semantics

Revision ID: 20260724_0009
Revises: 20260724_0008
Create Date: 2026-07-26
"""

from alembic import op
import sqlalchemy as sa


revision = "20260724_0009"
down_revision = "20260724_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    unsafe_legacy_plans = connection.scalar(
        sa.text(
            "SELECT count(*) FROM trade_plans AS tp "
            "WHERE tp.status = 'approved' "
            "OR EXISTS ("
            "  SELECT 1 FROM rules AS r "
            "  JOIN rule_groups AS g ON g.id = r.group_id "
            "  WHERE r.plan_id = tp.id "
            "  AND (r.state IN ('pending','active','processing') "
            "       OR g.state IN ('pending','active','processing'))"
            ") "
            "OR EXISTS ("
            "  SELECT 1 FROM proposals AS p "
            "  JOIN rules AS r ON r.group_id = p.source_rule_group_id "
            "  WHERE r.plan_id = tp.id "
            ")"
        )
    )
    if unsafe_legacy_plans:
        raise RuntimeError(
            "migration cannot safely infer independent entry and "
            "fill-activated exit groups for active legacy plans or "
            "other unsafe legacy plans; "
            "cancel those plans before upgrading"
        )
    with op.batch_alter_table("rules") as batch_op:
        batch_op.add_column(
            sa.Column(
                "activation",
                sa.String(length=20),
                nullable=False,
                server_default="immediate",
            )
        )
        batch_op.add_column(
            sa.Column(
                "terminal_on_trigger",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            )
        )
    with op.batch_alter_table("trade_plans") as batch_op:
        batch_op.alter_column(
            "status",
            existing_type=sa.String(length=16),
            type_=sa.String(length=24),
            existing_nullable=False,
        )
        batch_op.add_column(
            sa.Column(
                "entry_filled_qty",
                sa.Numeric(20, 6),
                nullable=False,
                server_default="0",
            )
        )
        batch_op.add_column(
            sa.Column(
                "exit_filled_qty",
                sa.Numeric(20, 6),
                nullable=False,
                server_default="0",
            )
        )
        batch_op.add_column(
            sa.Column(
                "residual_generation",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
    with op.batch_alter_table("proposals") as batch_op:
        batch_op.add_column(
            sa.Column(
                "source_rule_id",
                sa.Integer(),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "plan_generation",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
        batch_op.create_foreign_key(
            "fk_proposals_source_rule_id_rules",
            "rules",
            ["source_rule_id"],
            ["id"],
        )
        batch_op.create_index(
            "ix_proposals_source_rule_id",
            ["source_rule_id"],
            unique=False,
        )


def downgrade() -> None:
    connection = op.get_bind()
    specialized = connection.scalar(
        sa.text(
            "SELECT count(*) FROM rules "
            "WHERE activation != 'immediate' "
            "OR terminal_on_trigger != 1 "
            "OR state = 'pending'"
        )
    )
    pending_groups = connection.scalar(
        sa.text(
            "SELECT count(*) FROM rule_groups WHERE state = 'pending'"
        )
    )
    linked_proposals = connection.scalar(
        sa.text(
            "SELECT count(*) FROM proposals "
            "WHERE source_rule_id IS NOT NULL"
        )
    )
    generated_plan_state = connection.scalar(
        sa.text(
            "SELECT count(*) FROM trade_plans "
            "WHERE status = 'protection_required' "
            "OR entry_filled_qty != 0 "
            "OR exit_filled_qty != 0 "
            "OR residual_generation != 0"
        )
    )
    if (
        specialized
        or pending_groups
        or linked_proposals
        or generated_plan_state
    ):
        raise RuntimeError(
            "downgrade would remove fill-activated plan protection; "
            "restore from a verified pre-upgrade backup instead"
        )
    with op.batch_alter_table("proposals") as batch_op:
        batch_op.drop_index("ix_proposals_source_rule_id")
        batch_op.drop_constraint(
            "fk_proposals_source_rule_id_rules",
            type_="foreignkey",
        )
        batch_op.drop_column("plan_generation")
        batch_op.drop_column("source_rule_id")
    with op.batch_alter_table("trade_plans") as batch_op:
        batch_op.drop_column("residual_generation")
        batch_op.drop_column("exit_filled_qty")
        batch_op.drop_column("entry_filled_qty")
        batch_op.alter_column(
            "status",
            existing_type=sa.String(length=24),
            type_=sa.String(length=16),
            existing_nullable=False,
        )
    with op.batch_alter_table("rules") as batch_op:
        batch_op.drop_column("terminal_on_trigger")
        batch_op.drop_column("activation")
