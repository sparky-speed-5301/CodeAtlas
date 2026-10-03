# CodeAtlas VS Code Extension (Phase 10C)

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
   - Shows policy decision, finding counts, test statuses, and patch validation states.

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
| `codeatlas.activeProfile` | `default` | Selected profile name |
| `codeatlas.profilePath` | `.codeatlas/profiles.json` | JSON file directly inside workspace `.codeatlas/` |
| `codeatlas.profiles` | `{}` | Named user profiles in VS Code user settings |

Install CodeAtlas into the Python environment used by VS Code (`python -m pip install -e '.[dev]'` for a development checkout). The extension does not install dependencies. It starts from the user's home directory rather than the repository, requires Workspace Trust, and passes only `--host 127.0.0.1 --port PORT`. Shell syntax, wrappers, extra command flags, and inline credentials are rejected. If discovery points to a different port, align the configuration before requesting managed startup.

Startup has a ten-second readiness deadline and verifies the CodeAtlas health identity and child PID. Concurrent starts share one attempt. Startup timeout stops the owned child; stopping cancels an in-flight start. Shutdown sends termination to the owned child handle, waits, and escalates only that child if needed. There is no PID lookup, process-group kill, or automatic crash restart.

The status view shows service mode, URL, ownership, version, active-review count, provider, active profile, last health-check time/result, startup message, and errors. Provider describes active requests (or the most recent request), with `mixed` for multiple providers. Older external services may report `unknown` for metadata added in 10C.

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

- The first workspace folder is used; multi-root service selection is not implemented.
- External connections support HTTP loopback only; remote/TLS/authenticated service discovery is out of scope.
- Python and CodeAtlas must already be installed; service commands cannot be arbitrary wrappers.
- Profile files are revalidated on selection and use; there is no filesystem watcher. Service configuration edits can be picked up with Check Service Health; VS Code settings changes reconfigure discovery and clear stale review state.
- Graceful extension deactivation awaits cleanup. An OS crash or forced extension-host termination cannot guarantee cleanup. Raw process diagnostics are intentionally unavailable.
- Lifecycle tests exercise Linux and a VS Code API mock; an interactive VS Code/Windows smoke test is still needed.
