# Change Log

All notable changes to the "codeatlas" extension will be documented in this file.

Check [Keep a Changelog](http://keepachangelog.com/) for recommendations on how to structure this file.

## [Unreleased] - Phase 11C-D/E/F

#### Added
- **Apply Validated Fix** (`codeatlas.applyFix`): explicit, modal-confirmed application of a FixProposal that passed isolated sandbox validation, requiring an apply-scoped approval token and the confirmed exact patch hash. The service re-verifies proposal identity, evidence identity, policy version, parse/policy/redaction, and a clean workspace immediately before writing; the applied result must byte-match the validated sandbox diff or the workspace is restored automatically.
- **Revert Applied Fix** (`codeatlas.revertAppliedFix`): byte-exact restoration of the captured pre-apply contents; refused if the target files changed since the apply, so user edits are never clobbered.
- **Show Fix Apply History** (`codeatlas.showApplyHistory`): bounded apply/revert event history with stable, redacted metadata.

#### Security Invariants
- **Nothing Applies Automatically**: generation, preview, approval, validation, and test success never modify the workspace; only the explicit Apply command with an apply-scoped token and confirmation hash can, and only for a validated proposal.
- **Operation-Scoped Tokens**: a validation token can never authorize an apply; tokens are never returned, logged, or displayed.

## [0.1.0] - 2026-10-03

### Initial Production Release

#### Added
- **Repository-Aware Quality Platform**: Evidence-first code review directly integrated into the VS Code sidebar.
- **Activity-Bar Sidebar**: Review Status (`codeatlas.statusView`), Findings (`codeatlas.findingsView`), and Finding Context (`codeatlas.contextView`).
- **Interactive Review Lifecycle**: Start review, cancel review, and refresh findings commands with live state polling.
- **Severity-Ordered Decorations**: In-editor line badges for Blocker, High, Medium, Low, and Info findings with bi-directional cursor synchronization.
- **QuickPick Navigation**: Quick jump to findings with formatted badges and category metadata (`codeatlas.findFinding`).
- **Detail Webview & Explanation**: In-depth finding detail view with evidence, confidence, limitations, and AI explanations (`codeatlas.explainFinding`).
- **Draft Fix Generation**: Generates contextual unified diff patch proposals (`codeatlas.generateDraftFix`).
- **Approval-Gated Validation**: Scoped-token approval workflow for executing candidate patches in detached isolated git worktrees (`codeatlas.validateApprovedFix`).
- **Service Process Lifecycle**: Managed process supervision, external service discovery, and disabled modes with PID ownership verification and clean shutdown (`codeatlas.startLocalService`, `codeatlas.stopLocalService`, `codeatlas.restartLocalService`).
- **Configuration Profiles**: Hierarchical profile loading supporting application and workspace-level overrides (`codeatlas.selectConfigurationProfile`).
- **Deterministic Packaging & Verification**: Strict release packaging excluding test fixtures and dev artifacts, with automated secret scanning and disposable environment smoke testing.

#### Security Invariants
- **Compiled TypeScript Runtime Only**: Strictly executes `./out/extension.js` with no legacy JavaScript fallback.
- **No Arbitrary Execution**: Managed service execution enforces strict command parsing; arbitrary shell scripts and unknown endpoints are rejected.
- **Original Worktree Preservation**: Validation executes in isolated, detached worktrees; the working directory, index, and unstaged modifications remain completely untouched.
- **Dry-Run Default**: GitHub pull request commenting defaults to dry-run mode.
- **Credential Protection**: Settings, diagnostics, logs, and packaged bundles are continuously scanned to guarantee zero secret, token, or private key leakage.
