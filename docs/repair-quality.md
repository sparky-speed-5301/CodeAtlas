# Phase 12: Real AI Repair Quality

CodeAtlas repair generation has two explicit boundaries:

1. `codeatlas.providers.LiveRepairProvider` accepts a single bounded
   `RepairRequest` and returns only the strict `codeatlas.repair.v1`
   `RepairOutput` contract.
2. `codeatlas.orchestrator.RepairVerificationLoop` adapts that output into the
   existing patch parser, policy, redaction, syntax, and isolated-validation
   systems.

Provider output is untrusted data. It cannot apply a file, approve validation,
mint a token, select a command, change policy, increase finding severity, or
raise the trusted CodeAtlas confidence bound. Provider exceptions are converted
to stable reason codes: `provider_unavailable`, `timeout`, `rate_limit`,
`malformed_response`, `schema_failure`, `output_too_large`, `secret_detected`,
`unsupported_model`, `budget_exceeded`, `network_disabled`, and
`retry_exhausted`.

## Configuration

Use `RepairProviderConfig` or `load_repair_provider_config`. Repository
configuration contains the provider/model name and bounded limits only. A
credential is resolved from the environment variable named by
`api_key_env_var`; the credential itself is never put in a request, proposal,
manifest, evidence record, message, or package artifact.

```python
from codeatlas.providers import RepairProviderConfig, load_repair_provider_config

config = RepairProviderConfig(
    provider_name="openai_compatible",
    model_name="gpt-4o-mini",
    mode="online",
    enabled=True,
    timeout_seconds=30.0,
    max_response_bytes=256_000,
    max_repair_attempts=2,
    deterministic_fallback=True,
)
```

`mode="offline"` fails closed at the online boundary. A deterministic provider
may be passed to the loop explicitly as a fallback; with no fallback, the loop
abstains rather than inventing a patch.

## Context and verification

`RepairContextBuilder` reads only the detached head snapshot, the eligible
finding, and already-bounded packet relationships. It records provenance
labels, truncation sections, trusted identity, target policy, language
capabilities, and a SHA-256 `context_hash`. Optional truncation lowers the
trusted confidence bound; required context that cannot fit is rejected.

The loop permits only a bounded number of attempts. Every candidate passes
structured parsing, target and hunk scope checks, secret scanning, patch
policy, in-memory syntax validation, and—only when the caller supplies an
explicit validation approval token—existing isolated validation and targeted
test execution. A successful loop result is still an approval-required draft;
there is no automatic apply path.

## Measuring quality

Run the deterministic benchmark:

```bash
python eval/eval_repair_quality.py
```

Real repository cases are declared in a JSON list containing `case_id`,
`language`, `target_file`, `changed_line_range`, and optional
`expected_source`. Candidate responses are supplied as a JSON object keyed by
case ID:

```bash
python eval/eval_repair_quality.py \
  --repository /path/to/repository \
  --cases repair-cases.json \
  --candidates repair-candidates.json
```

The benchmark reports parseability, scope and secret safety, syntax safety,
and usefulness. It reads declared files only and never invokes a provider,
command, package manager, or network operation.
