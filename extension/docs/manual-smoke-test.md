# CodeAtlas Extension — Manual Smoke Test Checklist

Phase 11A dogfooding checklist. Run against a real Git repository (one with a
`main` branch and local commits) in a trusted workspace with the packaged
extension installed.

**Rules for every step:**

- The original worktree must never change during review, Explain, Show
  Context, or draft-fix generation (`git status --porcelain` stays empty and
  `git rev-parse HEAD` is stable).
- No API key, GitHub token, approval token (`CAT-APP-…`), raw prompt, or raw
  provider response may appear in any panel, notification, output channel, or
  the detail webview.
- Failures must always show a clear user-facing message — never a silent no-op.

Check a box only when the step passes completely. Record any failure in the
notes section at the bottom with the step number.

## Setup

- [ ] **S1.** Install the packaged VSIX (or run the extension from source with
  `npm run compile`) and reload the window.
- [ ] **S2.** `codeatlas serve` (or `python -m codeatlas.cli serve`) is
  available per `codeatlas.serviceCommand`; no credentials are required for
  the deterministic provider.

## Workflow

1. [ ] Open a real Git repository in the IDE (trusted workspace).
2. [ ] The CodeAtlas Activity Bar icon is visible; clicking it shows the
       CodeAtlas sidebar with Status, Findings, and Context views.
3. [ ] Run **CodeAtlas: Start Local Service**. A progress/info message appears
       and the Status view shows `Service Health: healthy` with a PID.
4. [ ] Run **CodeAtlas: Check Service Health**. A healthy-version message
       appears (or a clear "unreachable" warning if the service is stopped).
5. [ ] Run **CodeAtlas: Start Review**. Lifecycle states appear in the Status
       view (`preparing → … → completed`) without error toasts. The Status
       view shows the base branch used (`Base Branch:` line; from
       `codeatlas.baseBranch`, default `main`).
6. [ ] Status messages update during the run; the polling stops on completion
       (Status view shows the final state, no repeated spinner text).
7. [ ] Findings appear in the Findings sidebar with severity badges, file:line
       labels, and claim descriptions.
8. [ ] Select a finding in the sidebar.
9. [ ] The editor navigates to the finding's file and line
       (**Open Finding** reveals the exact line; the line is highlighted).
10. [ ] Move the cursor onto the finding's line: the Context view fills in and
       the active-line border appears. Move the cursor away: the border clears
       and Context shows "No finding at cursor".
11. [ ] Run **CodeAtlas: Show Finding Details**. The webview renders claim,
       severity, category, location, confidence, evidence, policy decision,
       limitations — with no raw source dumps or secret material.
12. [ ] Click **Explain Finding** in the webview (or run the command). A
       readable explanation and remediation advice appears.
13. [ ] Run **CodeAtlas: Show Context**. The Context view lists changed lines,
       containing symbol, imports/references, related tests, and truncation
       status.
14. [ ] Run **CodeAtlas: Generate Draft Fix**. A draft proposal is created;
       the notification says an approval token is required. No patch is
       applied anywhere; the worktree is unchanged.
15. [ ] Confirm the original worktree is unchanged
       (`git status --porcelain` empty, `HEAD` unchanged).
16. [ ] Run **CodeAtlas: Validate Approved Fix** and leave the token empty.
       It must fail closed: a clear "Approval token is required" warning and
       **no** validation request.
17. [ ] Obtain a scoped approval token (operator CLI: `codeatlas patch
       apply-isolated` tooling / test minting) and validate with it. The
       report shows `approval_verified`, `applies_cleanly`, syntax status, and
       `tests_status` — executed only in the isolated sandbox.
18. [ ] Inspect test/validation results in the notifications and Status view:
       `not_run` stays `not_run`; no "passed" claim without execution.
19. [ ] Dismiss a finding (sidebar or webview): the label shows
       `(Dismissed)`, the decoration clears, and the webview offers
       **Restore Finding**. Restore it: the decoration returns. Dismissal is
       local only — no request is sent to the service.
20. [ ] **Stop Local Service**, then **Start Local Service** again. Health
       returns to `healthy`; the extension never reports a foreign process as
       owned.
21. [ ] Reload the IDE window. The extension reactivates safely: discovery
       runs, an owned managed service is cleaned up on shutdown (never a
       foreign one), and no stale findings remain in the sidebar.

## Base-branch configuration

- [ ] Set `codeatlas.baseBranch` to an existing local branch (e.g.
      `develop`), run **Start Review**: the Status view shows
      `Base Branch: develop` and the review diffs against that branch.
- [ ] Set it to a whitespace-only value and run again: the review uses
      `main` (fallback) and the Status view shows it.
- [ ] Changing the setting takes effect on the next Start Review without
      reloading the window.

## Resolved revision display

- [ ] After a completed review, the Status view shows `Base SHA:` and
       `Head SHA:` abbreviated to 12 characters plus a
       `Comparison: <base>...<head>` line; hovering a SHA shows the full value.
- [ ] Open the comparison item's context menu and choose **CodeAtlas: Copy
       Comparison Range**. The clipboard contains the exact full
       `<base_sha>...<head_sha>` value, not the abbreviated labels.
- [ ] With either revision unresolved, the comparison item is absent (or the
       command reports that both SHAs are required) and the clipboard is not
       changed.
- [ ] While a review is still running, the SHAs read `unresolved` (the
      service has not resolved them yet) — never a previous run's values.
- [ ] Cancelling a review clears the displayed SHAs; a stale run (expired on
      the service) removes the SHA lines entirely; a service crash resets the
      display to `unresolved`.

## Negative checks

- [ ] With no active review run, **Explain Finding** and **Generate Draft
      Fix** show "No active review run" instead of a generic error.
- [ ] A finding whose range is inverted (end before start) still renders
      decorations and is still found by cursor navigation.
- [ ] With `serviceMode: disabled`, no service process is spawned and the
      Status view explains the disabled state.
- [ ] Killing the managed service externally shows a clear crash message; the
      service is restartable without reloading the IDE.

## Notes

| Step | Result | Details |
|---|---|---|
| | | |
