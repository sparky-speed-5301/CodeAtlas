"""Integration smoke tests for CLI help, shell completion, and combined patch export options."""

from __future__ import annotations

import json
from pathlib import Path
import pytest
from typer.testing import CliRunner

from codeatlas.cli import app
from codeatlas.patching.proposal import (
    create_patch_proposal,
    generate_approval_token,
)
from codeatlas.review.packet import ReviewPacket

DIFF_PASS = (
    "--- a/src/calc.py\n"
    "+++ b/src/calc.py\n"
    "@@ -1,2 +1,3 @@\n"
    " def add(a, b):\n"
    "+    # safe add\n"
    "     return a + b\n"
)


def _init_repo(path: Path) -> str:
    import subprocess
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "ci@codeatlas.local"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "CI Runner"], cwd=path, check=True, capture_output=True)
    src_dir = path / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    (src_dir / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=path, check=True, capture_output=True)
    res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, check=True, capture_output=True, text=True)
    return res.stdout.strip()


@pytest.mark.parametrize("command", ["validate", "apply-isolated"])
def test_help_output_patch_export_options(command: str) -> None:
    """Ensure all four export options appear in --help with the correct PATH description."""
    runner = CliRunner()
    res = runner.invoke(app, ["patch", command, "--help"])
    assert res.exit_code == 0, res.output

    expected_options = [
        ("--report-output", "PATH", "Write"),
        ("--packet-output", "PATH", "Write the generated ReviewPacket as separate JSON."),
        ("--manifest-output", "PATH", "Write the generated human-approval manifest as separate JSON."),
        ("--markdown-output", "PATH", "Write human-readable validation summary Markdown here."),
    ]

    cleaned = res.output.replace("│", "").replace("║", "").replace("─", "").replace("|", "")
    norm_output = " ".join(cleaned.split())

    for opt_name, metavar, desc_snippet in expected_options:
        assert opt_name in res.output, f"Missing {opt_name} in {command} --help"
        # Find lines containing the option
        matching_lines = [line for line in res.output.splitlines() if opt_name in line]
        assert matching_lines, f"No line found for {opt_name}"
        line = matching_lines[0]
        assert metavar in line, f"Expected {metavar} metavar on line for {opt_name}: {line}"
        assert desc_snippet in norm_output, f"Expected description snippet '{desc_snippet}' for {opt_name}"


@pytest.mark.parametrize("command", ["validate", "apply-isolated"])
def test_shell_completion_all_export_options(command: str) -> None:
    """Verify that all four export options appear in shell-completion output."""
    runner = CliRunner()
    res = runner.invoke(
        app,
        ["patch", command, "--"],
        prog_name="codeatlas",
        env={
            "_CODEATLAS_COMPLETE": "complete_bash",
            "COMP_WORDS": f"codeatlas patch {command} --",
            "COMP_CWORD": "3",
        },
    )
    assert res.exit_code == 0, res.output
    completion_lines = res.output.splitlines()

    assert "--report-output" in completion_lines
    assert "--packet-output" in completion_lines
    assert "--manifest-output" in completion_lines
    assert "--markdown-output" in completion_lines


@pytest.mark.parametrize("command", ["validate", "apply-isolated"])
@pytest.mark.parametrize("prefix,expected", [
    ("--rep", ["--report-output"]),
    ("--pac", ["--packet-output"]),
    ("--man", ["--manifest-output"]),
    ("--mar", ["--markdown-output"]),
    ("--m", ["--manifest-output", "--markdown-output"]),
])
def test_shell_completion_prefix_filtering(command: str, prefix: str, expected: list[str]) -> None:
    """Verify shell completion prefix filtering for patch export options."""
    runner = CliRunner()
    res = runner.invoke(
        app,
        ["patch", command, prefix],
        prog_name="codeatlas",
        env={
            "_CODEATLAS_COMPLETE": "complete_bash",
            "COMP_WORDS": f"codeatlas patch {command} {prefix}",
            "COMP_CWORD": "3",
        },
    )
    assert res.exit_code == 0, res.output
    completion_lines = res.output.splitlines()
    for exp in expected:
        assert exp in completion_lines, f"Expected {exp} in completions for {prefix}"


@pytest.mark.parametrize("command", ["validate", "apply-isolated"])
@pytest.mark.parametrize("selected_outputs", [
    {"report"},
    {"packet"},
    {"manifest"},
    {"markdown"},
    {"report", "packet", "manifest", "markdown"},
])
def test_export_options_independent_and_combined(tmp_path: Path, command: str, selected_outputs: set[str]) -> None:
    """Verify options can be used independently and all together."""
    repo = tmp_path / "repo"
    base_sha = _init_repo(repo)
    proposal = create_patch_proposal(
        finding_id="CA-HELP-TEST", provider_name="operator", provider_version="1",
        base_commit=base_sha, target_files=["src/calc.py"], unified_diff=DIFF_PASS,
        rationale="Help/completion validation test", expected_behavior="Valid export parsing",
    )
    proposal_path = tmp_path / "proposal.json"
    proposal_path.write_text(proposal.model_dump_json(), encoding="utf-8")
    token = generate_approval_token(proposal.proposal_id, base_sha, proposal.patch_hash, proposal.target_files)

    report_path = tmp_path / "out" / "report.json"
    packet_path = tmp_path / "out" / "packet.json"
    manifest_path = tmp_path / "out" / "manifest.json"
    markdown_path = tmp_path / "out" / "summary.md"

    args = [
        "patch", command,
        "--repo", str(repo),
        "--proposal", str(proposal_path),
        "--approval-token", token,
    ]
    if "report" in selected_outputs:
        args.extend(["--report-output", str(report_path)])
    if "packet" in selected_outputs:
        args.extend(["--packet-output", str(packet_path)])
    if "manifest" in selected_outputs:
        args.extend(["--manifest-output", str(manifest_path)])
    if "markdown" in selected_outputs:
        args.extend(["--markdown-output", str(markdown_path)])

    runner = CliRunner()
    res = runner.invoke(app, args)
    assert res.exit_code == 0, res.output

    # Check existence matching selected_outputs
    assert report_path.exists() is ("report" in selected_outputs)
    assert packet_path.exists() is ("packet" in selected_outputs)
    assert manifest_path.exists() is ("manifest" in selected_outputs)
    assert markdown_path.exists() is ("markdown" in selected_outputs)

    if "report" in selected_outputs:
        report_data = json.loads(report_path.read_text(encoding="utf-8"))
        assert report_data["valid"] is True
        assert token not in report_path.read_text(encoding="utf-8")
    if "packet" in selected_outputs:
        packet_data = json.loads(packet_path.read_text(encoding="utf-8"))
        ReviewPacket.model_validate(packet_data)
        assert token not in packet_path.read_text(encoding="utf-8")
    if "manifest" in selected_outputs:
        manifest_text = manifest_path.read_text(encoding="utf-8")
        assert token not in manifest_text
    if "markdown" in selected_outputs:
        md_text = markdown_path.read_text(encoding="utf-8")
        assert "Test status:" in md_text
        assert token not in md_text
