# Change Log

All notable changes to the "codeatlas" extension will be documented in this file.

Check [Keep a Changelog](http://keepachangelog.com/) for recommendations on how to structure this file.

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
