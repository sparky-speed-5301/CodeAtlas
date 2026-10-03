"""GitHub transports: the `gh` CLI adapter (default) and a deterministic fake.

The `gh` transport performs read-only subprocess calls (`gh pr view`,
`gh pr diff`, `gh api` GET) plus a single write method (`post_comment`) that
is only ever invoked in explicit ``--post`` mode.  Authentication is gh's own
existing configuration; this module never reads, stores, or logs tokens.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any, Protocol

from .errors import (
    GitHubAuthError,
    GitHubNetworkError,
    GitHubRateLimitError,
    GitHubResponseError,
)
from .models import CheckStatus, GitHubComment, PRFile, PRMetadata

_GH_TIMEOUT_SECONDS = 60


class GitHubTransport(Protocol):
    """Typed, read-only-first interface to a GitHub provider."""

    def get_pr_metadata(self, repo: str, pr: int) -> PRMetadata: ...

    def get_pr_files(self, repo: str, pr: int) -> list[PRFile]: ...

    def get_pr_diff(self, repo: str, pr: int) -> str: ...

    def get_checks(self, repo: str, pr: int) -> CheckStatus: ...

    def list_comments(self, repo: str, pr: int) -> list[GitHubComment]: ...

    def post_comment(self, repo: str, pr: int, body: str) -> GitHubComment: ...

    def list_inline_comments(self, repo: str, pr: int) -> list[GitHubComment]: ...

    def post_inline_comment(
        self,
        repo: str,
        pr: int,
        body: str,
        *,
        commit_id: str,
        path: str,
        line: int,
        start_line: int | None = None,
    ) -> GitHubComment: ...

    def update_comment(self, repo: str, pr: int, comment_id: Any, body: str) -> GitHubComment: ...

    def list_check_runs(self, repo: str, head_sha: str) -> list[dict[str, Any]]: ...

    def create_check_run(
        self,
        repo: str,
        head_sha: str,
        *,
        name: str,
        title: str,
        summary: str,
        conclusion: str,
        external_id: str,
    ) -> dict[str, Any]: ...

    def update_check_run(
        self,
        repo: str,
        check_run_id: Any,
        *,
        title: str,
        summary: str,
        conclusion: str,
    ) -> dict[str, Any]: ...


# ---------------------------------------------------------------------------
# gh CLI parsing helpers (exposed for deterministic malformed-response tests)
# ---------------------------------------------------------------------------

def parse_pr_metadata(json_text: str) -> PRMetadata:
    """Parse `gh pr view --json ...` output; malformed input fails closed."""
    try:
        data = json.loads(json_text)
    except json.JSONDecodeError as err:
        raise GitHubResponseError(f"Malformed PR metadata response: {err}") from err
    if not isinstance(data, dict):
        raise GitHubResponseError("PR metadata response is not a JSON object")
    required = ("number", "baseRefOid", "headRefOid")
    missing = [k for k in required if not data.get(k)]
    if missing:
        raise GitHubResponseError(f"PR metadata response missing fields: {missing}")
    try:
        return PRMetadata(
            number=int(data["number"]),
            title=str(data.get("title", "")),
            state=str(data.get("state", "")),
            author=str((data.get("author") or {}).get("login", "")),
            base_ref=str(data.get("baseRefName", "")),
            head_ref=str(data.get("headRefName", "")),
            base_sha=str(data["baseRefOid"]),
            head_sha=str(data["headRefOid"]),
            changed_files_count=int(data.get("changedFiles", 0) or 0),
            html_url=str(data.get("url", "")),
        )
    except (TypeError, ValueError) as err:
        raise GitHubResponseError(f"PR metadata has unexpected field types: {err}") from err


def parse_pr_files(json_text: str) -> list[PRFile]:
    try:
        data = json.loads(json_text)
    except json.JSONDecodeError as err:
        raise GitHubResponseError(f"Malformed PR files response: {err}") from err
    files_field = data.get("files", []) if isinstance(data, dict) else data
    if not isinstance(files_field, list):
        raise GitHubResponseError("PR files response is not a list")
    files: list[PRFile] = []
    for entry in files_field:
        if not isinstance(entry, dict) or not entry.get("path"):
            raise GitHubResponseError("PR file entry missing 'path'")
        files.append(
            PRFile(
                filename=str(entry["path"]),
                status=str(entry.get("state", "modified")),
                additions=int(entry.get("additions", 0) or 0),
                deletions=int(entry.get("deletions", 0) or 0),
            )
        )
    return files


def parse_comments(json_text: str) -> list[GitHubComment]:
    try:
        data = json.loads(json_text)
    except json.JSONDecodeError as err:
        raise GitHubResponseError(f"Malformed comments response: {err}") from err
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise GitHubResponseError("Comments response is not a list")
    comments: list[GitHubComment] = []
    for entry in data:
        if not isinstance(entry, dict):
            raise GitHubResponseError("Comment entry is not an object")
        comments.append(
            GitHubComment(
                comment_id=entry.get("id", ""),
                author=str((entry.get("author") or {}).get("login", "")),
                body=str(entry.get("body", "")),
                created_at=str(entry.get("createdAt", "")),
            )
        )
    return comments


# ---------------------------------------------------------------------------
# gh CLI transport
# ---------------------------------------------------------------------------

class GhCliTransport:
    """Read-only-first GitHub transport built on the authenticated `gh` CLI.

    Never handles tokens itself: gh supplies credentials from its own
    configuration.  ``post_comment`` is a write and must only be called from
    explicit post mode.
    """

    name = "gh-cli"

    def _run(self, args: list[str]) -> str:
        gh = shutil.which("gh")
        if gh is None:
            raise GitHubAuthError(
                "GitHub CLI ('gh') is not installed or not on PATH. Install gh and run "
                "'gh auth login' to use the GitHub integration."
            )
        try:
            proc = subprocess.run(
                [gh, *args], capture_output=True, text=True, timeout=_GH_TIMEOUT_SECONDS
            )
        except subprocess.TimeoutExpired as err:
            raise GitHubNetworkError(f"gh command timed out after {_GH_TIMEOUT_SECONDS}s") from err
        except OSError as err:
            raise GitHubNetworkError(f"gh command failed to start: {err}") from err
        if proc.returncode == 4:
            raise GitHubAuthError("GitHub authentication failed (gh exit code 4)")
        if proc.returncode != 0:
            message = (proc.stderr or proc.stdout or "").strip()
            if "rate limit" in message.lower() or "API rate limit" in message:
                raise GitHubRateLimitError(f"GitHub rate limit reached: {message[:200]}")
            raise GitHubNetworkError(f"gh command failed (exit {proc.returncode}): {message[:300]}")
        return proc.stdout

    def get_pr_metadata(self, repo: str, pr: int) -> PRMetadata:
        out = self._run([
            "pr", "view", str(pr), "--repo", repo, "--json",
            "number,title,state,author,baseRefName,baseRefOid,headRefName,headRefOid,changedFiles,url",
        ])
        return parse_pr_metadata(out)

    def get_pr_files(self, repo: str, pr: int) -> list[PRFile]:
        out = self._run([
            "pr", "view", str(pr), "--repo", repo,
            "--json", "files",
        ])
        return parse_pr_files(out)

    def get_pr_diff(self, repo: str, pr: int) -> str:
        return self._run(["pr", "diff", str(pr), "--repo", repo])

    def get_checks(self, repo: str, pr: int) -> CheckStatus:
        out = self._run([
            "pr", "checks", str(pr), "--repo", repo, "--json", "state,bucket",
        ])
        try:
            entries = json.loads(out)
        except json.JSONDecodeError as err:
            raise GitHubResponseError(f"Malformed checks response: {err}") from err
        if not isinstance(entries, list):
            raise GitHubResponseError("Checks response is not a list")
        status = CheckStatus()
        buckets: dict[str, int] = {}
        for entry in entries:
            if isinstance(entry, dict):
                bucket = str(entry.get("bucket", "unknown"))
                buckets[bucket] = buckets.get(bucket, 0) + 1
        status.total = sum(buckets.values())
        status.successful = buckets.get("pass", 0)
        status.failed = buckets.get("fail", 0)
        status.pending = buckets.get("pending", 0) + buckets.get("skipping", 0)
        status.state = (
            "success" if buckets.get("fail", 0) == 0 and buckets.get("pending", 0) == 0 and status.total
            else "failure" if buckets.get("fail", 0)
            else "pending" if buckets.get("pending", 0)
            else "unknown"
        )
        return status

    def list_comments(self, repo: str, pr: int) -> list[GitHubComment]:
        out = self._run(["api", f"repos/{repo}/issues/{pr}/comments"])
        return parse_comments(out)

    def post_comment(self, repo: str, pr: int, body: str) -> GitHubComment:
        out = self._run([
            "api", f"repos/{repo}/issues/{pr}/comments", "-f", f"body={body}",
        ])
        comments = parse_comments(out)
        return comments[0] if comments else GitHubComment(body=body)

    def list_inline_comments(self, repo: str, pr: int) -> list[GitHubComment]:
        out = self._run(["api", f"repos/{repo}/pulls/{pr}/comments"])
        return parse_comments(out)

    def post_inline_comment(
        self,
        repo: str,
        pr: int,
        body: str,
        *,
        commit_id: str,
        path: str,
        line: int,
        start_line: int | None = None,
    ) -> GitHubComment:
        args = [
            "api", f"repos/{repo}/pulls/{pr}/comments",
            "-f", f"commit_id={commit_id}",
            "-f", f"path={path}",
            "-f", f"line={line}",
        ]
        if start_line is not None:
            args += ["-f", f"start_line={start_line}", "-f", "side=RIGHT", "-f", "start_side=RIGHT"]
        out = self._run(args)
        comments = parse_comments(out)
        return comments[0] if comments else GitHubComment(body=body)

    def update_comment(self, repo: str, pr: int, comment_id: Any, body: str) -> GitHubComment:
        out = self._run([
            "api", f"repos/{repo}/issues/comments/{comment_id}", "-X", "PATCH", "-f", f"body={body}",
        ])
        comments = parse_comments(out)
        return comments[0] if comments else GitHubComment(comment_id=comment_id, body=body)

    def list_check_runs(self, repo: str, head_sha: str) -> list[dict[str, Any]]:
        out = self._run(["api", f"repos/{repo}/commits/{head_sha}/check-runs"])
        try:
            data = json.loads(out)
        except json.JSONDecodeError as err:
            raise GitHubResponseError(f"Malformed check-runs response: {err}") from err
        if not isinstance(data, dict) or not isinstance(data.get("check_runs", []), list):
            raise GitHubResponseError("Check-runs response has unexpected shape")
        return list(data.get("check_runs", []))

    def create_check_run(
        self,
        repo: str,
        head_sha: str,
        *,
        name: str,
        title: str,
        summary: str,
        conclusion: str,
        external_id: str,
    ) -> dict[str, Any]:
        out = self._run([
            "api", f"repos/{repo}/check-runs",
            "-f", f"name={name}",
            "-f", f"head_sha={head_sha}",
            "-f", "status=completed",
            "-f", f"conclusion={conclusion}",
            "-F", f"output[title]={title}",
            "-F", f"output[summary]={summary}",
            "-f", f"external_id={external_id}",
        ])
        try:
            data = json.loads(out)
        except json.JSONDecodeError as err:
            raise GitHubResponseError(f"Malformed check-run create response: {err}") from err
        if not isinstance(data, dict) or not data.get("id"):
            raise GitHubResponseError("Check-run create response missing id")
        return data

    def update_check_run(
        self,
        repo: str,
        check_run_id: Any,
        *,
        title: str,
        summary: str,
        conclusion: str,
    ) -> dict[str, Any]:
        out = self._run([
            "api", f"repos/{repo}/check-runs/{check_run_id}",
            "-X", "PATCH",
            "-f", "status=completed",
            "-f", f"conclusion={conclusion}",
            "-F", f"output[title]={title}",
            "-F", f"output[summary]={summary}",
        ])
        try:
            data = json.loads(out)
        except json.JSONDecodeError as err:
            raise GitHubResponseError(f"Malformed check-run update response: {err}") from err
        if not isinstance(data, dict) or not data.get("id"):
            raise GitHubResponseError("Check-run update response missing id")
        return data


# ---------------------------------------------------------------------------
# Fake transport for tests and evaluation (no network, no real writes)
# ---------------------------------------------------------------------------

class FakeGitHubTransport:
    """Deterministic in-memory GitHub transport.

    Records every call (method and safe arguments) and counts write calls so
    tests can assert ``write_calls == 0`` in dry-run mode.
    """

    name = "fake"

    def __init__(
        self,
        metadata: dict[str, Any] | PRMetadata,
        files: list[dict[str, Any]] | list[PRFile] | None = None,
        diff_text: str = "",
        comments: list[dict[str, Any]] | list[GitHubComment] | None = None,
        checks: CheckStatus | None = None,
        *,
        metadata_error: Exception | None = None,
        metadata_error_on_second_fetch: Exception | None = None,
        diff_error: Exception | None = None,
        comments_error: Exception | None = None,
        post_error: Exception | None = None,
        head_sha_on_refetch: str | None = None,
        head_sha_on_write_check: str | None = None,
        head_sha_after_writes: int | None = None,
        inline_comments: list[dict[str, Any]] | list[GitHubComment] | None = None,
        check_runs: list[dict[str, Any]] | None = None,
        check_run_error: Exception | None = None,
    ) -> None:
        self._metadata = metadata if isinstance(metadata, PRMetadata) else PRMetadata(**metadata)
        self._files = [
            f if isinstance(f, PRFile) else PRFile(**f) for f in (files or [])
        ]
        self._diff_text = diff_text
        self._comments = [
            c if isinstance(c, GitHubComment) else GitHubComment(**c) for c in (comments or [])
        ]
        self._checks = checks or CheckStatus()
        self.metadata_error = metadata_error
        self.metadata_error_on_second_fetch = metadata_error_on_second_fetch
        self.diff_error = diff_error
        self.comments_error = comments_error
        self.post_error = post_error
        self.head_sha_on_refetch = head_sha_on_refetch
        self.head_sha_on_write_check = head_sha_on_write_check
        self.head_sha_after_writes = head_sha_after_writes
        self.check_run_error = check_run_error
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self._metadata_fetches = 0
        self._next_comment_id = 1000
        self.posted_bodies: list[str] = []
        self.posted_inline: list[dict[str, Any]] = []
        self._issue_comments = self._comments
        self.check_runs: list[dict[str, Any]] = list(check_runs or [])
        self._next_check_id = 5000
        self._updated_comment_bodies: dict[Any, str] = {}
        self._inline_comments = [
            c if isinstance(c, GitHubComment) else GitHubComment(**c) for c in (inline_comments or [])
        ]

    @property
    def write_calls(self) -> int:
        return sum(
            1 for method, _ in self.calls
            if method in {"post_comment", "post_inline_comment", "update_comment", "create_check_run", "update_check_run"}
        )

    def get_pr_metadata(self, repo: str, pr: int) -> PRMetadata:
        self.calls.append(("get_pr_metadata", (repo, pr)))
        self._metadata_fetches += 1
        if self.head_sha_after_writes is not None and self.write_calls >= self.head_sha_after_writes:
            return self._metadata.model_copy(update={"head_sha": "d" * 40})
        if self._metadata_fetches >= 2 and self.metadata_error_on_second_fetch is not None:
            raise self.metadata_error_on_second_fetch
        if self._metadata_fetches >= 2 and self.head_sha_on_refetch is not None:
            return self._metadata.model_copy(update={"head_sha": self.head_sha_on_refetch})
        if self._metadata_fetches >= 3 and self.head_sha_on_write_check is not None:
            return self._metadata.model_copy(update={"head_sha": self.head_sha_on_write_check})
        if self._metadata_fetches == 1 and self.metadata_error is not None:
            raise self.metadata_error
        return self._metadata

    def get_pr_files(self, repo: str, pr: int) -> list[PRFile]:
        self.calls.append(("get_pr_files", (repo, pr)))
        return list(self._files)

    def get_pr_diff(self, repo: str, pr: int) -> str:
        self.calls.append(("get_pr_diff", (repo, pr)))
        if self.diff_error is not None:
            raise self.diff_error
        return self._diff_text

    def get_checks(self, repo: str, pr: int) -> CheckStatus:
        self.calls.append(("get_checks", (repo, pr)))
        return self._checks

    def list_comments(self, repo: str, pr: int) -> list[GitHubComment]:
        self.calls.append(("list_comments", (repo, pr)))
        if self.comments_error is not None:
            raise self.comments_error
        return list(self._comments)

    def post_comment(self, repo: str, pr: int, body: str) -> GitHubComment:
        self.calls.append(("post_comment", (repo, pr)))
        self.posted_bodies.append(body)
        if self.post_error is not None:
            raise self.post_error
        comment = GitHubComment(comment_id=self._next_comment_id, author="codeatlas", body=body)
        self._next_comment_id += 1
        self._comments.append(comment)
        return comment

    def list_inline_comments(self, repo: str, pr: int) -> list[GitHubComment]:
        self.calls.append(("list_inline_comments", (repo, pr)))
        if self.comments_error is not None:
            raise self.comments_error
        return list(self._inline_comments)

    def post_inline_comment(
        self,
        repo: str,
        pr: int,
        body: str,
        *,
        commit_id: str,
        path: str,
        line: int,
        start_line: int | None = None,
    ) -> GitHubComment:
        self.calls.append(("post_inline_comment", (repo, pr, path, line, start_line)))
        self.posted_bodies.append(body)
        self.posted_inline.append({
            "body": body, "commit_id": commit_id, "path": path, "line": line, "start_line": start_line,
        })
        if self.post_error is not None:
            raise self.post_error
        comment = GitHubComment(comment_id=self._next_comment_id, author="codeatlas", body=body)
        self._next_comment_id += 1
        self._inline_comments.append(comment)
        return comment

    def update_comment(self, repo: str, pr: int, comment_id: Any, body: str) -> GitHubComment:
        self.calls.append(("update_comment", (repo, pr, comment_id)))
        self.posted_bodies.append(body)
        self._updated_comment_bodies[comment_id] = body
        if self.post_error is not None:
            raise self.post_error
        return GitHubComment(comment_id=comment_id, author="codeatlas", body=body)

    def list_check_runs(self, repo: str, head_sha: str) -> list[dict[str, Any]]:
        self.calls.append(("list_check_runs", (repo, head_sha)))
        if self.check_run_error is not None:
            raise self.check_run_error
        return [c for c in self.check_runs if c.get("head_sha") == head_sha]

    def create_check_run(
        self,
        repo: str,
        head_sha: str,
        *,
        name: str,
        title: str,
        summary: str,
        conclusion: str,
        external_id: str,
    ) -> dict[str, Any]:
        self.calls.append(("create_check_run", (repo, head_sha, name, conclusion)))
        if self.check_run_error is not None:
            raise self.check_run_error
        self._next_check_id += 1
        check = {
            "id": self._next_check_id,
            "name": name,
            "head_sha": head_sha,
            "status": "completed",
            "conclusion": conclusion,
            "external_id": external_id,
            "title": title,
            "summary": summary,
        }
        self.check_runs.append(check)
        return dict(check)

    def update_check_run(
        self,
        repo: str,
        check_run_id: Any,
        *,
        title: str,
        summary: str,
        conclusion: str,
    ) -> dict[str, Any]:
        self.calls.append(("update_check_run", (repo, check_run_id, conclusion)))
        if self.check_run_error is not None:
            raise self.check_run_error
        for check in self.check_runs:
            if check.get("id") == check_run_id:
                check.update({"title": title, "summary": summary, "conclusion": conclusion})
                return dict(check)
        updated = {
            "id": check_run_id, "name": "CodeAtlas review", "head_sha": "",
            "status": "completed", "conclusion": conclusion,
            "title": title, "summary": summary, "external_id": "",
        }
        self.check_runs.append(updated)
        return dict(updated)


__all__ = [
    "GitHubTransport",
    "GhCliTransport",
    "FakeGitHubTransport",
    "parse_pr_metadata",
    "parse_pr_files",
    "parse_comments",
]
