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
import fcntl
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
    designate_parser = commands.add_parser(
        "designate",
        help="CHANGES STATE: designate this checkout as the runtime installation",
    )
    designate_parser.add_argument(
        "--replace",
        action="store_true",
        help="replace a different existing designation",
    )
    args = parser.parse_args(argv)

    try:
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
            return 0
        lines, healthy = status_lines()
        print("\n".join(lines))
        return 0 if healthy else 1
    except (InstallationError, OSError) as error:
        print(f"installation check failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
