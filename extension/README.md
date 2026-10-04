# CodeAtlas VS Code Extension

CodeAtlas provides evidence-first, repository-aware code review directly inside VS Code.

## Architecture

The extension acts as a lightweight client connecting to the local CodeAtlas background service:

`VS Code Extension -> Local HTTP Service (127.0.0.1:8765) -> Orchestrator & Analyzers`

- **Read-Only / Safe Execution:** The extension uses the existing Python service and review pipeline. Managed startup launches the installed CodeAtlas executable directly, without a shell. Configuration reads are bounded to named JSON files in `.codeatlas/`; repository analysis stays in Python.
- **Fail-Closed Patching:** Draft fixes generate `PatchProposal` records requiring explicit scoped operator approval tokens before isolated validation in ephemeral detached worktrees.
- **Redaction & Privacy:** Prompts, credentials, API keys, and unbounded source are never exposed in extension views or settings.

## Views

1. **Review Status (`codeatlas.statusView`):**
   - Displays current review lifecycle progress (`idle`, `preparing`, `indexing`, `analyzing`, `reviewing`, `findings_ready`, `completed`, `failed`, `cancelled`).
   - Action buttons: Start Review, Cancel Review, Refresh.
   - Shows policy decision, finding counts, test statuses, patch validation states, and the resolved base/head comparison range.
   - The comparison item displays abbreviated SHAs; its **Copy Comparison Range** context action writes the full validated `base...head` range to the clipboard.

2. **Findings (`codeatlas.findingsView`):**
   - Lists findings categorized by severity (`[BLOCKER]`, `[HIGH]`, `[MEDIUM]`, `[LOW]`, `[INFO]`).
   - Supports filtering by severity, category, or status.
   - Clicking a finding navigates directly to the exact file and line in the editor.

3. **Finding Context (`codeatlas.contextView`):**
   - Displays explainable context for the selected finding:
     - Changed diff lines
     - Enclosing symbol
     - Relevant imports and intra-file references
     - Related tests
     - Context retrieval candidates
     - Truncation status & evidence provenance

## Inline Editor Decorations

- Highlights exact line ranges for flagged findings.
- Uses visible text badges (`[BLOCKER]`, `[HIGH]`, `[MEDIUM]`, `[LOW]`) in addition to subtle background tints to ensure accessibility without relying on color alone.
- Hovering over a decoration reveals claim, confidence, evidence strength, and status.

## Settings

- `codeatlas.serviceUrl`: Explicit HTTP localhost URL. The displayed default (`http://127.0.0.1:8765`) is a hint; only an explicitly saved value overrides workspace discovery.
- `codeatlas.provider`: Reviewer provider (`mock` or `live`).
- `codeatlas.model`: Model name for live reviewer.
- `codeatlas.timeout`: Timeout in seconds (default: `30`).
- `codeatlas.maxFindings`: Maximum findings displayed (default: `50`).
- `codeatlas.enableLiveReviewer`: Enable live LLM reviewer (default: `false`).
- `codeatlas.enablePatchSuggestions`: Enable draft patch generation (default: `false`).
- `codeatlas.enableTestExecution`: Enable targeted sandbox test execution (default: `false`).
- `codeatlas.enableFullSuiteExecution`: Enable full-suite sandbox test execution (default: `false`).
- `codeatlas.githubDryRun`: Require GitHub dry-run by default (default: `true`).
- `codeatlas.commentMode`: GitHub PR comment mode (`summary`, `inline`, `both`).

## Service discovery and commands

The extension discovers a service in this order:

1. An explicitly saved `codeatlas.serviceUrl` (including an explicit value equal to the displayed default).
2. Workspace `.codeatlas/service.json`, containing either `{"url":"http://127.0.0.1:8765"}` or `{"host":"127.0.0.1","port":8765}`.
3. Start the existing `codeatlas serve` service only when both `serviceMode` is `managed` and `autoStartService` is `true`.
4. Display a setup message with the configuration and Start Local Service options.

A configured but unhealthy endpoint is reported, rather than falling through to another service. Malformed configuration fails closed. Disabled mode overrides discovery and blocks all service requests.

| Command | Behavior |
| --- | --- |
| **CodeAtlas: Start Local Service** | Explicitly start a local service in external or managed mode. A matching existing healthy service is reused without claiming ownership. |
| **CodeAtlas: Stop Local Service** | Stop only the child process created by this extension session. |
| **CodeAtlas: Restart Local Service** | Await owned-process shutdown, then perform startup and health verification again. |
| **CodeAtlas: Select Configuration Profile** | Validate profiles, select via QuickPick, and save only the active name in workspace settings (user settings when no workspace is open). |
| **CodeAtlas: Open Configuration** | Open VS Code settings filtered to CodeAtlas. |
| **CodeAtlas: Check Service Health** | Probe the existing `/health` endpoint and refresh status. |
| **CodeAtlas: Copy Comparison Range** | Copy the full validated `base...head` range from the Status view when both revisions are resolved. |

None of these commands starts a review. **Start Review** remains explicit. Changing a profile affects subsequent operations; it does not rerun an existing review.

### Lifecycle settings

| Setting | Default | Meaning |
| --- | --- | --- |
| `codeatlas.serviceMode` | `external` | `external`, `managed`, or `disabled` |
| `codeatlas.serviceCommand` | `codeatlas serve` | Direct executable; also supports `python -m codeatlas.cli serve`. Quote executable paths containing spaces. |
| `codeatlas.serviceHost` | `127.0.0.1` | `127.0.0.1` or `localhost`; managed binding always uses numeric loopback. |
| `codeatlas.servicePort` | `8765` | Integer from 1 to 65535 |
| `codeatlas.autoStartService` | `false` | Opt-in startup in managed mode |
| `codeatlas.healthCheckInterval` | `30` | Seconds between checks; `0` disables polling |
| `codeatlas.baseBranch` | `main` | Base branch or revision used when starting a review. Read at Start Review time; empty or whitespace-only values fall back to `main`, surrounding whitespace is trimmed. The active value is shown in the Status view under the review run. |
| `codeatlas.activeProfile` | `default` | Selected profile name |
| `codeatlas.profilePath` | `.codeatlas/profiles.json` | JSON file directly inside workspace `.codeatlas/` |
| `codeatlas.profiles` | `{}` | Named user profiles in VS Code user settings |

Install CodeAtlas into the Python environment used by VS Code (`python -m pip install -e '.[dev]'` for a development checkout). The extension does not install dependencies. It starts from the user's home directory rather than the repository, requires Workspace Trust, and passes only `--host 127.0.0.1 --port PORT`. Shell syntax, wrappers, extra command flags, and inline credentials are rejected. If discovery points to a different port, align the configuration before requesting managed startup.

Startup has a ten-second readiness deadline and verifies the CodeAtlas health identity and child PID. Concurrent starts share one attempt. Startup timeout stops the owned child; stopping cancels an in-flight start. Shutdown sends termination to the owned child handle, waits, and escalates only that child if needed. There is no PID lookup, process-group kill, or automatic crash restart.

The status view shows service mode, URL, ownership, version, active-review count, provider, active profile, last health-check time/result, startup message, and errors. For the active review run it also shows the base branch and the resolved base/head revisions: abbreviated to 12 hex characters with the full SHA in the hover tooltip and a `base...head` comparison line. Revisions appear only after the service resolves them (they arrive in the review status once the run's manifest exists); pending, cancelled, or stale runs — and any service crash — render `unresolved` or hide the values rather than showing a previous run's SHAs. Provider describes active requests (or the most recent request), with `mixed` for multiple providers. Older external services may report `unknown` for metadata added in 10C.

## Configuration profiles

Workspace `.codeatlas/profiles.json` example:

```json
{
  "local": {
    "provider": "mock",
    "timeout": 30,
    "maxFindings": 50,
    "enableLiveReviewer": false,
    "enablePatchSuggestions": false,
    "enableTestExecution": false,
    "enableFullSuiteExecution": false,
    "githubDryRun": true,
    "commentMode": "summary"
  },
  "drafts": {
    "provider": "mock",
    "enablePatchSuggestions": true
  }
}
```

Put the same named-profile mapping under `codeatlas.profiles` in **user** settings for reusable profiles. A workspace profile replaces the same-named user profile entirely; missing fields inherit the ordinary CodeAtlas settings and their safe defaults, never the previously selected profile. An unknown active name fails closed. `default` is built in and can be explicitly overridden.

Only the settings shown above plus `model` are accepted. `model` must be a short identifier, timeout is 0.1–300 seconds, and max findings is 1–200. Boolean values must be actual JSON booleans. The existing snake-case aliases (`max_findings`, `enable_live_reviewer`, `enable_patch_suggestions`, `enable_test_execution`, `enable_full_suite`, `github_dry_run`, `comment_mode`) are accepted in profile files; duplicate aliases are rejected.

Provider, model, timeout, max findings, and patch-suggestion preferences flow through the existing review client. `provider: "live"` additionally requires `enableLiveReviewer: true`. Test/full-suite preferences are used only by **Validate Approved Fix**, after a scoped approval token is supplied. GitHub dry-run and comment mode are validated preferences; the extension currently has no GitHub publishing command. They do not authorize writes.

Unknown fields, secret-like fields/credential values, automatic application, and merge settings are rejected. Configuration files must be regular non-symlink files, at most 64 KiB; profile maps have at most 100 entries. The extension does not write profile contents or copy credentials to settings. Approval tokens are transient password input and are never saved. Process stdout/stderr capture is bounded to 8 KiB per stream: only exact, known-safe lifecycle banners are retained; all other output becomes byte-count metadata. Split secrets, prompts, and provider responses cannot enter logs or UI.

## Verification and limitations

Run `node extension/test_sync.js` in an environment containing an installed CodeAtlas Python package. The suite includes the 10B synchronization regressions, 10C behavioral tests, and a real localhost Python start/restart/stop test. Set `CODEATLAS_TEST_PYTHON` to an absolute interpreter path if needed.

### Phase 10E: compiled developer workflow

With the existing Node/TypeScript, Python development dependencies, and Git installed:

```bash
cd extension
npm run compile
npm run test:workflow
```

`npm test` includes the workflow suite. `python -m pytest` also invokes it through
`tests/integration/test_extension_workflow.py`; compile first. The direct workflow
command deliberately does **not** compile or fall back: it fails if
`package.json.main` does not resolve to `out/extension.js` or the output is missing.
Negative guard tests include a legacy file beside missing compiled output.

The suite drives the compiled command handlers, providers, detail renderer,
decorations, HTTP client, profiles, and managed-process lifecycle. A headless VS Code
API adapter records editor events and UI output. Review polling is advanced explicitly
so every lifecycle transition is observed without sleep-based races. HTTP requests and
managed child processes are real, on ephemeral loopback ports.

`test/fixtures/workflow_service.py` uses the production HTTP router, Pydantic request
and response models, explanation/proposal APIs, scoped approval verification, patch
policy, Git detached-worktree application, and cleanup. It substitutes:

- A deterministic review provider with `preparing → indexing → analyzing → reviewing
  → findings_ready → completed` and scripted failure cases.
- A contextual, applicable fixture diff for the generic draft placeholder.
- A synthetic pass/fail test executor, which asserts that application occurred only in
  the detached sandbox. Outcomes are explicitly marked synthetic and never claim
  verified OS-level network isolation. Production test discovery and command policy
  still execute; no repository test subprocess is launched by this fixture.

The temporary repository contains committed source plus staged, unstaged, and untracked
operator changes. Byte snapshots, HEAD/refs, Git index, diffs, and worktree registrations
must remain identical after proposal creation, denied and approved validation, test
failure, and shutdown. No external repository, LLM/GitHub credential, package installation,
or external network access is needed to run the suite.

Coverage includes:

- All 20 developer-workflow steps: startup/health, review progress, sidebar and exact
  location, cursor enter/leave, QuickPick navigation, details/context/evidence/confidence/
  policy/limitations, explanation, draft proposal, explicit validation, local dismissal,
  refresh, and owned-process shutdown.
- Request/response schema checks against the production Python models; validation's
  wire field is `tests_status`. Test evidence and isolated-application/cleanup records
  are inspected through a private test-control pipe. The existing review/detail
  `test_status`/`test_result` fields remain `not_run`; validation does not automatically
  propagate results into those views.
- Missing approval and wrong proposal/base/hash/path/run scope, credential canaries,
  password input, sanitized process output, absent shell/file-read/apply/approval
  endpoints, and preservation of a separate external service during stop/deactivation.
- Unavailable service, malformed JSON and health schema, stale run, out-of-bounds
  ranges (existing clamping behavior), missing file, cancellation, provider failure,
  rejected validation, failed tests, and a real child-process crash without auto-restart.

Fixture control (including explicit operator token creation) uses an inherited private
pipe, never an HTTP endpoint. Tokens are redacted from its diagnostic transcript. All
children, temporary worktrees, and repositories are cleaned up on success or failure.
This is headless integration coverage; interactive VS Code rendering and Windows process
behavior still require their existing smoke checks.

### Phase 10F: packaging and real extension-host smoke validation

Package the extension and verify package boundaries:

```bash
cd extension
npm run compile
npm run package
```

Packaging runs `@vscode/vsce package` (or deterministic packaging) using `.vscodeignore` to enforce an explicit release allowlist (`out/**`, `resources/icon.svg`, `package.json`, `README.md`). All test fixtures (`test/**`), development artifacts (`src/**`, `tsconfig.json`, `test_*.js`), and deprecated JavaScript files are strictly excluded. The packaging command unpacks the generated VSIX, verifies that all included files match the allowlist, and scans all file contents for secret patterns, API keys, private keys, and tokens.

Run the real extension-host smoke test:

```bash
npm run test:smoke
```

If a real VS Code executable is present in the environment (e.g. via `VSCODE_PATH`, PATH, or standard installation directories), it launches the compiled extension inside the real VS Code extension host via `@vscode/test-electron`. It asserts:
- Extension discovery and activation;
- Configuration default values;
- Registration of all 22 contributed commands;
- Activity-bar views and tree data providers;
- Safe service discovery without automatic review execution;
- External and disabled service modes;
- Graceful health-check failure handling;
- Managed service startup, health verification, and clean shutdown;
If a full VS Code binary or graphical display is unavailable, the smoke test reports skipped with an explicit reason and never reports a false pass.

### Phase 10G: production release metadata and installation verification

#### Production Installation from VSIX

To install the packaged CodeAtlas extension in your local VS Code environment:

```bash
code --install-extension codeatlas-0.1.0.vsix
```

To run end-to-end package verification and disposable-profile installation checks:

```bash
cd extension
npm run package
npm run verify:package
```

The verification script (`verify_package.js`) validates:
- Package metadata consistency (version, display name, publisher, Apache-2.0 license, repository URL, minimum VS Code engine `^1.85.0`);
- Main entry point resolves strictly to `./out/extension.js`;
- Inclusion of required documentation and resources (`README.md`, `CHANGELOG.md`, `LICENSE`, `resources/icon.svg`);
- Package boundary integrity and explicit allowlist conformance;
- Complete absence of secrets, credentials, API keys, private keys, or fixture tokens;
- Isolated disposable-profile installation via `code --extensions-dir <tmp> --user-data-dir <tmp> --install-extension <vsix>` without touching the operator's personal VS Code configuration.

#### Source Maps and Declaration Files Review

TypeScript declarations (`.d.ts`) and source maps (`.js.map`) are intentionally included in the production VSIX package to provide typed interfaces for extension consumers and enable deterministic stack mapping during diagnostic reporting. All packaged declarations and source maps are scanned during packaging and verification to guarantee zero secret, token, prompt, or canary exposure.

- The first workspace folder is used; multi-root service selection is not implemented.
- External connections support HTTP loopback only; remote/TLS/authenticated service discovery is out of scope.
- Python and CodeAtlas must already be installed; service commands cannot be arbitrary wrappers.
- Profile files are revalidated on selection and use; there is no filesystem watcher. Service configuration edits can be picked up with Check Service Health; VS Code settings changes reconfigure discovery and clear stale review state.
- Graceful extension deactivation awaits cleanup. An OS crash or forced extension-host termination cannot guarantee cleanup. Raw process diagnostics are intentionally unavailable.
