"""Destination safety and redaction for separate validation artifact exports."""

import json
from pathlib import Path

import pytest

from codeatlas.orchestrator.artifacts import ArtifactOutputError, write_validation_artifacts
from codeatlas.orchestrator.manifest import RunManifest
from codeatlas.review.packet import ReviewPacket


@pytest.fixture
def artifacts():
    return {
        "review_packet": ReviewPacket(packet_id="packet-1", repository="repo").model_dump(mode="json"),
        "human_approval_manifest": RunManifest(
            run_id="run-1", repository="repo", base_ref="HEAD", head_ref="HEAD",
        ).model_dump(mode="json"),
    }


def test_no_options_have_no_filesystem_effect(artifacts, tmp_path):
    write_validation_artifacts(artifacts, repository_root=tmp_path / "repo")
    assert list(tmp_path.iterdir()) == []


def test_replaces_existing_external_artifact_atomically(artifacts, tmp_path):
    destination = tmp_path / "packet.json"
    destination.write_text("old contents", encoding="utf-8")
    write_validation_artifacts(artifacts, packet_output=str(destination), repository_root=tmp_path / "repo")
    assert json.loads(destination.read_text()) == artifacts["review_packet"]
    assert not list(tmp_path.glob(".codeatlas-*.tmp"))


@pytest.mark.parametrize("case", ["directory", "parent_file", "empty", "nul", "git_metadata", "existing_repository_file"])
def test_invalid_destinations_leave_other_output_unwritten(artifacts, tmp_path, case):
    repo = tmp_path / "repo"
    repo.mkdir()
    destination = tmp_path / "invalid"
    if case == "directory":
        destination.mkdir()
    elif case == "parent_file":
        destination.write_text("preserve")
        destination = destination / "output.json"
    elif case == "empty":
        destination = ""
    elif case == "nul":
        destination = "invalid\0path"
    elif case == "git_metadata":
        destination = repo / ".git" / "output.json"
    else:
        destination = repo / "source.py"
        destination.write_text("preserve")
    other = tmp_path / "new-parent" / "packet.json"
    with pytest.raises(ArtifactOutputError, match="--manifest-output"):
        write_validation_artifacts(
            artifacts, packet_output=str(other), manifest_output=str(destination), repository_root=repo,
        )
    assert not other.parent.exists()
    assert not (repo / ".git").exists()
    if case == "existing_repository_file":
        assert destination.read_text() == "preserve"


@pytest.mark.parametrize("alias", ["same_file", "parent_of_output", "child_of_output", "input_file", "report_file"])
def test_destination_collisions_fail_before_writing(artifacts, tmp_path, alias):
    packet = tmp_path / "new" / "packet.json"
    manifest = tmp_path / "new" / "manifest.json"
    protected = []
    if alias == "same_file":
        manifest = packet
    elif alias == "parent_of_output":
        manifest = packet.parent
    elif alias == "child_of_output":
        manifest = packet / "manifest.json"
    else:
        protected = [packet if alias == "input_file" else manifest]
    with pytest.raises(ArtifactOutputError, match="output"):
        write_validation_artifacts(
            artifacts, packet_output=str(packet), manifest_output=str(manifest),
            repository_root=tmp_path / "repo", protected_paths=protected,
        )
    assert not packet.parent.exists()


@pytest.mark.parametrize("kind", ["is_symlink", "is_junction"])
def test_linked_parents_are_rejected(artifacts, tmp_path, monkeypatch, kind):
    parent = tmp_path / "linked"
    original = getattr(Path, kind, lambda self: False)
    monkeypatch.setattr(Path, kind, lambda self: self == parent or original(self), raising=False)
    with pytest.raises(ArtifactOutputError, match="links and junctions"):
        write_validation_artifacts(
            artifacts, packet_output=str(parent / "packet.json"), repository_root=tmp_path / "repo",
        )
    assert not parent.exists()


@pytest.mark.parametrize("secret", [
    "ghp_" + "X" * 36,
    'api_key="example-private-value-123456789"',
    "postgres://user:private-password@localhost/database",
    "CAT-APP-" + "a" * 32,
    "short",
])
def test_secret_rejection_before_json_escaping_or_file_creation(artifacts, tmp_path, secret):
    artifacts["human_approval_manifest"]["validation_limitations"] = [secret]
    packet = tmp_path / "new" / "packet.json"
    manifest = tmp_path / "new" / "manifest.json"
    with pytest.raises(ArtifactOutputError, match="redaction check failed") as exc:
        write_validation_artifacts(
            artifacts, packet_output=str(packet), manifest_output=str(manifest),
            repository_root=tmp_path / "repo", extra_tokens=["short"],
        )
    assert secret not in str(exc.value)
    assert not packet.parent.exists()


@pytest.mark.parametrize("operation", ["mkdir", "replace"])
def test_write_failure_is_clear_and_preserves_existing_files(artifacts, tmp_path, monkeypatch, operation):
    import codeatlas.orchestrator.artifacts as exporter
    destination = tmp_path / "new" / "packet.json"
    if operation == "replace":
        destination.parent.mkdir()
        destination.write_text("preserve")

    def deny(*args, **kwargs):
        raise PermissionError("sensitive OS exception text must not be printed")

    monkeypatch.setattr(Path if operation == "mkdir" else exporter.os, operation, deny)
    with pytest.raises(ArtifactOutputError, match=r"--packet-output: cannot write artifact \(PermissionError\)") as exc:
        write_validation_artifacts(artifacts, packet_output=str(destination), repository_root=tmp_path / "repo")
    assert "sensitive" not in str(exc.value)
    assert not list(tmp_path.rglob(".codeatlas-*.tmp"))
    if operation == "replace":
        assert destination.read_text() == "preserve"


def test_replaces_existing_external_markdown_atomically(artifacts, tmp_path):
    from codeatlas.review.rendering import render_test_evidence

    destination = tmp_path / "summary.md"
    destination.write_text("old contents", encoding="utf-8")
    write_validation_artifacts(artifacts, markdown_output=str(destination), repository_root=tmp_path / "repo")
    expected = render_test_evidence(ReviewPacket.model_validate(artifacts["review_packet"]))
    assert destination.read_text(encoding="utf-8") == expected
    assert not list(tmp_path.glob(".codeatlas-*.tmp"))


def test_markdown_parent_directory_creation(artifacts, tmp_path):
    from codeatlas.review.rendering import render_test_evidence

    destination = tmp_path / "new" / "nested" / "summary.md"
    write_validation_artifacts(artifacts, markdown_output=str(destination), repository_root=tmp_path / "repo")
    expected = render_test_evidence(ReviewPacket.model_validate(artifacts["review_packet"]))
    assert destination.read_text(encoding="utf-8") == expected
    assert not list(tmp_path.glob(".codeatlas-*.tmp"))


@pytest.mark.parametrize("case", ["directory", "parent_file", "empty", "nul", "git_metadata", "existing_repository_file"])
def test_invalid_markdown_destinations_rejected(artifacts, tmp_path, case):
    repo = tmp_path / "repo"
    repo.mkdir()
    destination = tmp_path / "invalid"
    if case == "directory":
        destination.mkdir()
    elif case == "parent_file":
        destination.write_text("preserve")
        destination = destination / "output.md"
    elif case == "empty":
        destination = ""
    elif case == "nul":
        destination = "invalid\0path"
    elif case == "git_metadata":
        destination = repo / ".git" / "output.md"
    else:
        destination = repo / "source.py"
        destination.write_text("preserve")
    with pytest.raises(ArtifactOutputError, match="--markdown-output"):
        write_validation_artifacts(
            artifacts, markdown_output=str(destination), repository_root=repo,
        )
    if case == "existing_repository_file":
        assert destination.read_text() == "preserve"


def test_markdown_linked_parents_are_rejected(artifacts, tmp_path, monkeypatch):
    parent = tmp_path / "linked"
    original = getattr(Path, "is_symlink", lambda self: False)
    monkeypatch.setattr(Path, "is_symlink", lambda self: self == parent or original(self), raising=False)
    with pytest.raises(ArtifactOutputError, match="links and junctions"):
        write_validation_artifacts(
            artifacts, markdown_output=str(parent / "summary.md"), repository_root=tmp_path / "repo",
        )
    assert not parent.exists()


def test_markdown_secret_rejection_before_writing(artifacts, tmp_path):
    secret = "ghp_" + "X" * 36
    artifacts["markdown_summary"] = f"Sensitive header\n{secret}\n"
    destination = tmp_path / "new" / "summary.md"
    with pytest.raises(ArtifactOutputError, match="redaction check failed") as exc:
        write_validation_artifacts(
            artifacts, markdown_output=str(destination), repository_root=tmp_path / "repo",
        )
    assert secret not in str(exc.value)
    assert not destination.parent.exists()
