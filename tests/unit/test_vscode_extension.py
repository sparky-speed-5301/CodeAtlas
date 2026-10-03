"""Unit tests for Phase 10A VS Code extension manifest, commands, and settings."""

from __future__ import annotations

import json
from pathlib import Path


def test_extension_package_json():
    ext_dir = Path(__file__).resolve().parents[2] / "extension"
    package_json = ext_dir / "package.json"
    assert package_json.exists(), "extension/package.json must exist"

    manifest = json.loads(package_json.read_text(encoding="utf-8"))
    assert manifest["name"] == "codeatlas"
    assert manifest["publisher"] == "codeatlas"
    assert manifest["main"] in ("./extension.js", "./out/extension.js")

    # Verify ViewContainers and Views
    contributes = manifest.get("contributes", {})
    views_containers = contributes.get("viewsContainers", {})
    assert "activitybar" in views_containers
    assert any(c["id"] == "codeatlas-sidebar" for c in views_containers["activitybar"])

    views = contributes.get("views", {})
    assert "codeatlas-sidebar" in views
    view_ids = {v["id"] for v in views["codeatlas-sidebar"]}
    assert "codeatlas.statusView" in view_ids
    assert "codeatlas.findingsView" in view_ids
    assert "codeatlas.contextView" in view_ids

    # Verify Phase 10A & 10B Commands
    commands = {c["command"] for c in contributes.get("commands", [])}
    assert "codeatlas.startReview" in commands
    assert "codeatlas.cancelReview" in commands
    assert "codeatlas.refresh" in commands
    assert "codeatlas.explainFinding" in commands
    assert "codeatlas.showContext" in commands
    assert "codeatlas.generateDraftFix" in commands
    assert "codeatlas.validateApprovedFix" in commands
    assert "codeatlas.copyFinding" in commands
    assert "codeatlas.dismissFinding" in commands
    # Phase 10B QuickPick commands
    assert "codeatlas.findFinding" in commands
    assert "codeatlas.explainCurrentFinding" in commands
    # Phase 10C Service & Profile commands
    assert "codeatlas.startLocalService" in commands
    assert "codeatlas.stopLocalService" in commands
    assert "codeatlas.restartLocalService" in commands
    assert "codeatlas.selectConfigurationProfile" in commands
    assert "codeatlas.openConfiguration" in commands
    assert "codeatlas.checkServiceHealth" in commands

    titles = {c["command"]: c.get("title", "") for c in contributes.get("commands", [])}
    assert titles["codeatlas.findFinding"] == "CodeAtlas: Find Finding"
    assert titles["codeatlas.explainCurrentFinding"] == "CodeAtlas: Explain Current Finding"
    assert titles["codeatlas.showContext"] == "CodeAtlas: Show Context"
    assert titles["codeatlas.generateDraftFix"] == "CodeAtlas: Generate Draft Fix"
    assert titles["codeatlas.validateApprovedFix"] == "CodeAtlas: Validate Approved Fix"
    assert titles["codeatlas.dismissFinding"] == "CodeAtlas: Dismiss Finding"
    assert titles["codeatlas.startLocalService"] == "CodeAtlas: Start Local Service"
    assert titles["codeatlas.stopLocalService"] == "CodeAtlas: Stop Local Service"
    assert titles["codeatlas.restartLocalService"] == "CodeAtlas: Restart Local Service"
    assert titles["codeatlas.selectConfigurationProfile"] == "CodeAtlas: Select Configuration Profile"
    assert titles["codeatlas.openConfiguration"] == "CodeAtlas: Open Configuration"
    assert titles["codeatlas.checkServiceHealth"] == "CodeAtlas: Check Service Health"

    # Verify Safe Settings
    properties = contributes.get("configuration", {}).get("properties", {})
    assert "codeatlas.serviceUrl" in properties
    assert properties["codeatlas.serviceUrl"]["default"] == "http://127.0.0.1:8765"

    assert "codeatlas.provider" in properties
    assert properties["codeatlas.provider"]["default"] == "mock"

    assert "codeatlas.enableLiveReviewer" in properties
    assert properties["codeatlas.enableLiveReviewer"]["default"] is False

    assert "codeatlas.enablePatchSuggestions" in properties
    assert properties["codeatlas.enablePatchSuggestions"]["default"] is False

    assert "codeatlas.enableTestExecution" in properties
    assert properties["codeatlas.enableTestExecution"]["default"] is False

    assert "codeatlas.enableFullSuiteExecution" in properties
    assert properties["codeatlas.enableFullSuiteExecution"]["default"] is False

    assert "codeatlas.githubDryRun" in properties
    assert properties["codeatlas.githubDryRun"]["default"] is True

    # Phase 10C Service & Profile Settings
    assert "codeatlas.serviceMode" in properties
    assert properties["codeatlas.serviceMode"]["default"] == "external"
    assert set(properties["codeatlas.serviceMode"]["enum"]) == {"external", "managed", "disabled"}

    assert "codeatlas.serviceCommand" in properties
    assert properties["codeatlas.serviceCommand"]["default"] == "codeatlas serve"

    assert "codeatlas.serviceHost" in properties
    assert properties["codeatlas.serviceHost"]["default"] == "127.0.0.1"

    assert "codeatlas.servicePort" in properties
    assert properties["codeatlas.servicePort"]["default"] == 8765

    assert "codeatlas.autoStartService" in properties
    assert properties["codeatlas.autoStartService"]["default"] is False

    assert "codeatlas.healthCheckInterval" in properties
    assert properties["codeatlas.healthCheckInterval"]["default"] == 30

    assert "codeatlas.activeProfile" in properties
    assert properties["codeatlas.activeProfile"]["default"] == "default"

    assert "codeatlas.profilePath" in properties
    assert properties["codeatlas.profilePath"]["default"] == ".codeatlas/profiles.json"

    # Security check: No API keys or secret tokens in extension settings
    for key, spec in properties.items():
        assert "key" not in key.lower() or "serviceurl" in key.lower() or "mode" in key.lower()
        assert "token" not in key.lower()
        assert "secret" not in key.lower()


def test_extension_js_present():
    ext_dir = Path(__file__).resolve().parents[2] / "extension"
    ext_js = ext_dir / "extension.js"
    assert ext_js.exists(), "extension/extension.js must exist"
    content = ext_js.read_text(encoding="utf-8")
    assert "activate" in content
    assert "deactivate" in content
    assert "CodeAtlasClient" in content
    assert "decorationTypes" in content
    assert "findFindingsAtCursor" in content
    assert "formatQuickPickItem" in content
    assert "clampLine" in content
    assert "sortFindingsBySeverity" in content
    assert "ProfileManager" in content
    assert "ServiceLifecycleManager" in content
    assert "validateProfile" in content


def test_extension_node_sync_suite():
    """Execute synchronization/lifecycle tests including the real Python service."""
    import os
    import subprocess
    import sys
    ext_dir = Path(__file__).resolve().parents[2] / "extension"
    test_script = ext_dir / "test_sync.js"
    assert test_script.exists()

    result = subprocess.run(
        ["node", str(test_script)],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(ext_dir),
        env={**os.environ, "CODEATLAS_TEST_PYTHON": sys.executable},
    )
    assert result.returncode == 0, f"Node test suite failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    assert "All Phase 10C VS Code extension unit tests passed successfully!" in result.stdout
