# First-class CLI and local API (Work Package 3)

CodeAtlas Core is shared by the standalone CLI, local HTTP service, and IDE
extension. The CLI's service commands are adapters over the same public API
available to CI scripts and future clients. Review, repair, policy, and validation
decisions remain in the Python core.

```text
CodeAtlas Core
├── Python review/repair orchestration and policy
├── codeatlas review                 (in-process, standalone)
└── codeatlas serve                  (local, process-scoped state)
    └── /api/v1
        ├── codeatlas reviews / findings / fix
        ├── codeatlas.client.CodeAtlasClient
        ├── VS Code / Antigravity extension (compatible unversioned routes)
        └── CI and other HTTP clients
```

## Install and run

```bash
python -m pip install -e .
codeatlas --version
codeatlas --help
codeatlas doctor
# Equivalent entry point:
python -m codeatlas --help
```

### Standalone review and CI

```bash
codeatlas review --repo /path/to/repo --base main --head HEAD
codeatlas review --repo /path/to/repo --base main --head HEAD --json --fail-on high
```

`review --json` prints exactly one run manifest to stdout. Live-provider banners
go to stderr. Existing `--json-output`, `--markdown-output`, `--manifest-output`,
and evidence/context/packet export options remain available. A standalone review
does not create a service run: use `reviews start` when you want to continue into
the stateful fix workflow.

`--fail-on none|info|low|medium|high|blocker` defaults to `none`. A threshold matches
that severity or higher; abstained and suppressed findings do not trigger it.

| Exit | Meaning |
| --- | --- |
| 0 | Command succeeded (findings alone do not fail a default review) |
| 1 | Review/operation failed, was rejected, or a transport/wait deadline expired |
| 2 | CLI usage error, including missing required confirmation arguments |
| 3 | Standalone review completed and findings met `--fail-on` |

### Service-managed reviews

Start a service in one terminal, then use any client from another:

```bash
codeatlas serve --host 127.0.0.1 --port 8765
codeatlas --json health
codeatlas --json capabilities
codeatlas --json reviews start --repo /path/to/repo --base main --wait
codeatlas --json reviews list --limit 50 --offset 0
codeatlas --json reviews status RUN_ID
codeatlas --json reviews wait RUN_ID --wait-timeout 300
codeatlas --json findings list RUN_ID --severity high
codeatlas --json findings show RUN_ID FINDING_ID
codeatlas --json findings explain RUN_ID FINDING_ID
codeatlas --json findings feedback RUN_ID FINDING_ID useful
codeatlas reviews cancel RUN_ID
```

Global options precede the command:

```bash
codeatlas --service-url http://127.0.0.1:9000 --request-timeout 120 --json reviews list
```

`CODEATLAS_SERVICE_URL` also sets the origin; an explicit flag takes precedence.
The Python/CLI client accepts loopback HTTP origins, bypasses proxies, and does
not follow redirects. Each request has a timeout and bounded payload/response
size. Requests, especially mutations, are never automatically retried. If a
mutation times out, inspect its current proposal state before taking another
action. `reviews start --wait` preserves the created run ID on stdout if polling
fails. The service keeps working after a wait timeout.

For `health`, `capabilities`, `reviews`, `findings`, and `fix`, global `--json`
emits one result object to stdout. Runtime errors are `{ "error": "code",
"message": "description" }` on stderr. Usage errors retain the normal CLI help
format. Legacy `patch`, `github`, and `eval` commands retain their existing export
options. `findings list` respects the review's `max_findings` display bound
(default 50, maximum 200); review status retains total counts and reports
truncation. Feedback labels are `useful`, `not_useful`, `false_positive`,
`accepted`, `dismissed`, `needs_more_context`, and CLI `clear` (API `null`).

### Full fix workflow

Group scope options go before the fix subcommand. Use the returned IDs and the
full patch hash from `inspect`:

```bash
codeatlas --json fix --run-id RUN_ID --finding-id FINDING_ID eligibility
codeatlas --json fix --run-id RUN_ID --finding-id FINDING_ID generate
codeatlas --json fix --run-id RUN_ID --finding-id FINDING_ID --proposal-id PROPOSAL_ID inspect
codeatlas --json fix --run-id RUN_ID --finding-id FINDING_ID --proposal-id PROPOSAL_ID approve-validation --approve
codeatlas --json fix --run-id RUN_ID --finding-id FINDING_ID --proposal-id PROPOSAL_ID validate
# Optional targeted/full-suite execution uses the existing execution policy:
# ... validate --run-tests
# ... validate --run-full-suite
codeatlas --json fix --run-id RUN_ID --finding-id FINDING_ID --proposal-id PROPOSAL_ID apply --approve --confirmed-patch-hash HASH
codeatlas --json fix --run-id RUN_ID --finding-id FINDING_ID --proposal-id PROPOSAL_ID history
codeatlas --json fix --run-id RUN_ID --finding-id FINDING_ID --proposal-id PROPOSAL_ID revert --confirmed-patch-hash HASH
```

`reject` selects a proposal ID; `regenerate` operates on the finding's latest
proposal. The CLI uses the existing operation-scoped approval token construction
locally, without printing tokens. `validate` requires the separately recorded
validation approval. Apply requires validated evidence, the exact confirmed hash,
an apply-scoped token, and the core's workspace checks. Revert uses the existing
byte-exact restore operation. Generation alone never applies a patch.

By default the service uses the existing offline mock repair provider. To use
Work Package 2's structured provider, supply its explicit configuration:

```bash
codeatlas serve --repair-config /path/to/repair.json
```

Example JSON (the credential is read from the service process's environment):

```json
{
  "provider_name": "openai_compatible",
  "model_name": "gpt-4o-mini",
  "api_key_env_var": "CODEATLAS_API_KEY",
  "mode": "online",
  "enabled": true,
  "timeout_seconds": 30.0,
  "max_repair_attempts": 2,
  "deterministic_fallback": true
}
```

JSON works with the base installation. YAML configuration requires PyYAML
(included in the development extra). Provider mode, budgets, limits, and fallback
settings are passed through to the existing repair verification loop.

## HTTP v1 contract

Base URL: `http://127.0.0.1:8765/api/v1`.

```bash
curl http://127.0.0.1:8765/api/v1/health
curl http://127.0.0.1:8765/api/v1/capabilities
curl http://127.0.0.1:8765/api/v1/openapi.json
curl -X POST http://127.0.0.1:8765/api/v1/reviews \
  -H 'Content-Type: application/json' \
  -d '{"repo":"/absolute/path/to/repo","base":"main","head":"HEAD"}'
```

`openapi.json` is generated from the request/response Pydantic models and
describes all supported v1 operations. Responses include
`CodeAtlas-API-Version: 1`. `version` in health is the package version;
`api_version` is the independent API major version. `schema_version` on a
FixProposal is its existing domain schema version, not the HTTP version.

| Resource | Operations |
| --- | --- |
| `/health`, `/capabilities`, `/openapi.json` | GET discovery |
| `/reviews` | POST start (201); GET paginated runs, newest first |
| `/reviews/{run_id}` | GET status |
| `/reviews/{run_id}/cancel` | POST cooperative cancellation |
| `/reviews/{run_id}/findings` | GET filtered findings |
| `/reviews/{run_id}/findings/{finding_id}` | GET detail |
| `…/feedback`, `…/dismiss` | POST feedback or toggle dismissal |
| `…/fix-eligibility` | GET eligibility |
| `…/fix-proposal` | POST generate (201); GET latest proposal |
| `…/fix-proposal/regenerate`, `…/fix-proposal/reject` | POST lifecycle operations |
| `…/fix-proposal/approve-validation`, `…/fix-proposal/validate` | POST scoped approval/validation |
| `…/fix-proposal/apply`, `…/fix-proposal/revert` | POST guarded workspace operations |
| `…/fix-proposal/apply-history` | GET history |
| `/fix-proposals/{proposal_id}` | GET, with required `run_id` and `finding_id` query scope |
| `/fix-proposals/{proposal_id}/apply-history` | GET with the same query scope |
| `/fix-proposals/{proposal_id}/approve-validation`, `…/validate` | POST with complete scope in the body |
| `/findings/{finding_id}/explain`, `…/patch-proposal` | POST existing explanation/draft patch operations |
| `/proposals/{proposal_id}/validate` | POST existing PatchProposal validation |

POST bodies must be JSON objects with schema-valid types and no unknown fields.
Errors use the stable `error`/`message` shape: 400 invalid request, 404 unknown
resource/version, 405 unsupported method, 409 state conflict, 413 oversized
request, 500 unexpected service failure. Proposal validation/application may
return HTTP 200 with a negative domain outcome; inspect `valid`,
`approval_verified`, `generation_status`, or `apply_status`. The CLI translates
negative requested-operation outcomes to exit 1.

### Compatibility and lifetime

- Existing unversioned routes remain compatibility aliases to the same state
  and core operations. Existing IDE clients can continue using them.
- New clients should use `/api/v1`. Additive response fields may appear within
  v1; clients should tolerate them. Breaking changes require a new major path.
- Unknown API majors never fall back to unversioned behavior.
- IDs, validation approvals, proposal history, and revert bytes are held in
  memory for the service process lifetime. Restarting the service clears them.
  Review artifacts written by the standalone CLI remain ordinary files.
- Repository paths and refs are evaluated on the service host. Cancellation is
  cooperative; it prevents publishing a cancelled review's results but does not
  forcibly terminate an in-flight provider call.

## Python client

```python
from codeatlas.client import CodeAtlasClient, ClientError
from codeatlas.service.models import ReviewCreateRequest

client = CodeAtlasClient("http://127.0.0.1:8765", timeout=30)
run = client.start_review(ReviewCreateRequest(repo="/path/to/repo", base="main"))
completed = client.wait_for_review(run.run_id, timeout=300)
findings = client.get_findings(run.run_id)
print(completed.status, findings.filtered_total)
```

Typed methods cover review, findings, feedback, FixProposal generation,
validation, application, revert, and history. Mutation methods accept the same
request models documented in OpenAPI. `request(method, v1_relative_path, body)`
provides access to the full contract. `ClientError.code` and `.status` expose
machine-readable failures without reflecting arbitrary server response text.

## Verification

`tests/integration/test_platform_cli_api.py` exercises real HTTP, standalone CLI
subprocesses, disposable Git repositories, JSON/exit contracts, OpenAPI response
validation, cross-client state, scoped repair lifecycle, compatibility aliases,
multi-server isolation, provider startup, and bounded transport failures.

```bash
python -m pytest tests/integration/test_platform_cli_api.py -q
```

Verification in the Linux sandbox:

- New CLI/API integration coverage: **31 passed** on Python 3.11.
- Full suite on Python 3.11: **787 passed**.
- Full suite on Python 3.12: **787 passed**.
- The worktree apply matrix covers LF, CRLF, mixed endings, both
  `core.autocrlf=true` and `core.autocrlf=false`, isolated validation, exact
  post-apply diff hashes, byte-exact restore, apply failure, syntax failure,
  and HEAD/ref/index/worktree invariance. The original hard-coded CRLF
  assertion was replaced by explicit logical-content and raw-line-ending
  assertions; Windows behavior is represented by the CRLF/autocrlf cases.
- Extension compile and npm headless suites passed. Real extension-host smoke
  was skipped because the sandbox has no VS Code executable.
- Targeted Ruff E9/F checks passed. Targeted mypy passed for the client, platform
  CLI, contract generator, and service models; wider baseline typing issues remain.
- Wheel build, clean installation with base dependencies only, and installed
  CLI/client/HTTP/OpenAPI smoke passed on Python 3.11 outside the source tree,
  including `python -m codeatlas --help`, `doctor`, JSON health, and
  capabilities.

### Pending host matrix

The current verification host is Linux under WSL2. Python 3.14 is not installed,
and no Windows interpreter, PowerShell, or `cmd.exe` is available, so those
executions are **pending**, not passed. Run the same matrix on Windows with:

```powershell
py -3.11 -m pip install -e ".[dev]"
py -3.11 -m codeatlas doctor
py -3.11 -m pytest -q
py -3.11 -m pytest tests/unit/test_worktree_apply.py tests/unit/test_fix_apply.py tests/integration/test_autofix_workflow.py tests/integration/test_platform_cli_api.py -q
git -C path\to\repo config core.autocrlf true
py -3.11 -m pytest tests/unit/test_worktree_apply.py -q
git -C path\to\repo config core.autocrlf false
py -3.11 -m pytest tests/unit/test_worktree_apply.py -q
py -3.12 -m pytest -q
```

The CRLF cases do not depend on a global Git configuration and can run on any
host. The explicit Windows commands additionally verify native Windows process,
path, and filesystem behavior. For Python 3.14, substitute the installed
interpreter in the same commands once available.
