"""Safe export of already-generated validation review artifacts."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from codeatlas.verification.redaction import redact_test_output


class ArtifactOutputError(ValueError):
    """An artifact cannot be exported safely; messages contain no payloads or paths."""


def _check_destination(path: Path, repository: Path) -> None:
    for entry in (path, *path.parents):
        if entry.is_symlink() or (hasattr(entry, "is_junction") and entry.is_junction()):
            raise ArtifactOutputError("symbolic links and junctions are not valid artifact destinations")
        if entry != path and entry.exists() and not entry.is_dir():
            raise ArtifactOutputError("an output parent is not a directory")
    if any(part.lower() == ".git" for part in path.parts):
        raise ArtifactOutputError("Git metadata is not an artifact destination")
    if path.exists():
        if not path.is_file():
            raise ArtifactOutputError("output must be a regular file")
        if path.resolve().is_relative_to(repository):
            raise ArtifactOutputError("existing repository files cannot be overwritten by artifact exports")


def _check_redaction(value: Any, tokens: Sequence[str]) -> None:
    # Audit original strings before JSON escaping can obscure sensitive patterns.
    if isinstance(value, str):
        _, audit = redact_test_output(value, extra_tokens=tokens, target_name="artifact")
        if not audit.safe or audit.raw_value_matches or any(token and token in value for token in tokens):
            raise ArtifactOutputError("artifact redaction check failed")
    elif isinstance(value, dict):
        for key, item in value.items():
            _check_redaction(key, tokens)
            _check_redaction(item, tokens)
    elif isinstance(value, list):
        for item in value:
            _check_redaction(item, tokens)


def write_validation_artifacts(
    artifacts: Mapping[str, Any],
    *,
    packet_output: str | None = None,
    manifest_output: str | None = None,
    markdown_output: str | None = None,
    repository_root: Path,
    protected_paths: Sequence[str | Path] = (),
    extra_tokens: Sequence[str] = (),
) -> None:
    """Export the exact report subobjects, without rebuilding or changing them.

    Preflight all requested destinations and payloads before creating directories.
    Each file is replaced atomically; outputs are not a multi-file transaction.
    Existing inputs, reports, evidence logs, and repository files are protected.
    """
    requests = [
        ("--packet-output", "review_packet", packet_output),
        ("--manifest-output", "human_approval_manifest", manifest_output),
        ("--markdown-output", "markdown_summary", markdown_output),
    ]
    prepared: list[tuple[str, Path, str]] = []
    for option, key, destination in requests:
        if destination is None:
            continue
        try:
            if not destination or "\0" in destination:
                raise ArtifactOutputError("output path is empty or invalid")
            path = Path(destination).expanduser().absolute()
            if hasattr(os.path, "isreserved") and os.path.isreserved(path):
                raise ArtifactOutputError("output path is reserved")
            _check_destination(path, repository_root.resolve())
            path = path.resolve()
            forbidden = [Path(p).expanduser().resolve() for p in protected_paths]
            forbidden.extend(other for _, other, _ in prepared)
            for other in forbidden:
                if path.resolve() == other.resolve() or (path.exists() and other.exists() and path.samefile(other)):
                    raise ArtifactOutputError("output paths must be distinct from inputs and other outputs")
                if path in other.parents or other in path.parents:
                    raise ArtifactOutputError("an output file cannot be another file's parent")
            raw_content: Any
            if key == "markdown_summary":
                if "markdown_summary" in artifacts:
                    raw_content = artifacts["markdown_summary"]
                elif "review_packet" in artifacts:
                    from codeatlas.review.packet import ReviewPacket
                    from codeatlas.review.rendering import render_test_evidence

                    rp = artifacts["review_packet"]
                    if not isinstance(rp, ReviewPacket):
                        rp = ReviewPacket.model_validate(rp)
                    raw_content = render_test_evidence(rp)
                else:
                    raise ArtifactOutputError("missing review packet for markdown rendering")
            else:
                raw_content = artifacts[key]
            _check_redaction(raw_content, extra_tokens)
            if key == "markdown_summary":
                content = raw_content if raw_content.endswith("\n") else raw_content + "\n"
            else:
                content = json.dumps(raw_content, indent=2, sort_keys=True, allow_nan=False) + "\n"
            prepared.append((option, path, content))
        except (OSError, ValueError, TypeError, RuntimeError) as error:
            # Do not interpolate OS exception text: it may include a sensitive path.
            reason = str(error) if isinstance(error, ArtifactOutputError) else type(error).__name__
            raise ArtifactOutputError(f"{option}: cannot export artifact ({reason})") from None

    for option, path, content in prepared:
        temporary: Path | None = None
        try:
            _check_destination(path, repository_root.resolve())
            if not path.parent.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
            _check_destination(path, repository_root.resolve())
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="\n", dir=path.parent,
                prefix=".codeatlas-", suffix=".tmp", delete=False,
            ) as stream:
                temporary = Path(stream.name)
                stream.write(content)
            os.replace(temporary, path)
        except (OSError, ValueError) as error:
            raise ArtifactOutputError(f"{option}: cannot write artifact ({type(error).__name__})") from None
        finally:
            try:
                if temporary is not None and temporary.exists():
                    temporary.unlink()
            except OSError as error:
                raise ArtifactOutputError(f"{option}: temporary artifact cleanup failed ({type(error).__name__})") from None
