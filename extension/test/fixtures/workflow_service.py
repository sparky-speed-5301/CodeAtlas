"""Phase 10E child fixture: production HTTP/approval/sandbox code, scripted review/test provider.

Control travels over an inherited private pipe (fd 3), never a service endpoint.
No provider, credential, repository test process, or external network is needed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

import codeatlas.patching.apply as patch_apply
import codeatlas.service.state as service_state
from codeatlas.patching.proposal import compute_patch_hash, generate_approval_token
from codeatlas.review.packet import ReviewPacket, redact_text
from codeatlas.service import models
from codeatlas.service.server import ReviewHttpHandler, create_server
from codeatlas.service.state import ReviewRunRecord, ReviewStateManager, validate_service_repo_path
from codeatlas.verification.models import TestResult

CLAIM = "Fixture requires operator review"
LIMITATION = "Deterministic workflow fixture; not a live reviewer assessment."
SECRET = "sk-phase10e-synthetic-secret-canary"
STATES = ["preparing", "indexing", "analyzing", "reviewing", "findings_ready", "completed"]


def git(root, *args):
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


class FixtureManager(ReviewStateManager):
    def __init__(self):
        super().__init__()
        self.scenario = "success"
        self.fault = None
        self.audit = []
        self.contract_errors = []
        self.events = []
        self.validation_reports = []
        self.test_calls = []
        self.next_run = 0

    def start_review(self, request):
        root = validate_service_repo_path(request.repo)
        assert request.review_provider == "mock"
        self.next_run += 1
        run_id = f"rev-workflow-{self.next_run}"
        record = ReviewRunRecord(run_id, request, root)
        record.status = "preparing"
        record.scenario = self.scenario
        record.progress_text = "Fixture: preparing"
        record.policy_decision = "review_only"
        record.limitations = [LIMITATION]
        # Resolved revisions mirror the real contract: the head commit is the
        # fixture HEAD; the base commit resolves the requested base ref.  The
        # unresolved_shas scenario simulates a run whose manifest has not
        # resolved revisions yet.
        try:
            base_commit = None if self.scenario == "unresolved_shas" else git(root, "rev-parse", request.base)
        except Exception:
            base_commit = None
        record.result = SimpleNamespace(manifest=SimpleNamespace(
            head_commit=None if self.scenario == "unresolved_shas" else git(root, "rev-parse", "HEAD"),
            base_commit=base_commit,
        ))
        record.packet = ReviewPacket(
            packet_id=f"packet-{run_id}", repository=str(root),
            changed_line_ranges={"src/app.py": [[3, 3]]},
            changed_symbols=[{"file": "src/app.py", "kind": "function", "name": "run", "start_line": 1, "end_line": 3}],
            relevant_tests=["tests/test_app.py"],
        )
        self._reviews[run_id] = record
        return super().get_review_status(run_id)

    def cancel_review(self, run_id: str) -> bool:
        cancelled = super().cancel_review(run_id)
        if cancelled:
            record = self._reviews.get(run_id)
            # A cancelled run never resolves revisions; drop the eager fixture
            # manifest so the status payload reports unresolved SHAs.
            if record is not None and record.status == "cancelled":
                record.result = None
        return cancelled

    def get_review_status(self, run_id):
        record = self.get_review(run_id)
        if record and record.status in STATES[:-1]:
            record.status = STATES[STATES.index(record.status) + 1]
            record.progress_text = f"Fixture: {record.status}"
            if record.scenario == "provider_failure" and record.status == "reviewing":
                record.status = "failed"
                record.errors = ["Deterministic provider failed; no findings produced."]
            if record.status == "findings_ready":
                record.findings = [{
                    "id": f"finding-{run_id}", "file": "src/app.py", "line": 3,
                    "start_line": 3, "end_line": 3, "severity": "high", "category": "code_quality",
                    "claim": CLAIM, "confidence": 0.95, "evidence_strength": "supported",
                    "status": "review_only", "impact": "Fixture review evidence needs operator inspection.",
                    "limitations": [LIMITATION], "tools_consulted": ["deterministic-fixture"],
                    "evidence": ["Exact changed line: src/app.py:3", redact_text(f'api_key = "{SECRET}"')[0]],
                }]
                if record.scenario == "invalid_range":
                    record.findings[0].update(start_line=-10, end_line=9999)
                if record.scenario == "inverted_range":
                    # Malformed service data: end before start must never crash
                    # editor decorations (real vscode.Range throws).
                    record.findings[0].update(start_line=3, end_line=2)
                if record.scenario == "missing_file":
                    record.findings[0]["file"] = "src/missing.py"
        return super().get_review_status(run_id)

    def emit(self, event, **details):
        self.events.append({"event": event, **details})

    def create_patch_proposal(self, finding_id, run_id=None):
        response = super().create_patch_proposal(finding_id, run_id)
        proposal, _ = self.get_proposal(response.proposal_id)
        # The product's generic draft is a placeholder. The fake patch provider adds
        # deterministic context so the real git-apply path can validate this fixture.
        proposal.unified_diff = (
            "--- a/src/app.py\n+++ b/src/app.py\n@@ -1,5 +1,5 @@\n"
            " def run(value):\n     return value\n"
            f"-# CodeAtlas finding: {CLAIM}\n"
            f"+# TODO(operator): resolved CodeAtlas finding {finding_id}\n"
            " \n # end\n"
        )
        proposal.patch_hash = compute_patch_hash(proposal.unified_diff)
        response.unified_diff = proposal.unified_diff
        return response


manager = FixtureManager()
real_validate = service_state.validate_patch_proposal


def observed_validation(**kwargs):
    """Observe real gates/application/cleanup without changing service response semantics."""
    result = real_validate(**kwargs, evidence=manager)
    manager.validation_reports.append(result.model_dump(mode="json"))
    return result


def fixture_test_executor(sandbox_path, command, **kwargs):
    """Deterministic test outcome at the executor boundary; no test subprocess is run."""
    proposal, root = manager.get_proposal(kwargs["proposal_id"])
    sandbox_path = Path(sandbox_path)
    assert sandbox_path.resolve() != root.resolve()
    assert git(sandbox_path, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"
    assert "TODO(operator)" in (sandbox_path / "src/app.py").read_text()
    assert "TODO(operator)" not in (root / "src/app.py").read_text()
    assert kwargs["limits"].network_allowed is False
    assert kwargs["limits"].dependency_install_allowed is False
    assert kwargs["extra_tokens"]  # approval is passed only to the redaction boundary
    assert "tests/test_app.py" in command
    failed = manager.scenario == "test_failure"
    status = "failed" if failed else "passed"
    manager.test_calls.append({"sandbox": str(sandbox_path), "command": command, "status": status})
    manager.emit("fixture_test_outcome", status=status, sandbox_id=kwargs["sandbox_id"])
    return TestResult(
        status=status, runner="pytest", language="python", command=command,
        tests_run=["tests/test_app.py"], tests_passed=0 if failed else 1,
        tests_failed=1 if failed else 0, exit_code=1 if failed else 0,
        failures=["Deterministic fixture assertion failed"] if failed else [],
        failed_test_names=["tests/test_app.py::test_run"] if failed else [],
        execution_allowed=True, execution_started=True,
        sandbox_id=kwargs["sandbox_id"], proposal_id=proposal.proposal_id,
        diagnostics_limitations=["Synthetic test executor outcome; no repository test process executed."],
        network_isolation_verified=False,
    )


service_state.validate_patch_proposal = observed_validation
patch_apply.execute_test_command = fixture_test_executor


def response_model(path):
    if path == "/health":
        return models.HealthResponse
    if path == "/reviews" or re.fullmatch(r"/reviews/[^/]+", path):
        return models.ReviewStatusResponse
    if re.fullmatch(r"/reviews/[^/]+/findings", path):
        return models.FindingsListResponse
    if re.fullmatch(r"/reviews/[^/]+/findings/[^/]+", path):
        return models.FindingDetailResponse
    return {"cancel": models.CancelResponse, "explain": models.ExplainResponse,
            "patch-proposal": models.PatchProposalResponse, "validate": models.ValidateProposalResponse}.get(path.rsplit("/", 1)[-1])


class ContractHandler(ReviewHttpHandler):
    state_manager = manager

    def _read_json_body(self):
        body = super()._read_json_body()
        path = urlparse(self.path).path
        model = models.ReviewCreateRequest if path == "/reviews" else {
            "explain": models.ExplainRequest, "patch-proposal": models.PatchProposalRequest,
            "validate": models.ValidateProposalRequest,
        }.get(path.rsplit("/", 1)[-1])
        # Deliberately invalid requests are separately asserted as HTTP 400 by the driver.
        if model and self.headers.get("X-Workflow-Invalid") != "true":
            try:
                assert set(body) <= set(model.model_fields), "Unknown request field"
                model.model_validate(body, strict=True)
            except Exception:
                manager.contract_errors.append(f"Invalid {model.__name__}")
        self.safe_body = {k: ("[REDACTED]" if k == "approval_token" else v) for k, v in body.items()}
        return body

    def _send_json(self, status, data):
        path = urlparse(self.path).path
        model = response_model(path)
        if 200 <= status < 300 and model:
            try:
                assert set(data) == set(model.model_fields), "Response field drift"
                model.model_validate(data, strict=True)
            except Exception:
                manager.contract_errors.append(f"Invalid {model.__name__}")
        elif status >= 400:
            assert set(data) == {"error", "message"}
            assert all(isinstance(v, str) for v in data.values())
        manager.audit.append({"method": self.command, "path": self.path,
                              "body": getattr(self, "safe_body", None), "status": status, "response": data})
        super()._send_json(status, data)

    def do_GET(self):
        fault, manager.fault = manager.fault, None
        if fault == "malformed":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"invalid":')
            return
        if fault == "bad_health":
            # Intentionally bypass contract checking to exercise safeHealth's schema gate.
            super()._send_json(200, {"status": "ok", "service": "wrong-service", "version": "0.1.0"})
            return
        if fault == "secret_error":
            # Hostile body is never retained in the fixture's diagnostic transcript.
            super()._send_json(503, {"error": "provider_error", "message": SECRET})
            return
        super().do_GET()


def control(stream):
    for line in stream:
        request = json.loads(line)
        op = request["op"]
        try:
            if op == "scenario":
                assert request["value"] in {"success", "provider_failure", "test_failure", "invalid_range",
                                            "inverted_range", "missing_file", "unresolved_shas"}
                manager.scenario = request["value"]
                result = True
            elif op == "fault":
                manager.fault = request["value"]
                result = True
            elif op == "expire":
                manager._reviews.pop(request["run_id"])
                result = True
            elif op == "approve":
                # Only explicit test-driver operator action can mint tokens; never automatic.
                proposal, _ = manager.get_proposal(request["proposal_id"])
                scope = dict(proposal_id=proposal.proposal_id, base_commit=proposal.base_commit,
                             patch_hash=proposal.patch_hash, allowed_paths=proposal.target_files, run_id="default")
                field = request.get("wrong_scope")
                if field:
                    assert field in scope
                    scope[field] = ["different.py"] if field == "allowed_paths" else "wrong-scope"
                result = generate_approval_token(**scope)
            elif op == "inspect":
                result = {"audit": manager.audit, "contract_errors": manager.contract_errors,
                          "events": manager.events, "validations": manager.validation_reports,
                          "test_calls": manager.test_calls,
                          "sandboxes_removed": all(not Path(c["sandbox"]).exists() for c in manager.test_calls)}
            elif op == "noise":
                os.write(1, b"sk-phase10e-")
                os.write(1, b"synthetic-secret-canary")
                os.write(2, b"CAT-APP-synthetic-log-canary raw_prompt")
                result = True
            elif op == "crash":
                os._exit(17)
            else:
                raise ValueError("Unknown fixture control")
            reply = {"id": request["id"], "result": result}
        except Exception:
            reply = {"id": request["id"], "error": "Fixture control failed"}
        stream.write((json.dumps(reply) + "\n").encode())
        stream.flush()
    # Parent disappeared: never leave the fixture running as an orphan.
    os._exit(0)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", choices=["127.0.0.1"], required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    server = create_server(args.host, args.port, manager)
    server.RequestHandlerClass = ContractHandler
    pipe = os.fdopen(3, "r+b", buffering=0)
    threading.Thread(target=control, args=(pipe,), daemon=True).start()
    server.serve_forever(poll_interval=0.05)
