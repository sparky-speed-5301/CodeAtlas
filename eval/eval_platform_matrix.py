"""Machine-readable native platform observations without inferred portability claims."""

from __future__ import annotations

import json
import platform
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

FEATURES = (
    "static_analysis",
    "runner_policy",
    "wheel_install_uninstall",
    "vsix_install_uninstall",
    "native_sandbox",
    "network_isolation",
)
BENCHMARK_VERSION = "real-repositories-v1"


def _probe(command: list[str]) -> tuple[bool, str | None]:
    try:
        process = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, None
    text = (process.stdout or process.stderr).strip().splitlines()
    return process.returncode == 0, text[0][:256] if text else None


def _environment(
    name: str,
    command: list[str],
    *,
    limitation: str,
    skipped_tests: list[str] | None = None,
) -> dict[str, object]:
    available, version = _probe(command)
    return {
        "environment": name,
        "availability": "available" if available else "unavailable",
        "version": version,
        "command": command,
        "result": "detected" if available else "unavailable",
        "skipped_tests": skipped_tests or [],
        "limitations": [limitation],
    }


def _python_command(version: str) -> list[str]:
    current = f"{sys.version_info.major}.{sys.version_info.minor}"
    if version == current:
        return [sys.executable, "--version"]
    if sys.platform == "win32":
        return ["py", f"-{version}", "--version"]
    return [f"python{version}", "--version"]


def platform_matrix(observations: dict[str, str] | None = None) -> dict[str, object]:
    observations = observations or {}
    if set(observations) - set(FEATURES):
        raise ValueError("Unknown platform feature")
    if any(value not in {"passed", "failed", "blocked", "not_run"} for value in observations.values()):
        raise ValueError("Invalid observation status")
    host = {"win32": "windows", "linux": "linux", "darwin": "macos"}.get(sys.platform, "other")
    rows = []
    for os_name in ("windows", "linux", "macos"):
        for feature in FEATURES:
            rows.append(
                {
                    "os": os_name,
                    "feature": feature,
                    "status": observations.get(feature, "not_run") if os_name == host else "not_run",
                    "evidence_kind": "native_observation" if os_name == host and feature in observations else "none",
                }
            )

    environments = [
        _environment(
            f"Python {version}",
            _python_command(version),
            limitation="Version detection only; the full suite was not run by this matrix command.",
            skipped_tests=["full Python suite"],
        )
        for version in ("3.11", "3.12", "3.14")
    ]
    for os_name in ("Linux", "Windows"):
        native = host == os_name.lower()
        environments.append(
            {
                "environment": os_name,
                "availability": "available" if native else "unavailable",
                "version": platform.platform() if native else None,
                "command": [sys.executable, "-c", "import platform; print(platform.platform())"],
                "result": "native_host" if native else "unavailable",
                "skipped_tests": [] if native else ["native platform suite"],
                "limitations": [
                    "Only the current operating system is native evidence; portability is not inferred."
                ],
            }
        )
    tool_specs = (
        ("Go", [shutil.which("go") or "go", "version"]),
        ("Java", [shutil.which("java") or "java", "-version"]),
        ("Rust", [shutil.which("rustc") or "rustc", "--version"]),
        ("VS Code host", [shutil.which("code") or shutil.which("code.cmd") or "code", "--version"]),
    )
    environments.extend(
        _environment(
            name,
            command,
            limitation="Availability and version detection only; language or host integration tests are reported separately.",
            skipped_tests=["environment-specific integration suite"],
        )
        for name, command in tool_specs
    )
    return {
        "schema_version": 2,
        "benchmark_version": BENCHMARK_VERSION,
        "timestamp": datetime.now(UTC).isoformat(),
        "host": {
            "os": host,
            "architecture": platform.machine(),
            "python": platform.python_version(),
        },
        "declared_python_requirement": ">=3.11",
        "production_ready": False,
        "environments": environments,
        "rows": rows,
        "limitations": [
            "Simulated launcher tests are not native operating-system evidence.",
            "No network-isolation guarantee is inferred from offline command policy.",
            "Detection does not convert skipped environment-specific suites into passes.",
        ],
    }


if __name__ == "__main__":
    output = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("eval/results/platform-matrix.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(platform_matrix(), indent=2) + "\n", encoding="utf-8")
