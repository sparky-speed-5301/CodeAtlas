"""Provider transport layer separating HTTP communication from prompt construction."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Protocol
from pydantic import BaseModel, ConfigDict, Field

from .config import ProviderConfig
from .errors import (
    ProviderAuthError,
    ProviderRateLimitError,
    ProviderResponseSizeError,
    ProviderTimeoutError,
    ProviderTransportError,
)


class TransportResponse(BaseModel):
    """Normalized response returned from a provider transport."""

    model_config = ConfigDict(extra="ignore")

    raw_content: str
    status_code: int = 200
    latency_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    request_id: str | None = None
    retries_attempted: int = 0
    headers: dict[str, str] = Field(default_factory=dict)


class ProviderTransport(Protocol):
    """Protocol for provider network transports."""

    def send(self, payload: dict[str, Any], config: ProviderConfig) -> TransportResponse: ...


class HttpTransport:
    """Standard HTTP transport using bounded retries and timeout controls."""

    def __init__(self, default_endpoint: str = "https://api.openai.com/v1/chat/completions") -> None:
        self.default_endpoint = default_endpoint

    def send(self, payload: dict[str, Any], config: ProviderConfig) -> TransportResponse:
        """Send an HTTP request with bounded retries and exponential backoff."""
        if config.dry_run:
            return TransportResponse(
                raw_content=json.dumps({"findings": [], "summary": "Dry run: no provider request made", "limitations": ["dry_run"]}),
                status_code=200,
                latency_ms=0.0,
                input_tokens=0,
                output_tokens=0,
                request_id="dry-run-000",
                retries_attempted=0,
            )

        api_key = config.resolve_api_key()
        url = config.api_base_url or self.default_endpoint
        body_bytes = json.dumps(payload).encode("utf-8")

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "CodeAtlas-Reviewer/1.0",
        }

        retries = 0
        last_error: Exception | None = None

        while retries <= config.max_retries:
            t0 = time.perf_counter()
            req = urllib.request.Request(url, data=body_bytes, headers=headers, method="POST")

            try:
                with urllib.request.urlopen(req, timeout=config.timeout_seconds) as resp:
                    latency = (time.perf_counter() - t0) * 1000.0
                    content_bytes = resp.read(config.max_response_bytes + 1)
                    if len(content_bytes) > config.max_response_bytes:
                        raise ProviderResponseSizeError(
                            f"Provider response exceeded size limit of {config.max_response_bytes} bytes"
                        )

                    raw_text = content_bytes.decode("utf-8", errors="replace")
                    status_code = resp.status
                    resp_headers = dict(resp.headers.items())
                    req_id = resp_headers.get("x-request-id") or resp_headers.get("request-id")

                    # Parse usage if OpenAI-compatible format
                    input_tok = 0
                    output_tok = 0
                    try:
                        parsed = json.loads(raw_text)
                        usage = parsed.get("usage", {})
                        input_tok = int(usage.get("prompt_tokens", 0))
                        output_tok = int(usage.get("completion_tokens", 0))
                        # If choices format, extract message content
                        choices = parsed.get("choices", [])
                        if choices and "message" in choices[0]:
                            raw_text = choices[0]["message"].get("content", "")
                    except Exception:
                        pass

                    return TransportResponse(
                        raw_content=raw_text,
                        status_code=status_code,
                        latency_ms=latency,
                        input_tokens=input_tok,
                        output_tokens=output_tok,
                        request_id=req_id,
                        retries_attempted=retries,
                    )

            except urllib.error.HTTPError as err:
                latency = (time.perf_counter() - t0) * 1000.0
                if err.code in (401, 403):
                    raise ProviderAuthError(f"Provider authentication failed with status {err.code}") from err
                elif err.code == 429:
                    if retries >= config.max_retries:
                        raise ProviderRateLimitError("Provider rate limit reached (HTTP 429)") from err
                elif err.code >= 500:
                    last_error = err
                else:
                    raise ProviderTransportError(f"Provider request failed with HTTP status {err.code}") from err

            except urllib.error.URLError as err:
                latency = (time.perf_counter() - t0) * 1000.0
                if "timed out" in str(err).lower():
                    if retries >= config.max_retries:
                        raise ProviderTimeoutError(f"Provider request timed out after {config.timeout_seconds}s") from err
                last_error = err

            except TimeoutError as err:
                if retries >= config.max_retries:
                    raise ProviderTimeoutError(f"Provider request timed out after {config.timeout_seconds}s") from err
                last_error = err

            # Exponential backoff sleep before retry
            retries += 1
            if retries <= config.max_retries:
                sleep_sec = min((2 ** retries) * 0.2, 2.0)
                time.sleep(sleep_sec)

        raise ProviderTransportError(f"Provider request failed after {config.max_retries} retries: {last_error}")


class FakeTransport:
    """Configurable in-memory transport for tests and adversarial evaluations."""

    def __init__(
        self,
        *,
        simulated_response: str | dict[str, Any] | None = None,
        simulated_error: Exception | None = None,
        latency_ms: float = 5.0,
        input_tokens: int = 150,
        output_tokens: int = 80,
    ) -> None:
        self.simulated_response = simulated_response
        self.simulated_error = simulated_error
        self.latency_ms = latency_ms
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.captured_payloads: list[dict[str, Any]] = []

    def send(self, payload: dict[str, Any], config: ProviderConfig) -> TransportResponse:
        """Record the request payload and return simulated response or raise simulated error."""
        self.captured_payloads.append(payload)

        if config.dry_run:
            return TransportResponse(
                raw_content=json.dumps({"findings": [], "summary": "Dry run: no provider request made", "limitations": ["dry_run"]}),
                status_code=200,
                latency_ms=0.0,
                input_tokens=0,
                output_tokens=0,
                request_id="fake-dry-run",
            )

        if self.simulated_error is not None:
            raise self.simulated_error

        if self.simulated_response is None:
            content = json.dumps({
                "findings": [],
                "summary": "Clean review; no issues detected by mock backend.",
                "limitations": [],
                "abstentions": [],
            })
        elif isinstance(self.simulated_response, dict):
            content = json.dumps(self.simulated_response)
        else:
            content = str(self.simulated_response)

        return TransportResponse(
            raw_content=content,
            status_code=200,
            latency_ms=self.latency_ms,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            request_id=f"fake-req-{len(self.captured_payloads)}",
            retries_attempted=0,
        )


__all__ = [
    "TransportResponse",
    "ProviderTransport",
    "HttpTransport",
    "FakeTransport",
]
