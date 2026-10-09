"""Untrusted-input and structured-output models for AI repair generation."""

from __future__ import annotations

import json
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codeatlas.orchestrator.repair_models import RepairContext, RepairLanguage, RepairLimits, repair_payload_is_safe


RepairRisk = Literal["low", "medium", "high"]
Bounded = Annotated[str, Field(min_length=1, max_length=2_000)]
PathText = Annotated[str, Field(min_length=1, max_length=512)]
_SAFE_DATA_PATH = re.compile(r"^[A-Za-z0-9_./:-]+$")


def _is_safe_data_path(value: str) -> bool:
    normalized = value.replace("\\", "/")
    return bool(
        normalized
        and normalized == value
        and not normalized.startswith(("/", ".", "-"))
        and _SAFE_DATA_PATH.fullmatch(normalized)
        and all(part not in {"", ".", ".."} for part in normalized.split("/"))
    )


class RepairFinding(BaseModel):
    """The minimum trusted finding projection sent to a repair provider."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    id: Annotated[str, Field(pattern=r"^CA-[A-Z0-9][A-Z0-9._-]*$", max_length=128)]
    file: PathText
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    category: Annotated[str, Field(min_length=1, max_length=128)]
    claim: Bounded
    impact: Bounded
    quality_decision: str = Field(min_length=1, max_length=64)
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    evidence_strength: str = Field(min_length=1, max_length=32)
    evidence: tuple[Bounded, ...] = Field(min_length=1, max_length=10)
    limitations: tuple[Bounded, ...] = Field(default=(), max_length=10)

    @model_validator(mode="after")
    def valid_range(self) -> RepairFinding:
        if self.end_line < self.start_line:
            raise ValueError("finding line range is invalid")
        if not repair_payload_is_safe(self.model_dump(mode="json")):
            raise ValueError("finding projection contains unsafe material")
        return self


class RepairLanguageCapabilities(BaseModel):
    """Trusted capabilities discovered by the local language adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    language: RepairLanguage
    syntax_validation: bool = True
    formatter: bool = False
    linter: bool = False
    type_checker: bool = False
    targeted_tests: bool = False
    test_runners: tuple[Annotated[str, Field(max_length=64)], ...] = Field(default=(), max_length=4)
    limitations: tuple[Bounded, ...] = Field(default=(), max_length=8)


class RepairPolicy(BaseModel):
    """Explicit policy projection; provider output cannot alter it."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    policy_version: Annotated[str, Field(min_length=1, max_length=64)] = "12.0"
    max_target_files: Literal[1] = 1
    max_patch_lines: int = Field(default=60, ge=1, le=150)
    max_patch_bytes: int = Field(default=32_000, ge=1, le=64_000)
    prohibited_paths: tuple[PathText, ...] = Field(default=RepairLimits().prohibited_paths, max_length=32)
    require_human_approval: Literal[True] = True
    allow_dependency_changes: Literal[False] = False
    allow_test_changes: Literal[False] = False


class RepairRequest(BaseModel):
    """Complete bounded request accepted by a production repair provider."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    finding: RepairFinding
    context: RepairContext
    policy: RepairPolicy
    capabilities: RepairLanguageCapabilities
    target_file_limits: Literal[1] = 1
    patch_size_limits: tuple[int, int] = (60, 32_000)
    required_response_schema: Literal["codeatlas.repair.v1"] = "codeatlas.repair.v1"

    @model_validator(mode="after")
    def request_matches_context(self) -> RepairRequest:
        if self.finding.id != self.context.finding_id or self.finding.file != self.context.changed_file:
            raise ValueError("repair finding and context identity do not match")
        if self.capabilities.language != self.context.language:
            raise ValueError("repair language capability does not match context")
        if self.target_file_limits != self.policy.max_target_files:
            raise ValueError("target-file limit must match policy")
        if self.patch_size_limits != (self.policy.max_patch_lines, self.policy.max_patch_bytes):
            raise ValueError("patch limits must match policy")
        serialized = self.model_dump_json()
        if not repair_payload_is_safe(self.model_dump(mode="json")) or len(serialized.encode()) > 48_000:
            raise ValueError("repair request is unsafe or unbounded")
        return self


class RepairOutput(BaseModel):
    """Exact provider response schema.  Every field is untrusted data."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    diagnosis: Bounded
    patch: Annotated[str, Field(min_length=1, max_length=64_000)]
    target_files: list[PathText] = Field(min_length=1, max_length=1)
    explanation: Bounded
    assumptions: list[Bounded] = Field(default_factory=list, max_length=8)
    expected_behavior_change: Bounded
    risk_level: RepairRisk
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    tests_to_run: list[PathText] = Field(default_factory=list, max_length=8)
    limitations: list[Bounded] = Field(default_factory=list, max_length=12)

    @model_validator(mode="after")
    def output_is_safe(self) -> RepairOutput:
        if not repair_payload_is_safe(self.model_dump(mode="json")):
            raise ValueError("repair response contains unsafe secret material")
        if any("\x00" in value for value in (self.diagnosis, self.patch, self.explanation, self.expected_behavior_change)):
            raise ValueError("repair response contains binary content")
        if any(not _is_safe_data_path(path) for path in (*self.target_files, *self.tests_to_run)):
            raise ValueError("repair response contains an unsafe path or command")
        return self


# Names used by integrations that describe the result as a candidate or response.
RepairCandidate = RepairOutput
RepairResponse = RepairOutput


def repair_output_schema() -> dict[str, object]:
    """Return a JSON-schema projection safe to include in a provider prompt."""
    schema = RepairOutput.model_json_schema()
    return json.loads(json.dumps(schema, sort_keys=True))


def safe_test_paths(paths: list[str], *, max_items: int = 8) -> tuple[str, ...]:
    """Keep provider-selected test *paths* data-only; commands are never accepted."""
    result: list[str] = []
    for path in paths[:max_items]:
        normalized = path.replace("\\", "/").strip("/")
        if (
            normalized and len(normalized) <= 512 and _SAFE_DATA_PATH.fullmatch(normalized)
            and not normalized.startswith((".", "-"))
            and all(part not in {"", ".", ".."} for part in normalized.split("/"))
        ):
            result.append(normalized)
    return tuple(dict.fromkeys(result))


__all__ = [
    "RepairCandidate",
    "RepairFinding",
    "RepairLanguage",
    "RepairLanguageCapabilities",
    "RepairOutput",
    "RepairPolicy",
    "RepairRequest",
    "RepairResponse",
    "repair_output_schema",
    "safe_test_paths",
]
