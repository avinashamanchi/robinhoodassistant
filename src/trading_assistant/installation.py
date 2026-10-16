"""Which checkout is the operator's designated runtime installation.

The operator launcher, the operator terminal, runtime consolidation, and the
launchd installer act on one runtime: its database, TLS material, virtual
environment, and logs. A second checkout using the same Keychain credentials
would be a second runtime against the same Alpaca paper account, so these
entry points refuse to run unless this checkout is the one the operator
designated.

That property used to be enforced by a hard-coded absolute path, which broke
every entry point (and every launchd job) as soon as the checkout moved. The
designation is now explicit, per user, and outside the repository:

    ~/Library/Application Support/trading-assistant/installation-root

It holds one absolute, symlink-free path. It must be a regular file owned by
the current user with no group or other permissions, inside a private
(0700, non-symlink) directory. The home directory comes from ``HOME``
(validated), matching the shell launcher.

Read-only commands never change the designation:

    python -m trading_assistant.installation status
    python -m trading_assistant.installation check --project PATH [--venv-python PY]

Only ``designate`` changes it, and it refuses to replace a different existing
designation unless ``--replace`` is given:

    python -m trading_assistant.installation designate [--replace]

The module is stdlib-only, so ``install.sh`` can run this file directly with
the venv interpreter even when the package itself cannot be imported.

Scope of the protection: one designated checkout per macOS user account.
It does not stop another user account, another machine, a second ``HOME``,
or someone deliberately re-designating, from running a second runtime
against the same Alpaca paper account; nothing local can. Within that scope,
production runtimes refuse to bind a real broker outside the designated
checkout (``bootstrap``), and each runtime's database tenure still prevents
two writers on one database.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import pwd
import stat
import subprocess
import sys
from typing import Iterator
import uuid

DESIGNATION_RELATIVE = (
    Path("Library") / "Application Support" / "trading-assistant"
    / "installation-root"
)
_MAX_DESIGNATION_BYTES = 4096
DATABASE_CHOICE_RELATIVE = DESIGNATION_RELATIVE.with_name("runtime-database")
RUNTIME_DATABASE_NAME = "trading_assistant.db"
_SQLITE_PREFIX = "sqlite:///"
_DATABASE_SIDECARS = ("-wal", "-shm", "-journal")
_EDITABLE_PTH = "_editable_impl_trading_assistant.pth"


class InstallationError(RuntimeError):
    """A stable, value-free reason the installation cannot be trusted."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def _require_checkout(root: Path) -> None:
    markers = (
        root / "pyproject.toml",
        root / "scripts" / "operator.sh",
        root / "src" / "trading_assistant" / "__init__.py",
    )
    if not all(marker.is_file() for marker in markers):
        raise InstallationError("installation_root_invalid", str(root))


def source_root() -> Path:
    """The checkout this code was imported from."""
    root = Path(__file__).resolve(strict=True).parents[2]
    _require_checkout(root)
    return root


def account_home() -> Path:
    raw = os.environ.get("HOME") or pwd.getpwuid(os.getuid()).pw_dir
    home = Path(raw)
    if not home.is_absolute() or home.is_symlink() or not home.is_dir():
        raise InstallationError("account_home_invalid")
    return home


def designation_path(home: Path | None = None) -> Path:
    return (home or account_home()) / DESIGNATION_RELATIVE


def _require_private_directory(directory: Path) -> None:
    try:
        info = os.lstat(directory)
    except FileNotFoundError:
        return
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        raise InstallationError(
            "installation_designation_untrusted", str(directory)
        )


def _require_physical_components(home: Path, path: Path) -> None:
    """No component between HOME and the record may be a symlink."""
    current = home
    for part in path.relative_to(home).parts:
        current = current / part
        if current.is_symlink():
            raise InstallationError(
                "installation_designation_untrusted", str(current)
            )


def read_designation(home: Path | None = None) -> Path:
    """The designated installation root, after validating the record."""
    base = home or account_home()
    path = designation_path(base)
    _require_physical_components(base, path)
    _require_private_directory(path.parent)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise InstallationError("installation_not_designated", str(path)) from None
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
        or info.st_nlink != 1
        or info.st_size > _MAX_DESIGNATION_BYTES
    ):
        raise InstallationError("installation_designation_untrusted", str(path))
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise InstallationError("installation_designation_untrusted", str(path)) from None
    value = text[:-1] if text.endswith("\n") else text
    if not value or "\n" in value or "\0" in value or not value.startswith("/"):
        raise InstallationError("installation_designation_untrusted", str(path))
    root = Path(value)
    try:
        physical = root.resolve(strict=True)
    except OSError:
        raise InstallationError("designated_root_missing", value) from None
    if physical != root or str(root) != os.path.normpath(value):
        raise InstallationError("designated_root_not_physical", value)
    _require_checkout(root)
    return root


def require_designated(
    root: Path | None = None,
    *,
    home: Path | None = None,
) -> Path:
    """Return the designated root, or refuse if this checkout is not it."""
    actual = (root if root is not None else source_root()).resolve(strict=True)
    designated = read_designation(home)
    if actual != designated:
        raise InstallationError(
            "installation_root_mismatch",
            f"this checkout is {actual}; the designated runtime is {designated}",
        )
    return designated


@contextmanager
def _designation_lock(directory: Path) -> Iterator[None]:
    """Serialise designation changes (read-check-replace is one step)."""
    descriptor = os.open(
        directory / ".installation-root.lock",
        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
        0o600,
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def designate(
    root: Path | None = None,
    *,
    home: Path | None = None,
    replace: bool = False,
) -> tuple[Path, Path | None]:
    """Record ``root`` (default: this checkout) as the designated runtime.

    Returns ``(designated, previous)``. Replacing a *different* existing,
    valid designation requires ``replace=True``; re-designating the same
    root, or replacing an unusable record (for example after the old
    checkout was moved away), does not.
    """
    target = (root if root is not None else source_root()).resolve(strict=True)
    _require_checkout(target)
    base = home or account_home()
    path = designation_path(base)
    _require_physical_components(base, path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    _require_private_directory(path.parent)
    with _designation_lock(path.parent):
        previous: Path | None
        try:
            previous = read_designation(home)
        except InstallationError:
            previous = None
        if previous is not None and previous != target and not replace:
            raise InstallationError(
                "installation_already_designated",
                f"{previous} is designated; pass --replace to change it",
            )
        staging = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        descriptor = os.open(
            staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(f"{target}\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(staging, path)
        finally:
            staging.unlink(missing_ok=True)
    return target, previous


# -- runtime database choice --------------------------------------------------
#
# The runtime database is chosen explicitly, never by working directory. The
# choice is a second private record beside the designation:
#
#     ~/Library/Application Support/trading-assistant/runtime-database
#
# It holds the database's absolute path and file identity (device, inode).
# Production startup refuses unless the configured database_url resolves to
# exactly that file, so a relative URL run from another directory, a worktree
# database, or a file swapped in place (a new inode) is refused until the
# operator chooses again.


def database_choice_path(home: Path | None = None) -> Path:
    return (home or account_home()) / DATABASE_CHOICE_RELATIVE


def sqlite_database_path(database_url: str, *, cwd: Path | None = None) -> Path:
    """The file a ``sqlite:///`` URL names, relative paths resolved against cwd."""
    if not isinstance(database_url, str) or not database_url.startswith(
        _SQLITE_PREFIX
    ):
        raise InstallationError("database_url_unsupported")
    raw = database_url[len(_SQLITE_PREFIX):]
    if not raw or raw.startswith(":memory:") or any(c in raw for c in "?#\0"):
        raise InstallationError("database_url_unsupported")
    path = Path(raw)
    return path if path.is_absolute() else (cwd or Path.cwd()) / path


def _require_database_file(path: Path) -> os.stat_result:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise InstallationError("database_missing", str(path)) from None
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
        or info.st_nlink != 1
    ):
        raise InstallationError("database_file_untrusted", str(path))
    return info


def read_database_choice(home: Path | None = None) -> dict[str, object]:
    """The chosen runtime database, after validating the record."""
    base = home or account_home()
    path = database_choice_path(base)
    _require_physical_components(base, path)
    _require_private_directory(path.parent)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise InstallationError("database_not_chosen", str(path)) from None
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
        or info.st_nlink != 1
        or info.st_size > _MAX_DESIGNATION_BYTES
    ):
        raise InstallationError("database_choice_untrusted", str(path))
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        chosen = Path(record["path"])
        device = record["device"]
        inode = record["inode"]
    except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError):
        raise InstallationError("database_choice_untrusted", str(path)) from None
    if (
        not chosen.is_absolute()
        or str(chosen) != os.path.normpath(str(chosen))
        or type(device) is not int
        or type(inode) is not int
    ):
        raise InstallationError("database_choice_untrusted", str(path))
    return {**record, "path": chosen}


def _write_database_choice(
    database: Path,
    info: os.stat_result,
    *,
    sha256: str,
    home: Path | None = None,
) -> None:
    base = home or account_home()
    path = database_choice_path(base)
    _require_physical_components(base, path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    _require_private_directory(path.parent)
    record = {
        "path": str(database),
        "device": info.st_dev,
        "inode": info.st_ino,
        "sha256_at_choice": sha256,
        "chosen_at": datetime.now(timezone.utc).isoformat(),
    }
    staging = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(
        staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staging, path)
    finally:
        staging.unlink(missing_ok=True)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def choose_database(
    database: Path,
    *,
    sha256: str,
    root: Path | None = None,
    home: Path | None = None,
    replace: bool = False,
) -> tuple[dict[str, object], dict[str, object] | None]:
    """Record ``database`` as the runtime database.

    The file must be ``trading_assistant.db`` in the designated checkout, a
    private regular file with no SQLite sidecars (no writer is active), and
    its current SHA-256 must equal ``sha256``: the operator names the exact
    contents they inspected. Replacing a different valid choice requires
    ``replace=True``.
    """
    designated = require_designated(root, home=home)
    target = database.resolve(strict=True)
    if target != designated / RUNTIME_DATABASE_NAME:
        raise InstallationError(
            "database_outside_installation",
            f"{target} is not {designated / RUNTIME_DATABASE_NAME}",
        )
    for suffix in _DATABASE_SIDECARS:
        if target.with_name(target.name + suffix).exists():
            raise InstallationError("database_in_use", f"{target}{suffix} exists")
    info = _require_database_file(target)
    if not (
        isinstance(sha256, str)
        and len(sha256) == 64
        and all(c in "0123456789abcdef" for c in sha256)
    ):
        raise InstallationError("database_hash_invalid")
    if _sha256_file(target) != sha256:
        raise InstallationError("database_hash_mismatch", str(target))
    base = home or account_home()
    directory = database_choice_path(base).parent
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    _require_private_directory(directory)
    with _designation_lock(directory):
        try:
            previous: dict[str, object] | None = read_database_choice(home)
        except InstallationError:
            previous = None
        same = previous is not None and (
            previous["path"],
            previous["device"],
            previous["inode"],
        ) == (target, info.st_dev, info.st_ino)
        if previous is not None and not same and not replace:
            raise InstallationError(
                "database_already_chosen",
                f"{previous['path']} is chosen; pass --replace to change it",
            )
        _write_database_choice(target, info, sha256=sha256, home=home)
    return read_database_choice(home), previous


def require_chosen_database(
    database_url: str,
    *,
    cwd: Path | None = None,
    home: Path | None = None,
) -> Path:
    """Return the configured database, or refuse unless it is the chosen file."""
    choice = read_database_choice(home)
    configured = sqlite_database_path(database_url, cwd=cwd)
    try:
        resolved = configured.resolve(strict=True)
    except OSError:
        raise InstallationError("database_missing", str(configured)) from None
    if resolved != choice["path"]:
        raise InstallationError(
            "database_not_the_chosen_file",
            f"configured {resolved}; chosen {choice['path']}",
        )
    info = _require_database_file(resolved)
    if (info.st_dev, info.st_ino) != (choice["device"], choice["inode"]):
        raise InstallationError(
            "database_identity_changed",
            f"{resolved} was replaced since it was chosen; choose it again",
        )
    return resolved


def venv_imports(venv_python: Path) -> Path:
    """The package directory a virtual environment's interpreter imports."""
    completed = subprocess.run(
        [
            str(venv_python),
            "-I",
            "-c",
            "import pathlib, trading_assistant; "
            "print(pathlib.Path(trading_assistant.__file__).resolve().parent)",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        raise InstallationError(
            "venv_cannot_import_package",
            "repair with: uv sync --all-extras --dev",
        )
    return Path(completed.stdout.strip())


def venv_editable_targets(root: Path) -> list[Path]:
    """Source paths the checkout's virtual environment imports this package from."""
    targets: list[Path] = []
    for pth in sorted((root / ".venv" / "lib").glob(f"python*/site-packages/{_EDITABLE_PTH}")):
        for line in pth.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.startswith(("#", "import ")):
                targets.append(Path(line.strip()))
    return targets


def launchd_references(home: Path | None = None) -> Iterator[tuple[str, str]]:
    """(label, WorkingDirectory) for installed com.trading.* LaunchAgents."""
    agents = (home or account_home()) / "Library" / "LaunchAgents"
    for plist in sorted(agents.glob("com.trading.*.plist")):
        try:
            with plist.open("rb") as handle:
                payload = plistlib.load(handle)
        except (OSError, plistlib.InvalidFileException, ValueError):
            yield plist.stem, "<unreadable>"
            continue
        yield str(payload.get("Label", plist.stem)), str(
            payload.get("WorkingDirectory", "<none>")
        )


def status_lines(home: Path | None = None) -> tuple[list[str], bool]:
    """Human-readable installation status and whether everything agrees."""
    healthy = True
    lines: list[str] = []
    root = source_root()
    lines.append(f"checkout:      {root}")
    try:
        designated = read_designation(home)
        same = designated == root
        healthy &= same
        lines.append(
            f"designated:    {designated}"
            + ("" if same else "   <- differs from this checkout")
        )
    except InstallationError as error:
        healthy = False
        lines.append(f"designated:    NOT USABLE ({error.code})")
    targets = venv_editable_targets(root)
    if not targets:
        lines.append("venv import:   no editable install found (run: uv sync --all-extras --dev)")
        healthy = False
    for target in targets:
        ok = target == root / "src"
        healthy &= ok
        lines.append(
            f"venv import:   {target}"
            + ("" if ok else "   <- stale; repair: uv sync --all-extras --dev")
        )
    try:
        choice = read_database_choice(home)
        lines.append(f"database:      {choice['path']} (chosen {choice.get('chosen_at', '?')})")
    except InstallationError as error:
        healthy = False
        lines.append(f"database:      NOT CHOSEN ({error.code})")
    for label, working in launchd_references(home):
        ok = working == str(root)
        healthy &= ok
        lines.append(
            f"launchd:       {label} -> {working}"
            + ("" if ok else "   <- stale; re-run scripts/launchd/install.sh")
        )
    return lines, healthy


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m trading_assistant.installation",
        description=(
            "Designated runtime installation. status and check are "
            "read-only; only designate changes the designation."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "status",
        help="read-only: checkout, designation, venv and launchd paths",
    )
    check = commands.add_parser(
        "check",
        help="read-only: exit 0 only if PROJECT is this checkout and designated",
    )
    check.add_argument("--project", required=True)
    check.add_argument(
        "--venv-python",
        help="also require this interpreter to import this checkout",
    )
    check.add_argument(
        "--require-database",
        action="store_true",
        help="also require PROJECT/trading_assistant.db to be the chosen database",
    )
    designate_parser = commands.add_parser(
        "designate",
        help="CHANGES STATE: designate this checkout as the runtime installation",
    )
    designate_parser.add_argument(
        "--replace",
        action="store_true",
        help="replace a different existing designation",
    )
    choose_parser = commands.add_parser(
        "choose-database",
        help="CHANGES STATE: choose the runtime database (trading_assistant.db here)",
    )
    choose_parser.add_argument("--path", required=True)
    choose_parser.add_argument(
        "--sha256",
        required=True,
        help="the file's current SHA-256 (shasum -a 256 PATH), proving which file",
    )
    choose_parser.add_argument(
        "--replace",
        action="store_true",
        help="replace a different existing choice",
    )
    args = parser.parse_args(argv)

    try:
        if args.command == "choose-database":
            chosen, previous = choose_database(
                Path(args.path), sha256=args.sha256, replace=args.replace
            )
            print(f"runtime database: {chosen['path']} (inode {chosen['inode']})")
            if previous is not None and previous["path"] != chosen["path"]:
                print(f"  replaced previous choice: {previous['path']}")
            return 0
        if args.command == "designate":
            target, previous = designate(replace=args.replace)
            if previous is None or previous == target:
                print(f"designated runtime installation: {target}")
            else:
                print(f"designated runtime installation: {target} (was {previous})")
            return 0
        if args.command == "check":
            project = Path(args.project).resolve(strict=True)
            if source_root() != project:
                raise InstallationError(
                    "venv_imports_other_checkout",
                    f"{project} runs code from {source_root()}",
                )
            require_designated(project)
            if args.venv_python is not None:
                imported = venv_imports(Path(args.venv_python))
                expected = project / "src" / "trading_assistant"
                if imported != expected:
                    raise InstallationError(
                        "venv_imports_other_checkout",
                        f"{args.venv_python} imports {imported}; "
                        "repair with: uv sync --all-extras --dev",
                    )
            if args.require_database:
                require_chosen_database(
                    f"sqlite:///{project / RUNTIME_DATABASE_NAME}"
                )
            return 0
        lines, healthy = status_lines()
        print("\n".join(lines))
        return 0 if healthy else 1
    except (InstallationError, OSError) as error:
        print(f"installation check failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
