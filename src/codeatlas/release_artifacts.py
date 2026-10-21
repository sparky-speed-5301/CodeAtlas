"""Independent, bounded release-artifact identity and checksum verification."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Literal
from zipfile import BadZipFile, ZipFile

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codeatlas.git.executable import run_git
from codeatlas.orchestrator.repair_models import repair_payload_is_safe

ArtifactStatus = Literal["passed", "failed", "blocked", "unavailable", "not_run"]
ArtifactType = Literal["wheel", "vsix", "checksum_manifest"]
MAX_ARTIFACT_BYTES = 32 * 1024 * 1024
MAX_ARTIFACT_ENTRIES = 1_000


class ArtifactInventoryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    file_name: str = Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    size_bytes: int = Field(ge=0, le=MAX_ARTIFACT_BYTES)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ArtifactCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: Literal[
        "checksum_verification",
        "artifact_inventory",
        "package_boundary",
        "secret_scan",
        "reproducibility",
        "source_revision_identity",
    ]
    status: ArtifactStatus
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")


class ReleaseArtifactEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    report_version: Literal["release-artifact-evidence-v1"] = "release-artifact-evidence-v1"
    status: ArtifactStatus
    source_revision: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    source_tree_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    reproducibility_identity: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    artifacts: tuple[ArtifactInventoryEntry, ...] = Field(default=(), max_length=20)
    checks: tuple[ArtifactCheck, ...] = Field(min_length=6, max_length=6)
    limitations: tuple[str, ...] = Field(default=(), max_length=20)
    production_ready: Literal[False] = False

    @model_validator(mode="after")
    def validate_report(self) -> ReleaseArtifactEvidence:
        if len({item.file_name for item in self.artifacts}) != len(self.artifacts):
            raise ValueError("artifact inventory entries must be unique")
        if self.status == "passed" and any(check.status != "passed" for check in self.checks):
            raise ValueError("passed artifact evidence requires every check to pass")
        if self.status in {"blocked", "unavailable", "not_run"} and not self.limitations:
            raise ValueError("non-passed artifact evidence requires limitations")
        if not repair_payload_is_safe(self.model_dump(mode="json")):
            raise ValueError("artifact evidence contains secret-like material")
        return self


def load_artifact_evidence(path: str | Path) -> ReleaseArtifactEvidence:
    source = Path(path).expanduser().absolute()
    if source.is_symlink() or not source.is_file() or source.stat().st_nlink != 1:
        raise ValueError("artifact evidence must be an unlinked regular file")
    if source.stat().st_size > MAX_ARTIFACT_BYTES:
        raise ValueError("artifact evidence exceeds its fixed size bound")
    try:
        return ReleaseArtifactEvidence.model_validate_json(source.read_bytes())
    except (OSError, ValueError) as error:
        raise ValueError("artifact evidence is invalid") from error


def _file_digest(path: Path) -> tuple[int, str]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("artifact must be an unlinked regular file")
    size = path.stat().st_size
    if size > MAX_ARTIFACT_BYTES:
        raise ValueError("artifact exceeds its fixed size bound")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return size, digest.hexdigest()


def verify_checksum_manifest(
    directory: str | Path, manifest_name: str = "SHA256SUMS"
) -> tuple[ArtifactStatus, tuple[ArtifactInventoryEntry, ...], str]:
    """Verify an explicit checksum manifest without discovering other directories."""
    root = Path(directory).expanduser().absolute()
    manifest = root / manifest_name
    if not manifest.is_file() or manifest.is_symlink():
        return "unavailable", (), "checksum_manifest_unavailable"
    entries: list[ArtifactInventoryEntry] = []
    seen: set[str] = set()
    try:
        lines = manifest.read_text(encoding="utf-8").splitlines()
    except OSError:
        return "unavailable", (), "checksum_manifest_unavailable"
    for line in lines:
        match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9][A-Za-z0-9._-]*)", line)
        if match is None:
            return "failed", tuple(entries), "checksum_manifest_format_invalid"
        expected, name = match.groups()
        if name in seen:
            return "failed", tuple(entries), "checksum_manifest_duplicate"
        seen.add(name)
        try:
            size, actual = _file_digest(root / name)
        except (OSError, ValueError):
            return "failed", tuple(entries), "checksum_artifact_unavailable"
        entries.append(ArtifactInventoryEntry(file_name=name, size_bytes=size, sha256=actual))
        if actual != expected:
            return "failed", tuple(entries), "checksum_mismatch"
    return "passed", tuple(entries), "checksums_verified"


def verify_package_boundary(path: str | Path) -> tuple[ArtifactStatus, str]:
    artifact = Path(path).expanduser().absolute()
    try:
        size, _ = _file_digest(artifact)
        with ZipFile(artifact) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ARTIFACT_ENTRIES or sum(item.file_size for item in infos) > MAX_ARTIFACT_BYTES:
                return "failed", "archive_bounds_exceeded"
            names = [item.filename for item in infos]
            if len({name.casefold() for name in names}) != len(names):
                return "failed", "archive_duplicate_entry"
            if artifact.suffix == ".whl":
                allowed = all(
                    name.startswith("codeatlas/") or ".dist-info/" in name
                    for name in names
                )
                if not allowed:
                    return "failed", "wheel_boundary_invalid"
            elif artifact.suffix == ".vsix":
                allowed = all(
                    name in {"extension.vsixmanifest", "[Content_Types].xml"}
                    or name.startswith("extension/")
                    for name in names
                )
                if not allowed:
                    return "failed", "vsix_boundary_invalid"
        return "passed", "package_boundary_verified"
    except (OSError, BadZipFile, ValueError):
        return "failed", "package_boundary_invalid"


def source_revision_identity(root: str | Path) -> tuple[ArtifactStatus, str | None, str | None, str]:
    """Return immutable Git revision and a digest of declared release inputs."""
    repository = Path(root).expanduser().absolute()
    try:
        revision = run_git(("rev-parse", "HEAD"), cwd=repository).stdout.strip()
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            return "blocked", None, None, "source_revision_invalid"
        status = run_git(("status", "--porcelain", "--untracked-files=all"), cwd=repository).stdout
        if status:
            return "blocked", revision, None, "source_worktree_dirty"
        digest = hashlib.sha256()
        inputs = (repository / "pyproject.toml", repository / "README.md")
        inputs += tuple(sorted((repository / "src" / "codeatlas").rglob("*.py")))
        inputs += tuple(sorted((repository / "extension").rglob("*")))
        for path in inputs:
            if path.is_file() and not path.is_symlink():
                relative = path.relative_to(repository).as_posix().encode("utf-8")
                data = path.read_bytes()
                digest.update(len(relative).to_bytes(4, "big"))
                digest.update(relative)
                digest.update(len(data).to_bytes(8, "big"))
                digest.update(data)
        return "passed", revision, digest.hexdigest(), "source_identity_verified"
    except (OSError, ValueError):
        return "unavailable", None, None, "source_revision_unavailable"


def build_artifact_evidence(
    root: str | Path,
    artifact_directory: str | Path,
    *,
    reproducible: bool,
) -> ReleaseArtifactEvidence:
    """Verify explicitly supplied release artifacts and their source identity."""
    checksum_status, inventory, checksum_reason = verify_checksum_manifest(artifact_directory)
    source_status, revision, tree_digest, source_reason = source_revision_identity(root)
    secret_status: ArtifactStatus = "passed"
    for item in inventory:
        if Path(item.file_name).suffix not in {".whl", ".vsix"}:
            continue
        try:
            from eval.eval_release_evidence import scan_archive

            if scan_archive(Path(artifact_directory) / item.file_name)["status"] != "passed":
                secret_status = "failed"
        except (OSError, ValueError, BadZipFile):
            secret_status = "failed"
    package_files = [
        item for item in inventory if Path(item.file_name).suffix in {".whl", ".vsix"}
    ]
    package_status: ArtifactStatus = "unavailable"
    if package_files:
        package_status = "passed" if all(
            verify_package_boundary(Path(artifact_directory) / item.file_name)[0] == "passed"
            for item in package_files
        ) else "failed"
    checks = [
        ArtifactCheck(name="checksum_verification", status=checksum_status, reason_code=checksum_reason),
        ArtifactCheck(
            name="artifact_inventory",
            status="passed" if inventory else "unavailable",
            reason_code="inventory_recorded" if inventory else "inventory_unavailable",
        ),
        ArtifactCheck(
            name="package_boundary",
            status=package_status,
            reason_code="package_boundaries_verified" if package_status == "passed" else "package_boundaries_unavailable",
        ),
        ArtifactCheck(name="secret_scan", status=secret_status, reason_code="artifact_secret_scan_verified" if secret_status == "passed" else "artifact_secret_scan_failed"),
        ArtifactCheck(name="reproducibility", status="passed" if reproducible else "failed", reason_code="reproducibility_verified" if reproducible else "reproducibility_failed"),
        ArtifactCheck(name="source_revision_identity", status=source_status, reason_code=source_reason),
    ]
    statuses = {item.status for item in checks}
    status: ArtifactStatus = "passed" if statuses == {"passed"} else "failed" if "failed" in statuses else "blocked" if "blocked" in statuses else "unavailable"
    limitations: tuple[str, ...] = () if status == "passed" else ("Artifact evidence is incomplete or the source worktree is not an immutable release input.",)
    identity = None
    if tree_digest is not None and inventory:
        identity_digest = hashlib.sha256()
        identity_digest.update(tree_digest.encode("ascii"))
        for item in inventory:
            identity_digest.update(item.file_name.encode("utf-8"))
            identity_digest.update(item.sha256.encode("ascii"))
        identity = identity_digest.hexdigest()
    return ReleaseArtifactEvidence(
        status=status,
        source_revision=revision,
        source_tree_sha256=tree_digest,
        reproducibility_identity=identity,
        artifacts=inventory,
        checks=tuple(checks),
        limitations=limitations,
    )


__all__ = [
    "ArtifactCheck",
    "ArtifactInventoryEntry",
    "ReleaseArtifactEvidence",
    "build_artifact_evidence",
    "load_artifact_evidence",
    "source_revision_identity",
    "verify_checksum_manifest",
    "verify_package_boundary",
]
