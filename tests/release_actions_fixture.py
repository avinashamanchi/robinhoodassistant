"""Load the standalone operator script without importing it into the product."""

import importlib.util
from pathlib import Path

path = Path(__file__).resolve().parents[1] / "scripts/sync_release_evidence.py"
spec = importlib.util.spec_from_file_location("release_sync_operator_fixture", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
ComposioActionsClient = module.ComposioActionsClient
