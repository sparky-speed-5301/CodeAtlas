"""Phase 11C-F child fixture: the real production service over a real repository.

Unlike the Phase 10E fixture nothing is scripted: the real review pipeline,
repair orchestrator, isolated validator, and the Phase 11C-D/E apply/revert
operations all run for real against a real Git working tree. The inherited
control pipe (fd 3) exists only to mint scoped approval tokens (an explicit
operator action that the production service itself never performs) and to
expose a bounded request audit; it never adds or bypasses an HTTP endpoint.
"""

from __future__ import annotations

import argparse
import json
import os
import threading
from pathlib import Path
from urllib.parse import urlparse

sys_path = str(Path(__file__).resolve().parents[3] / "src")
if sys_path not in __import__("sys").path:
    __import__("sys").path.insert(0, sys_path)

from codeatlas.patching.proposal import generate_validation_approval_token
from codeatlas.service.server import ReviewHttpHandler, create_server
from codeatlas.service.state import ReviewStateManager



class FixtureManager(ReviewStateManager):
    def __init__(self) -> None:
        super().__init__()
        self.audit: list[dict] = []

    def record(self, method: str, path: str, status: int) -> None:
        self.audit.append({"method": method, "path": path, "status": status})


manager = FixtureManager()


class AuditHandler(ReviewHttpHandler):
    state_manager = manager

    def _send_json(self, status, data):
        try:
            manager.record(self.command, urlparse(self.path).path, status)
        except Exception:
            pass
        super()._send_json(status, data)


def control(stream) -> None:
    for line in stream:
        request = json.loads(line)
        op = request["op"]
        try:
            if op == "mint_fix_token":
                fix_record = manager._fix_proposals[request["proposal_id"]]
                response = fix_record.response
                result = generate_validation_approval_token(
                    proposal_id=response.proposal_id,
                    finding_id=response.finding_id,
                    run_id=response.run_id,
                    repository_identity=response.repository,
                    base_commit=response.base_commit,
                    head_commit=response.head_commit,
                    patch_hash=response.patch_hash,
                    target_files=response.target_files,
                    operation=request["operation"],
                )
            elif op == "audit":
                result = {"audit": manager.audit}
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
    server.RequestHandlerClass = AuditHandler
    pipe = os.fdopen(3, "r+b", buffering=0)
    threading.Thread(target=control, args=(pipe,), daemon=True).start()
    server.serve_forever(poll_interval=0.05)
