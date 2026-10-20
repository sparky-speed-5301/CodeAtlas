# Controlled beta operations

CodeAtlas controlled beta mode is a local, explicit workflow. It preserves the
existing approval, validation, repository-isolation, persistence, redaction,
recovery, and process-ownership boundaries. Beta readiness is not broad public
production readiness.

The Work Package 9 [controlled beta evidence plan](controlled-beta-evidence-plan.md)
is a separate, predeclared collection boundary. It requires explicit repository
authorization and per-session opt-in; it does not add a telemetry collector or
change any existing approval, validation, persistence, recovery, or repository
scope rule.

The default configuration is `config/controlled-beta.json`. It selects offline
deterministic review and the CLI workflow. The configuration schema rejects
unknown fields and locks the following capabilities off:

- automatic apply, approval, commit, merge, and push;
- arbitrary commands and dependency installation;
- unrestricted network access;
- persisted-path cleanup and foreign-process termination;
- credentials, provider transcripts, and repository paths in diagnostics.

## Quick start: offline CLI

Use an approved installed CodeAtlas package, Python 3.11 or newer, and Git.
Installation is an operator prerequisite, not an onboarding action. For an
approved development checkout, the existing setup command is
`python -m pip install -e ".[dev]"`; it can download dependencies and is not an
offline command unless all dependencies are already available locally.

Choose a controlled local repository with a clean tracked and untracked worktree
and locally available base/head commits. Replace the angle-bracket placeholders
below with your values. Shell examples use Bash continuations; in PowerShell run
each command on one line rather than copying the trailing backslashes.

```bash
python -m codeatlas --help
python -m codeatlas --json beta-check --config config/controlled-beta.json
python -m codeatlas --json beta-onboard --repo <REPOSITORY> --base <BASE> --head <HEAD> --config config/controlled-beta.json --output onboarding.json
python -m codeatlas --json beta-start --config config/controlled-beta.json
python -m codeatlas review --repo <REPOSITORY> --base <BASE> --head <HEAD> --review-provider mock --json
python -m codeatlas --json beta-report --config config/controlled-beta.json
```

Run from a directory where the beta configuration exists and use new artifact
filenames in an existing operator-controlled directory, preferably outside the
reviewed repository. A report created inside that repository can itself make the
next clean-worktree check fail. Stop on a blocked or unavailable prerequisite;
read limitations even when the exit code is zero. `beta-start` only prints a
plan: the subsequent review is a separate explicit invocation. Use the session
workflow below when you need durable stage metadata for an execution report.

The default configuration is fully deterministic and requires neither a model
credential nor a running service. Offline local review does not contact GitHub
or a provider, install dependencies, execute repository tests, or apply fixes.
It may create owned review snapshots and explicitly requested artifacts; "no
automatic mutation" means no hidden source edits, approvals, or remote writes,
not that every command is free of temporary I/O.

## Preflight

```bash
python -m codeatlas --json beta-check --config config/controlled-beta.json
```

`beta-check` validates Python, Git, the local API contract, the strict beta
configuration, and the selected provider mode. It does not start a service,
review, provider request, GitHub request, test command, or workspace mutation.
The result always displays known limitations and keeps `production_ready=false`.

## Controlled repository onboarding

```bash
python -m codeatlas --json beta-onboard --repo <REPOSITORY> --base <BASE> --head <HEAD> --config config/controlled-beta.json
```

`beta-onboard` reuses the beta configuration, provider preflight, loopback client,
repository identity convention, deterministic benchmark, and no-overwrite export.
It does not change the five-check `beta-check` contract. Its separate
`beta-onboarding-v1` report contains at most 18 named checks and is capped at
32 KiB. `--output` uses the same atomic, no-overwrite export as `beta-report`.

The report distinguishes:

| Status | Meaning | Exit code |
| --- | --- | --- |
| `ready` | Every applicable check succeeded without a recorded limitation. | 0 |
| `ready_with_limitations` | No check blocked or was unavailable, but some evidence remains unverified. | 0 |
| `blocked` | A known invalid state or policy failure was observed. | 1 |
| `unavailable` | A required probe could not complete or a required tool/service was unavailable. | 4 |

Invalid command arguments or an unavailable export destination return exit code
2. A `blocked` check takes precedence over `unavailable`, which takes precedence
over limitations. Current onboarding deliberately records limitations: it is
never production-readiness, approval, or test-pass evidence.

### Local checks and bounds

The workflow checks Python, installed CLI distribution version, the configured
provider, and per-invocation `--allow-live-provider` authorization. Even an
authorized live configuration does not cause a provider request during onboarding.
Repository identity is a hash of its canonical local root, not its path or proof
of GitHub ownership. `--expected-repository-id`, `--expected-base-sha`, and
`--expected-head-sha` allow explicit comparison against previously recorded pins.
Revisions are resolved locally without fetching; filenames and raw command output
are never included in the report.

Cleanliness includes tracked and untracked changes. Supported languages are
detected from committed filenames at the requested head. Installed test-tool
availability is checked without executing tools, repository wrappers, tests, or
package installation. Python's standard-library test runner is available with
the interpreter; this does not imply pytest or project-specific dependencies are
installed. Other discovery checks look for supported JS/TS runners or installed
Go, Cargo, Maven, or Gradle tools. Discovery is not validation execution.

A fixed, embedded Python fixture runs through the existing deterministic repair
benchmark in memory. It checks parsing, scope, syntax, and expected output; it
does not measure this repository's review usefulness. Cleanup creates and removes
only a newly owned temporary directory containing a fixed marker. It never
cleans existing worktrees, deletes persisted paths, or terminates other processes.

The global `--timeout` option supplies a shared onboarding deadline, capped at
30 seconds. Each fixed Git or GitHub CLI probe is capped at five seconds and
64 KiB per output stream. Service calls use the remaining deadline, capped at ten
seconds per call. Local filesystem operations, thread shutdown, and SQLite's
existing busy timeout mean this is not a hard operating-system wall-clock bound.
Oversized repository inventories are unavailable rather than partially accepted.

### Service, storage, and extension checks

```bash
python -m codeatlas --json --service-url http://127.0.0.1:8765 beta-onboard --repo <REPOSITORY> --base <BASE> --service --extension-version 0.1.0
```

`--service` explicitly authorizes read-only loopback calls to `/health`,
`/capabilities`, and `/diagnostics/storage` under `/api/v1`. VS Code onboarding
requires a selected service; CLI-only onboarding reports service checks as
unverified when omitted. A reported service provider mode that differs from the
selected beta mode blocks onboarding. The extension version is operator-supplied compatibility
information, not evidence that an extension is currently connected.

The new storage endpoint operates on the service's already-owned database
connection. It performs a bounded `quick_check(1)` and verifies the current schema
version and migration ledger checksums, with a 250 ms elapsed/progress budget and
100,000 SQLite VM-step cap. It does not reopen databases, perform migrations,
change ownership, or write records. Process-local storage is `not_applicable`.
An older service with no endpoint records an explicit limitation; unhealthy or
contradictory storage responses cannot be treated as healthy. If corruption stops
the service from starting, onboarding reports service unavailability rather than
opening the database independently or guessing the cause.

### GitHub dry-run preflight

Adding both `--github-repo OWNER/REPO` and `--github-pr NUMBER` explicitly permits
one bounded `gh pr view` metadata request using existing GitHub CLI authentication.
The check compares the PR's exact base/head commits with the local resolved pins.
It never posts, approves, merges, or checks write permissions.

This metadata check is **not a full review smoke run**. The report retains
`github_review_not_run`. To execute a complete review-only dry-run separately,
use the existing explicitly invoked command:

```bash
python -m codeatlas github review --repo <OWNER/REPO> --pr <NUMBER> --local-repo <REPOSITORY> --dry-run
```

That command has its own existing review/worktree lifecycle and budgets; it is
not silently run inside onboarding's short deadline. Actual extension interaction
and a full GitHub review dry-run remain operator verification steps.

### Stable failure codes

The bounded check catalog includes `missing_provider`,
`invalid_provider_configuration`, `unavailable_service`, `corrupted_database`,
`migration_invalid`, `dirty_worktree`, `stale_revision`, `unsupported_language`,
`unavailable_test_tool`, `extension_service_mismatch`,
`github_permission_failure`, and `invalid_beta_configuration`. Additional codes
cover unavailable local probes, invalid repository identity, missing CLI/GitHub
installation, failed offline smoke, unavailable cleanup, and absent authorization.
All details are fixed text: secrets, raw source, paths, prompts, transcripts,
remote error bodies, and unrestricted environment values are excluded.

## Start a beta workflow

```bash
python -m codeatlas --json beta-start --config config/controlled-beta.json
```

`beta-start` returns a bounded workflow plan. It deliberately does not create a
hidden daemon or background process, infer approval, or execute a repository
command. This avoids introducing a second process-ownership system. The plan
contains explicit next commands for:

- a local CLI review;
- a GitHub review-only dry-run;
- a bounded beta report;
- a loopback service when the selected workflow is VS Code.

The VS Code workflow uses the existing managed extension or an explicitly
started loopback service. The CLI workflow uses the existing `review` command.
Neither workflow gains automatic repair authority from beta mode.

Live-provider mode requires all of the following:

1. `provider_mode` is explicitly `live_provider`;
2. a strict `live_provider` selection is present;
3. its separate provider configuration exists and matches the selected model,
   credential environment-variable name, and privacy mode;
4. the credential environment variable is present;
5. `beta-start` receives `--allow-live-provider` for that invocation.

The credential value is never persisted or exported. There is no automatic
fallback from live mode to another provider.

### Configure the provider explicitly

Keep the default `provider_mode: offline_deterministic` and `live_provider: null`
for offline operation. To select live mode, use an operator-reviewed beta JSON
configuration with `provider_mode: live_provider` and a `live_provider` selection
containing `provider_name` (`live`), `model_name`, `provider_config_path`,
`api_key_env_var`, and `privacy_mode`. The provider file path is relative to the
beta configuration directory; absolute paths, traversal, and linked paths are
not permitted. Keep all locked safety, diagnostics, and cleanup fields unchanged.

The separate JSON/YAML provider file uses the existing reviewer provider
contract: `provider_name`, `model_name`, `api_key_env_var`, `privacy_mode`, and
bounded settings such as `timeout_seconds`, `max_retries`, `request_budget`,
`max_output_tokens`, `max_response_bytes`, and `max_packet_bytes`. An optional
`api_base_url` must satisfy the provider URL policy. Unknown keys and inline
credentials are rejected. The model, credential-variable name, and privacy mode
must match the beta selection. Set the named credential only in the authorized
process environment; do not put its value in files, terminal examples, feedback,
or VS Code settings. A privacy-mode declaration is not independent verification
of the provider's retention practices.

`beta-check` checks configuration and credential availability without making a
request. Pass `--allow-live-provider` separately to live onboarding, startup,
session creation, and session review when requested. Authorization on one
invocation is not carried forward to another. Onboarding and startup still do
not call the provider. Ordinary `review` is a separate interface: live review
uses `--review-provider live --provider <MODEL> --provider-config <PROVIDER_FILE>`;
adding its `--dry-run` prepares the request without contacting the model. Beta
configuration does not silently configure an independently started service or
extension. Check their provider mode separately before proceeding.

### VS Code: use the existing extension

Install the approved existing CodeAtlas VSIX and use the Python environment
containing the matching CLI. Follow the [extension setup and service discovery
guide](../extension/README.md); beta mode does not add another extension or daemon.
Use Workspace Trust only for a repository you have reviewed and accepted.

1. In an operator-reviewed beta configuration, select `workflow: vscode`.
2. Explicitly start `python -m codeatlas serve` in a terminal and save
   `codeatlas.serviceUrl` as `http://127.0.0.1:8765`, or choose the existing managed
   service workflow and explicitly invoke **CodeAtlas: Start Local Service**.
   Leave automatic startup disabled unless deliberately opting into managed
   startup. A configured unhealthy endpoint does not trigger fallback.
3. Select an offline profile with `codeatlas.provider: mock`,
   `codeatlas.enableLiveReviewer: false`, and patch/test/full-suite preferences
   disabled. Live selection additionally requires `provider: live`,
   `enableLiveReviewer: true`, a selected model, and authorized service provider
   configuration. Preferences never grant approval or apply authority.
4. Invoke **CodeAtlas: Check Service Health**. Run `beta-onboard --service` with
   the same loopback URL, the VS Code beta configuration, and the actual installed
   `--extension-version`. A supplied version only checks compatibility; it does
   not establish extension activation or interactive health.
5. Set `codeatlas.baseBranch`, explicitly start a review, and inspect the status,
   resolved comparison SHAs, findings, context, evidence, and limitations. Review
   never starts merely because onboarding or a profile change succeeded.

Use **CodeAtlas: Stop Local Service** only for the child owned by that extension
session. An external service remains owned by its launching operator. Interactive
rendering and real extension-host behavior require separate observations; a
headless workflow fixture is not evidence of a successful user beta session.

## Report and diagnostic export

```bash
python -m codeatlas --json beta-report --config config/controlled-beta.json
python -m codeatlas --service-url http://127.0.0.1:8765 --json beta-report \
  --config config/controlled-beta.json --service --output beta-report.json
```

Without `--service`, `beta-report` is local and performs no service request.
With `--service`, it reuses the bounded read-only loopback diagnostics contract:
three sequential GET requests at most, a shared deadline capped at ten seconds,
and at most 50 review summaries. Reports are capped at 32 KiB and never include
repository paths, credentials, approval tokens, provider transcripts, source
content, or raw remote errors.

Exports require a new regular file in an existing directory. Existing files and
symbolic-link destinations are rejected, so reporting cannot overwrite project
or user data.

## GitHub dry-run

The beta configuration fixes GitHub mode to `review_only_dry_run`. The generated
plan uses `github review --dry-run`, which cannot approve, merge, apply, commit,
or push. Publishing remains a separate existing command path requiring an
explicit `--post`; beta mode never adds that flag.

### Review-only CI

The existing `.github/workflows/codeatlas-review.yml` uses `pull_request`,
read-only permissions, base-revision tooling, non-persisted checkout credentials,
per-PR cancellation, and a bounded timeout. Its PR checkout is review data, not
trusted executable tooling. Keep GitHub execution in dry-run mode: no `--post`,
write permissions, PR tests/build/install scripts, approvals, commits, merges,
or pushes. Trusted tooling installation is CI setup, not permission to install
or execute dependencies from the PR head. Inspect the local dry-run artifact and
stale-revision failures. Local mocks or workflow inspection do not establish that
a GitHub-hosted CI run passed. See [persistent review operations](persistent-review-operations.md).

## Controlled beta sessions

`beta-session` records one bounded, metadata-only session in an explicit file. It
reuses `beta-onboard` before creating a session, so repository identity, a clean
worktree, immutable base/head pins, selected language, beta configuration, and
per-invocation live-provider authorization are checked first.

```bash
python -m codeatlas --json beta-session create \
  --session beta-session.json --repo <REPOSITORY> --base <BASE> --head <HEAD> \
  --case-id <CASE> --language python --config config/controlled-beta.json
python -m codeatlas --json beta-session review \
  --session beta-session.json --repo <REPOSITORY> --config config/controlled-beta.json
python -m codeatlas --json beta-session github-dry-run \
  --session beta-session.json --repo <REPOSITORY> --github-repo OWNER/REPO --github-pr NUMBER
```

The session record is capped at 32 KiB and contains only an opaque session ID,
repository identity hash, case and language identifiers, immutable revisions,
provider mode, timestamps, bounded event history, safe aggregate finding data,
proposal/validation/test/apply/revert state, retry/cancellation/timeout state,
and optional safe model/context/patch identifiers. It never stores repository
paths, raw source, prompts, provider transcripts, credentials, approval tokens,
sandbox paths, raw command output, or unrestricted environment data. Updating a
session is explicit and atomic; exports use the existing no-overwrite beta
export mechanism:

```bash
python -m codeatlas --json beta-session export --session beta-session.json --output beta-session-export.json
```

Every stage is separately invoked. A session does not advance itself from
review to proposal, approval to validation, validation to tests, or validation
to apply. The status values have distinct meanings: `offline` is a deterministic
local review, `live` is an explicitly authorized provider request, `fallback`
is reserved for a future explicitly authorized fallback and is never inferred,
`unavailable` means an allowed dependency could not be used, `blocked` means a
safety or policy gate rejected the request, and `not_run` means no stage was
executed.

Fix proposal, validation, apply, and revert commands use the existing loopback
service and require its run, finding, and proposal identifiers. Validation first
requires `beta-session approve-validation` with the existing validate-scoped
approval token; the token is sent only to the service and is never written to
the session. `beta-session validate` runs isolated validation without tests.
The separate `beta-session targeted-tests` command explicitly requests targeted
tests during the service's approval-gated isolated validation, and existing
command policy may still block execution. It never runs a full suite. Apply requires all existing service gates plus
`--approve-apply`, an apply-scoped token, and the exact validated patch hash.
Revert is optional and uses the existing exact-byte, post-apply state check.
Neither command commits, merges, pushes, or starts an automatic action.

| Condition | Exit code |
| --- | --- |
| Explicit stage recorded successfully | 0 |
| Policy, approval, validation, or apply gate blocked the request | 1 |
| Explicitly requested service/provider/resource unavailable | 4 |
| Invalid inputs, session file, or export destination | 2 |

## Autofix safety and operator review

Review findings and generated proposals are advisory. The explicit chain is:
inspect finding and evidence, request a proposal, inspect its exact diff and
scope, approve validation, validate in an isolated worktree, optionally request
approved targeted tests, inspect observed results, then separately approve and
apply the exact validated patch. No stage approves or starts the next stage.

Validation approval is scoped to the proposal, finding, run, repository,
base/head commits, patch hash, target files, and validate operation. It does not
authorize workspace apply. Apply needs the separate apply-scoped token, explicit
confirmation and patch hash, matching fresh evidence, policy checks, and a clean
unchanged worktree. Never bypass a stale SHA/hash, protected-path rejection,
failed validation, unavailable tool, or apply conflict. Regenerate/review at the
current revision instead. Revert is a separate optional exact-byte operation and
is blocked if post-apply files have changed; it is not a force-reset mechanism.

A parsed patch, syntax success, approval, or green targeted test is **not proof
of semantic correctness**, security, or absence of unrelated regressions. Tests
cover only the recorded executed scope. Missing tools are `unavailable`; execution
that never started remains `not_run` or explicitly policy-blocked, never passed.
No dependency installation, arbitrary command, full-suite execution, commit,
merge, or push is implied by the session workflow. See the existing
[repair workflow](repair-orchestrator.md) for the underlying gates.

## Troubleshooting, incidents, and recovery

Stop the affected stage, preserve its safe identifiers/statuses, and inspect
local state before explicitly retrying. Record only bounded safe metadata in an
incident or feedback artifact. Incident creation/list/export and feedback
collection interfaces are still being integrated: the categories below are an
operator protocol, not invented CLI syntax. See [feedback and report
interpretation](controlled-beta-feedback.md) for the collection boundary.

| Symptom | Safe operator response |
| --- | --- |
| Invalid beta/provider configuration or missing credential | Check the selected file, permitted keys, matching model/privacy mode and credential-variable presence locally. Do not paste credentials or remote error bodies into a report. |
| Service unavailable, timeout, or extension mismatch | Check the configured loopback endpoint and installed versions. Restart only an owned service explicitly; do not kill a discovered PID or silently switch endpoints. Reconnect and start review explicitly. |
| Dirty worktree or stale base/head/repository identity | Inspect tracked and untracked changes and immutable SHAs locally. Preserve operator edits; do not automatically stash, reset, delete, or force apply. Create a new review/session at accepted pins. |
| Unsupported language or unavailable test/compiler tool | Inspect `/capabilities` and tool availability. Keep unsupported/unavailable/not-run outcomes explicit; install approved tools separately only if authorized. |
| Provider failure or deadline exceeded | Inspect safe failure code and request budget. Do not infer fallback or success; explicitly authorize a new attempt if appropriate. |
| GitHub auth, network, rate-limit, or revision failure | Check existing `gh` authentication locally and PR pins. Retry a dry-run only when explicitly requested; never broaden token permissions or post as a workaround. |
| Test failure, cancellation, unsafe patch, or apply conflict | Inspect the recorded scope/result and current diff locally. Request a safe replacement or new validation; no automatic re-run or mutation. |
| Database unavailable, corrupted, or migration-invalid | Use diagnostics through the owning service. Do not open, repair, migrate, delete, or replace its live database from a reporting tool. Stop the owner before any separately authorized offline recovery. |
| Interrupted review/validation or possible orphan worktree | Inspect recovery through the owning service and worktree ownership locally. Treat cleanup and workspace state as unverified until observed; a persisted path grants no deletion authority. |

For service-managed review recovery, existing read/cancel/retry commands are:

```bash
python -m codeatlas --json reviews status <RUN_ID>
python -m codeatlas --json reviews events <RUN_ID>
python -m codeatlas reviews cancel <RUN_ID>
python -m codeatlas reviews retry <RUN_ID>
```

Cancellation is cooperative, not proof that all work has already stopped.
Retry explicitly creates a new review and never replays a fix or restores an
approval. After service restart, recovered proposals, validations, approvals,
and apply/revert history are historical only. Previous approval authority is
cleared; missing original bytes mean recovered revert is unavailable. Recovery
never executes a provider, tests, Git mutation, or workspace write. Inspect
`pending_recovery`, `recovered`, `recovery_failed`, `expired`, and
`operation_interrupted` separately from the parent review lifecycle. See
[persistent review operations](persistent-review-operations.md) for the durable
recovery contract.

## Cleanup

Beta configuration permits cleanup only for temporary resources whose ownership
is established by the operation that created them. It does not infer deletion
authority from a persisted path and does not terminate foreign processes.
The underlying validator's failed-sandbox retention is opt-in, but the strict
beta configuration locks it off. A killed or interrupted validation may still
leave an isolated worktree; disabling retention cannot guarantee crash cleanup.
No automatic deletion is authorized by a stored sandbox/worktree path.

Inspect `git worktree list --porcelain` locally, identify the operation that
created a candidate, confirm that no active process uses it, and preserve any
operator data before a separately authorized manual cleanup. Do not publish the
raw inventory (it contains local paths), prune all worktrees, delete arbitrary
temporary directories, or terminate foreign processes. A cleanup warning is
uncertainty, not evidence that cleanup succeeded. Session/onboarding/report and
incident files remain operator-controlled artifacts; delete or archive them only
under an explicit retention decision. Reporting does not delete them or the
service database.

## Known limitations

Every check, start plan, and report displays the bounded limitations list. The
main limitations are live-provider generalization, unverified operating-system
network isolation, possible crash-left validation worktrees, historical
repository-wide Ruff and mypy debt, and the absence of evidence for broad public
production readiness.

### Data, languages, and platform bounds

Supported adapters cover Python, JavaScript, TypeScript, Go, Java, and Rust;
capability availability is not uniform across languages or machines. Static
parsers do not imply installed compilers, formatters, type checkers, test runners,
or project dependencies. Unsupported files can be review data without gaining
repair/test support. Consult the [language matrix](language-support.md) and the
actual capability response, not an assumed all-language pass.

Beta diagnostic/session/incident exports are metadata-only and exclude raw
source, repository paths, prompts, transcripts, credentials, approval tokens,
and raw command output. Other existing artifacts have different contracts:
proposal artifacts may contain a bounded redaction-audited diff, review UI may
show code context, and a separately authorized live request sends approved
bounded context to the selected provider. "Metadata-only report" does not mean
that every workflow artifact is source-free. Keep access and retention decisions
explicit; do not submit proprietary source as beta feedback.

Windows and Linux launchers, Python versions, native language toolchains, real
VS Code hosts, and hosted GitHub execution need separate runtime evidence.
Policy-level offline command restrictions do not prove OS network isolation,
host-filesystem isolation, or descendant-process timeout containment. Missing
platform/tool coverage remains unavailable or not run. Historical repository-wide
Ruff and mypy findings remain baseline debt: focused checks do not erase that
baseline or establish a clean repository-wide lint/type run. See [platform and
security evidence](platform-release-security.md) for observations and exclusions.

Read [feedback and report interpretation](controlled-beta-feedback.md) before
aggregating execution evidence. Contract fixtures, generated patches, and syntax
checks are not external user-beta observations or correctness claims.
