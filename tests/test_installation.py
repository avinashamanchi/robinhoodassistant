"""Designated runtime installation: record validation, changes, diagnostics.

Every test uses a temporary home and disposable checkouts; nothing here reads
or writes the operator's real designation.
"""

from __future__ import annotations

import multiprocessing
import os
from pathlib import Path
import sys

import pytest

from trading_assistant import installation
from trading_assistant.installation import (
    DESIGNATION_RELATIVE,
    InstallationError,
    designate,
    read_designation,
    require_designated,
)


def _checkout(root: Path) -> Path:
    for relative in ("pyproject.toml", "scripts/operator.sh", "src/trading_assistant/__init__.py"):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("", encoding="utf-8")
    return root.resolve()


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home dir"
    path.mkdir()
    return path


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    return _checkout(tmp_path / "trading assistant")


def _write_record(home: Path, text: str | bytes, *, mode: int = 0o600) -> Path:
    path = home / DESIGNATION_RELATIVE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8")
    path.chmod(mode)
    return path


def _code(callable_, *args, **kwargs) -> str:
    with pytest.raises(InstallationError) as raised:
        callable_(*args, **kwargs)
    return raised.value.code


# ── reading the record ────────────────────────────────────────────────────────
def test_absent_designation(home):
    assert _code(read_designation, home) == "installation_not_designated"


def test_valid_designation_with_spaces(home, checkout):
    _write_record(home, f"{checkout}\n")
    assert read_designation(home) == checkout
    assert require_designated(checkout, home=home) == checkout


def test_designation_without_trailing_newline_is_accepted(home, checkout):
    _write_record(home, str(checkout))
    assert read_designation(home) == checkout


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("", id="empty"),
        pytest.param("\n", id="newline-only"),
        pytest.param("relative/checkout\n", id="relative"),
        pytest.param("/a\n/b\n", id="multi-line"),
        pytest.param("/with\0nul\n", id="nul-byte"),
        pytest.param(b"/bad-utf8-\xff\n", id="invalid-utf8"),
        # Explicit id: the value itself would make a node ID longer than the
        # release verifier accepts.
        pytest.param("/" + "x" * 5000 + "\n", id="oversize"),
    ],
)
def test_malformed_designations_are_untrusted(home, text):
    _write_record(home, text)
    assert _code(read_designation, home) == "installation_designation_untrusted"


def test_designation_of_a_moved_checkout_is_stale(home, tmp_path):
    _write_record(home, f"{tmp_path / 'moved away'}\n")
    assert _code(read_designation, home) == "designated_root_missing"


def test_designation_of_a_directory_that_is_not_a_checkout(home, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    _write_record(home, f"{plain.resolve()}\n")
    assert _code(read_designation, home) == "installation_root_invalid"


def test_designation_through_a_symlinked_root_is_rejected(home, checkout, tmp_path):
    alias = tmp_path / "alias"
    alias.symlink_to(checkout, target_is_directory=True)
    _write_record(home, f"{alias}\n")
    assert _code(read_designation, home) == "designated_root_not_physical"


def test_non_normalised_designation_is_rejected(home, checkout):
    _write_record(home, f"{checkout}/../{checkout.name}\n")
    assert _code(read_designation, home) == "designated_root_not_physical"


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o620, 0o602, 0o700])
def test_shared_or_executable_records_are_untrusted(home, checkout, mode):
    _write_record(home, f"{checkout}\n", mode=mode)
    if mode & 0o077:
        assert _code(read_designation, home) == "installation_designation_untrusted"
    else:
        assert read_designation(home) == checkout


def test_symlinked_record_is_untrusted(home, checkout, tmp_path):
    real = tmp_path / "real-record"
    real.write_text(f"{checkout}\n", encoding="utf-8")
    real.chmod(0o600)
    path = home / DESIGNATION_RELATIVE
    path.parent.mkdir(parents=True)
    path.parent.chmod(0o700)
    path.symlink_to(real)
    assert _code(read_designation, home) == "installation_designation_untrusted"


def test_shared_designation_directory_is_untrusted(home, checkout):
    path = _write_record(home, f"{checkout}\n")
    path.parent.chmod(0o755)
    assert _code(read_designation, home) == "installation_designation_untrusted"


def test_record_owned_by_another_user_is_untrusted(home, checkout, monkeypatch):
    _write_record(home, f"{checkout}\n")
    real_uid = os.getuid()
    monkeypatch.setattr(installation.os, "getuid", lambda: real_uid + 1)
    assert _code(read_designation, home) == "installation_designation_untrusted"


def test_conflicting_checkout_is_refused(home, checkout, tmp_path):
    other = _checkout(tmp_path / "other checkout")
    _write_record(home, f"{other}\n")
    error = pytest.raises(InstallationError, require_designated, checkout, home=home).value
    assert error.code == "installation_root_mismatch"
    assert str(checkout) in error.detail and str(other) in error.detail


# ── changing the designation ─────────────────────────────────────────────────
def test_designate_creates_private_directories_and_record(home, checkout):
    target, previous = designate(checkout, home=home)
    path = home / DESIGNATION_RELATIVE

    assert (target, previous) == (checkout, None)
    assert path.read_text(encoding="utf-8") == f"{checkout}\n"
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert sorted(p.name for p in path.parent.iterdir()) == [
        ".installation-root.lock",
        "installation-root",
    ]


def test_designate_refuses_to_replace_a_different_designation(home, checkout, tmp_path):
    other = _checkout(tmp_path / "other checkout")
    designate(other, home=home)

    assert _code(designate, checkout, home=home) == "installation_already_designated"
    assert read_designation(home) == other
    assert designate(checkout, home=home, replace=True) == (checkout, other)
    assert read_designation(home) == checkout


def test_designate_may_replace_an_unusable_record(home, checkout, tmp_path):
    _write_record(home, f"{tmp_path / 'moved away'}\n")
    assert designate(checkout, home=home) == (checkout, None)


def test_designate_rejects_non_checkouts_and_symlinked_directories(home, tmp_path, checkout):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert _code(designate, plain, home=home) == "installation_root_invalid"

    (home / "Library").mkdir()
    real = tmp_path / "elsewhere"
    real.mkdir()
    (home / "Library" / "Application Support").symlink_to(real, target_is_directory=True)
    assert _code(designate, checkout, home=home) == "installation_designation_untrusted"
    assert _code(read_designation, home) == "installation_designation_untrusted"
    assert list(real.iterdir()) == []


def _designate_in_process(args):
    home, root, replace = args
    try:
        designate(Path(root), home=Path(home), replace=replace)
        return "ok"
    except InstallationError as error:
        return error.code


def test_concurrent_designations_never_tear_the_record(home, tmp_path):
    roots = [_checkout(tmp_path / f"checkout {index}") for index in range(4)]
    context = multiprocessing.get_context("spawn")
    with context.Pool(4) as pool:
        results = pool.map(
            _designate_in_process,
            [(str(home), str(root), True) for root in roots * 3],
        )

    assert set(results) == {"ok"}
    assert read_designation(home) in roots
    leftovers = [p.name for p in (home / DESIGNATION_RELATIVE).parent.iterdir()]
    assert not [name for name in leftovers if name.endswith(".tmp")]


def test_concurrent_first_designations_without_replace_pick_one_winner(home, tmp_path):
    roots = [_checkout(tmp_path / f"checkout {index}") for index in range(4)]
    context = multiprocessing.get_context("spawn")
    with context.Pool(4) as pool:
        results = pool.map(
            _designate_in_process,
            [(str(home), str(root), False) for root in roots],
        )

    assert results.count("ok") == 1
    assert results.count("installation_already_designated") == 3
    assert read_designation(home) == roots[results.index("ok")]


# ── diagnostics ───────────────────────────────────────────────────────────────
def test_stale_editable_install_is_reported(home, checkout, monkeypatch, tmp_path):
    site = checkout / ".venv/lib/python3.11/site-packages"
    site.mkdir(parents=True)
    (site / "_editable_impl_trading_assistant.pth").write_text(
        f"{tmp_path / 'old location' / 'src'}\n", encoding="utf-8"
    )
    designate(checkout, home=home)
    monkeypatch.setattr(installation, "source_root", lambda: checkout)

    lines, healthy = installation.status_lines(home)

    assert healthy is False
    assert any("stale; repair: uv sync --all-extras --dev" in line for line in lines)


def test_stale_launchd_jobs_are_reported(home, checkout, monkeypatch, tmp_path):
    import plistlib

    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    (agents / "com.trading.app.plist").write_bytes(
        plistlib.dumps({"Label": "com.trading.app", "WorkingDirectory": str(tmp_path / "old")})
    )
    designate(checkout, home=home)
    monkeypatch.setattr(installation, "source_root", lambda: checkout)

    lines, healthy = installation.status_lines(home)

    assert healthy is False
    assert any("com.trading.app" in line and "stale" in line for line in lines)


def test_venv_import_probe_reports_the_imported_package(tmp_path):
    fake = tmp_path / "python"
    fake.write_text(
        f"#!{sys.executable}\nprint('/somewhere/src/trading_assistant')\n",
        encoding="utf-8",
    )
    fake.chmod(0o700)
    assert installation.venv_imports(fake) == Path("/somewhere/src/trading_assistant")

    broken = tmp_path / "broken"
    broken.write_text(f"#!{sys.executable}\nraise SystemExit(1)\n", encoding="utf-8")
    broken.chmod(0o700)
    assert _code(installation.venv_imports, broken) == "venv_cannot_import_package"


def test_cli_read_only_commands_do_not_create_a_designation(home, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(home))
    assert installation.main(["status"]) == 1
    assert installation.main(["check", "--project", str(installation.source_root())]) == 1
    assert not (home / DESIGNATION_RELATIVE).exists()
    assert "installation_not_designated" in capsys.readouterr().err


def test_cli_check_passes_only_for_the_designated_checkout(home, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(home))
    assert installation.main(["designate"]) == 0
    root = installation.source_root()
    assert installation.main(["check", "--project", str(root)]) == 0
    assert installation.main(["check", "--project", str(tmp_path)]) == 1
