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
                f"**Quality:** {item.get('quality_decision', 'review_only')} (score {float(item.get('quality_score', 0.0) or 0.0):.2f})",
                "",
                str(item.get("claim", "")),
                "",
                f"**Impact:** {item.get('impact', '')}",
                "",
            ]
        )
        explanation = (item.get("provenance") or {}).get("quality_explanation")
        if explanation:
            sections.extend([f"**Quality explanation:** {str(explanation)[:2000]}", ""])
        if item.get("abstention_reason"):
            sections.extend([f"**Abstention:** {str(item['abstention_reason'])[:1200]}", ""])
        quality_limitations = [str(value)[:300] for value in (item.get("quality_limitations") or [])[:10]]
        if quality_limitations:
            sections.extend(["**Quality limitations:**", *[f"- {value}" for value in quality_limitations], ""])
    return "\n".join(sections)


__all__ = ["render_json", "render_markdown"]
