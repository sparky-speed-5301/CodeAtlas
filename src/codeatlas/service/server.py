"""HTTP server and request handlers for the CodeAtlas local service."""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import socket
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from pydantic import ValidationError

from codeatlas import __version__
from codeatlas.adapters import language_capability_reports
from codeatlas.service.contract import (
    API_PREFIX,
    API_VERSION,
    ENDPOINTS,
    endpoint_for,
    openapi_document,
)
from codeatlas.service.models import (
    CapabilitiesResponse,
    ExplainRequest,
    FindingFeedbackRequest,
    FixApplyRequest,
    FixProposalFeedbackRequest,
    FixProposalRequest,
    FixRejectRequest,
    FixRevertRequest,
    FixValidationApprovalRequest,
    FixValidationRequest,
    HealthResponse,
    LanguageCapabilityResponse,
    PatchProposalRequest,
    ReviewCreateRequest,
    ValidateProposalRequest,
)
from codeatlas.service.persistence import default_database_path
from codeatlas.service.state import (
    ReviewStateManager,
    ServicePathError,
    ServiceStateError,
)

logger = logging.getLogger("codeatlas.service")

MAX_REQUEST_BYTES = 1_048_576  # 1 MB maximum request payload

# ServiceStateError reasons that describe a resolvable state conflict rather
# than a malformed request; the apply/revert operations share them.
FIX_CONFLICT_REASONS = frozenset({
    "scope_mismatch",
    "validation_not_approved",
    "validation_not_available",
    "already_applied",
    "apply_not_available",
    "apply_not_validated",
    "workspace_dirty",
    "repository_state_stale",
    "resulting_diff_mismatch",
    "revert_not_available",
    "revert_state_changed",
    "recovered_operation_unavailable",
})


class ReviewHttpHandler(BaseHTTPRequestHandler):
    """Handles REST API requests for CodeAtlas reviews, findings, and proposals."""

    server_version = f"CodeAtlasService/{__version__}"
    state_manager: ReviewStateManager

    def log_message(self, format: str, *args: Any) -> None:
        # Avoid noisy console logging in tests; use module logger
        logger.debug("%s - - [%s] %s\n", self.address_string(), self.log_date_time_string(), format % args)

    def _send_json(self, status: int, data: Any) -> None:
        """Send a bounded JSON response."""
        encoded = json.dumps(data, indent=2, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("CodeAtlas-API-Version", API_VERSION)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(encoded)

    def _send_error_json(self, status: int, message: str, code: str = "error") -> None:
        self._send_json(status, {"error": code, "message": message})

    def _read_json_body(self) -> dict[str, Any]:
        """Read and parse incoming JSON payload within strict size bounds."""
        content_length_header = self.headers.get("Content-Length")
        if not content_length_header:
            return {}

        try:
            length = int(content_length_header)
        except ValueError:
            raise ValueError("Invalid Content-Length header")

        if length > MAX_REQUEST_BYTES:
            raise OverflowError(f"Request payload exceeds limit of {MAX_REQUEST_BYTES} bytes")
        if length < 0:
            raise ValueError("Invalid Content-Length header")

        raw_body = self.rfile.read(length)
        if not raw_body:
            return {}

        try:
            parsed = json.loads(raw_body.decode("utf-8"))
            if not isinstance(parsed, dict):
                raise ValueError("JSON request body must be an object")
            return parsed
        except json.JSONDecodeError as err:
            raise ValueError(f"Malformed JSON: {err}") from err

    def _route_path(self) -> str | None:
        """Resolve v1 once, retaining unversioned aliases for existing clients."""
        path = urlparse(self.path).path
        if path.startswith(API_PREFIX + "/"):
            path = path[len(API_PREFIX):]
            if endpoint_for(self.command, path) is None:
                known = any(item.matches(item.method, path) for item in ENDPOINTS)
                self._send_error_json(
                    HTTPStatus.METHOD_NOT_ALLOWED if known else HTTPStatus.NOT_FOUND,
                    "Method not allowed" if known else "Endpoint not found",
                    "method_not_allowed" if known else "not_found",
                )
                return None
        elif path == "/api" or path.startswith("/api/"):
            self._send_error_json(HTTPStatus.NOT_FOUND, "Unsupported API version or endpoint", "unsupported_api_version")
            return None
        return path

    def _validate_v1_body(self, path: str, body: dict[str, Any]) -> bool:
        if not urlparse(self.path).path.startswith(API_PREFIX + "/"):
            return True
        endpoint = endpoint_for(self.command, path)
        model = endpoint.request if endpoint else None
        try:
            if model:
                if body.keys() - model.model_fields.keys():
                    raise ValueError("Unknown request fields")
                model.model_validate(body, strict=True)
            elif body:
                raise ValueError("This operation takes no request body")
        except (ValueError, ValidationError):
            # Pydantic errors can contain raw input, including approval tokens.
            self._send_error_json(HTTPStatus.BAD_REQUEST, "Request does not match the API schema", "bad_request")
            return False
        return True

    def do_OPTIONS(self) -> None:
        """Handle CORS preflight requests."""
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _unsupported_method(self) -> None:
        if self._route_path() is not None:
            self._send_error_json(HTTPStatus.METHOD_NOT_ALLOWED, "Method not allowed", "method_not_allowed")

    do_PUT = _unsupported_method
    do_PATCH = _unsupported_method
    do_DELETE = _unsupported_method

    def do_GET(self) -> None:
        """Route GET endpoints."""
        try:
            parsed_url = urlparse(self.path)
            path = self._route_path()
            if path is None:
                return
            query = parse_qs(parsed_url.query)

            if path == "/diagnostics/storage":
                try:
                    peer = ipaddress.ip_address(self.client_address[0])
                    local = peer.is_loopback or (
                        isinstance(peer, ipaddress.IPv6Address)
                        and peer.ipv4_mapped is not None and peer.ipv4_mapped.is_loopback
                    )
                except ValueError:
                    local = False
                if not local:
                    self._send_error_json(HTTPStatus.FORBIDDEN, "Loopback access required", "forbidden")
                    return
                self._send_json(HTTPStatus.OK, self.state_manager.storage_health().model_dump())
                return

            if path == "/openapi.json":
                self._send_json(HTTPStatus.OK, openapi_document())
                return
            if path == "/capabilities":
                reports = [
                    LanguageCapabilityResponse(
                        language=item.language,
                        language_version=item.language_version,
                        parser=item.parser,
                        syntax_validation=item.syntax_validation,
                        formatter=item.formatter,
                        linter=item.linter,
                        type_checker=item.type_checker,
                        test_runner=item.test_runner,
                        protected_generated_files=item.protected_generated_files,
                        limitations=list(item.limitations),
                    )
                    for item in language_capability_reports()
                ]
                self._send_json(HTTPStatus.OK, CapabilitiesResponse(
                    state_lifetime="persistent" if self.state_manager.persistent else "process",
                    language_capabilities=reports,
                ).model_dump())
                return
            if path == "/reviews":
                try:
                    result = self.state_manager.list_reviews(
                        limit=int(query.get("limit", ["50"])[0]),
                        offset=int(query.get("offset", ["0"])[0]),
                    )
                    self._send_json(HTTPStatus.OK, result.model_dump())
                except ValueError:
                    self._send_error_json(HTTPStatus.BAD_REQUEST, "Invalid pagination parameters", "bad_request")
                return

            # 1. GET /health
            if path == "/health":
                resp = HealthResponse(
                    status="ok",
                    service="codeatlas-service",
                    version=__version__,
                    timestamp=datetime.now(UTC).isoformat(),
                    pid=os.getpid(),
                    **self.state_manager.health_summary(),
                )
                self._send_json(HTTPStatus.OK, resp.model_dump())
                return

            # 2. GET /reviews/{run_id}/findings/{finding_id}
            m_detail = re.match(r"^/reviews/([^/]+)/findings/([^/]+)$", path)
            if m_detail:
                run_id = m_detail.group(1)
                finding_id = m_detail.group(2)
                try:
                    detail = self.state_manager.get_finding_detail(run_id, finding_id)
                    self._send_json(HTTPStatus.OK, detail.model_dump())
                except KeyError as err:
                    self._send_error_json(HTTPStatus.NOT_FOUND, str(err), "not_found")
                return

            # 2b. GET /reviews/{run_id}/findings/{finding_id}/fix-eligibility
            m_fix_elig = re.match(r"^/reviews/([^/]+)/findings/([^/]+)/fix-eligibility$", path)
            if m_fix_elig:
                run_id, finding_id = m_fix_elig.group(1), m_fix_elig.group(2)
                try:
                    eligibility = self.state_manager.get_fix_eligibility(run_id, finding_id)
                    self._send_json(HTTPStatus.OK, eligibility.model_dump())
                except KeyError as err:
                    self._send_error_json(HTTPStatus.NOT_FOUND, str(err), "not_found")
                except ServiceStateError as err:
                    self._send_error_json(HTTPStatus.CONFLICT, err.explanation, err.reason)
                except Exception:
                    self._send_error_json(HTTPStatus.BAD_REQUEST, "Cannot evaluate fix eligibility", "bad_request")
                return

            # 2c. GET /reviews/{run_id}/findings/{finding_id}/fix-proposal (latest)
            m_fix_latest = re.match(r"^/reviews/([^/]+)/findings/([^/]+)/fix-proposal$", path)
            if m_fix_latest:
                run_id, finding_id = m_fix_latest.group(1), m_fix_latest.group(2)
                try:
                    proposal = self.state_manager.get_fix_proposal(run_id, finding_id)
                    self._send_json(HTTPStatus.OK, proposal.model_dump())
                except KeyError as err:
                    self._send_error_json(HTTPStatus.NOT_FOUND, str(err), "not_found")
                except ServiceStateError as err:
                    self._send_error_json(HTTPStatus.NOT_FOUND, err.explanation, err.reason)
                except Exception:
                    self._send_error_json(HTTPStatus.BAD_REQUEST, "Cannot retrieve fix proposal", "bad_request")
                return

            # 2d. GET /fix-proposals/{proposal_id}?run_id=&finding_id= (scope-checked retrieval)
            m_fix_by_id = re.match(r"^/fix-proposals/([^/]+)$", path)
            if m_fix_by_id:
                proposal_id = m_fix_by_id.group(1)
                run_id = query.get("run_id", [None])[0]
                finding_id = query.get("finding_id", [None])[0]
                if not run_id or not finding_id:
                    self._send_error_json(
                        HTTPStatus.BAD_REQUEST,
                        "Both run_id and finding_id query parameters are required",
                        "bad_request",
                    )
                    return
                try:
                    proposal = self.state_manager.get_fix_proposal(run_id, finding_id, proposal_id)
                    self._send_json(HTTPStatus.OK, proposal.model_dump())
                except KeyError as err:
                    self._send_error_json(HTTPStatus.NOT_FOUND, str(err), "not_found")
                except ServiceStateError as err:
                    status = HTTPStatus.CONFLICT if err.reason == "scope_mismatch" else HTTPStatus.NOT_FOUND
                    self._send_error_json(status, err.explanation, err.reason)
                except Exception:
                    self._send_error_json(HTTPStatus.BAD_REQUEST, "Cannot retrieve fix proposal", "bad_request")
                return

            # 2e. GET .../fix-proposal/apply-history (latest proposal, or by ID below)
            m_fix_apply_hist = re.match(r"^/reviews/([^/]+)/findings/([^/]+)/fix-proposal/apply-history$", path)
            if m_fix_apply_hist:
                run_id, finding_id = m_fix_apply_hist.group(1), m_fix_apply_hist.group(2)
                try:
                    history = self.state_manager.get_fix_apply_history(run_id, finding_id)
                    self._send_json(HTTPStatus.OK, history.model_dump())
                except KeyError as err:
                    self._send_error_json(HTTPStatus.NOT_FOUND, str(err), "not_found")
                except ServiceStateError as err:
                    status = HTTPStatus.CONFLICT if err.reason == "scope_mismatch" else HTTPStatus.NOT_FOUND
                    self._send_error_json(status, err.explanation, err.reason)
                except Exception:
                    self._send_error_json(HTTPStatus.BAD_REQUEST, "Cannot retrieve apply history", "bad_request")
                return

            # 2f. GET /fix-proposals/{proposal_id}/apply-history?run_id=&finding_id=
            m_fix_apply_hist_by_id = re.match(r"^/fix-proposals/([^/]+)/apply-history$", path)
            if m_fix_apply_hist_by_id:
                proposal_id = m_fix_apply_hist_by_id.group(1)
                run_id = query.get("run_id", [None])[0]
                finding_id = query.get("finding_id", [None])[0]
                if not run_id or not finding_id:
                    self._send_error_json(
                        HTTPStatus.BAD_REQUEST,
                        "Both run_id and finding_id query parameters are required",
                        "bad_request",
                    )
                    return
                try:
                    history = self.state_manager.get_fix_apply_history(run_id, finding_id, proposal_id)
                    self._send_json(HTTPStatus.OK, history.model_dump())
                except KeyError as err:
                    self._send_error_json(HTTPStatus.NOT_FOUND, str(err), "not_found")
                except ServiceStateError as err:
                    status = HTTPStatus.CONFLICT if err.reason == "scope_mismatch" else HTTPStatus.NOT_FOUND
                    self._send_error_json(status, err.explanation, err.reason)
                except Exception:
                    self._send_error_json(HTTPStatus.BAD_REQUEST, "Cannot retrieve apply history", "bad_request")
                return

            # 3. GET /reviews/{run_id}/findings
            m_findings = re.match(r"^/reviews/([^/]+)/findings$", path)
            if m_findings:
                run_id = m_findings.group(1)
                severity = query.get("severity", [None])[0]
                category = query.get("category", [None])[0]
                status = query.get("status", [None])[0]
                include_dismissed = query.get("include_dismissed", ["false"])[0].lower() in {"1", "true", "yes"}

                try:
                    items = self.state_manager.get_findings(
                        run_id,
                        severity=severity,
                        category=category,
                        status=status,
                        include_dismissed=include_dismissed,
                    )
                    record = self.state_manager.get_review(run_id)
                    total = len(record.findings) if record else len(items)
                    self._send_json(
                        HTTPStatus.OK,
                        {
                            "run_id": run_id,
                            "total": total,
                            "filtered_total": len(items),
                            "findings": [item.model_dump() for item in items],
                        },
                    )
                except KeyError as err:
                    self._send_error_json(HTTPStatus.NOT_FOUND, str(err), "not_found")
                return

            # 4. GET /reviews/{run_id}
            feedback_export_match = re.fullmatch(r"/reviews/([^/]+)/beta-feedback", path)
            if feedback_export_match:
                try:
                    identity = query.get("repository_identity", [""])
                    if len(identity) != 1:
                        raise ValueError("invalid scope")
                    result = self.state_manager.export_beta_feedback(feedback_export_match.group(1), identity[0])
                    self._send_json(HTTPStatus.OK, result.model_dump())
                except KeyError:
                    self._send_error_json(HTTPStatus.NOT_FOUND, "Review not found", "not_found")
                except (ValueError, ServiceStateError):
                    self._send_error_json(HTTPStatus.BAD_REQUEST, "Invalid feedback scope", "scope_mismatch")
                return
            events_match = re.fullmatch(r"/reviews/([^/]+)/events", path)
            if events_match:
                try:
                    self._send_json(HTTPStatus.OK, self.state_manager.get_review_events(events_match.group(1)).model_dump())
                except KeyError:
                    self._send_error_json(HTTPStatus.NOT_FOUND, "Review not found", "not_found")
                return
            m_run = re.match(r"^/reviews/([^/]+)$", path)
            if m_run:
                run_id = m_run.group(1)
                try:
                    summary = self.state_manager.get_review_status(run_id)
                    self._send_json(HTTPStatus.OK, summary.model_dump())
                except KeyError as err:
                    self._send_error_json(HTTPStatus.NOT_FOUND, str(err), "not_found")
                return

            self._send_error_json(HTTPStatus.NOT_FOUND, f"Endpoint '{path}' not found", "not_found")
        except Exception:
            logger.exception("Internal error in GET")
            self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "Internal server error", "internal_error")

    def do_POST(self) -> None:
        """Route POST endpoints."""
        try:
            path = self._route_path()
            if path is None:
                return

            try:
                body = self._read_json_body()
            except OverflowError as err:
                self._send_error_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, str(err), "payload_too_large")
                return
            except ValueError as err:
                self._send_error_json(HTTPStatus.BAD_REQUEST, str(err), "bad_request")
                return
            if not self._validate_v1_body(path, body):
                return

            # 1. POST /reviews
            if path == "/reviews":
                try:
                    req = ReviewCreateRequest.model_validate(body)
                    status = self.state_manager.start_review(req)
                    self._send_json(HTTPStatus.CREATED, status.model_dump())
                except ServicePathError as err:
                    self._send_error_json(HTTPStatus.BAD_REQUEST, str(err), "invalid_path")
                except ServiceStateError as err:
                    status_code = HTTPStatus.TOO_MANY_REQUESTS if err.reason == "review_queue_full" else HTTPStatus.CONFLICT
                    self._send_error_json(status_code, err.explanation, err.reason)
                except Exception:
                    self._send_error_json(HTTPStatus.BAD_REQUEST, "Invalid review request", "bad_request")
                return

            # 2. POST /reviews/{run_id}/cancel
            retry = re.fullmatch(r"/reviews/([^/]+)/retry", path)
            if retry:
                try:
                    result = self.state_manager.retry_review(retry.group(1))
                    self._send_json(HTTPStatus.CREATED, result.model_dump())
                except KeyError:
                    self._send_error_json(HTTPStatus.NOT_FOUND, "Review not found", "not_found")
                except ServiceStateError as error:
                    code = HTTPStatus.TOO_MANY_REQUESTS if error.reason == "review_queue_full" else HTTPStatus.CONFLICT
                    self._send_error_json(code, error.explanation, error.reason)
                return
            m_cancel = re.match(r"^/reviews/([^/]+)/cancel$", path)
            if m_cancel:
                run_id = m_cancel.group(1)
                if self.state_manager.cancel_review(run_id):
                    current = self.state_manager.get_review_status(run_id)
                    self._send_json(
                        HTTPStatus.OK,
                        {"run_id": run_id, "status": current.status,
                         "message": "Review cancelled successfully" if current.status == "cancelled" else "Review already finished"},
                    )
                else:
                    self._send_error_json(HTTPStatus.NOT_FOUND, f"Review '{run_id}' not found", "not_found")
                return

            # 3. POST /findings/{finding_id}/explain
            m_explain = re.match(r"^/findings/([^/]+)/explain$", path)
            if m_explain:
                finding_id = m_explain.group(1)
                try:
                    req = ExplainRequest.model_validate(body)
                    explanation = self.state_manager.explain_finding(finding_id, run_id=req.run_id)
                    self._send_json(HTTPStatus.OK, explanation)
                except KeyError as err:
                    self._send_error_json(HTTPStatus.NOT_FOUND, str(err), "not_found")
                except Exception:
                    self._send_error_json(HTTPStatus.BAD_REQUEST, "Cannot explain finding", "bad_request")
                return

            # 4. POST /findings/{finding_id}/patch-proposal
            m_proposal = re.match(r"^/findings/([^/]+)/patch-proposal$", path)
            if m_proposal:
                finding_id = m_proposal.group(1)
                try:
                    req = PatchProposalRequest.model_validate(body)
                    proposal_resp = self.state_manager.create_patch_proposal(finding_id, run_id=req.run_id)
                    self._send_json(HTTPStatus.CREATED, proposal_resp.model_dump())
                except KeyError as err:
                    self._send_error_json(HTTPStatus.NOT_FOUND, str(err), "not_found")
                except Exception:
                    self._send_error_json(HTTPStatus.BAD_REQUEST, "Cannot propose fix", "bad_request")
                return

            # 5. POST /proposals/{proposal_id}/validate
            m_validate = re.match(r"^/proposals/([^/]+)/validate$", path)
            if m_validate:
                proposal_id = m_validate.group(1)
                try:
                    req = ValidateProposalRequest.model_validate(body)
                    val_resp = self.state_manager.validate_patch_proposal(
                        proposal_id,
                        approval_token=req.approval_token,
                        run_tests=req.run_tests,
                        run_full_suite=req.run_full_suite,
                    )
                    self._send_json(HTTPStatus.OK, val_resp.model_dump())
                except KeyError as err:
                    self._send_error_json(HTTPStatus.NOT_FOUND, str(err), "not_found")
                except Exception:
                    self._send_error_json(HTTPStatus.BAD_REQUEST, "Validation failed", "bad_request")
                return

            # 6. POST /reviews/{run_id}/findings/{finding_id}/dismiss
            m_dismiss = re.match(r"^/reviews/([^/]+)/findings/([^/]+)/dismiss$", path)
            if m_dismiss:
                run_id = m_dismiss.group(1)
                finding_id = m_dismiss.group(2)
                try:
                    dismissed = self.state_manager.toggle_dismiss_finding(run_id, finding_id)
                    self._send_json(
                        HTTPStatus.OK,
                        {"run_id": run_id, "finding_id": finding_id, "dismissed": dismissed},
                    )
                except KeyError as err:
                    self._send_error_json(HTTPStatus.NOT_FOUND, str(err), "not_found")
                return

            # 7. POST /reviews/{run_id}/findings/{finding_id}/feedback
            m_feedback = re.match(r"^/reviews/([^/]+)/findings/([^/]+)/feedback$", path)
            if m_feedback:
                run_id = m_feedback.group(1)
                finding_id = m_feedback.group(2)
                try:
                    req = FindingFeedbackRequest.model_validate(body)
                    result = self.state_manager.set_finding_feedback(
                        run_id, finding_id, req.feedback, outcome=req.outcome, reason=req.reason,
                    )
                    self._send_json(HTTPStatus.OK, result.model_dump())
                except KeyError as err:
                    self._send_error_json(HTTPStatus.NOT_FOUND, str(err), "not_found")
                except Exception:
                    self._send_error_json(HTTPStatus.BAD_REQUEST, "Invalid finding feedback", "bad_request")
                return

            m_fix_feedback = re.match(r"^/reviews/([^/]+)/findings/([^/]+)/fix-proposal/feedback$", path)
            if m_fix_feedback:
                run_id, finding_id = m_fix_feedback.group(1), m_fix_feedback.group(2)
                try:
                    req = FixProposalFeedbackRequest.model_validate(body)
                    if req.run_id != run_id or req.finding_id != finding_id:
                        self._send_error_json(HTTPStatus.BAD_REQUEST, "Feedback scope does not match the URL", "scope_mismatch")
                        return
                    result = self.state_manager.set_fix_proposal_feedback(
                        run_id, finding_id, req.proposal_id, req.feedback, outcome=req.outcome, reason=req.reason,
                    )
                    self._send_json(HTTPStatus.OK, result.model_dump())
                except KeyError:
                    self._send_error_json(HTTPStatus.NOT_FOUND, "Feedback target not found", "not_found")
                except ServiceStateError as err:
                    status = HTTPStatus.CONFLICT if err.reason == "scope_mismatch" else HTTPStatus.BAD_REQUEST
                    self._send_error_json(status, err.explanation, err.reason)
                except Exception:
                    self._send_error_json(HTTPStatus.BAD_REQUEST, "Invalid FixProposal feedback", "bad_request")
                return

            # 7b. POST .../fix-proposal/apply — explicit apply of a validated proposal
            m_fix_apply = re.match(r"^/reviews/([^/]+)/findings/([^/]+)/fix-proposal/apply$", path)
            if m_fix_apply:
                run_id, finding_id = m_fix_apply.group(1), m_fix_apply.group(2)
                try:
                    req = FixApplyRequest.model_validate(body)
                    if req.run_id != run_id or req.finding_id != finding_id:
                        self._send_error_json(
                            HTTPStatus.BAD_REQUEST,
                            "Apply request scope does not match the URL",
                            "scope_mismatch",
                        )
                        return
                    applied = self.state_manager.apply_fix_proposal(
                        run_id,
                        finding_id,
                        req.proposal_id,
                        approval_token=req.approval_token,
                        confirmed_patch_hash=req.confirmed_patch_hash,
                    )
                    self._send_json(HTTPStatus.OK, applied.model_dump())
                except KeyError as err:
                    self._send_error_json(HTTPStatus.NOT_FOUND, str(err), "not_found")
                except ServiceStateError as err:
                    status = HTTPStatus.CONFLICT if err.reason in FIX_CONFLICT_REASONS else HTTPStatus.BAD_REQUEST
                    self._send_error_json(status, err.explanation, err.reason)
                except Exception:
                    self._send_error_json(HTTPStatus.BAD_REQUEST, "Invalid FixProposal apply request", "bad_request")
                return

            # 7c. POST .../fix-proposal/revert — safe revert of an applied proposal
            m_fix_revert = re.match(r"^/reviews/([^/]+)/findings/([^/]+)/fix-proposal/revert$", path)
            if m_fix_revert:
                run_id, finding_id = m_fix_revert.group(1), m_fix_revert.group(2)
                try:
                    req = FixRevertRequest.model_validate(body)
                    if req.run_id != run_id or req.finding_id != finding_id:
                        self._send_error_json(
                            HTTPStatus.BAD_REQUEST,
                            "Revert request scope does not match the URL",
                            "scope_mismatch",
                        )
                        return
                    reverted = self.state_manager.revert_fix_apply(
                        run_id,
                        finding_id,
                        req.proposal_id,
                        confirmed_patch_hash=req.confirmed_patch_hash,
                    )
                    self._send_json(HTTPStatus.OK, reverted.model_dump())
                except KeyError as err:
                    self._send_error_json(HTTPStatus.NOT_FOUND, str(err), "not_found")
                except ServiceStateError as err:
                    status = HTTPStatus.CONFLICT if err.reason in FIX_CONFLICT_REASONS else HTTPStatus.BAD_REQUEST
                    self._send_error_json(status, err.explanation, err.reason)
                except Exception:
                    self._send_error_json(HTTPStatus.BAD_REQUEST, "Invalid FixProposal revert request", "bad_request")
                return

            # 8. POST /reviews/{run_id}/findings/{finding_id}/fix-proposal (generate)
            m_fix_by_id_approve = re.match(r"^/fix-proposals/([^/]+)/approve-validation$", path)
            if m_fix_by_id_approve:
                proposal_id = m_fix_by_id_approve.group(1)
                try:
                    if body.get("proposal_id") not in {None, proposal_id}:
                        self._send_error_json(HTTPStatus.BAD_REQUEST, "Proposal ID does not match the URL", "scope_mismatch")
                        return
                    req = FixValidationApprovalRequest.model_validate({**body, "proposal_id": proposal_id})
                    approval = self.state_manager.approve_fix_proposal_for_validation(
                        req.run_id, req.finding_id, proposal_id, approval_token=req.approval_token,
                    )
                    self._send_json(HTTPStatus.OK, approval.model_dump())
                except KeyError as err:
                    self._send_error_json(HTTPStatus.NOT_FOUND, str(err), "not_found")
                except ServiceStateError as err:
                    status = HTTPStatus.CONFLICT if err.reason in {"scope_mismatch", "validation_not_available", "recovered_operation_unavailable"} else HTTPStatus.BAD_REQUEST
                    self._send_error_json(status, err.explanation, err.reason)
                except Exception:
                    self._send_error_json(HTTPStatus.BAD_REQUEST, "Invalid FixProposal approval request", "bad_request")
                return

            m_fix_by_id_validate = re.match(r"^/fix-proposals/([^/]+)/validate$", path)
            if m_fix_by_id_validate:
                proposal_id = m_fix_by_id_validate.group(1)
                try:
                    if body.get("proposal_id") not in {None, proposal_id}:
                        self._send_error_json(HTTPStatus.BAD_REQUEST, "Proposal ID does not match the URL", "scope_mismatch")
                        return
                    req = FixValidationRequest.model_validate({**body, "proposal_id": proposal_id})
                    validation = self.state_manager.validate_fix_proposal(
                        req.run_id, req.finding_id, proposal_id,
                        approval_token=req.approval_token,
                        run_tests=req.run_tests,
                        run_full_suite=req.run_full_suite,
                    )
                    self._send_json(HTTPStatus.OK, validation.model_dump())
                except KeyError as err:
                    self._send_error_json(HTTPStatus.NOT_FOUND, str(err), "not_found")
                except ServiceStateError as err:
                    status = HTTPStatus.CONFLICT if err.reason in {"scope_mismatch", "validation_not_approved", "validation_not_available", "recovered_operation_unavailable"} else HTTPStatus.BAD_REQUEST
                    self._send_error_json(status, err.explanation, err.reason)
                except Exception:
                    self._send_error_json(HTTPStatus.BAD_REQUEST, "Invalid FixProposal validation request", "bad_request")
                return

            m_fix_approve = re.match(r"^/reviews/([^/]+)/findings/([^/]+)/fix-proposal/approve-validation$", path)
            if m_fix_approve:
                run_id, finding_id = m_fix_approve.group(1), m_fix_approve.group(2)
                try:
                    req = FixValidationApprovalRequest.model_validate(body)
                    if req.run_id != run_id or req.finding_id != finding_id:
                        self._send_error_json(
                            HTTPStatus.BAD_REQUEST,
                            "Approval request scope does not match the URL",
                            "scope_mismatch",
                        )
                        return
                    approval = self.state_manager.approve_fix_proposal_for_validation(
                        run_id,
                        finding_id,
                        req.proposal_id,
                        approval_token=req.approval_token,
                    )
                    self._send_json(HTTPStatus.OK, approval.model_dump())
                except KeyError as err:
                    self._send_error_json(HTTPStatus.NOT_FOUND, str(err), "not_found")
                except ServiceStateError as err:
                    status = HTTPStatus.CONFLICT if err.reason in {"scope_mismatch", "validation_not_available", "recovered_operation_unavailable"} else HTTPStatus.BAD_REQUEST
                    self._send_error_json(status, err.explanation, err.reason)
                except Exception:
                    self._send_error_json(HTTPStatus.BAD_REQUEST, "Invalid FixProposal approval request", "bad_request")
                return

            m_fix_validate = re.match(r"^/reviews/([^/]+)/findings/([^/]+)/fix-proposal/validate$", path)
            if m_fix_validate:
                run_id, finding_id = m_fix_validate.group(1), m_fix_validate.group(2)
                try:
                    req = FixValidationRequest.model_validate(body)
                    if req.run_id != run_id or req.finding_id != finding_id:
                        self._send_error_json(
                            HTTPStatus.BAD_REQUEST,
                            "Validation request scope does not match the URL",
                            "scope_mismatch",
                        )
                        return
                    validation = self.state_manager.validate_fix_proposal(
                        run_id,
                        finding_id,
                        req.proposal_id,
                        approval_token=req.approval_token,
                        run_tests=req.run_tests,
                        run_full_suite=req.run_full_suite,
                    )
                    self._send_json(HTTPStatus.OK, validation.model_dump())
                except KeyError as err:
                    self._send_error_json(HTTPStatus.NOT_FOUND, str(err), "not_found")
                except ServiceStateError as err:
                    status = HTTPStatus.CONFLICT if err.reason in {"scope_mismatch", "validation_not_approved", "validation_not_available", "recovered_operation_unavailable"} else HTTPStatus.BAD_REQUEST
                    self._send_error_json(status, err.explanation, err.reason)
                except Exception:
                    self._send_error_json(HTTPStatus.BAD_REQUEST, "Invalid FixProposal validation request", "bad_request")
                return

            m_fix_generate = re.match(r"^/reviews/([^/]+)/findings/([^/]+)/fix-proposal$", path)
            if m_fix_generate:
                run_id, finding_id = m_fix_generate.group(1), m_fix_generate.group(2)
                try:
                    req = FixProposalRequest.model_validate(body)
                    if req.run_id != run_id:
                        self._send_error_json(
                            HTTPStatus.BAD_REQUEST,
                            "Request run_id does not match the review run in the URL",
                            "scope_mismatch",
                        )
                        return
                    proposal = self.state_manager.request_fix_proposal(run_id, finding_id)
                    self._send_json(HTTPStatus.CREATED, proposal.model_dump())
                except KeyError as err:
                    self._send_error_json(HTTPStatus.NOT_FOUND, str(err), "not_found")
                except ServiceStateError as err:
                    self._send_error_json(HTTPStatus.CONFLICT, err.explanation, err.reason)
                except Exception:
                    self._send_error_json(HTTPStatus.BAD_REQUEST, "Cannot generate fix proposal", "bad_request")
                return

            # 9. POST /reviews/{run_id}/findings/{finding_id}/fix-proposal/reject
            m_fix_reject = re.match(r"^/reviews/([^/]+)/findings/([^/]+)/fix-proposal/reject$", path)
            if m_fix_reject:
                run_id, finding_id = m_fix_reject.group(1), m_fix_reject.group(2)
                try:
                    req = FixRejectRequest.model_validate(body)
                    if req.run_id != run_id:
                        self._send_error_json(
                            HTTPStatus.BAD_REQUEST,
                            "Request run_id does not match the review run in the URL",
                            "scope_mismatch",
                        )
                        return
                    proposal = self.state_manager.reject_fix_proposal(run_id, finding_id, req.proposal_id)
                    self._send_json(HTTPStatus.OK, proposal.model_dump())
                except KeyError as err:
                    self._send_error_json(HTTPStatus.NOT_FOUND, str(err), "not_found")
                except ServiceStateError as err:
                    status = HTTPStatus.CONFLICT if err.reason == "scope_mismatch" else HTTPStatus.BAD_REQUEST
                    self._send_error_json(status, err.explanation, err.reason)
                except Exception:
                    self._send_error_json(HTTPStatus.BAD_REQUEST, "Cannot reject fix proposal", "bad_request")
                return

            # 10. POST /reviews/{run_id}/findings/{finding_id}/fix-proposal/regenerate
            m_fix_regen = re.match(r"^/reviews/([^/]+)/findings/([^/]+)/fix-proposal/regenerate$", path)
            if m_fix_regen:
                run_id, finding_id = m_fix_regen.group(1), m_fix_regen.group(2)
                try:
                    req = FixProposalRequest.model_validate(body)
                    if req.run_id != run_id:
                        self._send_error_json(
                            HTTPStatus.BAD_REQUEST,
                            "Request run_id does not match the review run in the URL",
                            "scope_mismatch",
                        )
                        return
                    proposal = self.state_manager.regenerate_fix_proposal(run_id, finding_id)
                    self._send_json(HTTPStatus.CREATED, proposal.model_dump())
                except KeyError as err:
                    self._send_error_json(HTTPStatus.NOT_FOUND, str(err), "not_found")
                except ServiceStateError as err:
                    self._send_error_json(HTTPStatus.CONFLICT, err.explanation, err.reason)
                except Exception:
                    self._send_error_json(HTTPStatus.BAD_REQUEST, "Cannot regenerate fix proposal", "bad_request")
                return

            self._send_error_json(HTTPStatus.NOT_FOUND, f"Endpoint '{path}' not found", "not_found")
        except Exception as err:
            logger.exception("Internal error in POST: %s", err)
            self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "Internal server error", "internal_error")


class CodeAtlasServer(ThreadingHTTPServer):
    """HTTP Server hosting CodeAtlas local review service."""

    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        state_manager: ReviewStateManager | None = None,
        database_path: str | Path | None = None,
    ) -> None:
        if state_manager is not None and database_path is not None:
            raise ValueError("Choose state_manager or database_path, not both")
        self.state_manager = state_manager or ReviewStateManager(persistence_path=database_path)

        # Each server owns its handler state; two embedded services must not share runs.
        handler = type("BoundReviewHttpHandler", (ReviewHttpHandler,), {"state_manager": self.state_manager})
        if ":" in server_address[0]:
            self.address_family = socket.AF_INET6
        super().__init__(server_address, handler)

    def server_close(self) -> None:
        try:
            super().server_close()
        finally:
            self.state_manager.close()


def create_server(
    host: str = "127.0.0.1",
    port: int = 8765,
    state_manager: ReviewStateManager | None = None,
    database_path: str | Path | None = None,
) -> CodeAtlasServer:
    """Create and configure a local CodeAtlas HTTP server."""
    if host not in {"127.0.0.1", "localhost", "::1"}:
        logger.warning("CodeAtlas service is binding to non-localhost address '%s'", host)
    return CodeAtlasServer((host, port), state_manager=state_manager, database_path=database_path)


def run_service(
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    repair_config: str | Path | None = None,
    database_path: str | Path | None = None,
) -> None:
    """Start and run the CodeAtlas service indefinitely."""
    manager = None
    if repair_config is not None:
        from codeatlas.providers import (
            LiveRepairProvider,
            ProviderConfigError,
            load_repair_provider_config,
        )

        try:
            config = load_repair_provider_config(repair_config)
        except Exception:
            raise ProviderConfigError("Invalid repair provider configuration") from None
        manager = ReviewStateManager(
            repair_provider=LiveRepairProvider(config),
            persistence_path=database_path or default_database_path(),
        )
    elif database_path is not None:
        manager = ReviewStateManager(persistence_path=database_path)
    else:
        manager = ReviewStateManager(persistence_path=default_database_path())
    server = create_server(host, port, state_manager=manager)
    display_host = f"[{host}]" if ":" in host else host
    print(f"CodeAtlas service listening on http://{display_host}:{port}{API_PREFIX} (press Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down CodeAtlas service...")
    finally:
        server.server_close()
