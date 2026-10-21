import importlib.util
import json
from pathlib import Path
from typing import Optional, cast

import typer

from codeatlas.cli_color import configure_cli_color
from codeatlas.cli_platform import FailOn, emit, findings_fail, options, register
from codeatlas.orchestrator import run_review

configure_cli_color()
app = typer.Typer(help="Evidence-first repository-aware code review.")
register(app)


@app.command()
def review(
    ctx: typer.Context,
    repo: str = typer.Option(".", "--repo", help="Path to the target Git repository."),
    base: str = typer.Option("main", "--base", help="Base Git reference."),
    head: str = typer.Option("HEAD", "--head", help="Head Git reference."),
    json_output: Optional[str] = typer.Option(None, "--json-output", help="Write the run JSON here."),
    markdown_output: Optional[str] = typer.Option(None, "--markdown-output", help="Write the Markdown report here."),
    manifest_output: Optional[str] = typer.Option(None, "--manifest-output", help="Write the run manifest here."),
    evidence_output: Optional[str] = typer.Option(None, "--evidence-output", help="Write lifecycle JSONL here."),
    context_output: Optional[str] = typer.Option(None, "--context-output", help="Write retrieved context JSON here."),
    packet_output: Optional[str] = typer.Option(None, "--packet-output", help="Write review packet JSON here."),
    policy_output: Optional[str] = typer.Option(None, "--policy-output", help="Write policy decision JSON here."),
    analyzer: list[str] = typer.Option([], "--analyzer", help="Analyzer name(s), repeatable or comma-separated."),
    no_analyzers: bool = typer.Option(False, "--no-analyzers", help="Disable deterministic analyzers."),
    index_repository: Optional[bool] = typer.Option(None, "--index-repository/--no-index-repository", help="Build repository symbol index and context."),
    assemble_review_packet: Optional[bool] = typer.Option(None, "--assemble-review-packet/--no-assemble-review-packet", help="Assemble bounded review packet."),
    review_provider: Optional[str] = typer.Option(None, "--review-provider", help="Reviewer provider name (mock or live). Live is opt-in and review-only."),
    provider: Optional[str] = typer.Option(None, "--provider", help="Model name for the live provider (requires --review-provider live)."),
    provider_config: Optional[str] = typer.Option(None, "--provider-config", help="Path to a provider settings file (YAML/JSON, no API keys allowed)."),
    provider_timeout: Optional[float] = typer.Option(None, "--provider-timeout", min=0.1, help="Live provider request timeout in seconds."),
    provider_max_output_tokens: Optional[int] = typer.Option(None, "--provider-max-output-tokens", min=1, help="Maximum output tokens for the live provider."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Prepare the live review but never contact the provider."),
    allow_patch_suggestions: bool = typer.Option(
        False, "--allow-patch-suggestions",
        help="Opt in to materializing provider draft PatchProposals (never applied automatically).",
    ),
    json_stdout: bool = typer.Option(False, "--json", help="Print one run manifest JSON object to stdout."),
    fail_on: FailOn = typer.Option(FailOn.none, "--fail-on", help="Exit 3 when active findings meet this severity threshold."),
) -> None:
    """Collect read-only repository and diff evidence for a review run."""
    machine_output = json_stdout or options(ctx).json_output
    result = run_review(
        repo,
        base,
        head,
        evidence_output=evidence_output,
        json_output=json_output,
        markdown_output=markdown_output,
        manifest_output=manifest_output,
        context_output=context_output,
        packet_output=packet_output,
        policy_output=policy_output,
        analyzers=analyzer,
        no_analyzers=no_analyzers,
        index_repository=index_repository,
        assemble_review_packet=assemble_review_packet,
        review_provider=review_provider,
        provider_model=provider,
        provider_config_path=provider_config,
        provider_timeout=provider_timeout,
        provider_max_output_tokens=provider_max_output_tokens,
        provider_dry_run=dry_run,
        banner_callback=lambda message: typer.echo(message, err=machine_output),
        allow_patch_suggestions=allow_patch_suggestions,
    )
    if machine_output:
        typer.echo(json.dumps(result.manifest.model_dump(mode="json"), indent=2, sort_keys=True))
        if result.manifest.errors:
            raise typer.Exit(code=1)
        if findings_fail(result.manifest.findings, fail_on):
            raise typer.Exit(code=3)
        return
    typer.echo(result.summary)
    typer.echo(f"Evidence log: {result.evidence_path}")
    if json_output:
        typer.echo(f"JSON output: {json_output}")
    if markdown_output:
        typer.echo(f"Markdown output: {markdown_output}")
    if manifest_output:
        typer.echo(f"Manifest output: {manifest_output}")
    if context_output:
        typer.echo(f"Context output: {context_output}")
    if packet_output:
        typer.echo(f"Packet output: {packet_output}")
    if policy_output:
        typer.echo(f"Policy output: {policy_output}")
    if result.manifest.errors:
        for error in result.manifest.errors:
            typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=1)
    if findings_fail(result.manifest.findings, fail_on):
        raise typer.Exit(code=3)


@app.command()
def benchmark(
    suite: str = typer.Option(..., "--suite", help="Versioned repository benchmark suite JSON."),
    review_observations: Optional[str] = typer.Option(None, "--review-observations", help="Review observations JSON."),
    repair_observations: Optional[str] = typer.Option(None, "--repair-observations", help="Repair observations JSON."),
    provider_comparisons: Optional[str] = typer.Option(None, "--provider-comparisons", help="Controlled provider-comparison metadata JSON."),
    fixture_root: Optional[str] = typer.Option(None, "--fixture-root", help="Root used to resolve local fixture references."),
    output: Optional[str] = typer.Option(None, "--output", help="Write the JSON report to this path."),
) -> None:
    """Score offline review and repair observations without cloning or execution."""
    from codeatlas.benchmark.runner import run_benchmark_files, write_benchmark_report

    try:
        result = run_benchmark_files(
            suite,
            review_observations_path=review_observations,
            repair_observations_path=repair_observations,
            provider_comparisons_path=provider_comparisons,
            fixture_root=fixture_root,
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=2) from error
    serialized = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if output:
        inputs = tuple(path for path in (suite, review_observations, repair_observations, provider_comparisons) if path is not None)
        try:
            write_benchmark_report(result, output, protected_inputs=inputs)
        except (OSError, ValueError) as error:
            typer.echo(f"Error: {error}", err=True)
            raise typer.Exit(code=2) from error
    else:
        typer.echo(serialized, nl=False)


@app.command("benchmark-collect")
def benchmark_collect(
    mode: str = typer.Option(..., "--mode", help="Collection mode: offline or public."),
    suite: str = typer.Option(..., "--suite", help="Versioned repository benchmark suite JSON."),
    case_id: str = typer.Option(..., "--case-id", help="One immutable benchmark case ID."),
    fixture_root: str | None = typer.Option(None, "--fixture-root", help="Root used to resolve local fixture references."),
    repository_url: str | None = typer.Option(None, "--repository-url", help="Pinned public HTTPS repository URL."),
    base_sha: str | None = typer.Option(None, "--base-sha", help="Exact 40-character base commit SHA."),
    head_sha: str | None = typer.Option(None, "--head-sha", help="Exact 40-character head commit SHA."),
    license_spdx: str | None = typer.Option(None, "--license-spdx", help="License identifier for the public source."),
    provenance: str | None = typer.Option(None, "--provenance", help="Short source provenance record."),
    timeout_seconds: float = typer.Option(30.0, "--timeout-seconds", min=0.1, max=300.0),
) -> None:
    """Collect metadata for one offline fixture or pinned public repository."""
    from codeatlas.benchmark.collection import CollectionMode, CollectionRequest, collect_benchmark

    try:
        request = CollectionRequest(
            mode=cast(CollectionMode, mode), suite_path=suite, case_id=case_id, fixture_root=fixture_root,
            repository_url=repository_url, base_sha=base_sha, head_sha=head_sha,
            license_spdx=license_spdx, provenance=provenance, timeout_seconds=timeout_seconds,
        )
        result = collect_benchmark(request)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=2) from error
    typer.echo(json.dumps(result.model_dump(mode="json"), indent=2, sort_keys=True))
    if result.status == "unavailable":
        raise typer.Exit(code=4)


@app.command("beta-check")
def beta_check(
    ctx: typer.Context,
    config: Optional[str] = typer.Option(None, "--config", help="Strict controlled-beta JSON configuration."),
) -> None:
    """Validate local controlled-beta configuration and runtime prerequisites."""
    from codeatlas.beta import check_beta, load_beta_config

    try:
        report = check_beta(load_beta_config(config))
    except (OSError, TypeError, ValueError) as error:
        typer.echo("Error [invalid_beta_config]: Controlled-beta configuration is unavailable or invalid.", err=True)
        raise typer.Exit(code=2) from error
    emit(ctx, report)
    if report.status == "blocked":
        raise typer.Exit(code=1)


@app.command("beta-start")
def beta_start(
    ctx: typer.Context,
    config: Optional[str] = typer.Option(None, "--config", help="Strict controlled-beta JSON configuration."),
    allow_live_provider: bool = typer.Option(
        False, "--allow-live-provider",
        help="Explicitly authorize the configured live-provider mode for this invocation.",
    ),
    output: Optional[str] = typer.Option(None, "--output", help="Write the bounded startup plan to a new JSON file."),
) -> None:
    """Initialize a safe beta workflow plan without starting hidden processes or mutations."""
    from codeatlas.beta import load_beta_config, start_beta, write_beta_export

    try:
        plan = start_beta(load_beta_config(config), allow_live_provider=allow_live_provider)
        if output:
            write_beta_export(plan, output)
    except (OSError, TypeError, ValueError) as error:
        typer.echo("Error [beta_start_blocked]: Controlled-beta startup was blocked by configuration or safety policy.", err=True)
        raise typer.Exit(code=2) from error
    emit(ctx, plan)


@app.command("beta-report")
def beta_report(
    ctx: typer.Context,
    config: Optional[str] = typer.Option(None, "--config", help="Strict controlled-beta JSON configuration."),
    service: bool = typer.Option(False, "--service", help="Include bounded read-only diagnostics from the configured loopback service."),
    output: Optional[str] = typer.Option(None, "--output", help="Write the bounded report to a new JSON file."),
    session: list[str] | None = typer.Option(None, "--session", help="Explicit session record; repeat at most 50 times."),
    onboarding: str | None = typer.Option(None, "--onboarding", help="Explicit bounded onboarding JSON evidence."),
    incidents: str | None = typer.Option(None, "--incidents", help="Explicit repository-scoped incident store."),
    repository: str | None = typer.Option(None, "--repository", help="Repository used only to verify incident scope."),
    feedback: list[str] | None = typer.Option(None, "--feedback", help="Explicit safe feedback export; repeat at most 50 times."),
) -> None:
    """Report configuration and explicitly supplied safe beta execution evidence."""
    from codeatlas.beta import build_beta_report, load_beta_config, write_beta_export
    from codeatlas.beta_execution import load_onboarding_evidence, summarize_execution
    from codeatlas.beta_session import BetaSessionStore

    try:
        if len(session or []) > 50:
            raise ValueError("too many beta session inputs")
        if (incidents is None) != (repository is None):
            raise ValueError("incident evidence requires both --incidents and --repository")
        incident_report = None
        if incidents is not None and repository is not None:
            from codeatlas.beta_incidents import BetaIncidentStore
            incident_report = BetaIncidentStore(incidents, repository=repository).load()
        if len(feedback or []) > 50:
            raise ValueError("too many feedback inputs")
        from codeatlas.beta_execution import load_feedback_evidence
        execution = summarize_execution(
            tuple(BetaSessionStore(path).load() for path in (session or [])),
            onboarding=load_onboarding_evidence(onboarding) if onboarding else None,
            incidents=incident_report,
            feedback=tuple(load_feedback_evidence(path) for path in (feedback or [])),
        )
        opts = options(ctx)
        report = build_beta_report(
            load_beta_config(config),
            service_url=opts.service_url if service else None,
            timeout=opts.timeout if service else None,
            execution=execution,
        )
        if output:
            write_beta_export(report, output)
    except (OSError, TypeError, ValueError) as error:
        typer.echo("Error [beta_report_unavailable]: The bounded beta report could not be produced.", err=True)
        raise typer.Exit(code=2) from error
    emit(ctx, report)
    if report.status == "blocked":
        raise typer.Exit(code=1)


@app.command("release-check")
def release_check(
    ctx: typer.Context,
    plan: str = typer.Option(
        "config/controlled-beta-evidence-plan.json",
        "--plan",
        help="Explicit controlled-beta evidence plan JSON.",
    ),
    evidence: str | None = typer.Option(
        None,
        "--evidence",
        help="Explicit limited-release evidence JSON; no evidence is discovered automatically.",
    ),
    artifact_evidence: str | None = typer.Option(
        None,
        "--artifact-evidence",
        help="Explicit independently verified artifact-evidence JSON.",
    ),
    artifact_directory: str | None = typer.Option(
        None,
        "--artifact-directory",
        help="Explicit directory containing artifacts and SHA256SUMS.",
    ),
    source_root: str | None = typer.Option(
        None,
        "--source-root",
        help="Explicit source checkout used for independent source identity verification.",
    ),
    kpi_catalog: str = typer.Option(
        "eval/product-validation/metrics-v1.json",
        "--kpi-catalog",
        help="Explicit versioned KPI catalog JSON.",
    ),
    output: str | None = typer.Option(
        None,
        "--output",
        help="Write the bounded release-check result to a new JSON file.",
    ),
) -> None:
    """Run the fail-closed limited-release gate over explicitly supplied evidence."""
    from codeatlas.release_check import run_release_check, write_release_check

    try:
        result = run_release_check(
            repository_root=".",
            plan_path=plan,
            evidence_path=evidence,
            artifact_evidence_path=artifact_evidence,
            artifact_directory=artifact_directory,
            source_root=source_root,
            kpi_catalog_path=kpi_catalog,
        )
        if output:
            write_release_check(result, output)
    except (OSError, TypeError, ValueError):
        typer.echo("Error [release_check_invalid]: Release-check inputs were invalid or unavailable.", err=True)
        raise typer.Exit(code=2) from None
    emit(ctx, result)
    if result.decision == "blocked":
        raise typer.Exit(code=1)


@app.command("beta-onboard")
def beta_onboard(
    ctx: typer.Context,
    repo: str = typer.Option(..., "--repo", help="Controlled local Git repository."),
    base: str = typer.Option(..., "--base", help="Base revision to resolve without fetching."),
    head: str = typer.Option("HEAD", "--head", help="Head revision to resolve without fetching."),
    config: str | None = typer.Option(None, "--config", help="Strict controlled-beta JSON configuration."),
    expected_repository_id: str | None = typer.Option(None, "--expected-repository-id"),
    expected_base_sha: str | None = typer.Option(None, "--expected-base-sha"),
    expected_head_sha: str | None = typer.Option(None, "--expected-head-sha"),
    allow_live_provider: bool = typer.Option(False, "--allow-live-provider"),
    service: bool = typer.Option(False, "--service", help="Probe the explicitly configured loopback service."),
    extension_version: str | None = typer.Option(None, "--extension-version", help="Installed extension version for compatibility checking."),
    github_repository: str | None = typer.Option(None, "--github-repo", help="Explicitly authorize read-only PR metadata probing for OWNER/REPO."),
    github_pr: int | None = typer.Option(None, "--github-pr"),
    output: str | None = typer.Option(None, "--output", help="Export to a new bounded JSON file without overwriting."),
) -> None:
    """Check controlled-repository onboarding without tests, live requests, or remote writes."""
    from codeatlas.beta import write_beta_export
    from codeatlas.onboarding import OnboardingRequest, onboard_repository

    try:
        opts = options(ctx)
        request = OnboardingRequest(
            repository=repo, base=base, head=head,
            expected_repository_id=expected_repository_id,
            expected_base_sha=expected_base_sha, expected_head_sha=expected_head_sha,
            allow_live_provider=allow_live_provider,
            service_url=opts.service_url if service else None,
            extension_version=extension_version,
            github_repository=github_repository, github_pr=github_pr,
            timeout_seconds=min(opts.timeout, 30.0),
        )
        report = onboard_repository(request, config_path=config)
        if output:
            write_beta_export(report, output)
    except (OSError, TypeError, ValueError) as error:
        typer.echo("Error [invalid_beta_configuration]: Onboarding input or export is unavailable or invalid.", err=True)
        raise typer.Exit(code=2) from error
    emit(ctx, report)
    if report.status == "blocked":
        raise typer.Exit(code=1)
    if report.status == "unavailable":
        raise typer.Exit(code=4)


beta_session_app = typer.Typer(help="Run explicit, metadata-only controlled-beta session stages.")
app.add_typer(beta_session_app, name="beta-session")


class _BetaSessionScopeError(ValueError):
    pass


class _BetaSessionTerminalError(ValueError):
    pass


def _load_beta_session(path: str):
    from codeatlas.beta_session import BetaSessionRunner, BetaSessionStore

    store = BetaSessionStore(path)
    return store, BetaSessionRunner(store.load())


def _beta_exit_code(runner) -> int:
    result = runner.session.events[-1].result
    if result in {"blocked", "failed"}:
        return 1
    if result == "unavailable":
        return 4
    return 0


def _save_beta_session(ctx: typer.Context, store, runner) -> None:
    store.save(runner.session)
    emit(ctx, runner.session)


def _save_beta_result(ctx: typer.Context, store, runner) -> None:
    _save_beta_session(ctx, store, runner)
    if (code := _beta_exit_code(runner)):
        raise typer.Exit(code=code)


def _require_active_beta_session(runner) -> None:
    if runner.session.lifecycle_status != "active":
        raise _BetaSessionTerminalError("terminal beta sessions cannot contact the service")


def _verify_service_review_scope(client, runner, run_id: str) -> None:
    from codeatlas.service.persistence import repository_id

    status = client.get_review(run_id)
    if (status.run_id != run_id
            or repository_id(status.repository) != runner.session.repository_identity_hash
            or status.base_commit != runner.session.base_sha
            or status.head_commit != runner.session.head_sha):
        raise _BetaSessionScopeError("service review does not match the beta session")


def _verify_service_proposal_scope(client, runner, run_id: str, finding_id: str, proposal_id: str):
    _verify_service_review_scope(client, runner, run_id)
    proposal = client.get_fix(run_id, finding_id, proposal_id)
    from codeatlas.service.persistence import repository_id

    if (proposal.run_id != run_id or proposal.finding_id != finding_id
            or proposal.proposal_id != proposal_id
            or repository_id(proposal.repository) != runner.session.repository_identity_hash
            or proposal.base_commit != runner.session.base_sha
            or proposal.head_commit != runner.session.head_sha
            or proposal.patch_hash != runner.session.patch_hash):
        raise _BetaSessionScopeError("service proposal does not match the beta session")
    return proposal


def _record_service_failure(ctx: typer.Context, store, runner, stage: str, error) -> None:
    from codeatlas.beta_session import SessionStage

    runner.record_service_failure(
        stage=cast(SessionStage, stage), timed_out=error.code in {"timeout", "wait_timeout"},
    )
    _save_beta_result(ctx, store, runner)


def _record_scope_failure(ctx: typer.Context, store, runner, stage: str) -> None:
    from codeatlas.beta_session import SessionStage

    runner.record_service_failure(
        stage=cast(SessionStage, stage), blocked=True, failure_code="session_scope_mismatch",
    )
    _save_beta_result(ctx, store, runner)


@beta_session_app.command("create")
def beta_session_create(
    ctx: typer.Context,
    session: str = typer.Option(..., "--session", help="New explicit session metadata file."),
    repo: str = typer.Option(..., "--repo"),
    base: str = typer.Option(..., "--base"),
    head: str = typer.Option("HEAD", "--head"),
    case_id: str = typer.Option(..., "--case-id"),
    language: str = typer.Option(..., "--language"),
    config: str | None = typer.Option(None, "--config"),
    allow_live_provider: bool = typer.Option(False, "--allow-live-provider"),
) -> None:
    """Create one session only after bounded repository onboarding succeeds."""
    from codeatlas.beta import load_beta_config
    from codeatlas.beta_session import BetaSessionRunner, BetaSessionStore
    from codeatlas.onboarding import OnboardingRequest, onboard_repository

    try:
        selected = load_beta_config(config)
        report = onboard_repository(OnboardingRequest(
            repository=repo, base=base, head=head, allow_live_provider=allow_live_provider,
            timeout_seconds=min(options(ctx).timeout, 30.0),
        ), config_path=config)
        if not report.beta_ready or report.base_sha is None or report.head_sha is None:
            typer.echo("Error [session_not_authorized]: Repository onboarding did not authorize a session.", err=True)
            raise typer.Exit(code=1 if report.status == "blocked" else 4)
        if language not in report.languages:
            typer.echo("Error [unsupported_language]: The selected language was not detected in committed source.", err=True)
            raise typer.Exit(code=1)
        runner = BetaSessionRunner.create(repository=repo, case_id=case_id, language=language,
                                          base_sha=report.base_sha, head_sha=report.head_sha, config=selected)
        BetaSessionStore(session).create(runner.session)
    except typer.Exit:
        raise
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        typer.echo("Error [invalid_session]: The bounded beta session could not be created.", err=True)
        raise typer.Exit(code=2) from error
    emit(ctx, runner.session)


@beta_session_app.command("review")
def beta_session_review(ctx: typer.Context, session: str = typer.Option(..., "--session"),
                        repo: str = typer.Option(..., "--repo"), config: str | None = typer.Option(None, "--config"),
                        allow_live_provider: bool = typer.Option(False, "--allow-live-provider")) -> None:
    """Run exactly one offline or explicitly authorized live review stage."""
    from codeatlas.beta import load_beta_config

    try:
        store, runner = _load_beta_session(session)
        selected = load_beta_config(config)
        if runner.session.provider_mode == "live":
            runner.run_live_review(repository=repo, config=selected, allow_live_provider=allow_live_provider)
        else:
            runner.run_offline_review(repository=repo)
        _save_beta_result(ctx, store, runner)
    except typer.Exit:
        raise
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        typer.echo("Error [review_stage_failed]: The selected review stage could not complete.", err=True)
        raise typer.Exit(code=1) from error


@beta_session_app.command("github-dry-run")
def beta_session_github_dry_run(ctx: typer.Context, session: str = typer.Option(..., "--session"),
                                repo: str = typer.Option(..., "--repo"),
                                github_repository: str = typer.Option(..., "--github-repo"),
                                pull_request: int = typer.Option(..., "--github-pr", min=1)) -> None:
    """Run a read-only GitHub review with zero write calls."""
    try:
        store, runner = _load_beta_session(session)
        runner.run_github_dry_run(repository=repo, github_repository=github_repository, pull_request=pull_request)
        _save_beta_result(ctx, store, runner)
    except typer.Exit:
        raise
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        typer.echo("Error [github_review_failed]: The GitHub dry-run stage could not complete.", err=True)
        raise typer.Exit(code=1) from error


@beta_session_app.command("propose")
def beta_session_propose(ctx: typer.Context, session: str = typer.Option(..., "--session"),
                         run_id: str = typer.Option(..., "--run-id"), finding_id: str = typer.Option(..., "--finding-id")) -> None:
    """Generate one draft FixProposal through the loopback service."""
    from codeatlas.client import ClientError, CodeAtlasClient

    try:
        store, runner = _load_beta_session(session)
        _require_active_beta_session(runner)
        opts = options(ctx)
        client = CodeAtlasClient(opts.service_url, timeout=opts.timeout)
        _verify_service_review_scope(client, runner, run_id)
        proposal = client.generate_fix(run_id, finding_id)
        from codeatlas.service.persistence import repository_id

        if (
            proposal.run_id != run_id
            or proposal.finding_id != finding_id
            or repository_id(proposal.repository) != runner.session.repository_identity_hash
            or proposal.base_commit != runner.session.base_sha
            or proposal.head_commit != runner.session.head_sha
        ):
            raise _BetaSessionScopeError("generated proposal does not match the beta session")
        runner.record_fix_proposal(
            available=proposal.generation_status == "draft_ready",
            context_hash=None,
            patch_hash=proposal.patch_hash or None,
        )
        _save_beta_result(ctx, store, runner)
    except typer.Exit:
        raise
    except ClientError as error:
        _record_service_failure(ctx, store, runner, "fix_proposal", error)
    except _BetaSessionScopeError:
        _record_scope_failure(ctx, store, runner, "fix_proposal")
    except _BetaSessionTerminalError:
        typer.echo("Error [terminal_session]: Terminal sessions cannot contact the service.", err=True)
        raise typer.Exit(code=1) from None
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        typer.echo("Error [invalid_session_input]: The proposal stage could not be completed.", err=True)
        raise typer.Exit(code=2) from error


def _service_scoped_validation(ctx: typer.Context, session: str, run_id: str, finding_id: str, proposal_id: str):
    from codeatlas.client import CodeAtlasClient

    store, runner = _load_beta_session(session)
    _require_active_beta_session(runner)
    opts = options(ctx)
    client = CodeAtlasClient(opts.service_url, timeout=opts.timeout)
    _verify_service_proposal_scope(client, runner, run_id, finding_id, proposal_id)
    return store, runner, client


@beta_session_app.command("approve-validation")
def beta_session_approve_validation(ctx: typer.Context, session: str = typer.Option(..., "--session"),
                                    run_id: str = typer.Option(..., "--run-id"), finding_id: str = typer.Option(..., "--finding-id"),
                                    proposal_id: str = typer.Option(..., "--proposal-id"),
                                    approval_token: str = typer.Option(..., "--approval-token", hide_input=True)) -> None:
    """Verify an existing validate-scoped token without persisting it."""
    from codeatlas.client import ClientError
    from codeatlas.service.models import FixValidationApprovalRequest

    try:
        store, runner, client = _service_scoped_validation(ctx, session, run_id, finding_id, proposal_id)
        response = client.approve_fix_validation(FixValidationApprovalRequest(
            run_id=run_id, finding_id=finding_id, proposal_id=proposal_id, approval_token=approval_token))
        runner.record_validation_approval(approved=response.approval_verified)
        _save_beta_result(ctx, store, runner)
    except typer.Exit:
        raise
    except ClientError as error:
        _record_service_failure(ctx, store, runner, "validation_approval", error)
    except _BetaSessionScopeError:
        _record_scope_failure(ctx, store, runner, "validation_approval")
    except _BetaSessionTerminalError:
        typer.echo("Error [terminal_session]: Terminal sessions cannot contact the service.", err=True)
        raise typer.Exit(code=1) from None
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        typer.echo("Error [invalid_session_input]: Validation approval could not be completed.", err=True)
        raise typer.Exit(code=2) from error


@beta_session_app.command("validate")
def beta_session_validate(ctx: typer.Context, session: str = typer.Option(..., "--session"),
                          run_id: str = typer.Option(..., "--run-id"), finding_id: str = typer.Option(..., "--finding-id"),
                          proposal_id: str = typer.Option(..., "--proposal-id"),
                          approval_token: str = typer.Option(..., "--approval-token", hide_input=True)) -> None:
    """Run only approved isolated validation; this command never runs tests."""
    from codeatlas.beta_session import CleanupStatus
    from codeatlas.client import ClientError
    from codeatlas.service.models import FixValidationRequest

    try:
        store, runner, client = _service_scoped_validation(ctx, session, run_id, finding_id, proposal_id)
        response = client.validate_fix(FixValidationRequest(
            run_id=run_id, finding_id=finding_id, proposal_id=proposal_id, approval_token=approval_token,
            run_tests=False, run_full_suite=False))
        cleanup_status = response.cleanup_status if response.cleanup_status in {
            "completed", "retained", "not_applicable", "failed",
        } else "not_run"
        runner.record_validation(
            passed=response.valid and response.approval_verified,
            cleanup_status=cast(CleanupStatus, cleanup_status),
        )
        _save_beta_result(ctx, store, runner)
    except typer.Exit:
        raise
    except ClientError as error:
        _record_service_failure(ctx, store, runner, "validation", error)
    except _BetaSessionScopeError:
        _record_scope_failure(ctx, store, runner, "validation")
    except _BetaSessionTerminalError:
        typer.echo("Error [terminal_session]: Terminal sessions cannot contact the service.", err=True)
        raise typer.Exit(code=1) from None
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        typer.echo("Error [invalid_session_input]: Validation could not be completed.", err=True)
        raise typer.Exit(code=2) from error


@beta_session_app.command("targeted-tests")
def beta_session_targeted_tests(
    ctx: typer.Context, session: str = typer.Option(..., "--session"),
    run_id: str = typer.Option(..., "--run-id"), finding_id: str = typer.Option(..., "--finding-id"),
    proposal_id: str = typer.Option(..., "--proposal-id"),
    approval_token: str = typer.Option(..., "--approval-token", hide_input=True),
) -> None:
    """Explicitly request policy-allowed targeted tests during isolated validation."""
    from codeatlas.beta_session import CleanupStatus, ResultStatus
    from codeatlas.client import ClientError
    from codeatlas.service.models import FixValidationRequest

    try:
        store, runner, client = _service_scoped_validation(ctx, session, run_id, finding_id, proposal_id)
        response = client.validate_fix(FixValidationRequest(
            run_id=run_id, finding_id=finding_id, proposal_id=proposal_id, approval_token=approval_token,
            run_tests=True, run_full_suite=False))
        cleanup_status = response.cleanup_status if response.cleanup_status in {
            "completed", "retained", "not_applicable", "failed",
        } else "not_run"
        runner.record_validation(
            passed=response.valid and response.approval_verified,
            cleanup_status=cast(CleanupStatus, cleanup_status),
        )
        test_result = response.tests_status if response.tests_status in {
            "passed", "blocked", "unavailable", "not_run",
        } else "failed"
        runner.record_targeted_tests(result=cast(ResultStatus, test_result))
        _save_beta_result(ctx, store, runner)
    except typer.Exit:
        raise
    except ClientError as error:
        _record_service_failure(ctx, store, runner, "targeted_tests", error)
    except _BetaSessionScopeError:
        _record_scope_failure(ctx, store, runner, "targeted_tests")
    except _BetaSessionTerminalError:
        typer.echo("Error [terminal_session]: Terminal sessions cannot contact the service.", err=True)
        raise typer.Exit(code=1) from None
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        typer.echo("Error [invalid_session_input]: Targeted tests could not be completed.", err=True)
        raise typer.Exit(code=2) from error


@beta_session_app.command("apply")
def beta_session_apply(ctx: typer.Context, session: str = typer.Option(..., "--session"),
                       run_id: str = typer.Option(..., "--run-id"), finding_id: str = typer.Option(..., "--finding-id"),
                       proposal_id: str = typer.Option(..., "--proposal-id"),
                       confirmed_patch_hash: str = typer.Option(..., "--confirmed-patch-hash"),
                       approval_token: str = typer.Option(..., "--approval-token", hide_input=True),
                       approve_apply: bool = typer.Option(False, "--approve-apply")) -> None:
    """Apply only with an explicit flag, existing apply token, and exact patch hash."""
    from codeatlas.client import ClientError
    from codeatlas.service.models import FixApplyRequest

    try:
        store, runner = _load_beta_session(session)
        _require_active_beta_session(runner)
        if not approve_apply or runner.session.validation_result != "passed" or confirmed_patch_hash != runner.session.patch_hash:
            runner.record_apply(status="blocked", explicitly_approved=False)
            _save_beta_result(ctx, store, runner)
            return
        opts = options(ctx)
        from codeatlas.client import CodeAtlasClient

        client = CodeAtlasClient(opts.service_url, timeout=opts.timeout)
        _verify_service_proposal_scope(client, runner, run_id, finding_id, proposal_id)
        response = client.apply_fix(FixApplyRequest(
            run_id=run_id, finding_id=finding_id, proposal_id=proposal_id, approval_token=approval_token,
            confirmed_patch_hash=confirmed_patch_hash))
        runner.record_apply(status="applied" if response.apply_status == "applied" else "not_applied",
                            explicitly_approved=response.approval_verified)
        _save_beta_result(ctx, store, runner)
    except typer.Exit:
        raise
    except ClientError as error:
        _record_service_failure(ctx, store, runner, "apply", error)
    except _BetaSessionScopeError:
        _record_scope_failure(ctx, store, runner, "apply")
    except _BetaSessionTerminalError:
        typer.echo("Error [terminal_session]: Terminal sessions cannot contact the service.", err=True)
        raise typer.Exit(code=1) from None
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        typer.echo("Error [invalid_session_input]: Explicit apply could not be completed.", err=True)
        raise typer.Exit(code=2) from error


@beta_session_app.command("revert")
def beta_session_revert(ctx: typer.Context, session: str = typer.Option(..., "--session"),
                        run_id: str = typer.Option(..., "--run-id"), finding_id: str = typer.Option(..., "--finding-id"),
                        proposal_id: str = typer.Option(..., "--proposal-id"),
                        confirmed_patch_hash: str = typer.Option(..., "--confirmed-patch-hash")) -> None:
    """Restore exact pre-apply bytes only for the session's applied proposal."""
    from codeatlas.client import ClientError
    from codeatlas.service.models import FixRevertRequest

    try:
        store, runner, client = _service_scoped_validation(ctx, session, run_id, finding_id, proposal_id)
        if runner.session.apply_status != "applied" or confirmed_patch_hash != runner.session.patch_hash:
            runner.record_revert(status="blocked")
            _save_beta_result(ctx, store, runner)
            return
        response = client.revert_fix(FixRevertRequest(
            run_id=run_id, finding_id=finding_id, proposal_id=proposal_id,
            confirmed_patch_hash=confirmed_patch_hash))
        runner.record_revert(status="reverted" if response.apply_status == "reverted" else "failed")
        _save_beta_result(ctx, store, runner)
    except typer.Exit:
        raise
    except ClientError as error:
        _record_service_failure(ctx, store, runner, "revert", error)
    except _BetaSessionScopeError:
        _record_scope_failure(ctx, store, runner, "revert")
    except _BetaSessionTerminalError:
        typer.echo("Error [terminal_session]: Terminal sessions cannot contact the service.", err=True)
        raise typer.Exit(code=1) from None
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        typer.echo("Error [invalid_session_input]: Revert could not be completed.", err=True)
        raise typer.Exit(code=2) from error


@beta_session_app.command("inspect")
def beta_session_inspect(ctx: typer.Context, session: str = typer.Option(..., "--session")) -> None:
    """Display one safe persisted beta-session projection."""
    try:
        _store, runner = _load_beta_session(session)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        typer.echo("Error [invalid_session]: Session inspection failed.", err=True)
        raise typer.Exit(code=2) from error
    emit(ctx, runner.session)


@beta_session_app.command("complete")
def beta_session_complete(ctx: typer.Context, session: str = typer.Option(..., "--session")) -> None:
    """Mark an active beta session complete without service work."""
    try:
        store, runner = _load_beta_session(session)
        runner.complete()
        _save_beta_result(ctx, store, runner)
    except typer.Exit:
        raise
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        typer.echo("Error [session_completion_failed]: Session completion could not be recorded.", err=True)
        raise typer.Exit(code=1) from error


@beta_session_app.command("retry")
def beta_session_retry(ctx: typer.Context, session: str = typer.Option(..., "--session")) -> None:
    """Record one explicit retry decision without executing work."""
    try:
        store, runner = _load_beta_session(session)
        runner.retry()
        _save_beta_result(ctx, store, runner)
    except typer.Exit:
        raise
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        typer.echo("Error [retry_failed]: Retry could not be recorded.", err=True)
        raise typer.Exit(code=1) from error


@beta_session_app.command("cancel")
def beta_session_cancel(ctx: typer.Context, session: str = typer.Option(..., "--session")) -> None:
    """Cancel a session without starting or cancelling hidden work."""
    try:
        store, runner = _load_beta_session(session)
        runner.cancel()
        _save_beta_result(ctx, store, runner)
    except typer.Exit:
        raise
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        typer.echo("Error [session_cancelled]: Session cancellation could not be recorded.", err=True)
        raise typer.Exit(code=1) from error


@beta_session_app.command("export")
def beta_session_export(ctx: typer.Context, session: str = typer.Option(..., "--session"),
                        output: str = typer.Option(..., "--output")) -> None:
    """Export a bounded safe snapshot to a new file without overwriting."""
    try:
        _store, runner = _load_beta_session(session)
        runner.export(output)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        typer.echo("Error [session_export_failed]: Session export failed.", err=True)
        raise typer.Exit(code=2) from error
    emit(ctx, runner.session)


@app.command()
def eval(suite: str = "eval/cases", output: str = "eval/results/run-001.jsonl") -> None:
    """Run the deterministic Phase 1 evaluation baseline."""
    runner_path = Path(__file__).resolve().parents[2] / "eval" / "run_eval.py"
    if not runner_path.is_file():
        raise typer.BadParameter(f"evaluation runner is unavailable: {runner_path}")
    spec = importlib.util.spec_from_file_location("codeatlas_eval_runner", runner_path)
    if spec is None or spec.loader is None:
        raise typer.BadParameter("could not load the evaluation runner")
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    raise typer.Exit(runner.main(["--cases", suite, "--output", output]))


patch_app = typer.Typer(help="Patch proposal inspection, validation, and isolated application.")
app.add_typer(patch_app, name="patch")


@patch_app.command(name="list")
def patch_list(
    manifest: str = typer.Option(..., "--manifest", help="Path to a run manifest JSON file."),
) -> None:
    """List patch proposals recorded in a run manifest (no patch content printed)."""
    import json

    m_path = Path(manifest)
    if not m_path.is_file():
        typer.echo(f"Error: Manifest file not found: {manifest}", err=True)
        raise typer.Exit(code=1)
    try:
        data = json.loads(m_path.read_text(encoding="utf-8"))
    except Exception as err:
        typer.echo(f"Error reading manifest JSON: {err}", err=True)
        raise typer.Exit(code=1)

    proposals = data.get("patch_proposals") or []
    validations = {
        item.get("proposal_id"): item
        for item in ((data.get("patch_validation") or {}).get("proposals") or [])
        if isinstance(item, dict)
    }
    if not proposals:
        typer.echo("No patch proposals recorded in this manifest.")
        return

    typer.echo(f"Patch proposals: {len(proposals)}")
    for prop in proposals:
        proposal_id = prop.get("proposal_id", "?")
        validation = validations.get(proposal_id, {})
        policy_decision = prop.get("policy_decision") or {}
        typer.echo("")
        typer.echo(f"Proposal ID:        {proposal_id}")
        typer.echo(f"Finding ID:         {prop.get('finding_id', '?')}")
        typer.echo(f"Status:             {prop.get('status', '?')}")
        typer.echo(f"Target Files:       {', '.join(prop.get('target_files', [])) or 'none'}")
        typer.echo(f"Risk Level:         {prop.get('risk_level', '?')}")
        typer.echo(f"Policy Decision:    {policy_decision.get('decision', 'none')}")
        typer.echo(f"Validation Status:  {'valid' if validation.get('valid') else 'invalid' if validation else 'not_validated'}")
        typer.echo(
            f"Approval Required:  {'yes' if prop.get('status') == 'requires_human_approval' else 'no'}"
        )
    typer.echo("")
    typer.echo("Full diff content is intentionally not printed; use patch inspect --proposal <file> on exported proposals.")


@patch_app.command(name="inspect")
def patch_inspect(
    proposal: str = typer.Option(..., "--proposal", help="Path to patch proposal JSON file."),
) -> None:
    """Inspect a PatchProposal file and display its summary."""
    import json
    from codeatlas.patching import PatchProposal

    p_path = Path(proposal)
    if not p_path.is_file():
        typer.echo(f"Error: Proposal file not found: {proposal}", err=True)
        raise typer.Exit(code=1)
    try:
        data = json.loads(p_path.read_text(encoding="utf-8"))
        prop = PatchProposal.model_validate(data)
    except Exception as err:
        typer.echo(f"Error parsing proposal JSON: {err}", err=True)
        raise typer.Exit(code=1)

    typer.echo(f"Proposal ID:      {prop.proposal_id}")
    typer.echo(f"Finding ID:       {prop.finding_id}")
    typer.echo(f"Provider:         {prop.provider_name} v{prop.provider_version}")
    typer.echo(f"Base Commit:      {prop.base_commit}")
    typer.echo(f"Status:           {prop.status}")
    typer.echo(f"Risk Level:       {prop.risk_level}")
    typer.echo(f"Patch Hash:       {prop.patch_hash}")
    typer.echo(f"Redaction Safe:   {prop.redaction_audit.safe}")
    typer.echo(f"Target Files:     {', '.join(prop.target_files)}")
    typer.echo(f"Rationale:        {prop.rationale}")


def _validation_review_outputs(prop, res, repo, run_id, evidence_output):
    """Render and export the same packet/manifest for both validation commands."""
    from codeatlas.evidence import EvidenceLogger
    from codeatlas.orchestrator.validation import build_validation_review
    from codeatlas.review.rendering import render_test_evidence

    if evidence_output:
        with EvidenceLogger(evidence_output) as evidence:
            packet, manifest = build_validation_review(
                prop, res, repository=str(Path(repo).resolve()), run_id=run_id, evidence=evidence,
            )
    else:
        packet, manifest = build_validation_review(
            prop, res, repository=str(Path(repo).resolve()), run_id=run_id,
        )
    rendered = render_test_evidence(packet)
    typer.echo(rendered)
    return {
        "review_packet": packet.model_dump(mode="json"),
        "human_approval_manifest": manifest.model_dump(mode="json"),
        "markdown_summary": rendered,
    }


def _write_separate_validation_outputs(
    artifacts, packet_output, manifest_output, markdown_output, repo, protected_paths, approval_token
):
    from codeatlas.orchestrator.artifacts import ArtifactOutputError, write_validation_artifacts

    try:
        write_validation_artifacts(
            artifacts, packet_output=packet_output, manifest_output=manifest_output,
            markdown_output=markdown_output,
            repository_root=Path(repo), protected_paths=[p for p in protected_paths if p is not None],
            extra_tokens=[approval_token] if approval_token else [],
        )
    except ArtifactOutputError as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=1)


@patch_app.command(name="validate")
def patch_validate(
    proposal: str = typer.Option(..., "--proposal", help="Path to patch proposal JSON file."),
    repo: str = typer.Option(".", "--repo", help="Path to Git repository root."),
    base: str = typer.Option("HEAD", "--base", help="Expected base Git commit/reference."),
    approval_token: Optional[str] = typer.Option(None, "--approval-token", help="Scoped human approval token."),
    run_tests: bool = typer.Option(False, "--run-tests", help="Execute targeted repository tests in isolated sandbox."),
    run_full_suite: bool = typer.Option(False, "--run-full-suite", help="Execute full repository test suite in isolated sandbox."),
    test_timeout: float = typer.Option(30.0, "--test-timeout", help="Test execution timeout in seconds."),
    max_output_bytes: int = typer.Option(100_000, "--max-output-bytes", help="Maximum output bytes to capture."),
    retain_sandbox_on_failure: bool = typer.Option(False, "--retain-sandbox-on-failure", help="Keep sandbox on failure."),
    report_output: Optional[str] = typer.Option(None, "--report-output", metavar="PATH", help="Write validation report JSON here."),
    packet_output: Optional[str] = typer.Option(None, "--packet-output", metavar="PATH", help="Write the generated ReviewPacket as separate JSON."),
    manifest_output: Optional[str] = typer.Option(None, "--manifest-output", metavar="PATH", help="Write the generated human-approval manifest as separate JSON."),
    markdown_output: Optional[str] = typer.Option(None, "--markdown-output", metavar="PATH", help="Write human-readable validation summary Markdown here."),
    evidence_output: Optional[str] = typer.Option(None, "--evidence-output", help="Write validation lifecycle JSONL here."),
    run_id: str = typer.Option("default", "--run-id", help="Approval scope run ID."),
    config: Optional[str] = typer.Option(None, "--config", help="Path to optional configuration JSON file."),
) -> None:
    """Validate a patch proposal against schema, paths, redaction, and policy.

    When --run-tests is passed, executes targeted repository tests in an isolated sandbox.
    When --run-full-suite is passed, executes full repository test suite if opted in.
    """
    import json
    from codeatlas.evidence import EvidenceLogger
    from codeatlas.patching import PatchProposal, validate_patch_proposal

    p_path = Path(proposal)
    if not p_path.is_file():
        typer.echo(f"Error: Proposal file not found: {proposal}", err=True)
        raise typer.Exit(code=1)
    try:
        data = json.loads(p_path.read_text(encoding="utf-8"))
        prop = PatchProposal.model_validate(data)
    except Exception as err:
        typer.echo(f"Error parsing proposal JSON: {err}", err=True)
        raise typer.Exit(code=1)

    cfg_dict = None
    if config:
        c_path = Path(config)
        if c_path.is_file():
            try:
                cfg_dict = json.loads(c_path.read_text(encoding="utf-8"))
            except Exception as err:
                typer.echo(f"Error parsing config JSON: {err}", err=True)
                raise typer.Exit(code=1)

    if not run_tests and not run_full_suite:
        res = validate_patch_proposal(
            prop,
            repository_root=Path(repo).resolve(),
            allow_isolated_apply=False,
            run_id=run_id,
        )
        typer.echo(f"Valid:            {res.valid}")
        dec = prop.policy_decision.get("decision") if prop.policy_decision else "none"
        typer.echo(f"Policy Decision:  {dec}")
        if res.errors:
            typer.echo("Errors:")
            for err in res.errors:
                typer.echo(f"  - {err}")
        if res.warnings:
            typer.echo("Warnings:")
            for warn in res.warnings:
                typer.echo(f"  - {warn}")

        artifacts = _validation_review_outputs(prop, res, repo, run_id, evidence_output)
        if report_output:
            report_path = Path(report_output)
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report = res.model_dump(mode="json", exclude_none=True)
            report["proposal_status"] = prop.status
            report["review_packet"] = artifacts["review_packet"]
            report["human_approval_manifest"] = artifacts["human_approval_manifest"]
            report_path.write_text(
                json.dumps(report, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            typer.echo(f"Report output: {report_output}")

        _write_separate_validation_outputs(
            artifacts, packet_output, manifest_output, markdown_output, repo,
            [proposal, config, report_output, evidence_output], approval_token,
        )
        if not res.valid:
            raise typer.Exit(code=1)
        return

    # Run tests requested: isolated sandbox execution
    evidence_ctx = EvidenceLogger(evidence_output) if evidence_output else None
    try:
        test_cfg = cfg_dict.get("test") if isinstance(cfg_dict, dict) else None
        if evidence_ctx is not None:
            with evidence_ctx as evidence:
                res = validate_patch_proposal(
                    prop,
                    repository_root=Path(repo).resolve(),
                    approval_token=approval_token,
                    allow_isolated_apply=True,
                    evidence=evidence,
                    run_id=run_id,
                    retain_sandbox_on_failure=retain_sandbox_on_failure,
                    run_tests=run_tests,
                    run_full_suite=run_full_suite,
                    test_timeout=test_timeout,
                    max_output_bytes=max_output_bytes,
                    config=cfg_dict,
                    test_config=test_cfg,
                )
        else:
            res = validate_patch_proposal(
                prop,
                repository_root=Path(repo).resolve(),
                approval_token=approval_token,
                allow_isolated_apply=True,
                run_id=run_id,
                retain_sandbox_on_failure=retain_sandbox_on_failure,
                run_tests=run_tests,
                run_full_suite=run_full_suite,
                test_timeout=test_timeout,
                max_output_bytes=max_output_bytes,
                config=cfg_dict,
                test_config=test_cfg,
            )
    except Exception as err:
        typer.echo(f"Error: test validation failed: {type(err).__name__}: {err}", err=True)
        raise typer.Exit(code=1)

    artifacts = _validation_review_outputs(prop, res, repo, run_id, evidence_output)
    typer.echo(f"Proposal ID:        {prop.proposal_id}")
    typer.echo(f"Sandbox ID:         {res.sandbox_id or 'none'}")
    typer.echo(f"Test Runner:        {res.test_runner or 'none'}")
    targets_str = ", ".join(res.tests_run) if res.tests_run else "none"
    typer.echo(f"Test Targets:       {targets_str}")
    typer.echo(f"Tests Status:       {res.tests_status}")
    if run_full_suite or res.full_suite_requested:
        typer.echo(f"Full Suite Status:  {res.full_suite_status or 'not_run'}")
        typer.echo(f"Full Suite Opt-in:  {'enabled' if res.full_suite_policy_opted_in else 'disabled'}")
        if res.full_suite_blocked_reason:
            typer.echo(f"Full Suite Blocked: {res.full_suite_blocked_reason}")
        if res.full_suite_command:
            typer.echo(f"Full Suite Command: {' '.join(res.full_suite_command)}")
    typer.echo(f"Execution Policy:   network={'allowed' if res.network_allowed else 'disabled'}, dependencies={'allowed' if res.dependency_install_allowed else 'forbidden'}")
    typer.echo(f"Dependencies:       {'enabled' if res.dependency_install_allowed else 'disabled'}")
    typer.echo(f"Network:            {'enabled' if res.network_allowed else 'disabled'}")
    typer.echo(f"Valid:              {res.valid}")
    typer.echo(f"Lifecycle Status:   {prop.status}")

    if res.tests_status in {"failed", "timed_out", "blocked", "error"}:
        typer.echo("")
        typer.echo(f"Test status: {res.tests_status}")
        typer.echo(f"Runner: {res.test_runner or 'none'}")
        typer.echo(f"Target tests: {len(res.tests_run)}")
        typer.echo(f"Failed tests: {res.test_failure_count or (1 if res.tests_status != 'passed' else 0)}")
        if res.diagnostic_summary:
            typer.echo(f"Failure: {res.diagnostic_summary[:120]}")
        dur_s = (res.test_duration_ms or 0.0) / 1000.0
        typer.echo(f"Duration: {dur_s:.2f}s")
        out_desc = "redacted and truncated" if res.test_output_truncated else "redacted"
        typer.echo(f"Output: {out_desc}")
        typer.echo(f"Network isolation: {'verified' if res.network_isolation_verified else 'not independently verified'}")

    if res.errors:
        typer.echo("Errors:")
        for err in res.errors:
            typer.echo(f"  - {err}")
    if res.warnings:
        typer.echo("Warnings:")
        for warn in res.warnings:
            typer.echo(f"  - {warn}")

    if report_output:
        report_path = Path(report_output)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report = res.model_dump(mode="json", exclude_none=True)
        report["proposal_status"] = prop.status
        report["review_packet"] = artifacts["review_packet"]
        report["human_approval_manifest"] = artifacts["human_approval_manifest"]
        report_path.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        typer.echo(f"Report output: {report_output}")

    _write_separate_validation_outputs(
        artifacts, packet_output, manifest_output, markdown_output, repo,
        [proposal, config, report_output, evidence_output], approval_token,
    )
    if not res.valid or (run_tests and res.tests_status != "passed") or (run_full_suite and res.full_suite_status != "passed"):
        raise typer.Exit(code=1)


@patch_app.command(name="apply-isolated")
def patch_apply_isolated(
    proposal: str = typer.Option(..., "--proposal", help="Path to patch proposal JSON file."),
    repo: str = typer.Option(".", "--repo", help="Path to Git repository root."),
    base: str = typer.Option("HEAD", "--base", help="Expected base Git commit/reference."),
    approval_token: Optional[str] = typer.Option(
        None, "--approval-token",
        help="Scoped human approval token. Omitting it fails closed before any application.",
    ),
    report_output: Optional[str] = typer.Option(None, "--report-output", metavar="PATH", help="Write the validation report JSON here."),
    packet_output: Optional[str] = typer.Option(None, "--packet-output", metavar="PATH", help="Write the generated ReviewPacket as separate JSON."),
    manifest_output: Optional[str] = typer.Option(None, "--manifest-output", metavar="PATH", help="Write the generated human-approval manifest as separate JSON."),
    markdown_output: Optional[str] = typer.Option(None, "--markdown-output", metavar="PATH", help="Write human-readable validation summary Markdown here."),
    evidence_output: Optional[str] = typer.Option(None, "--evidence-output", help="Write validation lifecycle JSONL here."),
    run_id: str = typer.Option("default", "--run-id", help="Approval scope run ID (must match token generation)."),
    retain_sandbox_on_failure: bool = typer.Option(
        False, "--retain-sandbox-on-failure",
        help="Local debugging only: keep the sandbox worktree when validation fails.",
    ),
    run_tests: bool = typer.Option(False, "--run-tests", help="Execute targeted repository tests in isolated sandbox."),
    run_full_suite: bool = typer.Option(False, "--run-full-suite", help="Execute full repository test suite in isolated sandbox."),
    test_timeout: float = typer.Option(30.0, "--test-timeout", help="Test execution timeout in seconds."),
    max_output_bytes: int = typer.Option(100_000, "--max-output-bytes", help="Maximum output bytes to capture."),
    config: Optional[str] = typer.Option(None, "--config", help="Path to optional configuration JSON file."),
) -> None:
    """Apply a patch proposal inside an ephemeral detached worktree sandbox.

    Requires a valid scoped approval token.  The original repository is never
    modified and the sandbox is always cleaned up unless retention was
    explicitly requested after a failure.
    """
    import json
    from codeatlas.evidence import EvidenceLogger
    from codeatlas.patching import PatchProposal, validate_patch_proposal

    p_path = Path(proposal)
    if not p_path.is_file():
        typer.echo(f"Error: Proposal file not found: {proposal}", err=True)
        raise typer.Exit(code=1)
    try:
        data = json.loads(p_path.read_text(encoding="utf-8"))
        prop = PatchProposal.model_validate(data)
    except Exception as err:
        typer.echo(f"Error parsing proposal JSON: {err}", err=True)
        raise typer.Exit(code=1)

    cfg_dict = None
    if config:
        c_path = Path(config)
        if c_path.is_file():
            try:
                cfg_dict = json.loads(c_path.read_text(encoding="utf-8"))
            except Exception as err:
                typer.echo(f"Error parsing config JSON: {err}", err=True)
                raise typer.Exit(code=1)

    test_cfg = cfg_dict.get("test") if isinstance(cfg_dict, dict) else None
    evidence_ctx = EvidenceLogger(evidence_output) if evidence_output else None
    try:
        if evidence_ctx is not None:
            with evidence_ctx as evidence:
                res = validate_patch_proposal(
                    prop,
                    repository_root=Path(repo).resolve(),
                    approval_token=approval_token,
                    allow_isolated_apply=True,
                    evidence=evidence,
                    run_id=run_id,
                    retain_sandbox_on_failure=retain_sandbox_on_failure,
                    run_tests=run_tests,
                    run_full_suite=run_full_suite,
                    test_timeout=test_timeout,
                    max_output_bytes=max_output_bytes,
                    config=cfg_dict,
                    test_config=test_cfg,
                )
        else:
            res = validate_patch_proposal(
                prop,
                repository_root=Path(repo).resolve(),
                approval_token=approval_token,
                allow_isolated_apply=True,
                run_id=run_id,
                retain_sandbox_on_failure=retain_sandbox_on_failure,
                run_tests=run_tests,
                run_full_suite=run_full_suite,
                test_timeout=test_timeout,
                max_output_bytes=max_output_bytes,
                config=cfg_dict,
                test_config=test_cfg,
            )
    except Exception as err:
        typer.echo(f"Error: isolated validation failed: {type(err).__name__}: {err}", err=True)
        raise typer.Exit(code=1)

    artifacts = _validation_review_outputs(prop, res, repo, run_id, evidence_output)
    typer.echo(f"Valid:              {res.valid}")
    typer.echo(f"Approval Verified:  {res.approval_verified}")
    typer.echo(f"Applies Cleanly:    {res.applies_cleanly}")
    typer.echo(f"Syntax Valid:       {res.syntax_valid}")
    typer.echo(f"Tests Status:       {res.tests_status}")
    if run_full_suite or res.full_suite_requested:
        typer.echo(f"Full Suite Status:  {res.full_suite_status or 'not_run'}")
        typer.echo(f"Full Suite Opt-in:  {'enabled' if res.full_suite_policy_opted_in else 'disabled'}")
        if res.full_suite_blocked_reason:
            typer.echo(f"Full Suite Blocked: {res.full_suite_blocked_reason}")
        if res.full_suite_command:
            typer.echo(f"Full Suite Command: {' '.join(res.full_suite_command)}")
    typer.echo(f"Build Status:       {res.build_status}")
    typer.echo(f"Sandbox ID:         {res.sandbox_id}")
    typer.echo(f"Cleanup Status:     {res.cleanup_status}")
    typer.echo(f"Sandbox Retained:   {res.sandbox_retained}")
    typer.echo(f"Execution Allowed:  {res.execution_allowed}")
    typer.echo(f"Changed Files:      {', '.join(res.changed_files)}")
    if res.resulting_diff_hash:
        typer.echo(f"Resulting Diff Hash: {res.resulting_diff_hash}")
    typer.echo("Lifecycle Status:   " + prop.status)
    if res.errors:
        typer.echo("Errors:")
        for err in res.errors:
            typer.echo(f"  - {err}")
    if res.warnings:
        typer.echo("Warnings:")
        for warn in res.warnings:
            typer.echo(f"  - {warn}")

    if report_output:
        report_path = Path(report_output)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report = res.model_dump(mode="json", exclude_none=True)
        report["proposal_status"] = prop.status
        report["review_packet"] = artifacts["review_packet"]
        report["human_approval_manifest"] = artifacts["human_approval_manifest"]
        report_path.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        typer.echo(f"Report output: {report_output}")

    _write_separate_validation_outputs(
        artifacts, packet_output, manifest_output, markdown_output, repo,
        [proposal, config, report_output, evidence_output], approval_token,
    )
    if not res.valid or not res.applies_cleanly or (run_tests and res.tests_status != "passed") or (run_full_suite and res.full_suite_status != "passed"):
        raise typer.Exit(code=1)


github_app = typer.Typer(help="Read-only GitHub pull-request review (no patches, merges, or approvals).")
app.add_typer(github_app, name="github")


@github_app.command(name="review")
def github_review(
    repo: str = typer.Option(..., "--repo", help="GitHub repository slug (OWNER/REPO)."),
    pr: int = typer.Option(..., "--pr", min=1, help="Pull-request number."),
    local_repo: str = typer.Option(".", "--local-repo", help="Path to the local Git checkout containing the PR commits."),
    post: bool = typer.Option(False, "--post", help="Publish review comments. Without this flag the run is draft/dry-run."),
    inline: bool = typer.Option(False, "--inline", help="Post inline comments on added diff lines (falls back to issue comments with a visible reason otherwise)."),
    summary_comment: bool = typer.Option(False, "--summary-comment", help="Post one consolidated, idempotent summary comment for this head SHA."),
    check_run: bool = typer.Option(False, "--check-run", help="Create or update a CodeAtlas check run for the head SHA (never a review verdict)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Force dry-run mode; never publishes anything."),
    review_provider: Optional[str] = typer.Option(None, "--review-provider", help="Optional reviewer provider (e.g. mock:echo_changed)."),
    json_output: Optional[str] = typer.Option(None, "--json-output", help="Write the GitHub review report JSON here."),
    evidence_output: Optional[str] = typer.Option(None, "--evidence-output", help="Write lifecycle JSONL here."),
) -> None:
    """Review a GitHub pull request read-only and optionally post comments.

    Default is draft/dry-run mode with zero write requests.  --post is
    required to publish comments; posting never approves, merges, applies
    patches, or changes repository state.
    """
    from codeatlas.github import GitHubError, run_github_pr_review

    if post and dry_run:
        typer.echo("Error: --post and --dry-run are mutually exclusive.", err=True)
        raise typer.Exit(code=1)

    try:
        report = run_github_pr_review(
            repo,
            pr,
            local_repo=local_repo,
            post=post,
            inline=inline,
            summary_comment=summary_comment,
            check_run=check_run,
            review_provider=review_provider,
            evidence_output=evidence_output,
            json_output=json_output,
        )
    except GitHubError as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=1)

    typer.echo("CodeAtlas GitHub PR review (read-only)")
    typer.echo(f"Repository:       {report.repository}")
    typer.echo(f"Pull request:     #{report.pr_number}")
    typer.echo(f"Head SHA:         {report.head_sha[:12]}")
    typer.echo(f"Mode:             {report.mode}")
    typer.echo(f"Findings:         {report.findings_total} total, {report.findings_anchored} diff-anchored")
    typer.echo(f"Comments planned: {report.comments_planned}")
    if post:
        typer.echo(f"Comments posted:  {report.comments_posted} (inline: {report.inline_comments_posted}, issue: {report.issue_comments_posted})")
        typer.echo(f"Duplicates skipped: {report.comments_skipped_duplicate}")
    typer.echo(f"Unanchored suppressed: {report.comments_suppressed_unanchored}")
    typer.echo(f"Redacted suppressed: {report.comments_suppressed_redacted}")
    if inline:
        typer.echo(f"Issue-comment fallbacks: {report.comments_fallback_issue}")
        for reason in report.fallback_reasons:
            typer.echo(f"  fallback: {reason}")
        typer.echo(f"Outside-diff suppressed: {report.comments_suppressed_outside_diff}")
    if summary_comment:
        outcome = "updated" if report.summary_comment_updated else "posted" if report.summary_comment_posted else "not written"
        typer.echo(f"Summary comment:  {outcome}"
                   + (f" (id {report.summary_comment_id})" if report.summary_comment_id else ""))
        if report.summary_comment_truncated:
            typer.echo("Summary truncated deterministically to fit the size limit.")
        if report.summary_comment_failure_reason:
            typer.echo(f"Summary failure:  {report.summary_comment_failure_reason}")
    if check_run:
        if report.check_run_created or report.check_run_updated:
            typer.echo(f"Check run:        {report.check_run_conclusion} (id {report.check_run_id})")
        elif report.check_run_failure_reason:
            typer.echo(f"Check run:        not written ({report.check_run_failure_reason})")
        else:
            typer.echo(f"Check run:        {report.check_run_conclusion or 'computed'} (dry-run)")
    if report.partial_write:
        typer.echo("Partial write:    some write groups were skipped; see errors", err=True)
    typer.echo(f"Write calls:      {report.write_calls}")
    typer.echo(f"Policy decision:  {report.review_policy}")
    if json_output:
        typer.echo(f"JSON output:      {json_output}")
    if report.errors:
        for error in report.errors:
            typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=1)


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host", help="Host address to bind (defaults to localhost)."),
    port: int = typer.Option(8765, "--port", min=1, max=65535, help="Port to listen on."),
    repair_config: Optional[str] = typer.Option(None, "--repair-config", help="Structured repair provider configuration (JSON/YAML; credentials via environment)."),
    database: Optional[str] = typer.Option(None, "--database", help="SQLite service database (defaults to ~/.codeatlas/service.sqlite3)."),
) -> None:
    """Start the versioned local API for CLI, CI, and IDE clients."""
    from codeatlas.service import run_service
    from codeatlas.providers import ProviderConfigError
    from codeatlas.service.persistence import PersistenceError

    if host not in {"127.0.0.1", "localhost", "::1"}:
        typer.echo(f"Warning: binding to non-localhost address '{host}'. Do not expose publicly.", err=True)

    try:
        run_service(host=host, port=port, repair_config=repair_config, database_path=database)
    except ProviderConfigError:
        typer.echo("Invalid repair provider configuration. Check the configured mode, provider, and limits.", err=True)
        raise typer.Exit(code=1)
    except PersistenceError as err:
        typer.echo(f"Service database unavailable [{err.code}]", err=True)
        raise typer.Exit(code=1)
    except OSError as err:
        typer.echo(f"Failed to start service on {host}:{port}: {err}", err=True)
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
