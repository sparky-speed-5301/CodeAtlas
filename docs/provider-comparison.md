# Controlled provider comparison

Work Package 6 Part 4 extends the offline benchmark scorer with strict provider
comparison metadata. It also provides an explicit Python collection function
for deterministic, live, fallback, and dry-run paths. Collection and scoring
are separate: `codeatlas benchmark` never contacts a provider.

## Comparison controls

Every run in a comparison must identify the same case and have byte-equivalent
control metadata:

- repository identity, base SHA, and head SHA;
- labelled finding anchor;
- bounded repair context hash;
- canonical full repair request hash;
- policy version;
- effective file, changed-line, and byte patch limits;
- validation-policy identifier and controlled test-command policy;
- language and target files.

The full request hash covers the finding, bounded context, policy, capabilities,
and response contract. The context hash alone is insufficient to establish
that requests are equivalent. Neither hash replaces redaction: request
collection first validates the existing bounded `RepairRequest` contract.

Controls are checked against the benchmark case. Effective patch limits may be
stricter than the case's ceiling, but all runs in the comparison must use the
same limits. Changed controls cause scoring to fail rather than producing a
misleading provider comparison.

## Explicit modes

| Mode | Behavior |
|---|---|
| `offline` | An explicit non-network provider is used. |
| `live` | An online primary provider is contacted only with `allow_live=True`. |
| `fallback` | An online primary failed and an explicit offline fallback was actually attempted. |
| `dry_run` | The request is validated and hashed, but no provider is called and no response, patch, validation, tests, usage, retries, or cost is recorded. |

A requested fallback path whose primary succeeds is recorded as `live`, not as
a fallback success. An actual fallback records both its primary trigger code
and any final fallback failure code. The effective provider name belongs to
the provider that produced the result, not to the failed primary.

## Safe metadata

Comparison observations allow only bounded provider/model identifiers, modes,
control hashes and versions, request/response byte counts, durations, bounded
input/output token counts, stable failure codes, retries, patch hashes,
validation and test statuses, and optional bounded USD cost metadata with a
`reported` or `estimated` basis.

Unknown fields are rejected. Prompts, transcripts, source, patches, diagnostic
text, credentials, API keys, approval tokens, request headers, and full provider
endpoint URLs are not fields in the comparison contract. Existing repair secret
scanners audit metadata values. Validation errors hide input values so rejected
provider content is not echoed through CLI diagnostics.

Request and response byte counts state their basis. The Python collector reports
serialized structured-request and accepted structured-output sizes; these are
not claimed to be HTTP wire sizes. Token counts, transport retry counts, and
cost remain unavailable when the provider interface does not expose them.

The collector never applies a patch, approves validation, runs commands, commits,
merges, or pushes. It records generated output only as a normalized patch hash.
Validation and test statuses remain `not_run`. Generation alone proves no fix.
Externally collected validation and test outcomes may be supplied as metadata,
but the scorer does not execute or independently reproduce them.

## Python collection

```python
from codeatlas.benchmark.provider_collection import collect_provider_comparison

observation = collect_provider_comparison(
    request,  # existing bounded, validated RepairRequest
    comparison_id="public-case-1",
    case_id="case-1",
    finding_anchor_id="finding-1",
    validation_policy="static-only-v1",
    run_mode="dry_run",
    provider=provider,
)
```

Use `run_mode="live", allow_live=True` only for a deliberately authorized live
collection. A fallback run additionally requires `fallback_provider` whose
`network_access` is false. The function returns metadata, not the prompt,
response, repository source, or patch.

## Offline scoring

Supply a JSON list of `ProviderComparisonObservation` objects:

```bash
codeatlas benchmark \
  --suite benchmark-suite.json \
  --provider-comparisons provider-comparisons.json \
  --output comparison-report.json
```

The checked-in contract fixture can be exercised without network access:

```bash
codeatlas benchmark \
  --suite eval/cases/real-repositories/v1/suite.json \
  --provider-comparisons eval/cases/real-repositories/v1/example-provider-comparisons.json
```

`python -m codeatlas.benchmark.runner` supports the same options. Comparisons may
be scored alone or alongside separate review and repair observation files.
Reports retain per-run metadata, control hashes, present/missing modes, and
separate operational summaries for each mode. These summaries are not model
accuracy or patch-correctness scores. Missing modes are explicit, and no combined
provider accuracy is emitted.

Report writing remains atomic, and the comparison input is protected against
being overwritten by `--output`. Comparison files are bounded to 8 MB and 10,000
runs. Tests use deterministic providers and fake online adapters; no real network
requests, credentials, or public-repository quality claims are needed.

## Remaining work

Actual public-repository observations and authorized live-provider collection
remain separate from contract tests. Detailed requirements for Work Package 6
Parts 5–10 have not yet been supplied; this change implements Part 4 only.
