# Parts 11–13: platform, release and scoped security evidence

These checks are local engineering evidence, not production-readiness or a
security certification. Machine-readable statuses are `passed`, `failed`,
`blocked`, and `not_run`. Declared requirements are separate from observations.

## Reproduce offline

From the repository root, run:

- `python -m pytest tests/unit/test_release_platform_evidence.py tests/unit/test_security_boundaries.py -q`
- `python -m eval.eval_security_audit`
- `python -m eval.eval_release_evidence`

The release command returns nonzero for any failed or blocked check. It uses
local tooling only, sets `PIP_NO_INDEX=1`, never calls `npx`, and copies inputs
into disposable staging directories rather than modifying extension outputs.
The wheel is assembled with the standard library from the declared pure-Python
package files; it does not install a build backend. The extension requires local
TypeScript dependencies and VS Code CLI for installation checks.

Artifacts and reports are in `eval/results/release/`:

- `release-evidence.json`: actual command outcomes, output hashes, artifact
  inventories with per-file SHA-256 hashes, metadata checks, scans, and gaps.
- `SHA256SUMS`: SHA-256 of each artifact actually produced.
- `platform-matrix.json`: explicit Python 3.11/3.12/3.14, Linux, Windows,
  Go, Java, Rust, and VS Code host availability plus legacy operating-system
  feature rows. Unobserved environments remain `unavailable` or `not_run`.
- `codeatlas-0.1.0.vsix`: deterministic archive assembled from freshly compiled
  runtime files using the existing package allowlist and generated envelope.
- `codeatlas-0.1.0-py3-none-any.whl`: deterministic network-free pure-Python
  wheel with metadata, entry point, hashes, and a standard `RECORD` file.

Wheel reproducibility compares two independent standard-library archive
assemblies byte for byte. This verifies this package's artifact, not setuptools
backend equivalence. VSIX reproducibility compares two archive assemblies of the
same newly compiled inputs; it does not claim independently reproducible
compilation or Marketplace/vsce certification. Inputs are an unsnapshotted working tree, not
an immutable signed release. Re-running overwrites the evidence files; do not
concurrently write to the same destination. Inventory and checksum reports are
not signatures or attestations of origin.

Wheel install/uninstall, when available, uses a clean disposable venv with
`--no-index --no-deps`. It checks distribution metadata, package import, entry
point registration, uninstall and import absence. This is not a
dependency-complete CLI runtime smoke. VSIX install/uninstall uses disposable
`--user-data-dir` and `--extensions-dir` directories, verifies package presence
and removal from the extension registry; it does not exercise GUI activation.
User profiles are not altered. Temporary directories are removed on completion.

Archive scans cover all entries, including the VSIX envelope, with bounds on
compressed size, expanded size and entry count. They reject unsafe original
ZIP names, duplicates and links, scan credential patterns without persisting
matched values, and enforce wheel/VSIX package boundaries. Pattern coverage is
incomplete. No online vulnerability database or dependency CVE scan is run.

## Windows and Linux setup

On Windows PowerShell, create a virtual environment with `py -3.11 -m venv .venv`
(or another supported installed Python), activate it with
`.\.venv\Scripts\Activate.ps1`, and install the project using the approved local
package workflow. On Linux, use `python3.11 -m venv .venv`, activate with
`source .venv/bin/activate`, and use the same package workflow. A clean source
checkout does not install dependencies or contact providers automatically.

For the extension on either platform, enter `extension/`, run `npm ci`, then
`npm run compile`. Use `npm run verify:package` before packaging. Service use is
optional; deterministic offline benchmark collection and ordinary `doctor` do
not require a running service. Live-provider use requires explicit selection,
approved environment credentials, and network permission as documented in the
provider and benchmark guides.

## Recorded local observations

On Windows x86-64, Python 3.14.0 and Node 24.14.0:

- Platform/archive evidence regressions: 9 passed.
- Scoped security audit: 145 passed, 1 skipped across 146 selected tests.
- Local TypeScript compilation, wheel and VSIX byte comparisons, package-boundary
  and credential-pattern scans, and disposable install/uninstall passed.
- Python 3.14, Windows, Java, and the VS Code host were detected. Python 3.11,
  Python 3.12, Linux, Go, and Rust were unavailable and were not reported passed.
- Linux native execution, GUI activation, dependency-complete installation and
  OS-level network isolation remain unverified.

The JSON reports are authoritative for the latest run; this section records the
observations from the implementation session, not a promise of future outcomes.

## Scoped security audit

`eval/results/security-audit.json` records selected test files, real pytest
counts, failures/errors/skips and explicit exclusions. Boundaries covered are
repository paths; provider context/output (fake transports); patch paths and
approval; runner allowlists/output redaction; and extension package/workspace
trust tests. Persistence, service, GitHub and benchmark internals are excluded.
No credentials, live providers or proprietary data are required.

One root defect was reproduced: `scan_repository` checked lexical paths but
could classify a linked file resolving outside its root. It now checks resolved
containment before classification and skips failed resolution. The deterministic
regression failed before the fix and passes afterward. The real symlink test
skips on this Windows host because symlink creation lacks permission; the skip
is recorded, not counted as a native symlink pass.

Residual risks remain: resolve-before-read is subject to filesystem races;
repository traversal is not a hardened hostile-filesystem sandbox; approved
repository tests still execute untrusted code; subprocess capture can consume
unbounded memory before output truncation; descendant process timeout
containment and network isolation are not established; and Windows profile
paths exposed to child processes are not a host-filesystem isolation guarantee.
These architectural gaps are documented rather than hidden behind passing
policy tests. No broad lint/type or cross-agent implementation changes were made.
