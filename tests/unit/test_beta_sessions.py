"""Contract tests for bounded controlled-beta session metadata."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from codeatlas.beta import (
    ControlledBetaConfig,
    LiveProviderSelection,
    default_beta_config,
)
from codeatlas.beta_session import (
    MAX_SESSION_EVENTS,
    BetaSession,
    BetaSessionEvent,
    BetaSessionRunner,
    BetaSessionStore,
)
from codeatlas.cli import app

SHA_A = "a" * 40
SHA_B = "b" * 40
HASH_A = "a" * 64
HASH_B = "b" * 64


def make_runner(tmp_path) -> BetaSessionRunner:
    return BetaSessionRunner.create(
        repository=tmp_path,
        case_id="case-1",
        language="python",
        base_sha=SHA_A,
        head_sha=SHA_B,
        config=default_beta_config(),
    )


def test_beta_session_commands_are_explicitly_exposed() -> None:
    result = CliRunner().invoke(app, ["beta-session", "--help"])

    assert result.exit_code == 0
    for command in ("create", "review", "github-dry-run", "propose", "approve-validation", "validate", "targeted-tests", "apply", "revert", "cancel", "export"):
        assert command in result.stdout


def test_session_contains_required_safe_metadata(tmp_path) -> None:
    runner = make_runner(tmp_path)

    session = runner.session

    assert session.session_id.startswith("beta-session-")
    assert session.repository_identity_hash.startswith("repo-")
    assert session.case_id == "case-1"
    assert session.language == "python"
    assert session.base_sha == SHA_A
    assert session.head_sha == SHA_B
    assert session.provider_mode == "offline"
    assert session.lifecycle_status == "active"
    assert session.cleanup_status == "not_run"
    assert session.failure_code is None
    assert session.events_total == 1


def test_offline_review_projects_only_bounded_metadata(tmp_path) -> None:
    def git(*args: str) -> str:
        result = subprocess.run(
            ["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True,
        )
        return result.stdout.strip()

    git("init")
    git("config", "user.name", "Session Test")
    git("config", "user.email", "session@example.com")
    source = "def add(a, b):\n    return a + b\n"
    (tmp_path / "calc.py").write_text(source, encoding="utf-8")
    git("add", "calc.py")
    git("commit", "-m", "fixture")
    sha = git("rev-parse", "HEAD")
    runner = BetaSessionRunner.create(
        repository=tmp_path, case_id="offline-case", language="python",
        base_sha=sha, head_sha=sha, config=default_beta_config(),
    )

    runner.run_offline_review(repository=tmp_path)

    assert runner.session.events[-1].stage == "offline_review"
    assert runner.session.events[-1].result == "offline"
    serialized = runner.session.model_dump_json()
    assert str(tmp_path) not in serialized
    assert source.strip() not in serialized


def test_live_review_requires_authorization_before_provider_work(tmp_path, monkeypatch) -> None:
    config = ControlledBetaConfig(
        provider_mode="live_provider",
        live_provider=LiveProviderSelection(
            model_name="safe-model", provider_config_path="provider.json",
        ),
    )
    runner = BetaSessionRunner.create(
        repository=tmp_path, case_id="live-case", language="python",
        base_sha=SHA_A, head_sha=SHA_B, config=config,
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("provider work must not start")

    monkeypatch.setattr("codeatlas.orchestrator.review.run_review", forbidden)
    runner.run_live_review(repository=tmp_path, config=config, allow_live_provider=False)

    assert runner.session.events[-1].result == "blocked"
    assert runner.session.failure_code == "live_provider_not_authorized"


def test_github_dry_run_requires_zero_write_calls(tmp_path, monkeypatch) -> None:
    runner = make_runner(tmp_path)

    monkeypatch.setattr(
        "codeatlas.github.run_github_pr_review",
        lambda *args, **kwargs: SimpleNamespace(mode="dry_run", write_calls=0, errors=[]),
    )
    runner.run_github_dry_run(
        repository=tmp_path, github_repository="owner/repository", pull_request=1,
    )

    assert runner.session.github_dry_run_result == "passed"
    assert runner.session.events[-1].stage == "github_dry_run"


@pytest.mark.parametrize(
    "state", ["offline", "live", "fallback", "unavailable", "blocked", "not_run"],
)
def test_session_keeps_execution_states_distinct(state) -> None:
    event = BetaSessionEvent(
        sequence=1, stage="created", result=state, at=datetime.now(UTC),
        failure_code="service_unavailable" if state in {"unavailable", "blocked"} else None,
    )

    assert event.result == state


def test_approval_and_validation_are_distinct_and_apply_remains_gated(tmp_path) -> None:
    runner = make_runner(tmp_path)
    runner.record_fix_proposal(available=True, patch_hash=HASH_A)

    runner.record_validation(passed=True)
    assert runner.session.validation_result == "blocked"
    assert runner.session.apply_status == "not_run"

    runner.record_validation_approval(approved=True)
    runner.record_validation(passed=True)
    assert runner.session.validation_result == "passed"

    runner.record_apply(status="applied", explicitly_approved=False)
    assert runner.session.apply_status == "blocked"
    runner.record_apply(status="applied", explicitly_approved=True)
    assert runner.session.apply_status == "applied"

    runner.record_revert(status="reverted")
    assert runner.session.revert_status == "reverted"


def test_session_history_is_bounded_and_export_contains_no_secret_fields(tmp_path) -> None:
    runner = make_runner(tmp_path)
    for _ in range(MAX_SESSION_EVENTS + 3):
        runner.retry()

    payload = runner.session.model_dump(mode="json")
    encoded = json.dumps(payload).lower()
    assert len(runner.session.events) == MAX_SESSION_EVENTS
    assert runner.session.events_total > MAX_SESSION_EVENTS
    assert runner.session.events_truncated is True
    assert "approval_token" not in encoded
    assert "credential" not in encoded
    assert "prompt" not in encoded
    assert "transcript" not in encoded
    assert "source" not in encoded


def test_store_is_explicit_bounded_and_revision_monotonic(tmp_path) -> None:
    runner = make_runner(tmp_path)
    store = BetaSessionStore(tmp_path / "session.json")
    store.create(runner.session)

    runner.retry()
    store.save(runner.session)
    restored = store.load()
    assert restored.session_id == runner.session.session_id
    assert restored.revision == runner.session.revision

    with pytest.raises(ValueError, match="monotonically"):
        store.save(restored)


def test_terminal_session_rejects_another_stage(tmp_path) -> None:
    runner = make_runner(tmp_path)
    runner.cancel()

    with pytest.raises(ValueError, match="terminal"):
        runner.run_offline_review(repository=tmp_path)


def test_cli_apply_without_explicit_flag_never_contacts_service_or_persists_token(tmp_path, monkeypatch) -> None:
    runner = make_runner(tmp_path)
    runner.record_fix_proposal(available=True, patch_hash=HASH_A)
    runner.record_validation_approval(approved=True)
    runner.record_validation(passed=True)
    path = tmp_path / "session.json"
    BetaSessionStore(path).create(runner.session)

    class ForbiddenClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("service must not be contacted")

    monkeypatch.setattr("codeatlas.client.CodeAtlasClient", ForbiddenClient)
    token = "private-apply-token"
    result = CliRunner().invoke(app, [
        "beta-session", "apply", "--session", str(path), "--run-id", "run-1",
        "--finding-id", "CA-A", "--proposal-id", "proposal-1",
        "--confirmed-patch-hash", HASH_A, "--approval-token", token,
    ])

    assert result.exit_code == 1
    persisted = path.read_text(encoding="utf-8")
    assert token not in persisted
    assert BetaSessionStore(path).load().apply_status == "blocked"


@pytest.mark.parametrize(("command", "expected_tests"), [
    ("validate", False),
    ("targeted-tests", True),
])
def test_cli_validation_and_targeted_tests_are_separately_explicit(
    tmp_path, monkeypatch, command, expected_tests,
) -> None:
    runner = make_runner(tmp_path)
    runner.record_fix_proposal(available=True, patch_hash=HASH_A)
    runner.record_validation_approval(approved=True)
    path = tmp_path / f"{command}.json"
    BetaSessionStore(path).create(runner.session)
    requests = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def get_review(self, run_id):
            return SimpleNamespace(
                run_id=run_id, repository=str(tmp_path), base_commit=SHA_A, head_commit=SHA_B,
            )

        def get_fix(self, run_id, finding_id, proposal_id):
            return SimpleNamespace(
                run_id=run_id, finding_id=finding_id, proposal_id=proposal_id,
                repository=str(tmp_path), base_commit=SHA_A, head_commit=SHA_B,
                patch_hash=HASH_A,
            )

        def validate_fix(self, request):
            requests.append(request)
            return SimpleNamespace(
                valid=True, approval_verified=True, cleanup_status="completed",
                tests_status="passed" if request.run_tests else "not_run",
            )

    monkeypatch.setattr("codeatlas.client.CodeAtlasClient", FakeClient)
    token = "private-validation-token"
    result = CliRunner().invoke(app, [
        "beta-session", command, "--session", str(path), "--run-id", "run-1",
        "--finding-id", "CA-A", "--proposal-id", "proposal-1",
        "--approval-token", token,
    ])

    assert result.exit_code == 0, result.output
    assert requests[0].run_tests is expected_tests
    assert requests[0].run_full_suite is False
    persisted = path.read_text(encoding="utf-8")
    assert token not in persisted
    session = BetaSessionStore(path).load()
    assert session.validation_result == "passed"
    assert session.test_result == ("passed" if expected_tests else "not_run")


def test_cli_proposal_binds_verified_service_scope_and_persists_only_hashes(tmp_path, monkeypatch) -> None:
    runner = make_runner(tmp_path)
    path = tmp_path / "session.json"
    BetaSessionStore(path).create(runner.session)

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def get_review(self, run_id):
            return SimpleNamespace(
                run_id=run_id, repository=str(tmp_path), base_commit=SHA_A, head_commit=SHA_B,
            )

        def generate_fix(self, run_id, finding_id):
            return SimpleNamespace(
                run_id=run_id,
                finding_id=finding_id,
                repository=str(tmp_path),
                base_commit=SHA_A,
                head_commit=SHA_B,
                generation_status="draft_ready",
                patch_hash=HASH_B,
                proposal_id="proposal-1",
            )

    monkeypatch.setattr("codeatlas.client.CodeAtlasClient", FakeClient)
    result = CliRunner().invoke(app, [
        "beta-session", "propose", "--session", str(path), "--run-id", "run-1",
        "--finding-id", "CA-A",
    ])

    assert result.exit_code == 0, result.output
    session = BetaSessionStore(path).load()
    assert session.patch_hash == HASH_B
    assert session.context_hash is None
    persisted = path.read_text(encoding="utf-8")
    assert "run-1" not in persisted
    assert "CA-A" not in persisted
    assert "proposal-1" not in persisted


def test_event_and_session_contracts_reject_inconsistent_values(tmp_path) -> None:
    runner = make_runner(tmp_path)
    event = BetaSessionEvent(sequence=1, stage="created", result="offline", at=datetime.now(UTC))
    with pytest.raises(ValidationError, match="finding count"):
        BetaSession(
            session_id=runner.session.session_id,
            repository_identity_hash=runner.session.repository_identity_hash,
            case_id="case-1",
            language="python",
            base_sha=SHA_A,
            head_sha=SHA_B,
            provider_mode="offline",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            events=(event,),
            events_total=1,
            finding_count=1,
        )
    with pytest.raises(ValidationError, match="require a stable failure"):
        BetaSessionEvent(sequence=2, stage="validation", result="unavailable", at=datetime.now(UTC))
