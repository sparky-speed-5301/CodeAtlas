# Controlled beta operations

CodeAtlas controlled beta mode is a local, explicit workflow. It preserves the
existing approval, validation, repository-isolation, persistence, redaction,
recovery, and process-ownership boundaries. Beta readiness is not broad public
production readiness.

The default configuration is `config/controlled-beta.json`. It selects offline
deterministic review and the CLI workflow. The configuration schema rejects
unknown fields and locks the following capabilities off:

- automatic apply, approval, commit, merge, and push;
- arbitrary commands and dependency installation;
- unrestricted network access;
- persisted-path cleanup and foreign-process termination;
- credentials, provider transcripts, and repository paths in diagnostics.

## Preflight

```bash
python -m codeatlas --json beta-check --config config/controlled-beta.json
```

`beta-check` validates Python, Git, the local API contract, the strict beta
configuration, and the selected provider mode. It does not start a service,
review, provider request, GitHub request, test command, or workspace mutation.
The result always displays known limitations and keeps `production_ready=false`.

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

## Cleanup

Beta configuration permits cleanup only for temporary resources whose ownership
is established by the operation that created them. It does not infer deletion
authority from a persisted path and does not terminate foreign processes.
Failed validation sandbox retention remains opt-in and disabled by the beta
configuration. A crash-created worktree may require manual inspection and
cleanup.

## Known limitations

Every check, start plan, and report displays the bounded limitations list. The
main limitations are live-provider generalization, unverified operating-system
network isolation, possible crash-left validation worktrees, historical
repository-wide Ruff and mypy debt, and the absence of evidence for broad public
production readiness.
