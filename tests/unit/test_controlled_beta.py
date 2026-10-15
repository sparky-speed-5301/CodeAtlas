from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from codeatlas import beta
from codeatlas.cli import app


def stable_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(beta, "git_executable", lambda: "git")
    monkeypatch.setattr(
        beta.subprocess,
        "run",
        lambda command, **kwargs: SimpleNamespace(returncode=0, stdout="git version test"),
    )
    monkeypatch.setattr(beta, "openapi_document", lambda: {"openapi": "3.1.0", "paths": {"/health": {}}})


def live_config(tmp_path: Path) -> beta.ControlledBetaConfig:
    provider = tmp_path / "provider.json"
    provider.write_text(json.dumps({
        "provider_name": "live",
        "model_name": "review-model",
        "api_key_env_var": "CODEATLAS_BETA_KEY",
        "privacy_mode": "zero_retention",
    }), encoding="utf-8")
    return beta.ControlledBetaConfig.model_validate({
        "beta_mode": True,
        "provider_mode": "live_provider",
        "live_provider": {
            "provider_name": "live",
            "model_name": "review-model",
            "provider_config_path": provider.name,
            "api_key_env_var": "CODEATLAS_BETA_KEY",
            "privacy_mode": "zero_retention",
        },
        "workflow": "cli",
    })


def test_checked_in_beta_config_locks_all_automatic_authority_off() -> None:
    config = beta.load_beta_config("config/controlled-beta.json")
    assert config.beta_mode is True
    assert config.provider_mode == "offline_deterministic"
    assert config.github_mode == "review_only_dry_run"
    assert set(config.safety.model_dump().values()) == {False}
    assert config.cleanup.strategy == "owned_temporary_resources_only"
    assert config.cleanup.automatic_persisted_path_deletion is False
    assert config.cleanup.foreign_process_termination is False
    assert config.diagnostics.max_report_bytes == beta.MAX_BETA_REPORT_BYTES
    assert config.diagnostics.include_credentials is False
    assert config.diagnostics.include_provider_transcripts is False


@pytest.mark.parametrize("field", [
    "automatic_apply", "automatic_approval", "automatic_commit", "automatic_merge",
    "automatic_push", "arbitrary_commands", "dependency_installation",
    "unrestricted_network_access",
])
def test_beta_config_rejects_enabling_forbidden_capabilities(field: str) -> None:
    payload = beta.default_beta_config().model_dump(mode="json")
    payload["safety"][field] = True
    with pytest.raises(ValidationError):
        beta.ControlledBetaConfig.model_validate(payload)


@pytest.mark.parametrize("path", ["../provider.json", "/tmp/provider.json", "C:/provider.json", "folder\\..\\provider.json"])
def test_beta_config_rejects_unsafe_provider_paths(path: str) -> None:
    with pytest.raises(ValidationError, match="safe configuration-relative"):
        beta.LiveProviderSelection(model_name="model", provider_config_path=path)


def test_beta_config_rejects_unknown_fields_and_implicit_live_mode() -> None:
    payload = beta.default_beta_config().model_dump(mode="json")
    payload["telemetry"] = True
    with pytest.raises(ValidationError, match="Extra inputs"):
        beta.ControlledBetaConfig.model_validate(payload)

    payload = beta.default_beta_config().model_dump(mode="json")
    payload["provider_mode"] = "live_provider"
    with pytest.raises(ValidationError, match="explicit live_provider"):
        beta.ControlledBetaConfig.model_validate(payload)


def test_beta_config_loader_rejects_duplicate_keys_and_oversized_input(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"beta_mode":true,"beta_mode":true}', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate keys"):
        beta.load_beta_config(duplicate)

    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b" " * (beta.MAX_BETA_CONFIG_BYTES + 1))
    with pytest.raises(ValueError, match="size bound"):
        beta.load_beta_config(oversized)


def test_beta_check_is_bounded_and_does_not_require_a_service(monkeypatch: pytest.MonkeyPatch) -> None:
    stable_runtime(monkeypatch)
    report = beta.check_beta(beta.default_beta_config())
    assert report.status == "ready_with_limitations"
    assert report.beta_ready is True
    assert report.production_ready is False
    assert [check.name for check in report.checks] == [
        "python", "git", "api_contract", "configuration", "provider",
    ]
    assert report.checks[-1].code == "offline_selected"
    assert len(report.model_dump_json()) < 8192


def test_beta_start_only_returns_explicit_safe_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    stable_runtime(monkeypatch)
    plan = beta.start_beta(beta.default_beta_config())
    assert plan.status == "configured"
    assert plan.automatic_actions_enabled is False
    assert plan.production_ready is False
    rendered = "\n".join(plan.commands)
    assert "review " in rendered
    assert "github review" in rendered and "--dry-run" in rendered
    assert "beta-report" in rendered
    for forbidden in (" apply", " approve", " commit", " merge", " push", " clean", " reset"):
        assert forbidden not in rendered


def test_live_beta_requires_matching_config_credential_and_per_invocation_opt_in(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    stable_runtime(monkeypatch)
    monkeypatch.chdir(tmp_path)
    config = live_config(tmp_path)
    with pytest.raises(ValueError, match="--allow-live-provider"):
        beta.start_beta(config)
    with pytest.raises(ValueError, match="not available"):
        beta.start_beta(config, allow_live_provider=True)
    monkeypatch.setenv("CODEATLAS_BETA_KEY", "not-exported")
    plan = beta.start_beta(config, allow_live_provider=True)
    assert plan.provider_mode == "live_provider"
    assert "not-exported" not in plan.model_dump_json()


def test_loaded_live_provider_path_is_relative_to_beta_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    stable_runtime(monkeypatch)
    config_directory = tmp_path / "configuration"
    config_directory.mkdir()
    provider = config_directory / "provider.json"
    provider.write_text(json.dumps({
        "provider_name": "live",
        "model_name": "review-model",
        "api_key_env_var": "CODEATLAS_BETA_KEY",
        "privacy_mode": "zero_retention",
    }), encoding="utf-8")
    config_path = config_directory / "beta.json"
    payload = beta.default_beta_config().model_dump(mode="json")
    payload.update({
        "provider_mode": "live_provider",
        "live_provider": {
            "provider_name": "live",
            "model_name": "review-model",
            "provider_config_path": provider.name,
            "api_key_env_var": "CODEATLAS_BETA_KEY",
            "privacy_mode": "zero_retention",
        },
    })
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv("CODEATLAS_BETA_KEY", "not-exported")

    plan = beta.start_beta(beta.load_beta_config(config_path), allow_live_provider=True)
    assert plan.provider_mode == "live_provider"
    assert "not-exported" not in plan.model_dump_json()


def test_vscode_beta_plan_only_starts_explicit_loopback_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stable_runtime(monkeypatch)
    payload = beta.default_beta_config().model_dump(mode="json")
    payload["workflow"] = "vscode"
    plan = beta.start_beta(beta.ControlledBetaConfig.model_validate(payload))
    assert plan.workflow == "vscode"
    assert plan.commands[0] == "python -m codeatlas serve --host 127.0.0.1"
    assert all("--daemon" not in command for command in plan.commands)


def test_live_beta_rejects_inline_credentials_without_reflection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    stable_runtime(monkeypatch)
    monkeypatch.chdir(tmp_path)
    config = live_config(tmp_path)
    provider = tmp_path / "provider.json"
    provider.write_text(json.dumps({"api_key": "private-value"}), encoding="utf-8")
    monkeypatch.setenv("CODEATLAS_BETA_KEY", "private-value")
    report = beta.check_beta(config)
    check = next(item for item in report.checks if item.name == "provider")
    assert report.status == "blocked"
    assert report.beta_ready is False
    assert check.code == "provider_config_unavailable"
    assert "private-value" not in report.model_dump_json()
    with pytest.raises(ValueError, match="not available"):
        beta.start_beta(config, allow_live_provider=True)


def test_beta_report_is_local_by_default_bounded_and_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stable_runtime(monkeypatch)

    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("service diagnostics must be explicit")

    monkeypatch.setattr(beta, "collect_service_diagnostics", forbidden)
    report = beta.build_beta_report(beta.default_beta_config())
    serialized = report.model_dump_json()
    assert report.service_diagnostics is None
    assert report.configuration["automatic_actions_enabled"] is False
    assert report.cleanup["automatic_persisted_path_deletion"] is False
    assert report.production_ready is False
    assert len(serialized.encode()) < beta.MAX_BETA_REPORT_BYTES
    for prohibited in ("approval_token", "provider_transcript", "source_code"):
        assert prohibited not in serialized


@pytest.mark.parametrize(
    "service_url",
    [
        "https://127.0.0.1:8765",
        "http://example.com:8765",
        "http://localhost:8765/api",
        "http://user@localhost:8765",
        "http://localhost:not-a-port",
    ],
)
def test_beta_report_rejects_non_loopback_origins_before_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
    service_url: str,
) -> None:
    stable_runtime(monkeypatch)

    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("invalid service origins must not reach diagnostics")

    monkeypatch.setattr(beta, "collect_service_diagnostics", forbidden)
    with pytest.raises(ValueError, match="loopback HTTP origin"):
        beta.build_beta_report(beta.default_beta_config(), service_url=service_url)


def test_beta_export_is_atomic_bounded_and_never_overwrites(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    stable_runtime(monkeypatch)
    report = beta.build_beta_report(beta.default_beta_config())
    output = tmp_path / "beta-report.json"
    beta.write_beta_export(report, output)
    assert json.loads(output.read_text(encoding="utf-8"))["production_ready"] is False
    original = output.read_bytes()
    with pytest.raises(ValueError, match="new regular file"):
        beta.write_beta_export(report, output)
    assert output.read_bytes() == original


def test_beta_cli_commands_and_known_limitations(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    stable_runtime(monkeypatch)
    runner = CliRunner()
    checked = runner.invoke(app, ["--json", "beta-check", "--config", "config/controlled-beta.json"])
    assert checked.exit_code == 0, checked.output
    assert json.loads(checked.stdout)["production_ready"] is False

    started = runner.invoke(app, ["--json", "beta-start", "--config", "config/controlled-beta.json"])
    assert started.exit_code == 0, started.output
    start_payload = json.loads(started.stdout)
    assert start_payload["automatic_actions_enabled"] is False

    output = tmp_path / "report.json"
    reported = runner.invoke(app, [
        "--json", "beta-report", "--config", "config/controlled-beta.json",
        "--output", str(output),
    ])
    assert reported.exit_code == 0, reported.output
    payload = json.loads(reported.stdout)
    assert payload["status"] == "ready_with_limitations"
    assert payload["limitations"]
    assert output.is_file()


def test_beta_cli_rejects_invalid_config_without_reflecting_content(tmp_path: Path) -> None:
    secret = "private-provider-token"
    config = tmp_path / "invalid.json"
    config.write_text(json.dumps({"beta_mode": True, "api_key": secret}), encoding="utf-8")
    result = CliRunner().invoke(app, ["--json", "beta-check", "--config", str(config)])
    assert result.exit_code == 2
    assert secret not in result.output
    assert str(tmp_path) not in result.output
