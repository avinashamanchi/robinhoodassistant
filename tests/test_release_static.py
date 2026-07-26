from __future__ import annotations

import subprocess
import sys


def test_release_static_gate_passes_for_the_committed_runtime_sources():
    completed = subprocess.run(
        [sys.executable, "scripts/check_release_safety.py"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed.stdout.strip() == "release static checks: PASS"
