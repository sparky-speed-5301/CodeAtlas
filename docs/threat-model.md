# Threat model

## Assets and trust boundary

Source files, comments, README files, issues, pull requests, dependencies,
workflows, test output, and tool output are untrusted data. They may contain
prompt injection, malicious build steps, secrets, misleading claims, or paths
outside the repository. Findings, manifests, and credentials are sensitive
outputs.

## Phase 0/1 controls

- The evaluation runner does not execute case code, import it, install its
  dependencies, or contact the network.
- No real GitHub credentials or repository secrets are stored.
- Schemas constrain status values and evidence shape; malformed cases fail
  loudly rather than becoming passing results.
- Future review snapshots use read-only permissions and isolated Git worktrees.
- Future validation uses non-root, resource-limited containers with restricted
  egress and explicit command allowlists.
- Writes require explicit approval; there is no automatic merge or branch
  mutation.
- Logs record tool/test failures and limitations, with secret redaction before
  persistence.

## Residual risks

The earlier phase descriptions above are historical, not a current security
certification. Later modules implement scoped patch approval, detached-worktree
validation, runner allowlists, provider-output validation and pattern redaction.
Detached worktrees do not provide OS-level filesystem or network isolation;
approved tests execute untrusted repository code. Pattern redaction is incomplete
and no dependency vulnerability database is checked by this scoped audit.

See [platform, release and scoped security evidence](platform-release-security.md)
for reproducible offline checks, the repository path-containment regression and
remaining output-memory, filesystem-race, process-tree and host-profile risks.
Passing deterministic contract tests does not establish safe arbitrary hostile
repository execution.
