"""Phase 10E runs the packaged TypeScript runtime over the local HTTP contract."""

import os
from pathlib import Path
import subprocess
import sys


def test_compiled_extension_developer_workflow():
    extension = Path(__file__).resolve().parents[2] / "extension"
    result = subprocess.run(
        ["node", "test_workflow.js"],
        cwd=extension,
        env={**os.environ, "CODEATLAS_TEST_PYTHON": sys.executable},
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, f"Phase 10E failed:\n{result.stdout}\n{result.stderr}"
    assert "Phase 10E compiled-runtime workflow tests passed." in result.stdout
