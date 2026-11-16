"""The network/auth utility stays outside the trading runtime import graph."""

import importlib.util
from pathlib import Path
import sys

import pytest


@pytest.mark.parametrize(
    "source",
    [
        "import scripts.sync_release_evidence\n",
        "from scripts import sync_release_evidence\n",
        "import importlib; importlib.import_module('scripts.sync_release_evidence')\n",
        "import runpy; runpy.run_path('scripts/sync_release_evidence.py')\n",
    ],
)
def test_runtime_cannot_import_the_operator_transport(tmp_path, source):
    spec = importlib.util.spec_from_file_location(
        "release_operator_boundary_gate", Path("scripts/check_release_safety.py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    path = tmp_path / "src/trading_assistant/app/unsafe.py"
    path.parent.mkdir(parents=True)
    path.write_text(source)
    findings = module._scan_operator_release_imports(tmp_path)
    assert any(f.code == "OPERATOR_TOOL_RUNTIME_IMPORT" for f in findings)


def test_workflow_retains_only_canonical_report_after_successful_drill():
    import yaml

    workflow = yaml.load(
        Path(".github/workflows/ci.yml").read_text(), Loader=yaml.BaseLoader
    )
    steps = workflow["jobs"]["verification"]["steps"]
    artifact = [
        s for s in steps if s.get("name") == "Publish canonical release evidence"
    ]
    assert len(artifact) == 1
    assert steps.index(artifact[0]) > next(
        i
        for i, s in enumerate(steps)
        if s.get("name") == "Run the isolated mock safety drill"
    )
    assert artifact[0].get("if") is None
    assert artifact[0]["with"] == {
        "name": "release-evidence-${{ github.event.pull_request.head.sha || github.sha }}-${{ github.run_id }}-${{ github.run_attempt }}",
        "path": ".local/verification/release-results.json",
        "include-hidden-files": "true",
        "if-no-files-found": "error",
        "retention-days": "14",
    }
