import importlib.util
from pathlib import Path
from typing import Optional

import typer

from codeatlas.orchestrator import run_review

app = typer.Typer(help="Evidence-first repository-aware code review.")


@app.command()
def review(
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
) -> None:
    """Collect read-only repository and diff evidence for a review run."""
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
        banner_callback=typer.echo,
        allow_patch_suggestions=allow_patch_suggestions,
    )
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


@app.command()
def fix(finding: str, approve: bool = False) -> None:
    """Plan a validation-gated fix placeholder."""
    typer.echo(f"Fix scaffold for {finding}; approved={approve}")


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


if __name__ == "__main__":
    app()
