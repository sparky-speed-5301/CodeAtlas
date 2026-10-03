"""Human-readable rendering of the packet's bounded execution evidence."""

from .packet import ReviewPacket


def render_test_evidence(packet: ReviewPacket) -> str:
    lines = [
        f"Test status: targeted={packet.tests_status}; full_suite={packet.full_suite_status}",
        f"Test evidence attached: {str(packet.observed_test_evidence is not None).lower()}",
    ]
    observed = packet.observed_test_evidence
    if observed:
        lines.extend([
            f"Observed scope: {'full-suite' if observed.full_suite else 'targeted'}; {observed.status}",
            f"Runner: {observed.runner} ({observed.language})",
            f"Targets: {', '.join(observed.targets) or '(runner discovery scope)'}",
            f"Counts: {observed.tests_passed} passed / {observed.tests_failed} failed / {observed.tests_skipped} skipped",
            f"Duration: {observed.duration_ms:.2f} ms",
            f"Redaction: {'passed' if observed.redaction_audit.safe else 'failed'}",
            f"Output truncated: {str(observed.output_truncated).lower()}",
            f"Network policy: {observed.network_policy_requested}; enforced={str(observed.network_policy_enforced).lower()} (policy/process only)",
        ])
        if observed.failure_summary:
            lines.append(f"Failure: {observed.failure_summary}")
        if observed.failed_test_names:
            lines.append(f"Failed tests: {', '.join(observed.failed_test_names)}")
        if observed.stack_trace_summary:
            lines.append(f"Stack summary:\n{observed.stack_trace_summary}")
    else:
        lines.append(f"Evidence exclusion: {packet.test_evidence_exclusion_reason or 'not_run'}")
        lines.append("Redaction: failed" if packet.test_evidence_exclusion_reason in {"output_redaction_failed", "packet_redaction_failed"} else "Redaction: no attached output")
    lines.append(f"Network isolation: {'verified' if packet.network_isolation_verified else 'not independently verified'} (network_isolation_verified={str(packet.network_isolation_verified).lower()})")
    limitations = list(dict.fromkeys(packet.limitations + (observed.limitations if observed else [])))
    lines.extend(f"Limitation: {lim}" for lim in limitations)
    return "\n".join(lines) + "\n"
