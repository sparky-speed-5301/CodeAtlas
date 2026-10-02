"""Stable human- and machine-readable finding output."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from .models import Finding


def _as_dict(finding: Finding | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(finding, Finding):
        return finding.model_dump(mode="json")
    return dict(finding)


def render_json(findings: Iterable[Finding | Mapping[str, Any]], *, indent: int | None = 2) -> str:
    """Serialize findings as deterministic JSON."""

    return json.dumps([_as_dict(item) for item in findings], indent=indent, sort_keys=True)


def render_markdown(findings: Iterable[Finding | Mapping[str, Any]]) -> str:
    """Render findings as a concise Markdown report."""

    items = [_as_dict(item) for item in findings]
    if not items:
        return "# CodeAtlas Findings\n\nNo findings.\n"
    sections = ["# CodeAtlas Findings", ""]
    for item in items:
        location = f"{item.get('file', '?')}:{item.get('start_line', '?')}-{item.get('end_line', '?')}"
        sections.extend(
            [
                f"## {item.get('id', 'finding')} — {item.get('severity', 'unknown').upper()}",
                f"**Location:** `{location}`  ",
                f"**Category:** {item.get('category', '')}  ",
                f"**Confidence:** {item.get('confidence', '')}",
                "",
                str(item.get("claim", "")),
                "",
                f"**Impact:** {item.get('impact', '')}",
                "",
            ]
        )
    return "\n".join(sections)


__all__ = ["render_json", "render_markdown"]
