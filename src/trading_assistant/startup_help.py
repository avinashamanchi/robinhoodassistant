"""Turn a startup failure into a stable code and a next step the operator can take.

Codes and remedies are value-free: they come from exception types and stable
codes, never from secret values, paths in details, or broker responses.
"""

from __future__ import annotations

import re

_SAFE_CODE = re.compile(r"^[a-z0-9_.:-]{1,80}$")

_BY_CLASS = {
    "SchemaOutOfDate": "schema_out_of_date",
    "TenureUnavailable": "runtime_tenure_unavailable",
    "TenureLost": "runtime_tenure_lost",
    "TenureUncertain": "runtime_tenure_uncertain",
    "UnsafeKeyringBackend": "keychain_backend_unsafe",
    "UnsafeSecretProvider": "secret_provider_unsafe",
}

_REMEDIES = {
    "installation_not_designated": (
        "Designate this checkout: .venv/bin/python -I src/trading_assistant/installation.py designate"
    ),
    "installation_root_mismatch": (
        "This is not the designated checkout. Start from the designated one, or re-designate deliberately "
        "with `installation.py designate --replace`."
    ),
    "installation_designation_untrusted": (
        "The designation record is unsafe (permissions, links or content). Inspect `installation.py status`."
    ),
    "designated_root_missing": "The designated checkout moved or was removed. Re-designate the current checkout.",
    "database_not_chosen": (
        "Choose the runtime database: installation.py choose-database --path <checkout>/trading_assistant.db "
        "--sha256 <its sha256>. See docs/prototype/database-decision.md."
    ),
    "database_not_the_chosen_file": (
        "database_url resolves to a different file than the chosen one. Start from the designated checkout, "
        "or re-choose deliberately with choose-database --replace."
    ),
    "database_identity_changed": (
        "The chosen database file was replaced (new inode). If that was intended, for example a restore, "
        "choose it again with choose-database --replace."
    ),
    "database_missing": "The configured database file does not exist. Check the chosen database and database_url.",
    "database_url_unsupported": "Only sqlite:/// database URLs are supported.",
    "database_file_untrusted": "The database must be a private (0600) regular file you own with one link.",
    "database_choice_untrusted": "The database choice record is unsafe. Inspect `installation.py status`.",
    "schema_out_of_date": (
        "The database schema is older than this code. Back it up, then run "
        ".venv/bin/python -m trading_assistant.db.migrate upgrade on the chosen database."
    ),
    "runtime_tenure_unavailable": (
        "Another runtime (app, daemon or a maintenance command) holds this database. Stop it, "
        "or wait for its lease to expire, then retry. Only one owner may write."
    ),
    "runtime_tenure_lost": "This runtime lost ownership of the database to another process. Stop all runtimes, then start one.",
    "runtime_tenure_uncertain": "Database ownership could not be proven. Stop all runtimes, then start one.",
    "broker_reconciliation_dependency_unavailable": (
        "Alpaca paper could not be reached for startup reconciliation. Check the network and the paper keys "
        "(ops.secrets audit shows presence only), then retry."
    ),
    "missing": "A required Keychain item is missing. Run `python -m trading_assistant.ops.secrets audit` (presence only).",
}

_DEFAULT = "See logs/<role>.runtime.log and docs/prototype/paper-prototype-guide.md (Troubleshooting)."


def failure_code(error: BaseException) -> str:
    """A stable, value-free code for ``error``."""
    for attribute in ("stable_code", "code"):
        value = getattr(error, attribute, None)
        if isinstance(value, str) and _SAFE_CODE.match(value):
            return value
    return _BY_CLASS.get(type(error).__name__, type(error).__name__)


def remedy(code: str) -> str:
    """What the operator should do next for ``code``."""
    return _REMEDIES.get(code, _DEFAULT)
