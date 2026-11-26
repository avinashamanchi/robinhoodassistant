"""Synthetic verifier reports for tests only, never operational evidence."""

import ast
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[1]
STAGES = (
    "compile",
    "migration-tests",
    "security-tests",
    "safety-tests",
    "frontend-tests",
    "full-tests",
    "branch-coverage",
    "static-gate",
)
MANIFESTS = {
    "migration-tests": "_MIGRATION_TEST_MANIFEST",
    "security-tests": "_SECURITY_TEST_MANIFEST",
    "safety-tests": "_SAFETY_TEST_MANIFEST",
    "frontend-tests": "_FRONTEND_TEST_MANIFEST",
    "full-tests": "_FULL_TEST_MANIFEST",
    "branch-coverage": "_FULL_TEST_MANIFEST",
}


def report_fixture(commit="a" * 40):
    tree = ast.parse((SOURCE / "scripts/verify_loopback_release.py").read_text())
    counts = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in MANIFESTS.values():
                    counts[target.id] = next(
                        ast.literal_eval(k.value)
                        for k in node.value.keywords
                        if k.arg == "count"
                    )
    steps = []
    for name in STAGES:
        step = dict(
            name=name,
            status="passed",
            detail_code="PASS",
            returncode=0,
            duration_seconds=1.0,
            argv=["synthetic-test-command"],
            stdout="",
            stderr="",
        )
        if name in MANIFESTS:
            step["tests"] = dict(
                total=counts[MANIFESTS[name]], failures=0, errors=0, skipped=0
            )
        steps.append(step)
    return dict(
        schema_version=2,
        state="completed",
        passed=True,
        detail_code="PASS",
        commit=commit,
        migration_head="20260730_0018",
        run_id="1" * 32,
        started_at="2026-10-01T00:00:00Z",
        finished_at="2026-10-01T00:00:08Z",
        steps=steps,
        tool_isolation="canonical_fingerprint",
        tools={
            name: dict(
                path=f"/synthetic/{name}",
                fingerprint="sha256:" + "b" * 64,
                version="synthetic-test-version",
            )
            for name in ("git", "node", "uv", "python")
        },
        network_isolation=dict(
            boundary="python_runtime_only",
            hostile_local_process_boundary="external_clean_ci_required",
            kind="python_socket_guard",
            os_enforced=False,
            status="verified",
        ),
    )


def install_contract(root):
    path = root / "scripts/verify_loopback_release.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((SOURCE / "scripts/verify_loopback_release.py").read_bytes())
