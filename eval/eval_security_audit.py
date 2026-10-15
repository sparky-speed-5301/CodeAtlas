"""Offline, scoped trust-boundary audit using existing deterministic tests."""

from __future__ import annotations

import json
import platform
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

BOUNDARIES = {
    "repository_paths": ["tests/unit/test_security_boundaries.py"],
    "provider_data_and_output": ["tests/unit/test_live_provider.py", "tests/unit/test_patch_suggestions.py"],
    "approval_and_patch_paths": ["tests/unit/test_patching.py", "tests/unit/test_isolated_validation.py"],
    "runner_commands_environment_redaction": ["tests/unit/test_verification_runner.py", "tests/unit/test_failure_diagnostics.py"],
    "extension_package_and_workspace_trust": ["tests/unit/test_vscode_extension.py"],
}


def run_audit(root: Path) -> dict:
    results = []
    with tempfile.TemporaryDirectory(prefix="codeatlas-security-") as directory:
        for boundary, tests in BOUNDARIES.items():
            junit = Path(directory) / f"{boundary}.xml"
            command = [sys.executable, "-m", "pytest", *tests, "-q", f"--junitxml={junit}"]
            try:
                proc = subprocess.run(command, cwd=root, capture_output=True, timeout=240, check=False)
                counts = {key: 0 for key in ("tests", "failures", "errors", "skipped")}
                if junit.exists():
                    for suite in ET.parse(junit).getroot().iter("testsuite"):
                        for key in counts:
                            counts[key] += int(suite.get(key, "0"))
                status = "passed" if proc.returncode == 0 and counts["tests"] > counts["skipped"] else "failed"
                results.append({"boundary": boundary, "status": status, "exit_code": proc.returncode, "counts": counts, "tests": tests})
            except subprocess.TimeoutExpired:
                results.append({"boundary": boundary, "status": "failed", "reason": "timeout", "tests": tests})
    return {
        "schema_version": 1, "python": platform.python_version(), "platform": sys.platform,
        "production_ready": False, "results": results,
        "findings": [{"id": "repository-symlink-containment", "status": "fixed_with_regression", "root": "src/codeatlas/repository/scanner.py", "limitation": "Resolve-before-read is not race-proof filesystem isolation; native symlink test may skip without Windows privileges."}],
        "gaps": ["No live providers or credentials used.", "Persistence, service, GitHub and benchmark internals excluded from this audit.", "No dependency vulnerability database, penetration test, or OS-level network isolation checked.", "Subprocess capture truncates persisted output, not process memory; descendant timeout containment is not certified.", "Windows profile-directory access in runner environment remains an isolation risk; detached worktrees are not security sandboxes."],
    }


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    report = run_audit(root)
    output = Path(sys.argv[1]) if len(sys.argv) > 1 else root / "eval/results/security-audit.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output), "results": report["results"]}, indent=2))
    raise SystemExit(1 if any(row["status"] != "passed" for row in report["results"]) else 0)
