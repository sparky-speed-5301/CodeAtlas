"""CLI adapters for the public local API; domain decisions remain in the core."""

from __future__ import annotations

import json
import platform
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from functools import wraps
from pathlib import Path
from typing import Any, Callable

import typer
from pydantic import BaseModel, ValidationError

from codeatlas import __version__
from codeatlas.client import ClientError, CodeAtlasClient
from codeatlas.patching.proposal import generate_validation_approval_token
from codeatlas.service import models as m
from codeatlas.service.contract import openapi_document
from codeatlas.git.executable import git_executable


class FailOn(str, Enum):
    none = "none"
    info = "info"
    low = "low"
    medium = "medium"
    high = "high"
    blocker = "blocker"


def findings_fail(findings: list[dict[str, Any]], threshold: FailOn) -> bool:
    levels = ["info", "low", "medium", "high", "blocker"]
    if threshold == FailOn.none:
        return False
    return any(
        f.get("severity") in levels[levels.index(threshold.value):]
        and f.get("status") != "abstained"
        and f.get("quality_decision") not in {"abstain", "suppress_duplicate", "suppress_low_evidence"}
        for f in findings
    )


@dataclass
class Options:
    service_url: str
    timeout: float
    json_output: bool


def options(ctx: typer.Context) -> Options:
    return ctx.find_root().obj


def client(ctx: typer.Context) -> CodeAtlasClient:
    opts = options(ctx)
    return CodeAtlasClient(opts.service_url, timeout=opts.timeout)


def emit(ctx: typer.Context, result: BaseModel | dict[str, Any]) -> None:
    data = result.model_dump(mode="json") if isinstance(result, BaseModel) else result
    if options(ctx).json_output:
        typer.echo(json.dumps(data, indent=2, sort_keys=True, allow_nan=False))
        return
    for key, value in data.items():
        rendered = json.dumps(value, indent=2, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
        typer.echo(f"{key.replace('_', ' ').capitalize()}: {rendered}")


def guarded(function: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        ctx = kwargs["ctx"]
        try:
            return function(*args, **kwargs)
        except (ClientError, ValidationError) as error:
            code = error.code if isinstance(error, ClientError) else "invalid_request"
            message = str(error) if isinstance(error, ClientError) else "Request does not match the API schema"
            data = {"error": code, "message": message}
            typer.echo(json.dumps(data) if options(ctx).json_output else f"Error [{code}]: {message}", err=True)
            raise typer.Exit(1) from None
    return wrapped


reviews_app = typer.Typer(help="Create and inspect review runs in the local service.")
findings_app = typer.Typer(help="Inspect findings and submit explicit feedback.")
fix_app = typer.Typer(help="Generate, inspect, validate, apply, and revert scoped fixes.")


def register(app: typer.Typer) -> None:
    app.add_typer(reviews_app, name="reviews")
    app.add_typer(findings_app, name="findings")
    app.add_typer(fix_app, name="fix")

    @app.callback()
    def main(
        ctx: typer.Context,
        service_url: str = typer.Option("http://127.0.0.1:8765", "--service-url", envvar="CODEATLAS_SERVICE_URL",
                                       help="Local service origin for reviews, findings, fix, and health."),
        request_timeout: float = typer.Option(30.0, "--request-timeout", min=0.1, help="HTTP request timeout in seconds."),
        json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON for platform commands and review."),
        version: bool = typer.Option(False, "--version", is_eager=True, callback=_version),
    ) -> None:
        """Evidence-first review and repair for CLI, CI, and IDE clients."""
        ctx.obj = Options(service_url, request_timeout, json_output)

    @app.command()
    @guarded
    def health(ctx: typer.Context) -> None:
        """Check local service health and API version."""
        emit(ctx, client(ctx).health())

    @app.command()
    @guarded
    def capabilities(ctx: typer.Context) -> None:
        """Discover API capabilities and state lifetime."""
        emit(ctx, client(ctx).capabilities())

    @app.command()
    def doctor(ctx: typer.Context) -> None:
        """Check the installed CLI's local runtime without contacting a service."""
        checks: list[dict[str, str]] = []

        python_ok = sys.version_info >= (3, 11)
        checks.append({
            "name": "python",
            "status": "ok" if python_ok else "error",
            "detail": platform.python_version(),
        })

        try:
            git_path = git_executable()
            git_result = subprocess.run(
                [str(git_path), "--version"], capture_output=True, text=True,
                check=False, timeout=5,
            )
            git_ok = git_result.returncode == 0
            git_detail = git_result.stdout.strip().splitlines()[0] if git_ok and git_result.stdout.strip() else "Git is unavailable"
        except (OSError, subprocess.SubprocessError):
            git_ok = False
            git_detail = "Git is unavailable"
        checks.append({"name": "git", "status": "ok" if git_ok else "error", "detail": git_detail})

        try:
            openapi = openapi_document()
            contract_ok = openapi.get("openapi") == "3.1.0" and bool(openapi.get("paths"))
            contract_detail = "OpenAPI v1 contract loaded" if contract_ok else "OpenAPI contract is incomplete"
        except Exception:
            contract_ok = False
            contract_detail = "OpenAPI contract could not be loaded"
        checks.append({"name": "api_contract", "status": "ok" if contract_ok else "error", "detail": contract_detail})

        result = {
            "status": "ok" if python_ok and git_ok and contract_ok else "error",
            "version": __version__,
            "python": platform.python_version(),
            "platform": platform.platform(),
            "checks": checks,
            "service_required": False,
        }
        emit(ctx, result)
        if result["status"] != "ok":
            raise typer.Exit(1)


def _version(value: bool) -> None:
    if value:
        typer.echo(f"CodeAtlas {__version__}")
        raise typer.Exit()


@reviews_app.command("start")
@guarded
def start_review(
    ctx: typer.Context,
    repo: str = typer.Option(".", "--repo"),
    base: str = typer.Option("main", "--base"),
    head: str = typer.Option("HEAD", "--head"),
    review_provider: str | None = typer.Option(None, "--review-provider"),
    provider_model: str | None = typer.Option(None, "--provider"),
    provider_timeout: float | None = typer.Option(None, "--provider-timeout", min=0.1, max=300),
    index_repository: bool = typer.Option(True, "--index-repository/--no-index-repository"),
    assemble_review_packet: bool = typer.Option(True, "--assemble-review-packet/--no-assemble-review-packet"),
    allow_patch_suggestions: bool = typer.Option(False, "--allow-patch-suggestions"),
    max_findings: int = typer.Option(50, "--max-findings", min=1, max=200),
    wait: bool = typer.Option(False, "--wait", help="Wait for the terminal review status."),
    wait_timeout: float = typer.Option(300.0, "--wait-timeout", min=0.1),
) -> None:
    """Start a service-managed review; its run ID can be used by any client."""
    api = client(ctx)
    request = m.ReviewCreateRequest.model_validate({
        "repo": str(Path(repo).expanduser().resolve()), "base": base, "head": head,
        "review_provider": review_provider, "provider_model": provider_model,
        "provider_timeout": provider_timeout, "index_repository": index_repository,
        "assemble_review_packet": assemble_review_packet, "allow_patch_suggestions": allow_patch_suggestions,
        "max_findings": max_findings,
    })
    result = api.start_review(request)
    if wait:
        try:
            result = api.wait_for_review(result.run_id, timeout=wait_timeout)
        except ClientError:
            # Preserve the created run ID even when polling fails or times out.
            emit(ctx, result)
            raise
    emit(ctx, result)
    if result.status in {"failed", "cancelled"}:
        raise typer.Exit(1)


@reviews_app.command("list")
@guarded
def list_reviews(ctx: typer.Context, limit: int = typer.Option(50, min=1, max=200),
                 offset: int = typer.Option(0, min=0)) -> None:
    """List this service session's runs, newest first."""
    emit(ctx, client(ctx).list_reviews(limit=limit, offset=offset))


@reviews_app.command("status")
@guarded
def review_status(ctx: typer.Context, run_id: str) -> None:
    """Fetch the status of a run."""
    result = client(ctx).get_review(run_id)
    emit(ctx, result)
    if result.status in {"failed", "cancelled"}:
        raise typer.Exit(1)


@reviews_app.command("wait")
@guarded
def wait_review(ctx: typer.Context, run_id: str,
                wait_timeout: float = typer.Option(300.0, "--wait-timeout", min=0.1)) -> None:
    """Wait for completion, with a bounded overall deadline."""
    result = client(ctx).wait_for_review(run_id, timeout=wait_timeout)
    emit(ctx, result)
    if result.status in {"failed", "cancelled"}:
        raise typer.Exit(1)


@reviews_app.command("cancel")
@guarded
def cancel_review(ctx: typer.Context, run_id: str) -> None:
    """Request cooperative cancellation."""
    emit(ctx, client(ctx).cancel_review(run_id))


@findings_app.command("list")
@guarded
def list_findings(
    ctx: typer.Context, run_id: str,
    severity: str | None = typer.Option(None),
    category: str | None = typer.Option(None),
    status: str | None = typer.Option(None),
    include_dismissed: bool = typer.Option(False, "--include-dismissed"),
) -> None:
    """List bounded findings, optionally filtered by severity, category, or status."""
    emit(ctx, client(ctx).get_findings(run_id, severity=severity, category=category,
                                      status=status, include_dismissed=include_dismissed))


@findings_app.command("show")
@guarded
def show_finding(ctx: typer.Context, run_id: str, finding_id: str) -> None:
    """Show one finding's evidence and repository context."""
    emit(ctx, client(ctx).get_finding(run_id, finding_id))


@findings_app.command("explain")
@guarded
def explain_finding(ctx: typer.Context, run_id: str, finding_id: str) -> None:
    """Get the core's deterministic finding explanation."""
    emit(ctx, client(ctx).explain_finding(run_id, finding_id))


@findings_app.command("feedback")
@guarded
def feedback(ctx: typer.Context, run_id: str, finding_id: str, label: str) -> None:
    """Set a feedback label, or use 'clear' to remove feedback."""
    request = m.FindingFeedbackRequest.model_validate({"feedback": None if label == "clear" else label})
    emit(ctx, client(ctx).set_feedback(run_id, finding_id, request.feedback))


@fix_app.callback()
def fix_scope(ctx: typer.Context,
              run_id: str = typer.Option(..., "--run-id"),
              finding_id: str = typer.Option(..., "--finding-id"),
              proposal_id: str | None = typer.Option(None, "--proposal-id")) -> None:
    """Scope every fix operation to a review and finding; mutations also require a proposal ID."""
    ctx.meta["fix_scope"] = (run_id, finding_id, proposal_id)


def scope(ctx: typer.Context, *, proposal_required: bool = False) -> tuple[str, str, str | None]:
    run_id, finding_id, proposal_id = ctx.meta["fix_scope"]
    if proposal_required and not proposal_id:
        raise typer.BadParameter("This operation requires fix --proposal-id")
    return run_id, finding_id, proposal_id


def proposal(ctx: typer.Context) -> m.FixProposalResponse:
    return client(ctx).get_fix(*scope(ctx, proposal_required=True))


def token_for(prop: m.FixProposalResponse, operation: str) -> str:
    return generate_validation_approval_token(
        proposal_id=prop.proposal_id, finding_id=prop.finding_id, run_id=prop.run_id,
        repository_identity=prop.repository, base_commit=prop.base_commit, head_commit=prop.head_commit,
        patch_hash=prop.patch_hash, target_files=prop.target_files, operation=operation,
    )


def identity(prop: m.FixProposalResponse) -> dict[str, str]:
    return {"run_id": prop.run_id, "finding_id": prop.finding_id, "proposal_id": prop.proposal_id}


@fix_app.command("eligibility")
@guarded
def eligibility(ctx: typer.Context) -> None:
    """Explain whether a finding is eligible for a fix."""
    run_id, finding_id, _ = scope(ctx)
    emit(ctx, client(ctx).fix_eligibility(run_id, finding_id))


@fix_app.command("generate")
@guarded
def generate(ctx: typer.Context) -> None:
    """Generate a bounded draft fix using the service's configured provider."""
    run_id, finding_id, _ = scope(ctx)
    result = client(ctx).generate_fix(run_id, finding_id)
    emit(ctx, result)
    if result.generation_status != "draft_ready":
        raise typer.Exit(1)


@fix_app.command("inspect")
@guarded
def inspect_fix(ctx: typer.Context) -> None:
    """Show the proposal, full patch hash, patch preview, and validation evidence."""
    emit(ctx, client(ctx).get_fix(*scope(ctx)))


@fix_app.command("regenerate")
@guarded
def regenerate(ctx: typer.Context) -> None:
    """Request a replacement draft through the core's regeneration policy."""
    run_id, finding_id, _ = scope(ctx)
    result = client(ctx).generate_fix(run_id, finding_id, regenerate=True)
    emit(ctx, result)
    if result.generation_status != "draft_ready":
        raise typer.Exit(1)


@fix_app.command("reject")
@guarded
def reject(ctx: typer.Context) -> None:
    """Reject the explicitly selected draft."""
    prop = proposal(ctx)
    emit(ctx, client(ctx).reject_fix(prop.run_id, prop.finding_id, prop.proposal_id))


@fix_app.command("approve-validation")
@guarded
def approve_validation(ctx: typer.Context,
                       approve: bool = typer.Option(False, "--approve", help="Explicitly approve isolated validation.")) -> None:
    """Record approval for the selected proposal's isolated validation scope."""
    if not approve:
        raise typer.BadParameter("Pass --approve to approve the selected proposal for validation")
    prop = proposal(ctx)
    result = client(ctx).approve_fix_validation(m.FixValidationApprovalRequest(
        **identity(prop), approval_token=token_for(prop, "validate")))
    emit(ctx, result)
    if not result.approval_verified:
        raise typer.Exit(1)


@fix_app.command("validate")
@guarded
def validate(ctx: typer.Context,
             run_tests: bool = typer.Option(False, "--run-tests"),
             run_full_suite: bool = typer.Option(False, "--run-full-suite")) -> None:
    """Validate an already approved proposal in an isolated worktree."""
    prop = proposal(ctx)
    result = client(ctx).validate_fix(m.FixValidationRequest(
        **identity(prop), approval_token=token_for(prop, "validate"),
        run_tests=run_tests, run_full_suite=run_full_suite))
    emit(ctx, result)
    if (not result.valid or (run_tests and result.tests_status != "passed")
            or (run_full_suite and result.full_suite_status != "passed")):
        raise typer.Exit(1)


@fix_app.command("apply")
@guarded
def apply(ctx: typer.Context,
          confirmed_patch_hash: str = typer.Option(..., "--confirmed-patch-hash"),
          approve: bool = typer.Option(False, "--approve", help="Explicitly approve workspace application.")) -> None:
    """Apply the exact validated patch using the core's guarded workspace operation."""
    if not approve:
        raise typer.BadParameter("Pass --approve to apply the selected validated proposal")
    prop = proposal(ctx)
    result = client(ctx).apply_fix(m.FixApplyRequest(
        **identity(prop), approval_token=token_for(prop, "apply"), confirmed_patch_hash=confirmed_patch_hash))
    emit(ctx, result)
    if result.apply_status != "applied" or not result.approval_verified:
        raise typer.Exit(1)


@fix_app.command("revert")
@guarded
def revert(ctx: typer.Context,
           confirmed_patch_hash: str = typer.Option(..., "--confirmed-patch-hash")) -> None:
    """Restore pre-apply bytes if the core confirms that the applied files are unchanged."""
    prop = proposal(ctx)
    result = client(ctx).revert_fix(m.FixRevertRequest(**identity(prop), confirmed_patch_hash=confirmed_patch_hash))
    emit(ctx, result)
    if result.apply_status != "reverted":
        raise typer.Exit(1)


@fix_app.command("history")
@guarded
def history(ctx: typer.Context) -> None:
    """Show the selected proposal's bounded apply/revert history."""
    prop = proposal(ctx)
    emit(ctx, client(ctx).fix_history(prop.run_id, prop.finding_id, prop.proposal_id))
