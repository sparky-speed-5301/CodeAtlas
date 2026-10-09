"""Production-capable structured repair provider.

This module owns the network boundary, response parsing, and provider-side
failure mapping.  It never constructs proposals, runs a command, changes a
file, approves validation, or emits a provider transcript.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Protocol

from codeatlas.review.packet import SECRET_RAW_MARKERS

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
    RepairProviderFailure,
)
from .repair_config import RepairProviderConfig
from .repair_models import RepairOutput, RepairRequest
from .repair_prompt import build_repair_prompt
from .transport import HttpTransport, ProviderTransport, TransportResponse


class StructuredRepairProvider(Protocol):
    """Boundary used by the verification loop."""

    name: str
    version: str
    network_access: bool

    def generate(self, request: RepairRequest) -> RepairOutput: ...


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate response key")
        result[key] = value
    return result


def _safe_failure(error: Exception) -> RepairProviderFailure:
    if isinstance(error, RepairProviderFailure):
        return error
    if isinstance(error, ProviderTimeoutError):
        return RepairProviderFailure("timeout")
    if isinstance(error, ProviderRateLimitError):
        return RepairProviderFailure("rate_limit")
    if isinstance(error, ProviderResponseSizeError):
        return RepairProviderFailure("output_too_large")
    if isinstance(error, ProviderBudgetExceededError):
        return RepairProviderFailure("budget_exceeded")
    if isinstance(error, ProviderConfigError):
        if "model" in str(error).lower():
            return RepairProviderFailure("unsupported_model")
        return RepairProviderFailure("provider_unavailable")
    if isinstance(error, ProviderAuthError):
        return RepairProviderFailure("provider_unavailable")
    if isinstance(error, (ProviderTransportError, ProviderError)):
        return RepairProviderFailure("provider_unavailable")
    return RepairProviderFailure("provider_unavailable")


class LiveRepairProvider:
    """OpenAI-compatible provider that returns one strict ``RepairOutput``.

    The transport is injectable for deterministic tests.  Real credentials are
    resolved by the transport from the configured environment variable and
    are never copied into the request model, output, logs, or exception.
    """

    name = "openai_compatible"
    version = "12.0.0"
    network_access = True

    def __init__(
        self,
        config: RepairProviderConfig | None = None,
        transport: ProviderTransport | None = None,
    ) -> None:
        self.config = config or RepairProviderConfig()
        self.transport = transport or HttpTransport()
        self.name = self.config.provider_name
        self.requests_made = 0

    def _transport_config(self) -> ProviderConfig:
        """Project safe settings onto the existing bounded HTTP transport."""
        return ProviderConfig(
            provider_name="live",
            model_name=self.config.model_name,
            api_base_url=self.config.api_base_url,
            api_key_env_var=self.config.api_key_env_var,
            timeout_seconds=self.config.timeout_seconds,
            max_retries=self.config.max_retries,
            max_response_bytes=self.config.max_response_bytes,
            enabled=True,
            dry_run=False,
        )

    def _parse_output(self, response: TransportResponse, request: RepairRequest) -> RepairOutput:
        raw = response.raw_content
        if len(raw.encode("utf-8", errors="replace")) > self.config.max_response_bytes:
            raise RepairProviderFailure("output_too_large")
        # Scan the raw bytes/string before JSON parsing and escaping.  This
        # catches secrets in discarded or malformed fields too.
        if any(marker.lower() in raw.lower() for marker in SECRET_RAW_MARKERS):
            raise RepairProviderFailure("secret_detected")
        try:
            parsed = json.loads(raw, object_pairs_hook=_unique_json_object)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            del error
            raise RepairProviderFailure("malformed_response")
        if not isinstance(parsed, dict):
            raise RepairProviderFailure("schema_failure")
        if not _payload_is_safe(parsed):
            raise RepairProviderFailure("secret_detected")
        required = set(RepairOutput.model_fields)
        if set(parsed) != required:
            raise RepairProviderFailure("schema_failure")
        try:
            output = RepairOutput.model_validate(parsed)
        except Exception as error:
            del error
            # Do not expose Pydantic's value-bearing error text.
            if not _payload_is_safe(parsed):
                raise RepairProviderFailure("secret_detected")
            raise RepairProviderFailure("schema_failure")
        if output.target_files != [request.context.changed_file]:
            raise RepairProviderFailure("schema_failure")
        return output

    def generate(self, request: RepairRequest) -> RepairOutput:
        try:
            request = request if isinstance(request, RepairRequest) else RepairRequest.model_validate(request)
            self.config.validate_config()
        except RepairProviderFailure:
            raise
        except ProviderConfigError as error:
            raise _safe_failure(error)
        except Exception as error:
            del error
            raise RepairProviderFailure("schema_failure")
        if self.config.mode != "online":
            raise RepairProviderFailure("network_disabled")
        if self.requests_made >= self.config.max_repair_attempts:
            raise RepairProviderFailure("budget_exceeded")
        if self.config.provider_name not in {"openai_compatible", "live"}:
            raise RepairProviderFailure("unsupported_model")
        if self.config.model_name.lower().startswith(("unsupported", "unknown", "invalid")):
            raise RepairProviderFailure("unsupported_model")
        if self.config.token_budget is not None and self.config.token_budget <= 0:
            raise RepairProviderFailure("budget_exceeded")

        self.requests_made += 1
        try:
            response = self.transport.send(
                build_repair_prompt(request),
                self._transport_config(),
            )
            if (
                self.config.token_budget is not None
                and response.input_tokens + response.output_tokens > self.config.token_budget
            ):
                raise RepairProviderFailure("budget_exceeded")
            return self._parse_output(response, request)
        except RepairProviderFailure:
            raise
        except Exception as error:
            raise _safe_failure(error) from None

    # The existing provider abstraction calls repair proposals ``propose``.
    # Keep this alias data-only; the orchestrator/loop still owns validation.
    def propose(self, request: RepairRequest) -> RepairOutput:
        return self.generate(request)


def _payload_is_safe(payload: object) -> bool:
    """Use the repair contract's recursive scanner without exposing details."""
    from codeatlas.orchestrator.repair_models import repair_payload_is_safe

    return repair_payload_is_safe(payload)


class DeterministicRepairProvider:
    """Offline provider adapter used by tests and explicitly offline runs.

    A caller may supply a pure factory for a fixture candidate.  With no
    factory it abstains via ``network_disabled`` rather than inventing a fix.
    """

    name = "offline"
    version = "12.0.0"
    network_access = False

    def __init__(self, output_factory: Callable[[RepairRequest], RepairOutput | dict[str, Any]] | None = None) -> None:
        self.output_factory = output_factory
        self.calls = 0

    def generate(self, request: RepairRequest) -> RepairOutput:
        self.calls += 1
        if self.output_factory is None:
            raise RepairProviderFailure("network_disabled")
        try:
            value = self.output_factory(request)
            return RepairOutput.model_validate(value.model_dump(mode="json") if isinstance(value, RepairOutput) else value)
        except RepairProviderFailure:
            raise
        except Exception as error:
            del error
            raise RepairProviderFailure("schema_failure")

    propose = generate


# A descriptive production name for integrations that do not want to depend
# on a particular transport vendor.
RealRepairProvider = LiveRepairProvider


__all__ = [
    "DeterministicRepairProvider",
    "LiveRepairProvider",
    "RealRepairProvider",
    "StructuredRepairProvider",
]
