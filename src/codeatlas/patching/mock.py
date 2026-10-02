"""Deterministic MockFixer for testing patch proposal and validation lifecycles."""

from __future__ import annotations

from typing import Sequence

from .models import PatchProposal
from .proposal import create_patch_proposal


class MockFixer:
    """Offline, deterministic patch generator supporting test scenarios."""

    name = "mock-fixer"
    version = "1.0.0"

    def __init__(self, mode: str = "valid_patch") -> None:
        self.mode = mode

    def propose(
        self,
        *,
        finding_id: str,
        base_commit: str,
        target_files: Sequence[str] | None = None,
        context_file: str = "src/utils.py",
        context_line: int = 1,
        existing_line: str = "def helper():",
    ) -> PatchProposal:
        """Generate a deterministic PatchProposal matching the configured mode."""
        tf = list(target_files) if target_files else [context_file]
        primary_file = tf[0] if tf else context_file

        if self.mode == "empty_patch":
            diff = ""
        elif self.mode == "malformed_patch":
            diff = (
                f"--- a/{primary_file}\n"
                f"+++ b/{primary_file}\n"
                f"@@ -1,50 +1,50 @@\n"
                f" {existing_line}\n"
            )
        elif self.mode == "path_traversal":
            diff = (
                "--- a/../../etc/passwd\n"
                "+++ b/../../etc/passwd\n"
                "@@ -1,1 +1,1 @@\n"
                "-root:x:0:0:root:/root:/bin/bash\n"
                "+root:x:0:0:root:/root:/bin/sh\n"
            )
            tf = ["../../etc/passwd"]
        elif self.mode == "outside_snapshot_path":
            diff = (
                "--- /opt/secret/config.json\n"
                "+++ /opt/secret/config.json\n"
                "@@ -1,1 +1,1 @@\n"
                "-{}\n"
                '+{"safe": true}\n'
            )
            tf = ["/opt/secret/config.json"]
        elif self.mode == "conflict_patch":
            diff = (
                f"--- a/{primary_file}\n"
                f"+++ b/{primary_file}\n"
                f"@@ -9999,3 +9999,3 @@\n"
                "-THIS_LINE_DEFINITELY_DOES_NOT_EXIST_XYZ_98765\n"
                "+THIS_LINE_FIXED_XYZ_98765\n"
                " existing_context\n"
            )
        elif self.mode == "syntax_breaking_patch":
            diff = (
                f"--- a/{primary_file}\n"
                f"+++ b/{primary_file}\n"
                f"@@ -{context_line},2 +{context_line},3 @@\n"
                f" {existing_line}\n"
                "+    def broken(:\n"
                "         pass\n"
            )
        elif self.mode == "test_modifying_patch":
            test_file = "tests/test_example.py"
            diff = (
                f"--- a/{test_file}\n"
                f"+++ b/{test_file}\n"
                "@@ -1,2 +1,2 @@\n"
                "-def test_example():\n"
                "+def test_example_modified():\n"
                "     assert True\n"
            )
            tf = [test_file]
        elif self.mode == "workflow_modifying_patch":
            wf_file = ".github/workflows/ci.yml"
            diff = (
                f"--- a/{wf_file}\n"
                f"+++ b/{wf_file}\n"
                "@@ -1,2 +1,2 @@\n"
                "-name: CI\n"
                "+name: CI Modified\n"
                " on: [push]\n"
            )
            tf = [wf_file]
        elif self.mode == "dependency_modifying_patch":
            dep_file = "requirements.txt"
            diff = (
                f"--- a/{dep_file}\n"
                f"+++ b/{dep_file}\n"
                "@@ -1,1 +1,1 @@\n"
                "-requests==2.28.0\n"
                "+requests==2.31.0\n"
            )
            tf = [dep_file]
        elif self.mode == "secret_introducing_patch":
            diff = (
                f"--- a/{primary_file}\n"
                f"+++ b/{primary_file}\n"
                f"@@ -{context_line},2 +{context_line},3 @@\n"
                f" {existing_line}\n"
                '+    AWS_SECRET_KEY = "AKIA1234567890ABCDEF"\n'
                "         pass\n"
            )
        elif self.mode == "oversized_patch":
            added_lines = "\n".join(f"+line_{idx} = {idx}" for idx in range(160))
            diff = (
                f"--- a/{primary_file}\n"
                f"+++ b/{primary_file}\n"
                f"@@ -{context_line},1 +{context_line},161 @@\n"
                f" {existing_line}\n"
                f"{added_lines}\n"
            )
        else:
            # Default: valid_patch
            diff = (
                f"--- a/{primary_file}\n"
                f"+++ b/{primary_file}\n"
                f"@@ -{context_line},2 +{context_line},3 @@\n"
                f" {existing_line}\n"
                "+    # Applied safe null check\n"
                "         pass\n"
            )

        return create_patch_proposal(
            finding_id=finding_id,
            provider_name=self.name,
            provider_version=self.version,
            base_commit=base_commit,
            target_files=tf,
            unified_diff=diff,
            rationale=f"Deterministic mock remediation in mode {self.mode}",
            expected_behavior="Resolves finding without regressions",
            risk_level="low",
            requested_action="validate",
            provenance={"mode": self.mode},
        )


__all__ = [
    "MockFixer",
]
