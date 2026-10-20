"""Versioned, client-independent HTTP contract, shared with OpenAPI discovery."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from codeatlas import __version__
from codeatlas.beta_feedback import BetaFeedbackExport
from codeatlas.service import models as m

API_VERSION = "1"
API_PREFIX = "/api/v1"


@dataclass(frozen=True)
class Endpoint:
    method: str
    path: str
    operation: str
    response: type[BaseModel] | None = None
    request: type[BaseModel] | None = None
    status: int = 200
    query: tuple[str, ...] = ()

    def matches(self, method: str, path: str) -> bool:
        pattern = re.sub(r"\{[^}]+\}", r"[^/]+", self.path)
        return self.method == method and re.fullmatch(pattern, path) is not None


FINDING = "/reviews/{run_id}/findings/{finding_id}"
FIX = FINDING + "/fix-proposal"
ENDPOINTS = (
    Endpoint("GET", "/health", "health", m.HealthResponse),
    Endpoint("GET", "/diagnostics/storage", "storageHealth", m.StorageHealthResponse),
    Endpoint("GET", "/capabilities", "capabilities", m.CapabilitiesResponse),
    Endpoint("GET", "/openapi.json", "openapi"),
    Endpoint("GET", "/reviews", "listReviews", m.ReviewsListResponse, query=("limit", "offset")),
    Endpoint("POST", "/reviews", "startReview", m.ReviewStatusResponse, m.ReviewCreateRequest, 201),
    Endpoint("GET", "/reviews/{run_id}", "getReview", m.ReviewStatusResponse),
    Endpoint("GET", "/reviews/{run_id}/events", "getReviewEvents", m.ReviewEventsResponse),
    Endpoint("GET", "/reviews/{run_id}/beta-feedback", "exportBetaFeedback", BetaFeedbackExport,
             query=("repository_identity",)),
    Endpoint("POST", "/reviews/{run_id}/cancel", "cancelReview", m.CancelResponse),
    Endpoint("POST", "/reviews/{run_id}/retry", "retryReview", m.ReviewStatusResponse, status=201),
    Endpoint("GET", "/reviews/{run_id}/findings", "listFindings", m.FindingsListResponse,
             query=("severity", "category", "status", "include_dismissed")),
    Endpoint("GET", FINDING, "getFinding", m.FindingDetailResponse),
    Endpoint("POST", FINDING + "/feedback", "setFeedback", m.FindingFeedbackResponse, m.FindingFeedbackRequest),
    Endpoint("POST", FINDING + "/dismiss", "toggleDismiss", m.DismissResponse),
    Endpoint("POST", "/findings/{finding_id}/explain", "explainFinding", m.ExplainResponse, m.ExplainRequest),
    Endpoint("POST", "/findings/{finding_id}/patch-proposal", "proposePatch",
             m.PatchProposalResponse, m.PatchProposalRequest, 201),
    Endpoint("POST", "/proposals/{proposal_id}/validate", "validatePatch",
             m.ValidateProposalResponse, m.ValidateProposalRequest),
    Endpoint("GET", FINDING + "/fix-eligibility", "fixEligibility", m.FixEligibilityResponse),
    Endpoint("GET", FIX, "getLatestFix", m.FixProposalResponse),
    Endpoint("POST", FIX, "generateFix", m.FixProposalResponse, m.FixProposalRequest, 201),
    Endpoint("POST", FIX + "/regenerate", "regenerateFix", m.FixProposalResponse, m.FixProposalRequest, 201),
    Endpoint("POST", FIX + "/reject", "rejectFix", m.FixProposalResponse, m.FixRejectRequest),
    Endpoint("POST", FIX + "/feedback", "setFixFeedback",
             m.FixProposalFeedbackResponse, m.FixProposalFeedbackRequest),
    Endpoint("POST", FIX + "/approve-validation", "approveFixValidation",
             m.FixValidationApprovalResponse, m.FixValidationApprovalRequest),
    Endpoint("POST", FIX + "/validate", "validateFix", m.FixValidationResponse, m.FixValidationRequest),
    Endpoint("POST", FIX + "/apply", "applyFix", m.FixApplyResponse, m.FixApplyRequest),
    Endpoint("POST", FIX + "/revert", "revertFix", m.FixApplyResponse, m.FixRevertRequest),
    Endpoint("GET", FIX + "/apply-history", "latestFixHistory", m.FixApplyHistoryResponse),
    Endpoint("GET", "/fix-proposals/{proposal_id}", "getFix", m.FixProposalResponse,
             query=("run_id", "finding_id")),
    Endpoint("GET", "/fix-proposals/{proposal_id}/apply-history", "fixHistory", m.FixApplyHistoryResponse,
             query=("run_id", "finding_id")),
    Endpoint("POST", "/fix-proposals/{proposal_id}/approve-validation", "approveFixValidationById",
             m.FixValidationApprovalResponse, m.FixValidationApprovalRequest),
    Endpoint("POST", "/fix-proposals/{proposal_id}/validate", "validateFixById",
             m.FixValidationResponse, m.FixValidationRequest),
)


def endpoint_for(method: str, path: str) -> Endpoint | None:
    return next((item for item in ENDPOINTS if item.matches(method, path)), None)


def openapi_document() -> dict[str, Any]:
    """Build OpenAPI 3.1 from the same Pydantic models used by the server."""
    schemas: dict[str, Any] = {}

    def schema(model: type[BaseModel] | None, *, request: bool = False) -> dict[str, Any]:
        if model is None:
            return {"type": "object"}
        name = model.__name__
        if name not in schemas:
            value = model.model_json_schema(ref_template="#/components/schemas/{model}")
            schemas.update(value.pop("$defs", {}))
            schemas[name] = value
        if request:
            schemas[name]["additionalProperties"] = False
        return {"$ref": f"#/components/schemas/{name}"}

    paths: dict[str, Any] = {}
    error = {"description": "Request failed; inspect the stable error code.",
             "content": {"application/json": {"schema": schema(m.ErrorResponse)}}}
    for item in ENDPOINTS:
        parameters = [
            {"name": name, "in": "path", "required": True, "schema": {"type": "string"}}
            for name in re.findall(r"\{([^}]+)\}", item.path)
        ]
        for name in item.query:
            query_schema: dict[str, Any] = {"type": "string"}
            if name in {"limit", "offset"}:
                query_schema = {"type": "integer", "minimum": 1 if name == "limit" else 0}
                if name == "limit":
                    query_schema["maximum"] = 200
            elif name == "include_dismissed":
                query_schema = {"type": "boolean", "default": False}
            parameters.append({"name": name, "in": "query", "schema": query_schema,
                               "required": name in {"run_id", "finding_id"}})
        operation: dict[str, Any] = {
            "operationId": item.operation,
            "parameters": parameters,
            "responses": {
                str(item.status): {"description": "Success", "content": {
                    "application/json": {"schema": schema(item.response)}}},
                **{str(code): error for code in (400, 404, 405, 409, 413, 429, 500)},
            },
        }
        if item.operation == "storageHealth":
            operation["description"] = "Read-only, bounded storage observations; loopback peers only."
            operation["responses"]["403"] = error
        if item.request:
            operation["requestBody"] = {"required": True, "content": {
                "application/json": {"schema": schema(item.request, request=True)}}}
        paths.setdefault(item.path, {})[item.method.lower()] = operation
    return {
        "openapi": "3.1.0",
        "info": {"title": "CodeAtlas local API", "version": "1.0.0",
                 "description": f"Client-independent review and repair API. Service {__version__}."},
        "servers": [{"url": API_PREFIX}],
        "paths": paths,
        "components": {"schemas": schemas},
    }
