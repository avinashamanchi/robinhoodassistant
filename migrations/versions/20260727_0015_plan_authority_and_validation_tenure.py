"""bind reviewed plan authority and add validation runtime tenure

Revision ID: 20260727_0015
Revises: 20260727_0014
Create Date: 2026-07-27
"""

from alembic import op
import sqlalchemy as sa


revision = "20260727_0015"
down_revision = "20260727_0014"
branch_labels = None
depends_on = None


_RUNTIME_RESOURCE_ROLE = (
    "("
    "resource_key = 'runtime:app' AND role = 'app'"
    ") OR ("
    "resource_key = 'runtime:daemon' AND role = 'daemon'"
    ") OR ("
    "resource_key = 'runtime:mcp' AND role = 'mcp'"
    ") OR ("
    "resource_key = 'runtime:validation' AND role = 'validation'"
    ") OR ("
    "resource_key = 'sensitive-migration:global' "
    "AND role = 'maintenance'"
    ")"
)

_OLD_RUNTIME_RESOURCE_ROLE = (
    "("
    "resource_key = 'runtime:app' AND role = 'app'"
    ") OR ("
    "resource_key = 'runtime:daemon' AND role = 'daemon'"
    ") OR ("
    "resource_key = 'runtime:mcp' AND role = 'mcp'"
    ") OR ("
    "resource_key = 'sensitive-migration:global' "
    "AND role = 'maintenance'"
    ")"
)

_RUNTIME_STATE = "state IN ('held','released','fenced')"

_OLD_RUNTIME_STATE = "state IN ('held','released')"

_RUNTIME_LIFECYCLE = (
    "(state = 'held' AND released_at IS NULL "
    "AND renewed_at < expires_at) OR "
    "(state IN ('released','fenced') "
    "AND released_at IS NOT NULL "
    "AND released_at = expires_at)"
)

_OLD_RUNTIME_LIFECYCLE = (
    "(state = 'held' AND released_at IS NULL "
    "AND renewed_at < expires_at) OR "
    "(state = 'released' AND released_at IS NOT NULL "
    "AND released_at = expires_at)"
)


def upgrade() -> None:
    with op.batch_alter_table("trade_plans") as batch:
        batch.add_column(
            sa.Column(
                "authority_version",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch.add_column(
            sa.Column(
                "authority_digest",
                sa.String(length=64),
                nullable=True,
            )
        )
        batch.create_check_constraint(
            "ck_trade_plans_authority_evidence",
            "(authority_version = 0 AND authority_digest IS NULL) OR "
            "(authority_version = 1 "
            "AND length(authority_digest) = 64 "
            "AND authority_digest NOT GLOB '*[^0-9a-f]*')",
        )

    connection = op.get_bind()
    attributes = op.get_context().config.attributes
    schema_capability = attributes.get(
        "runtime_tenure_fence_schema"
    )
    assert_owned = attributes.get(
        "runtime_tenure_assert_owned"
    )
    if schema_capability is not None:
        option_name, option_value = schema_capability
        assert_owned(connection)
        connection.execution_options(
            **{option_name: option_value}
        )
    try:
        with op.batch_alter_table(
            "runtime_tenures",
            recreate="always",
        ) as batch:
            batch.drop_constraint(
                "ck_runtime_tenures_resource_role",
                type_="check",
            )
            batch.drop_constraint(
                "ck_runtime_tenures_state",
                type_="check",
            )
            batch.drop_constraint(
                "ck_runtime_tenures_lifecycle",
                type_="check",
            )
            batch.create_check_constraint(
                "ck_runtime_tenures_resource_role",
                _RUNTIME_RESOURCE_ROLE,
            )
            batch.create_check_constraint(
                "ck_runtime_tenures_state",
                _RUNTIME_STATE,
            )
            batch.create_check_constraint(
                "ck_runtime_tenures_lifecycle",
                _RUNTIME_LIFECYCLE,
            )
    finally:
        if schema_capability is not None:
            connection.execution_options(**{option_name: None})
    if schema_capability is not None:
        assert_owned(connection)


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
        validation_rows = connection.scalar(
            sa.text(
                "SELECT count(*) FROM runtime_tenures "
                "WHERE resource_key='runtime:validation'"
            )
        )
        held_validation = connection.scalar(
            sa.text(
                "SELECT count(*) FROM runtime_tenures "
                "WHERE resource_key='runtime:validation' "
                "AND state = 'held'"
            )
        )
    except Exception:
        raise RuntimeError("runtime_tenure_downgrade_blocked") from None
    if held_validation:
        raise RuntimeError("validation_tenure_downgrade_blocked")
    if validation_rows:
        connection.execute(
            sa.text(
                "DELETE FROM runtime_tenures "
                "WHERE resource_key='runtime:validation'"
            )
        )
    connection.execute(
        sa.text(
            "UPDATE runtime_tenures SET state='released' "
            "WHERE state='fenced'"
        )
    )

    with op.batch_alter_table(
        "runtime_tenures",
        recreate="always",
    ) as batch:
        batch.drop_constraint(
            "ck_runtime_tenures_resource_role",
            type_="check",
        )
        batch.drop_constraint(
            "ck_runtime_tenures_state",
            type_="check",
        )
        batch.drop_constraint(
            "ck_runtime_tenures_lifecycle",
            type_="check",
        )
        batch.create_check_constraint(
            "ck_runtime_tenures_resource_role",
            _OLD_RUNTIME_RESOURCE_ROLE,
        )
        batch.create_check_constraint(
            "ck_runtime_tenures_state",
            _OLD_RUNTIME_STATE,
        )
        batch.create_check_constraint(
            "ck_runtime_tenures_lifecycle",
            _OLD_RUNTIME_LIFECYCLE,
        )

    with op.batch_alter_table("trade_plans") as batch:
        batch.drop_constraint(
            "ck_trade_plans_authority_evidence",
            type_="check",
        )
        batch.drop_column("authority_digest")
        batch.drop_column("authority_version")
