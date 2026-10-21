from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

from codeatlas.git.executable import run_git
from codeatlas.release_artifacts import (
    build_artifact_evidence,
    source_revision_identity,
    verify_checksum_manifest,
    verify_package_boundary,
)


def _clean_repo(root: Path) -> None:
    run_git(("init", "--initial-branch=main"), cwd=root)
    run_git(("config", "user.name", "Release Test"), cwd=root)
    run_git(("config", "user.email", "release@example.invalid"), cwd=root)
    (root / "pyproject.toml").write_text("[project]\nname='test'\n", encoding="utf-8")
    (root / "README.md").write_text("release fixture\n", encoding="utf-8")
    (root / "src/codeatlas").mkdir(parents=True)
    (root / "src/codeatlas/__init__.py").write_text("\n", encoding="utf-8")
    (root / "extension").mkdir()
    (root / "extension/package.json").write_text("{}\n", encoding="utf-8")
    run_git(("add", "."), cwd=root)
    run_git(("commit", "-m", "release fixture"), cwd=root)


def _artifact(directory: Path, name: str = "codeatlas-0.1.0.whl") -> Path:
    path = directory / name
    with zipfile.ZipFile(path, "w") as archive:
        if path.suffix == ".whl":
            archive.writestr("codeatlas/__init__.py", b"\n")
            archive.writestr("codeatlas-0.1.0.dist-info/RECORD", b"\n")
        else:
            archive.writestr("extension.vsixmanifest", b"manifest")
    return path


def test_checksum_verification_and_tamper_detection(tmp_path: Path) -> None:
    artifact = _artifact(tmp_path)
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    (tmp_path / "SHA256SUMS").write_text(f"{digest}  {artifact.name}\n", encoding="utf-8")

    status, inventory, reason = verify_checksum_manifest(tmp_path)
    assert status == "passed"
    assert reason == "checksums_verified"
    assert inventory[0].sha256 == digest

    artifact.write_bytes(artifact.read_bytes() + b"tampered")
    status, _, reason = verify_checksum_manifest(tmp_path)
    assert status == "failed"
    assert reason == "checksum_mismatch"


def test_package_boundary_rejects_unexpected_entries(tmp_path: Path) -> None:
    artifact = _artifact(tmp_path)
    assert verify_package_boundary(artifact) == ("passed", "package_boundary_verified")
    with zipfile.ZipFile(artifact, "a") as archive:
        archive.writestr("outside.txt", b"not allowed")
    status, reason = verify_package_boundary(artifact)
    assert status == "failed"
    assert reason == "wheel_boundary_invalid"


def test_source_identity_distinguishes_clean_and_dirty_worktrees(tmp_path: Path) -> None:
    _clean_repo(tmp_path)
    status, revision, tree_digest, reason = source_revision_identity(tmp_path)
    assert status == "passed"
    assert revision is not None
    assert tree_digest is not None
    assert reason == "source_identity_verified"

    (tmp_path / "README.md").write_text("dirty\n", encoding="utf-8")
    status, revision, tree_digest, reason = source_revision_identity(tmp_path)
    assert status == "blocked"
    assert revision is not None
    assert tree_digest is None
    assert reason == "source_worktree_dirty"


def test_artifact_evidence_preserves_source_and_reproducibility_identity(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _clean_repo(root)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    wheel = _artifact(artifacts)
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    (artifacts / "SHA256SUMS").write_text(f"{digest}  {wheel.name}\n", encoding="utf-8")

    evidence = build_artifact_evidence(root, artifacts, reproducible=True)
    assert evidence.status == "passed"
    assert evidence.source_revision is not None
    assert evidence.source_tree_sha256 is not None
    assert evidence.reproducibility_identity is not None
    assert evidence.production_ready is False


def test_artifact_evidence_blocks_dirty_source(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _clean_repo(root)
    (root / "README.md").write_text("dirty\n", encoding="utf-8")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    wheel = _artifact(artifacts)
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    (artifacts / "SHA256SUMS").write_text(f"{digest}  {wheel.name}\n", encoding="utf-8")

    evidence = build_artifact_evidence(root, artifacts, reproducible=True)
    assert evidence.status == "blocked"
    assert evidence.source_revision is not None
    assert evidence.source_tree_sha256 is None
