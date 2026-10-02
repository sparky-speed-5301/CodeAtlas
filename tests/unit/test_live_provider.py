"""Unit tests for Phase 7A live reviewer provider: config, prompt, transport, gates."""

from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path

import pytest

from codeatlas.git.models import ChangeStatus, Diff, FileChange, LineRange
from codeatlas.providers import (
    FakeTransport,
    LiveReviewer,
    ProviderAuthError,
    ProviderConfig,
    ProviderConfigError,
    ProviderRateLimitError,
    ProviderResponseSizeError,
    ProviderTimeoutError,
    ProviderTransportError,
    load_provider_config,
)
from codeatlas.providers.prompt import build_review_prompt, redact_repository_identifier
from codeatlas.providers.transport import HttpTransport
from codeatlas.review.packet import assemble_review_packet
from codeatlas.review.validator import validate_provider_output


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_diff(path: str = "src/app.py", start: int = 1, count: int = 4) -> Diff:
    change = FileChange(
        path=path,
        old_path=None,
        status=ChangeStatus.MODIFIED,
        old_ranges=(LineRange(start=start, count=count),),
        new_ranges=(LineRange(start=start, count=count),),
    )
    return Diff("base", "head", files=(change,))


def _make_packet(tmp_path: Path, content: str = "def f():\n    return 1\n", **kwargs):
    (tmp_path / "src").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "app.py").write_text(content, encoding="utf-8")
    return assemble_review_packet(tmp_path, _make_diff(), base_commit="b", head_commit="h", **kwargs)


def _valid_finding(**overrides):
    finding = {
        "id": "CA-REV-100",
        "file": "src/app.py",
        "start_line": 1,
        "end_line": 2,
        "severity": "low",
        "category": "CODE_QUALITY",
        "claim": "Return statement could use a named constant",
        "impact": "Minor maintainability concern",
        "evidence": ["return 1 on a changed line"],
        "evidence_strength": "supported",
        "confidence": 0.9,
        "limitations": [],
        "status": "detected",
        "fixability": "review_required",
        "provenance": {"origin": "reviewer"},
    }
    finding.update(overrides)
    return finding


def _live(tmp_path: Path, response=None, error=None, **config_kwargs) -> tuple[LiveReviewer, FakeTransport]:
    config = ProviderConfig(enabled=True, **config_kwargs)
    transport = FakeTransport(simulated_response=response, simulated_error=error)
    return LiveReviewer(config=config, transport=transport), transport


# ---------------------------------------------------------------------------
# Provider configuration
# ---------------------------------------------------------------------------

class TestProviderConfig:
    def test_defaults_are_disabled_mock_stays_default(self):
        config = ProviderConfig()
        assert config.enabled is False
        assert config.dry_run is False

    def test_missing_credentials_raise_clear_error(self, monkeypatch):
        monkeypatch.delenv("CODEATLAS_API_KEY", raising=False)
        config = ProviderConfig(api_key_env_var="CODEATLAS_API_KEY")
        with pytest.raises(ProviderConfigError, match="CODEATLAS_API_KEY"):
            config.resolve_api_key()

    def test_dry_run_does_not_require_credentials(self, monkeypatch):
        monkeypatch.delenv("CODEATLAS_API_KEY", raising=False)
        config = ProviderConfig(dry_run=True)
        assert config.resolve_api_key() == "dry-run-sentinel"

    def test_validate_rejects_bad_values(self):
        from pydantic import ValidationError

        # Field-constraint violations are rejected at construction time.
        for bad in (
            {"timeout_seconds": 0},
            {"timeout_seconds": 601},
            {"max_retries": -1},
            {"max_retries": 11},
            {"max_output_tokens": 0},
            {"request_budget": 0},
            {"temperature": 3.0},
        ):
            with pytest.raises(ValidationError):
                ProviderConfig(**bad)
        # Semantic violations are rejected by explicit validation.
        with pytest.raises(ProviderConfigError):
            ProviderConfig(privacy_mode="store_everything").validate_config()
        with pytest.raises(ProviderConfigError):
            ProviderConfig(api_base_url="ftp://evil.example").validate_config()
        with pytest.raises(ProviderConfigError):
            ProviderConfig(model_name="bad model name").validate_config()
        with pytest.raises(ProviderConfigError):
            ProviderConfig(api_key_env_var="has space").validate_config()

    def test_safe_dict_contains_no_credentials(self):
        config = ProviderConfig()
        safe = config.to_safe_dict()
        serialized = json.dumps(safe)
        assert "api_key" not in serialized.replace("api_key_env_var", "")
        assert config.api_key_env_var in serialized  # only the env var NAME

    def test_load_provider_config_from_file(self, tmp_path):
        cfg_file = tmp_path / "provider.json"
        cfg_file.write_text(json.dumps({
            "model_name": "test-model",
            "timeout_seconds": 12.5,
            "request_budget": 2,
        }), encoding="utf-8")
        config = load_provider_config(cfg_file)
        assert config.model_name == "test-model"
        assert config.timeout_seconds == 12.5
        assert config.request_budget == 2
        assert config.enabled is True

    def test_load_rejects_inline_api_key_in_file(self, tmp_path):
        cfg_file = tmp_path / "provider.json"
        cfg_file.write_text(json.dumps({"api_key": "sk-leak"}), encoding="utf-8")
        with pytest.raises(ProviderConfigError, match="credential"):
            load_provider_config(cfg_file)

    def test_load_rejects_inline_api_key_in_overrides(self):
        with pytest.raises(ProviderConfigError, match="credential"):
            load_provider_config(None, overrides={"api_key": "sk-leak"})

    def test_load_rejects_unknown_keys(self, tmp_path):
        cfg_file = tmp_path / "provider.json"
        cfg_file.write_text(json.dumps({"model": "x"}), encoding="utf-8")
        with pytest.raises(ProviderConfigError, match="Unknown provider config keys"):
            load_provider_config(cfg_file)

    def test_overrides_win_over_file(self, tmp_path):
        cfg_file = tmp_path / "provider.json"
        cfg_file.write_text(json.dumps({"model_name": "file-model"}), encoding="utf-8")
        config = load_provider_config(cfg_file, overrides={"model_name": "cli-model"})
        assert config.model_name == "cli-model"

    def test_missing_config_file_fails_closed(self, tmp_path):
        with pytest.raises(ProviderConfigError, match="not found"):
            load_provider_config(tmp_path / "absent.json")


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

class TestPrompt:
    def test_prompt_is_deterministic(self, tmp_path):
        packet = _make_packet(tmp_path)
        assert build_review_prompt(packet) == build_review_prompt(packet)

    def test_untrusted_delimiters_present_and_content_confined(self, tmp_path):
        packet = _make_packet(tmp_path, content="def f():\n    return 'UNIQUE_REPO_MARKER'\n")
        prompt = build_review_prompt(packet)
        system_msg = prompt["messages"][0]["content"]
        user_msg = prompt["messages"][1]["content"]
        assert "<UNTRUSTED_REPOSITORY_CONTEXT>" in user_msg
        assert "</UNTRUSTED_REPOSITORY_CONTEXT>" in user_msg
        # Repository content must never appear in the system-instruction section.
        assert "UNIQUE_REPO_MARKER" not in system_msg
        assert "src/app.py" not in system_msg
        assert "UNIQUE_REPO_MARKER" in user_msg

    def test_absolute_repository_path_is_redacted(self, tmp_path):
        packet = _make_packet(tmp_path)
        packet = packet.model_copy(update={"repository": str(tmp_path)})
        prompt = build_review_prompt(packet)
        serialized = json.dumps(prompt)
        assert str(tmp_path) not in serialized
        assert tmp_path.name in serialized

    def test_redact_repository_identifier(self):
        assert redact_repository_identifier("D:\\code\\myrepo") == "myrepo"
        assert redact_repository_identifier("/home/dev/myrepo") == "myrepo"
        assert redact_repository_identifier("myrepo") == "myrepo"

    def test_system_instruction_contains_safety_rules(self):
        from codeatlas.providers.prompt import SYSTEM_INSTRUCTION

        for phrase in (
            "read-only",
            "UNTRUSTED",
            "Do not generate patches",
            "detected",
            "review_only",
            "abstentions",
            "no tools",
        ):
            assert phrase.lower() in SYSTEM_INSTRUCTION.lower()


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

class _FakeHttpResp:
    def __init__(self, payload: bytes, status: int = 200, request_id: str = "req-1"):
        self._payload = payload
        self.status = status
        self.headers = {"x-request-id": request_id}

    def read(self, amount: int = -1) -> bytes:
        return self._payload[:amount] if amount > 0 else self._payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class TestHttpTransport:
    FAKE_KEY = "sk-test-not-a-real-key-123456"

    def _config(self, **kwargs) -> ProviderConfig:
        defaults = {"enabled": True, "max_retries": 0, "timeout_seconds": 1.0}
        defaults.update(kwargs)
        return ProviderConfig(**defaults)

    @pytest.fixture(autouse=True)
    def _fake_credentials(self, monkeypatch):
        monkeypatch.setenv("CODEATLAS_API_KEY", self.FAKE_KEY)

    def test_dry_run_makes_no_request(self):
        transport = HttpTransport()
        config = ProviderConfig(dry_run=True, enabled=True)
        response = transport.send({"messages": []}, config)
        assert "Dry run" in response.raw_content

    def test_success_parses_usage_and_content(self, monkeypatch):
        body = json.dumps({
            "choices": [{"message": {"content": json.dumps({"findings": [], "summary": "ok"})}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7},
        }).encode()
        captured = {}

        def fake_urlopen(req, timeout):
            captured["authorization"] = req.headers.get("Authorization")
            return _FakeHttpResp(body)

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        transport = HttpTransport()
        response = transport.send({"messages": []}, self._config())
        assert response.input_tokens == 11
        assert response.output_tokens == 7
        assert "findings" in response.raw_content
        assert response.request_id == "req-1"
        # The key material must never be echoed into the normalized response.
        assert self.FAKE_KEY not in response.model_dump_json()

    def test_auth_error_is_immediate(self, monkeypatch):
        def raise_401(req, timeout):
            raise urllib.error.HTTPError("http://x", 401, "Unauthorized", {}, io.BytesIO(b""))

        monkeypatch.setattr("urllib.request.urlopen", raise_401)
        with pytest.raises(ProviderAuthError):
            HttpTransport().send({"messages": []}, self._config())

    def test_rate_limit_exhausts_retries_then_fails(self, monkeypatch):
        calls = {"n": 0}

        def raise_429(req, timeout):
            calls["n"] += 1
            raise urllib.error.HTTPError("http://x", 429, "Too Many Requests", {}, io.BytesIO(b""))

        monkeypatch.setattr("urllib.request.urlopen", raise_429)
        with pytest.raises(ProviderRateLimitError):
            HttpTransport().send({"messages": []}, self._config(max_retries=1))
        assert calls["n"] == 2  # initial + one bounded retry

    def test_timeout_maps_to_typed_error(self, monkeypatch):
        def raise_timeout(req, timeout):
            raise urllib.error.URLError("timed out")

        monkeypatch.setattr("urllib.request.urlopen", raise_timeout)
        with pytest.raises(ProviderTimeoutError):
            HttpTransport().send({"messages": []}, self._config(max_retries=0))

    def test_connection_error_maps_to_transport_error(self, monkeypatch):
        monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout: (_ for _ in ()).throw(urllib.error.URLError("refused")))
        with pytest.raises(ProviderTransportError):
            HttpTransport().send({"messages": []}, self._config(max_retries=0))

    def test_response_size_limit(self, monkeypatch):
        big = b"x" * 64
        monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout: _FakeHttpResp(big))
        with pytest.raises(ProviderResponseSizeError):
            HttpTransport().send({"messages": []}, self._config(max_response_bytes=16))


# ---------------------------------------------------------------------------
# LiveReviewer pre-provider gates
# ---------------------------------------------------------------------------

class TestLivePreflight:
    def test_disabled_provider_abstains_without_request(self, tmp_path):
        config = ProviderConfig(enabled=False)
        transport = FakeTransport()
        reviewer = LiveReviewer(config=config, transport=transport)
        result = reviewer.review(_make_packet(tmp_path))
        assert result.findings == []
        assert result.validation_status == "abstained"
        assert transport.captured_payloads == []

    def test_dry_run_never_calls_transport(self, tmp_path):
        config = ProviderConfig(enabled=True, dry_run=True)
        transport = FakeTransport(simulated_response={"findings": [], "summary": "should not happen"})
        reviewer = LiveReviewer(config=config, transport=transport)
        result = reviewer.review(_make_packet(tmp_path))
        assert result.validation_status == "not_requested"
        assert result.usage_metadata.get("dry_run") is True
        assert transport.captured_payloads == []

    def test_failed_redaction_audit_blocks_request(self, tmp_path, monkeypatch):
        packet = _make_packet(tmp_path)
        from codeatlas.review.packet import RedactionAudit

        broken = packet.model_copy(deep=True)
        broken.redaction_status = RedactionAudit(redacted=False, raw_value_matches=1, failed_checks=["unredacted_pattern:x"])
        transport = FakeTransport()
        reviewer = LiveReviewer(config=ProviderConfig(enabled=True), transport=transport)
        result = reviewer.review(broken)
        assert result.validation_status == "abstained"
        assert any("redaction" in lim.lower() for lim in result.limitations)
        assert transport.captured_payloads == []

    def test_oversized_packet_blocks_request(self, tmp_path):
        packet = _make_packet(tmp_path)
        config = ProviderConfig(enabled=True, max_packet_bytes=10)
        transport = FakeTransport()
        reviewer = LiveReviewer(config=config, transport=transport)
        result = reviewer.review(packet)
        assert result.validation_status == "abstained"
        assert any("exceeds hard limit" in lim for lim in result.limitations)
        assert transport.captured_payloads == []

    def test_truncated_changed_code_abstains(self, tmp_path):
        packet = _make_packet(tmp_path)
        truncated = packet.model_copy(deep=True)
        truncated.truncated = True
        item = truncated.context_candidates[0].model_copy(deep=True)
        item.truncation_status = "truncated"
        truncated.context_candidates = [item]
        transport = FakeTransport()
        reviewer = LiveReviewer(config=ProviderConfig(enabled=True), transport=transport)
        result = reviewer.review(truncated)
        assert result.validation_status == "abstained"
        assert transport.captured_payloads == []

    def test_request_budget_enforced(self, tmp_path):
        reviewer, transport = _live(
            tmp_path,
            response={"findings": [], "summary": "ok"},
            request_budget=1,
        )
        packet = _make_packet(tmp_path)
        first = reviewer.review(packet)
        assert first.validation_status in {"accepted", "sanitized"}
        second = reviewer.review(packet)
        assert second.validation_status == "abstained"
        assert any("budget" in lim.lower() for lim in second.limitations)
        assert len(transport.captured_payloads) == 1  # strict maximum, no retry loop

    def test_invalid_config_abstains(self, tmp_path):
        config = ProviderConfig(enabled=True, privacy_mode="bogus")
        config = config.model_copy(update={"privacy_mode": "bogus"})
        transport = FakeTransport()
        reviewer = LiveReviewer(config=config, transport=transport)
        result = reviewer.review(_make_packet(tmp_path))
        assert result.validation_status == "abstained"
        assert transport.captured_payloads == []


# ---------------------------------------------------------------------------
# LiveReviewer post-provider gates
# ---------------------------------------------------------------------------

class TestLiveOutputGates:
    def test_valid_output_accepted(self, tmp_path):
        reviewer, _ = _live(tmp_path, response={"findings": [_valid_finding()], "summary": "one finding"})
        result = reviewer.review(_make_packet(tmp_path))
        assert result.validation_status == "accepted"
        assert len(result.findings) == 1
        assert result.findings[0]["provenance"]["origin"] == "reviewer"

    def test_malformed_json_rejected_wholesale(self, tmp_path):
        reviewer, _ = _live(tmp_path, response="not json at all {{{")
        result = reviewer.review(_make_packet(tmp_path))
        assert result.validation_status == "rejected"
        assert result.findings == []

    def test_non_object_json_rejected(self, tmp_path):
        reviewer, _ = _live(tmp_path, response='[1, 2, 3]')
        result = reviewer.review(_make_packet(tmp_path))
        assert result.validation_status == "rejected"

    def test_markdown_wrapped_json_recovered(self, tmp_path):
        wrapped = "```json\n" + json.dumps({"findings": [_valid_finding()], "summary": "s"}) + "\n```"
        reviewer, _ = _live(tmp_path, response=wrapped)
        result = reviewer.review(_make_packet(tmp_path))
        assert len(result.findings) == 1

    def test_forbidden_status_rewritten(self, tmp_path):
        response = {"findings": [_valid_finding(status="validated", fixability="validated")], "summary": "s"}
        reviewer, _ = _live(tmp_path, response=response)
        result = reviewer.review(_make_packet(tmp_path))
        assert result.findings[0]["status"] == "review_only"
        assert result.findings[0]["fixability"] == "review_required"
        assert result.validation_status == "sanitized"
        assert any("status_rewrite" in e for e in reviewer.safety_events)

    def test_evidence_strength_cannot_be_upgraded(self, tmp_path):
        response = {"findings": [_valid_finding(evidence_strength="reproduced")], "summary": "s"}
        reviewer, _ = _live(tmp_path, response=response)
        result = reviewer.review(_make_packet(tmp_path))
        assert result.findings[0]["evidence_strength"] == "supported"
        assert "evidence_strength_clamped" in reviewer.safety_events

    def test_raw_secret_output_rejected_wholesale(self, tmp_path):
        response = {
            "findings": [_valid_finding(evidence=["AKIA1234567890EXAMPLE"])],
            "summary": "leak",
        }
        reviewer, _ = _live(tmp_path, response=response)
        result = reviewer.review(_make_packet(tmp_path))
        assert result.validation_status == "rejected"
        assert result.findings == []
        assert "output_rejected_secret_leak" in reviewer.safety_events

    def test_extra_top_level_keys_ignored(self, tmp_path):
        response = {"findings": [], "summary": "s", "policy_decision": "allowed_by_model", "patch": "--- a/x\n"}
        reviewer, _ = _live(tmp_path, response=response)
        result = reviewer.review(_make_packet(tmp_path))
        assert result.findings == []
        assert any("unsupported top-level keys" in e for e in result.validation_errors)
        assert "policy_decision" not in json.dumps(result.findings)

    def test_transport_timeout_failure(self, tmp_path):
        reviewer, _ = _live(tmp_path, error=ProviderTimeoutError("simulated"))
        result = reviewer.review(_make_packet(tmp_path))
        assert result.validation_status == "failed"
        assert "request_failed_timeout" in reviewer.safety_events

    def test_transport_rate_limit_failure(self, tmp_path):
        reviewer, _ = _live(tmp_path, error=ProviderRateLimitError("simulated"))
        result = reviewer.review(_make_packet(tmp_path))
        assert result.validation_status == "failed"
        assert "request_failed_rate_limit" in reviewer.safety_events

    def test_transport_auth_failure(self, tmp_path):
        reviewer, _ = _live(tmp_path, error=ProviderAuthError("simulated"))
        result = reviewer.review(_make_packet(tmp_path))
        assert result.validation_status == "failed"
        assert "request_failed_auth" in reviewer.safety_events

    def test_transport_response_size_failure(self, tmp_path):
        reviewer, _ = _live(tmp_path, error=ProviderResponseSizeError("simulated"))
        result = reviewer.review(_make_packet(tmp_path))
        assert result.validation_status == "failed"
        assert "request_failed_response_size" in reviewer.safety_events

    def test_usage_metadata_is_safe(self, tmp_path):
        reviewer, _ = _live(tmp_path, response={"findings": [], "summary": "s"})
        result = reviewer.review(_make_packet(tmp_path))
        serialized = json.dumps(result.usage_metadata)
        assert "authorization" not in serialized.lower()
        assert "raw_content" not in serialized.lower()


# ---------------------------------------------------------------------------
# Pipeline-level output validation (shared validator rules)
# ---------------------------------------------------------------------------

class TestOutputValidation:
    def _packet(self, tmp_path):
        return _make_packet(tmp_path, content="def f():\n    return 1\n\n\ndef g():\n    return 2\n")

    def test_invalid_path_rejected(self, tmp_path):
        result = validate_provider_output(self._packet(tmp_path), [_valid_finding(file="etc/passwd", start_line=1, end_line=1)])
        assert not result.is_valid and result.valid_findings == []

    def test_invalid_line_range_rejected(self, tmp_path):
        result = validate_provider_output(self._packet(tmp_path), [_valid_finding(start_line=10, end_line=2)])
        assert not result.is_valid

    def test_unchanged_line_finding_rejected(self, tmp_path):
        result = validate_provider_output(self._packet(tmp_path), [_valid_finding(start_line=50, end_line=51)])
        assert not result.is_valid

    def test_fabricated_test_rejected(self, tmp_path):
        finding = _valid_finding(tests_consulted=["tests/test_nonexistent.py"])
        result = validate_provider_output(self._packet(tmp_path), [finding])
        assert not result.is_valid
        assert result.valid_findings == []

    def test_duplicate_ids_rejected(self, tmp_path):
        result = validate_provider_output(
            self._packet(tmp_path), [_valid_finding(), _valid_finding()]
        )
        assert len(result.validation_errors) == 1
        assert len(result.valid_findings) == 1

    def test_supported_claim_accepted(self, tmp_path):
        result = validate_provider_output(self._packet(tmp_path), [_valid_finding()])
        assert result.is_valid and len(result.valid_findings) == 1
