"""persist plan-order cancellation intent independently from errors

Revision ID: 20260726_0010
Revises: 20260724_0009
Create Date: 2026-07-26
"""

from alembic import op
import sqlalchemy as sa


revision = "20260726_0010"
down_revision = "20260724_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("orders") as batch_op:
        batch_op.add_column(
            sa.Column(
                "plan_cancel_state",
                sa.String(length=16),
                nullable=False,
                server_default="none",
            )
        )
        batch_op.create_index(
            "ix_orders_plan_cancel_state",
            ["plan_cancel_state"],
            unique=False,
        )
    connection = op.get_bind()
    connection.execute(
        sa.text(
            "UPDATE orders "
            "SET plan_cancel_state = CASE "
            "  WHEN last_error_code = 'indeterminate_cancel' "
            "    THEN 'indeterminate' "
            "  ELSE 'requested' "
            "END "
            "WHERE last_error_code IN ("
            "  'plan_cancel',"
            "  'plan_exit_entry_cancel',"
            "  'indeterminate_cancel'"
            ") "
            "AND EXISTS ("
            "  SELECT 1 FROM proposals AS p "
            "  JOIN rules AS r ON r.id = p.source_rule_id "
            "  WHERE p.order_id = orders.id "
            "  AND r.plan_id IS NOT NULL"
            ")"
        )
    )


def downgrade() -> None:
    connection = op.get_bind()
    durable_intents = connection.scalar(
        sa.text(
            "SELECT count(*) FROM orders "
            "WHERE plan_cancel_state != 'none'"
        )
    )
    if durable_intents:
        raise RuntimeError(
            "downgrade would remove durable plan cancellation intent; "
            "restore from a verified pre-upgrade backup instead"
        )
    with op.batch_alter_table("orders") as batch_op:
        batch_op.drop_index("ix_orders_plan_cancel_state")
        batch_op.drop_column("plan_cancel_state")
