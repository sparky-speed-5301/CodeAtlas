"""Live reviewer provider conforming to the ReviewerProvider protocol.

The live provider is a data-in/data-out component only.  It receives a
bounded, redacted ReviewPacket and returns a structured ReviewerResult.  It
has no tools: no shell, no git, no filesystem access, no repository
execution, no credential access, and no approval-token generation.  It can
never apply patches, decide its own approval, or change review policy.
"""

from __future__ import annotations

import json
from typing import Any

from codeatlas.review.packet import SECRET_PATTERNS, ReviewPacket
from codeatlas.review.provider import ReviewerResult

from .config import ProviderConfig
from .errors import (
    ProviderAuthError,
    ProviderBudgetExceededError,
    ProviderConfigError,
    ProviderError,
    ProviderRateLimitError,
    ProviderResponseSizeError,
    ProviderTimeoutError,
    ProviderTransportError,
)
from .prompt import build_review_prompt
from .transport import HttpTransport, ProviderTransport

# Statuses a live provider may never claim; always rewritten to review_only.
_FORBIDDEN_STATUSES = frozenset({
    "validated", "applied", "merged", "tested", "compiled", "fixed", "approved",
})

# Live output may never upgrade evidence strength beyond 'supported'.
_MAX_LIVE_EVIDENCE_STRENGTH = "supported"
_EVIDENCE_RANK = {"none": 0, "weak": 1, "supported": 2, "strong": 3, "reproduced": 4}

_MAX_FINDINGS_PER_RESPONSE = 50
_MAX_PATCH_SUGGESTIONS_PER_RESPONSE = 10

# The only fields a patch suggestion may carry (Phase 7B output contract).
_SUGGESTION_ALLOWED_KEYS = frozenset({
    "suggestion_id", "finding_id", "unified_diff", "rationale", "expected_behavior",
    "target_files", "risk_level", "limitations", "provider_provenance",
})

# Any of these keys in a suggestion is provider overreach: approval tokens,
# policy claims, execution commands, or unverifiable status claims.
_SUGGESTION_FORBIDDEN_KEYS = frozenset({
    "approval_token", "approval", "token", "command", "commands", "shell", "shell_command",
    "execute", "execution", "tool_call", "tool_calls", "status", "fixability", "validated",
    "approved", "applied", "merged", "fixed", "tests_passed", "test_results", "compiler_output",
    "policy_decision", "policy_override", "patch_hash", "credential", "credentials", "api_key",
})

_SUGGESTION_RISK_LEVELS = frozenset({"low", "medium", "high"})


class LiveReviewer:
    """Configurable live reviewer provider executing bounded read-only reviews."""

    name = "live"
    version = "1.0.0"

    def __init__(
        self,
        config: ProviderConfig | None = None,
        transport: ProviderTransport | None = None,
        *,
        allow_patch_suggestions: bool = False,
    ) -> None:
        self.config = config or ProviderConfig()
        self.transport = transport or HttpTransport()
        self.name = self.config.provider_name
        self.allow_patch_suggestions = allow_patch_suggestions
        self.requests_made = 0
        self.safety_events: list[str] = []

    # ------------------------------------------------------------------
    # Pre-provider safety gates
    # ------------------------------------------------------------------
    def _note(self, event: str) -> None:
        self.safety_events.append(event)

    def _preflight(self, packet: ReviewPacket) -> ReviewerResult | None:
        """Run all pre-request gates; return a terminal result or None to proceed."""
        if self.config.dry_run:
            return ReviewerResult(
                provider_name=self.name,
                provider_version=self.version,
                findings=[],
                summary="Dry run: no provider request was made.",
                limitations=["dry_run"],
                usage_metadata={"dry_run": True},
                validation_status="not_requested",
            )

        # 1. Configuration must be valid and enabled.
        try:
            self.config.validate_config()
        except ProviderConfigError as err:
            self._note("preflight_config_invalid")
            return self._abstained([f"provider configuration invalid: {err}"], "config_invalid")
        if not self.config.enabled:
            self._note("preflight_provider_disabled")
            return self._abstained(["Live provider is disabled in configuration."], "provider_disabled")

        # 1b. Credentials are required only for the real HTTP transport; fake
        # transports (tests/evals) and dry runs must never need them.
        if isinstance(self.transport, HttpTransport):
            try:
                self.config.resolve_api_key()
            except ProviderConfigError as err:
                self._note("preflight_missing_credentials")
                return self._abstained([f"missing credentials: {err}"], "missing_credentials")

        # 2. Budget: strict maximum, no automatic provider switching.
        if self.requests_made >= self.config.request_budget:
            self._note("preflight_budget_exceeded")
            return self._abstained(
                [f"Provider request budget exhausted ({self.config.request_budget} request(s))."],
                "budget_exceeded",
            )

        # 3. Redaction audit must pass: fail closed.
        if not packet.redaction_status.redacted or packet.redaction_status.failed_checks:
            self._note("preflight_redaction_failed")
            return self._abstained(
                ["Packet redaction audit failed; refusing to transmit."],
                "redaction_failed",
            )

        # 4. Packet hard size limit.
        packet_bytes = packet.packet_size_statistics.total_bytes
        if packet_bytes > self.config.max_packet_bytes:
            self._note("preflight_packet_too_large")
            return self._abstained(
                [f"Packet size {packet_bytes} bytes exceeds hard limit {self.config.max_packet_bytes}."],
                "packet_too_large",
            )

        # 5. Required context truncated -> abstain rather than review partial evidence.
        if packet.truncated:
            truncated_changed = any(
                c.source_type in {"changed_code", "symbol"} and c.truncation_status != "full"
                for c in packet.context_candidates
            )
            if truncated_changed:
                self._note("preflight_truncated_context")
                return self._abstained(
                    ["Required changed-code context was truncated; policy requires abstention."],
                    "truncated_context",
                )
        return None

    def _abstained(self, limitations: list[str], reason: str) -> ReviewerResult:
        return ReviewerResult(
            provider_name=self.name,
            provider_version=self.version,
            findings=[],
            summary=f"Live review abstained: {reason}.",
            limitations=limitations,
            abstentions=[reason],
            usage_metadata={"dry_run": self.config.dry_run},
            validation_status="abstained",
        )

    # ------------------------------------------------------------------
    # Review execution
    # ------------------------------------------------------------------
    def review(self, packet: ReviewPacket) -> ReviewerResult:
        """Execute review against the provider backend, parsing and sanitizing findings."""
        preflight = self._preflight(packet)
        if preflight is not None:
            return preflight

        errors: list[str] = []
        self._note("request_started")
        self.requests_made += 1

        # 1. Deterministic prompt construction (never includes raw system-side data).
        prompt_payload = build_review_prompt(
            packet,
            allow_patch_suggestions=self.allow_patch_suggestions,
        )
        prompt_payload["model"] = self.config.model_name
        prompt_payload["max_tokens"] = self.config.max_output_tokens
        prompt_payload["temperature"] = self.config.temperature

        # 2. Transport dispatch (bounded retries inside transport).
        try:
            resp = self.transport.send(prompt_payload, self.config)
        except ProviderBudgetExceededError as err:
            self._note("request_failed_budget")
            return self._failed_result([f"Provider budget exceeded: {err}"], "budget_exceeded")
        except ProviderAuthError as err:
            self._note("request_failed_auth")
            return self._failed_result([f"Provider authentication failed: {err}"], "auth_error")
        except ProviderRateLimitError as err:
            self._note("request_failed_rate_limit")
            return self._failed_result([f"Provider rate limit reached: {err}"], "rate_limited")
        except ProviderTimeoutError as err:
            self._note("request_failed_timeout")
            return self._failed_result([f"Provider request timed out: {err}"], "timeout")
        except ProviderResponseSizeError as err:
            self._note("request_failed_response_size")
            return self._failed_result([f"Provider response too large: {err}"], "response_too_large")
        except (ProviderTransportError, ProviderError) as err:
            self._note("request_failed_transport")
            return self._failed_result([f"Provider transport error: {err}"], "transport_error")
        except Exception as err:  # unexpected; fail closed without leaking details
            self._note("request_failed_unexpected")
            return self._failed_result(
                [f"Unexpected provider error: {type(err).__name__}"], "unexpected_error"
            )

        # Safe usage metadata only: no headers, no key material, no raw content.
        input_cost = (resp.input_tokens / 1000.0) * self.config.estimated_cost_per_1k_input
        output_cost = (resp.output_tokens / 1000.0) * self.config.estimated_cost_per_1k_output
        total_cost = round(input_cost + output_cost, 6)
        usage = {
            "input_tokens": resp.input_tokens,
            "output_tokens": resp.output_tokens,
            "latency_ms": resp.latency_ms,
            "retries": resp.retries_attempted,
            "request_id": resp.request_id,
            "estimated_cost": total_cost,
            "status_code": resp.status_code,
        }

        # 3. Parse structured output, tolerating markdown-wrapped JSON.
        raw = resp.raw_content.strip()
        if raw.startswith("```"):
            lines = raw.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            raw = "\n".join(lines).strip()
            errors.append("Provider wrapped JSON in markdown fences; content extracted")

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as err:
            self._note("output_rejected_malformed_json")
            return ReviewerResult(
                provider_name=self.name,
                provider_version=self.version,
                findings=[],
                summary="Provider returned malformed JSON.",
                limitations=["malformed_json_response"],
                usage_metadata=usage,
                validation_errors=[f"Failed to parse provider JSON response: {err}"],
                validation_status="rejected",
            )
        if not isinstance(parsed, dict):
            self._note("output_rejected_not_object")
            return ReviewerResult(
                provider_name=self.name,
                provider_version=self.version,
                findings=[],
                summary="Provider output is not a JSON object.",
                limitations=["malformed_json_response"],
                usage_metadata=usage,
                validation_errors=["Provider output is not a JSON object"],
                validation_status="rejected",
            )

        allowed_top = {"summary", "findings", "limitations", "abstentions"}
        if self.allow_patch_suggestions:
            allowed_top.add("patch_suggestions")
        extra_keys = set(parsed) - allowed_top
        if extra_keys:
            errors.append(f"Ignoring unsupported top-level keys: {sorted(extra_keys)}")

        # 4. Raw-secret scans.  Secrets in the review payload (summary, findings,
        #    limitations, abstentions) reject the whole output; secrets inside a
        #    patch suggestion reject only that suggestion (Phase 7B).
        review_payload = json.dumps({
            "summary": parsed.get("summary"),
            "findings": parsed.get("findings"),
            "limitations": parsed.get("limitations"),
            "abstentions": parsed.get("abstentions"),
        })
        if any(pat.search(review_payload) for pat in SECRET_PATTERNS):
            self._note("output_rejected_secret_leak")
            return ReviewerResult(
                provider_name=self.name,
                provider_version=self.version,
                findings=[],
                summary="Provider output rejected due to credential detection.",
                limitations=["secret_leak_attempt"],
                usage_metadata=usage,
                validation_errors=["Provider response contained potential raw secret pattern; rejected"],
                validation_status="rejected",
            )

        # 5. Per-finding sanitization; schema/path/line validation happens in the
        #    pipeline's provider-output validator, which remains the single authority.
        raw_findings = parsed.get("findings", [])
        if not isinstance(raw_findings, list):
            raw_findings = []
            errors.append("findings field was not a list; ignored")
        clean_findings: list[dict[str, Any]] = []
        sanitized_any = False

        for f in raw_findings[:_MAX_FINDINGS_PER_RESPONSE]:
            if not isinstance(f, dict):
                errors.append("Dropped non-object finding entry")
                continue
            if len(raw_findings) > _MAX_FINDINGS_PER_RESPONSE and f is raw_findings[_MAX_FINDINGS_PER_RESPONSE - 1]:
                errors.append(
                    f"Truncated findings list to {_MAX_FINDINGS_PER_RESPONSE} entries"
                )
                self._note("output_sanitized_finding_cap")

            status = str(f.get("status", "review_only"))
            if status in _FORBIDDEN_STATUSES:
                errors.append(f"Finding claimed disallowed status '{status}'; rewritten to 'review_only'")
                status = "review_only"
                sanitized_any = True
                self._note(f"status_rewrite:{status}")

            strength = str(f.get("evidence_strength", "weak"))
            if _EVIDENCE_RANK.get(strength, 0) > _EVIDENCE_RANK[_MAX_LIVE_EVIDENCE_STRENGTH]:
                errors.append(
                    f"Finding evidence_strength '{strength}' downgraded to "
                    f"'{_MAX_LIVE_EVIDENCE_STRENGTH}' (live output cannot exceed supported)"
                )
                strength = _MAX_LIVE_EVIDENCE_STRENGTH
                sanitized_any = True
                self._note("evidence_strength_clamped")

            if f.get("fixability") != "review_required":
                errors.append(
                    f"Finding fixability '{f.get('fixability')}' forced to 'review_required' "
                    "(live output cannot claim verified fixes)"
                )
                sanitized_any = True
                self._note("fixability_forced")

            f["status"] = status
            f["evidence_strength"] = strength
            f["fixability"] = "review_required"
            if not isinstance(f.get("provenance"), dict):
                f["provenance"] = {}
            f["provenance"]["origin"] = "reviewer"
            f["provenance"]["provider"] = self.name
            f["provenance"]["model"] = self.config.model_name
            clean_findings.append(f)

        if sanitized_any:
            self._note("output_sanitized")

        # 6. Patch suggestion sanitization (Phase 7B): structured draft data only.
        patch_suggestions: list[dict[str, Any]] = []
        raw_suggestions = parsed.get("patch_suggestions", [])
        if raw_suggestions and not self.allow_patch_suggestions:
            errors.append(
                f"Ignored {len(raw_suggestions)} patch suggestion(s); patch suggestions are disabled"
            )
            self._note("patch_suggestions_disabled_ignored")
            raw_suggestions = []
        if isinstance(raw_suggestions, list):
            if len(raw_suggestions) > _MAX_PATCH_SUGGESTIONS_PER_RESPONSE:
                errors.append(
                    f"Truncated patch suggestions list to {_MAX_PATCH_SUGGESTIONS_PER_RESPONSE} entries"
                )
                self._note("patch_suggestions_capped")
            for suggestion in raw_suggestions[:_MAX_PATCH_SUGGESTIONS_PER_RESPONSE]:
                cleaned = self._sanitize_suggestion(suggestion, errors)
                if cleaned is not None:
                    patch_suggestions.append(cleaned)
        elif raw_suggestions:
            errors.append("patch_suggestions field was not a list; ignored")

        return ReviewerResult(
            provider_name=self.name,
            provider_version=self.version,
            findings=clean_findings,
            summary=str(parsed.get("summary", "Review completed by provider.")),
            limitations=[str(lim) for lim in parsed.get("limitations", []) if isinstance(lim, str)],
            abstentions=[str(a) for a in parsed.get("abstentions", []) if isinstance(a, str)],
            usage_metadata=usage,
            validation_errors=errors,
            validation_status="sanitized" if sanitized_any else "accepted",
            patch_suggestions=patch_suggestions,
        )

    def _sanitize_suggestion(self, suggestion: Any, errors: list[str]) -> dict[str, Any] | None:
        """Sanitize one provider patch suggestion; return None when it must be rejected.

        Suggestions are draft data only.  Approval tokens, policy claims,
        execution commands, status claims, and raw secrets are grounds for
        rejection; any other unsupported field is dropped and recorded.
        """
        if not isinstance(suggestion, dict):
            errors.append("Patch suggestion rejected: entry is not an object")
            self._note("patch_suggestion_rejected")
            return None
        sid = str(suggestion.get("suggestion_id", "unknown"))

        unexpected = [k for k in suggestion if k not in _SUGGESTION_ALLOWED_KEYS]
        forbidden = [k for k in unexpected if str(k).strip().lower() in _SUGGESTION_FORBIDDEN_KEYS]
        if forbidden:
            errors.append(f"Patch suggestion {sid} rejected: forbidden field(s) {sorted(str(k) for k in forbidden)}")
            self._note("patch_suggestion_rejected")
            return None

        if any(pat.search(json.dumps(suggestion)) for pat in SECRET_PATTERNS):
            errors.append(f"Patch suggestion {sid} rejected: contains raw secret data")
            self._note("patch_suggestion_rejected_secret")
            return None

        for key in ("suggestion_id", "finding_id", "unified_diff"):
            value = suggestion.get(key)
            if not isinstance(value, str) or not value:
                errors.append(f"Patch suggestion {sid} rejected: missing or invalid '{key}'")
                self._note("patch_suggestion_rejected")
                return None
        target_files = suggestion.get("target_files")
        if (
            not isinstance(target_files, list)
            or not target_files
            or not all(isinstance(t, str) and t.strip() for t in target_files)
        ):
            errors.append(f"Patch suggestion {sid} rejected: target_files must be a non-empty list of strings")
            self._note("patch_suggestion_rejected")
            return None

        risk = suggestion.get("risk_level", "medium")
        if risk not in _SUGGESTION_RISK_LEVELS:
            errors.append(f"Patch suggestion {sid} risk_level {risk!r} forced to 'medium'")
            risk = "medium"
            self._note("patch_suggestion_risk_downgraded")

        if unexpected:
            errors.append(f"Patch suggestion {sid}: ignored unsupported field(s) {sorted(str(k) for k in unexpected)}")

        provider_provenance = suggestion.get("provider_provenance")
        return {
            "suggestion_id": suggestion["suggestion_id"],
            "finding_id": suggestion["finding_id"],
            "unified_diff": suggestion["unified_diff"],
            "rationale": str(suggestion.get("rationale", "")),
            "expected_behavior": str(suggestion.get("expected_behavior", "")),
            "target_files": [t.replace("\\", "/").strip("/") for t in target_files],
            "risk_level": risk,
            "limitations": [str(x) for x in suggestion.get("limitations", []) if isinstance(x, str)],
            "provider_provenance": {
                "origin": "provider",
                "provider": self.name,
                "model": self.config.model_name,
                **(provider_provenance if isinstance(provider_provenance, dict) else {}),
            },
        }

    def _failed_result(self, messages: list[str], limitation: str) -> ReviewerResult:
        return ReviewerResult(
            provider_name=self.name,
            provider_version=self.version,
            findings=[],
            summary="Review provider request failed.",
            limitations=[f"transport_error:{limitation}"],
            usage_metadata={},
            validation_errors=messages,
            validation_status="failed",
        )


__all__ = [
    "LiveReviewer",
]
