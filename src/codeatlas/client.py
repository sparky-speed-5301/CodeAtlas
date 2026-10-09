"""Standard-library client for the CodeAtlas v1 local HTTP API.

No IDE, HTTP framework, or running service is needed to import this module.
Requests are never retried: a timed-out mutation may already have completed.
"""

from __future__ import annotations

import ipaddress
import json
import math
import re
import time
from http.client import HTTPException
from typing import Any, TypeVar
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from pydantic import BaseModel, ValidationError

from codeatlas.service import models as m
from codeatlas.service.contract import API_PREFIX, API_VERSION

Model = TypeVar("Model", bound=BaseModel)
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class ClientError(RuntimeError):
    """Stable client error; server input and credential-bearing bodies stay private."""

    def __init__(self, code: str, message: str, *, status: int | None = None) -> None:
        self.code = code
        self.status = status
        super().__init__(message)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        return None


def _read_response(response: Any) -> bytes:
    """A sized read can end early without raising IncompleteRead; check framing."""
    try:
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        length = response.headers.get("Content-Length")
        if length is not None and (int(length) < 0 or len(raw) != int(length)) and len(raw) <= MAX_RESPONSE_BYTES:
            raise ClientError("invalid_response", "CodeAtlas response was interrupted")
        return raw
    except (HTTPException, ValueError):
        raise ClientError("invalid_response", "CodeAtlas response was interrupted") from None


def identifier(value: str) -> str:
    """Encode one opaque API identifier, rejecting route delimiters."""
    if not value or len(value) > 256 or any(char in value for char in "/\\?#%") or value in {".", ".."}:
        raise ClientError("invalid_identifier", "Invalid API identifier")
    if any(ord(char) < 33 or ord(char) > 126 for char in value):
        raise ClientError("invalid_identifier", "Invalid API identifier")
    return quote(value, safe="-._~")


class CodeAtlasClient:
    """A bounded, loopback-only client. ``service_url`` is the server origin."""

    def __init__(self, service_url: str = "http://127.0.0.1:8765", *, timeout: float = 30.0) -> None:
        try:
            url = urlsplit(service_url)
            host = url.hostname or ""
            local = host == "localhost" or ipaddress.ip_address(host).is_loopback
            valid = (url.scheme == "http" and local and url.username is None and url.password is None
                     and url.path in {"", "/"} and not url.query and not url.fragment
                     and (url.port is None or 1 <= url.port <= 65535))
        except ValueError:
            valid = False
        if not valid:
            raise ClientError("invalid_service_url", "Service URL must be a loopback HTTP origin")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ClientError("invalid_timeout", "Timeout must be a positive finite number")
        self.service_url = service_url.rstrip("/")
        self.timeout = timeout
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())

    def request(self, method: str, endpoint: str, body: dict[str, Any] | None = None,
                *, timeout: float | None = None) -> dict[str, Any]:
        """Send a v1-relative request and return one JSON object."""
        if not endpoint.startswith("/") or endpoint.startswith("//") or "#" in endpoint:
            raise ClientError("invalid_endpoint", "Expected a v1-relative endpoint")
        if timeout is not None and (not math.isfinite(timeout) or timeout <= 0):
            raise ClientError("invalid_timeout", "Timeout must be a positive finite number")
        payload = json.dumps(body, allow_nan=False).encode("utf-8") if body is not None else None
        if payload is not None and len(payload) > 1_048_576:
            raise ClientError("payload_too_large", "Request exceeds the API size limit")
        request = Request(self.service_url + API_PREFIX + endpoint, data=payload, method=method,
                          headers={"Content-Type": "application/json", "Accept": "application/json"})
        try:
            with self._opener.open(request, timeout=timeout or self.timeout) as response:
                raw = _read_response(response)
                if response.headers.get("CodeAtlas-API-Version") != API_VERSION:
                    raise ClientError("incompatible_api", "Service does not advertise API v1")
        except HTTPError as error:
            with error:
                raw_error = _read_response(error)
            code = "http_error"
            try:
                data = json.loads(raw_error) if len(raw_error) <= MAX_RESPONSE_BYTES else {}
                candidate = data.get("error") if isinstance(data, dict) else None
                if isinstance(candidate, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", candidate):
                    code = candidate
            except (ValueError, UnicodeError):
                pass
            raise ClientError(code, f"CodeAtlas request failed (HTTP {error.code})", status=error.code) from None
        except (TimeoutError, URLError, OSError) as error:
            timed_out = isinstance(error, TimeoutError) or isinstance(getattr(error, "reason", None), TimeoutError)
            raise ClientError("timeout" if timed_out else "unreachable",
                              "CodeAtlas request timed out" if timed_out else "CodeAtlas service is unreachable") from None
        except HTTPException:
            raise ClientError("invalid_response", "CodeAtlas response was interrupted") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ClientError("response_too_large", "CodeAtlas response exceeds the size limit")
        try:
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError()
            return result
        except (ValueError, UnicodeError):
            raise ClientError("invalid_response", "CodeAtlas returned malformed JSON") from None

    def _model(self, model: type[Model], method: str, endpoint: str,
               body: BaseModel | None = None, *, timeout: float | None = None) -> Model:
        data = self.request(method, endpoint, body.model_dump(mode="json") if body else None, timeout=timeout)
        try:
            return model.model_validate(data)
        except ValidationError:
            raise ClientError("invalid_response", "CodeAtlas response does not match the API schema") from None

    def health(self) -> m.HealthResponse:
        return self._model(m.HealthResponse, "GET", "/health")

    def capabilities(self) -> m.CapabilitiesResponse:
        return self._model(m.CapabilitiesResponse, "GET", "/capabilities")

    def start_review(self, request: m.ReviewCreateRequest) -> m.ReviewStatusResponse:
        return self._model(m.ReviewStatusResponse, "POST", "/reviews", request)

    def get_review(self, run_id: str) -> m.ReviewStatusResponse:
        return self._model(m.ReviewStatusResponse, "GET", f"/reviews/{identifier(run_id)}")

    def wait_for_review(self, run_id: str, *, timeout: float = 300.0,
                        poll_interval: float = 0.2) -> m.ReviewStatusResponse:
        if not all(math.isfinite(v) and v > 0 for v in (timeout, poll_interval)):
            raise ClientError("invalid_timeout", "Wait timeout and poll interval must be positive and finite")
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ClientError("wait_timeout", "Review is still running; poll its run_id to resume")
            status = self._model(m.ReviewStatusResponse, "GET", f"/reviews/{identifier(run_id)}",
                                 timeout=min(self.timeout, remaining))
            if status.status in {"completed", "failed", "cancelled"}:
                return status
            time.sleep(min(poll_interval, max(0, deadline - time.monotonic())))

    def list_reviews(self, *, limit: int = 50, offset: int = 0) -> m.ReviewsListResponse:
        return self._model(m.ReviewsListResponse, "GET", f"/reviews?{urlencode({'limit': limit, 'offset': offset})}")

    def cancel_review(self, run_id: str) -> m.CancelResponse:
        return self._model(m.CancelResponse, "POST", f"/reviews/{identifier(run_id)}/cancel")

    def get_findings(self, run_id: str, *, severity: str | None = None, category: str | None = None,
                     status: str | None = None, include_dismissed: bool = False) -> m.FindingsListResponse:
        query = urlencode({k: v for k, v in {
            "severity": severity, "category": category, "status": status,
            "include_dismissed": str(include_dismissed).lower(),
        }.items() if v is not None})
        return self._model(m.FindingsListResponse, "GET", f"/reviews/{identifier(run_id)}/findings?{query}")

    @staticmethod
    def finding_path(run_id: str, finding_id: str) -> str:
        return f"/reviews/{identifier(run_id)}/findings/{identifier(finding_id)}"

    def get_finding(self, run_id: str, finding_id: str) -> m.FindingDetailResponse:
        return self._model(m.FindingDetailResponse, "GET", self.finding_path(run_id, finding_id))

    def explain_finding(self, run_id: str, finding_id: str) -> m.ExplainResponse:
        return self._model(m.ExplainResponse, "POST", f"/findings/{identifier(finding_id)}/explain",
                           m.ExplainRequest(run_id=run_id))

    def set_feedback(self, run_id: str, finding_id: str, feedback: m.FeedbackLabel | None) -> m.FindingFeedbackResponse:
        return self._model(m.FindingFeedbackResponse, "POST", self.finding_path(run_id, finding_id) + "/feedback",
                           m.FindingFeedbackRequest(feedback=feedback))

    def get_fix(self, run_id: str, finding_id: str, proposal_id: str | None = None) -> m.FixProposalResponse:
        path = self.finding_path(run_id, finding_id) + "/fix-proposal"
        if proposal_id:
            path = f"/fix-proposals/{identifier(proposal_id)}?{urlencode({'run_id': run_id, 'finding_id': finding_id})}"
        return self._model(m.FixProposalResponse, "GET", path)

    def generate_fix(self, run_id: str, finding_id: str, *, regenerate: bool = False) -> m.FixProposalResponse:
        path = self.finding_path(run_id, finding_id) + "/fix-proposal" + ("/regenerate" if regenerate else "")
        return self._model(m.FixProposalResponse, "POST", path, m.FixProposalRequest(run_id=run_id))

    def fix_eligibility(self, run_id: str, finding_id: str) -> m.FixEligibilityResponse:
        return self._model(m.FixEligibilityResponse, "GET", self.finding_path(run_id, finding_id) + "/fix-eligibility")

    def reject_fix(self, run_id: str, finding_id: str, proposal_id: str) -> m.FixProposalResponse:
        return self._model(m.FixProposalResponse, "POST", self.finding_path(run_id, finding_id) + "/fix-proposal/reject",
                           m.FixRejectRequest(run_id=run_id, proposal_id=proposal_id))

    def approve_fix_validation(self, request: m.FixValidationApprovalRequest) -> m.FixValidationApprovalResponse:
        return self._model(m.FixValidationApprovalResponse, "POST",
                           self.finding_path(request.run_id, request.finding_id) + "/fix-proposal/approve-validation", request)

    def validate_fix(self, request: m.FixValidationRequest) -> m.FixValidationResponse:
        return self._model(m.FixValidationResponse, "POST",
                           self.finding_path(request.run_id, request.finding_id) + "/fix-proposal/validate", request)

    def apply_fix(self, request: m.FixApplyRequest) -> m.FixApplyResponse:
        return self._model(m.FixApplyResponse, "POST",
                           self.finding_path(request.run_id, request.finding_id) + "/fix-proposal/apply", request)

    def revert_fix(self, request: m.FixRevertRequest) -> m.FixApplyResponse:
        return self._model(m.FixApplyResponse, "POST",
                           self.finding_path(request.run_id, request.finding_id) + "/fix-proposal/revert", request)

    def fix_history(self, run_id: str, finding_id: str, proposal_id: str) -> m.FixApplyHistoryResponse:
        query = urlencode({"run_id": run_id, "finding_id": finding_id})
        return self._model(m.FixApplyHistoryResponse, "GET", f"/fix-proposals/{identifier(proposal_id)}/apply-history?{query}")
