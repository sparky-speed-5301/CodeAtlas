"""Deterministic, bounded local data-flow detection for sensitive exposures."""
from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Sequence
from typing import Pattern

from codeatlas.core.language import Language, detect_language
from codeatlas.findings import Finding
from codeatlas.git.models import ChangeStatus, Diff, FileChange
from .models import AnalysisContext

_SUPPORTED = {Language.PYTHON, Language.JAVASCRIPT, Language.TYPESCRIPT}
_DEFAULT = {"password", "passwd", "secret", "token", "api_key", "apikey", "credential", "credentials", "private_key", "access_token", "session_id", "ssn", "social_security_number", "authorization", "credit_card"}
_PLACEHOLDER = re.compile(r"^(?:<[^>]+>|\$\{[^}]+\}|changeme|dummy|example|sample|test|todo|none|null|n/?a)$", re.I)
_SAFE = re.compile(r"\b(?:redact|redacted|mask|masked|hash|hashed|sha256|sanitize|sanitized|obfuscate)\s*\(", re.I)
_SINKS = re.compile(r"(?:\b(?:print|logging\.(?:debug|info|warning|error|exception|critical)|logger\.(?:debug|info|warn|warning|error|log)|console\.(?:log|debug|info|warn|error)|debug)\s*\(|\bthrow\s+(?:new\s+)?[A-Za-z_$][\w$]*\s*\(|\braise(?:\s+[A-Za-z_]\w*)?\s*\(|\b(?:return\s+)?(?:res|response|reply)\.(?:send|json|end|status)\s*\(|\breturn\s+[^;]*(?:Response|HttpResponse|JSONResponse)\s*\()", re.I)
_COMMENT = re.compile(r"^\s*(?:#|//|/\*|\*)")
_NAME = r"[A-Za-z_$][\w$]*"

class SensitiveDataExposureAnalyzer:
    name = "sensitive-data-exposure"
    supported_languages = {language.value for language in _SUPPORTED}

    def __init__(self, *, sensitive_identifiers: Iterable[str] = (), custom_identifiers: Iterable[str] = (), allow_patterns: Iterable[str | Pattern[str]] = (), safe_wrappers: Iterable[str] = (), safe_loggers: Iterable[str] = (), ignore_placeholders: bool = True, min_confidence: float = 0.0, max_alias_depth: int = 5, track_local_aliases: bool = True, track_object_access: bool = True, track_destructuring: bool = False, track_containers: bool = False) -> None:
        supplied = tuple(sensitive_identifiers) + tuple(custom_identifiers)
        if not all(isinstance(x, str) and x.strip() and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", x.strip()) for x in supplied): raise ValueError("sensitive identifiers must be non-empty identifier strings")
        if not isinstance(ignore_placeholders, bool) or not isinstance(min_confidence, (int, float)) or not 0 <= min_confidence <= 1: raise ValueError("invalid sensitive analyzer options")
        if not isinstance(max_alias_depth, int) or max_alias_depth < 0: raise ValueError("max_alias_depth must be a non-negative integer")
        self.identifiers = {x.strip().lower().replace("-", "_") for x in supplied} | _DEFAULT
        self.allow_patterns = tuple(allow_patterns)
        if not all(isinstance(x, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", x.strip()) for x in tuple(safe_wrappers) + tuple(safe_loggers)): raise ValueError("safe wrapper/logger names are invalid")
        self.safe_wrappers, self.safe_loggers = tuple(safe_wrappers), tuple(safe_loggers)
        self.ignore_placeholders, self.min_confidence = ignore_placeholders, float(min_confidence)
        self.max_alias_depth, self.track_local_aliases = max_alias_depth, track_local_aliases
        self.track_object_access, self.track_destructuring, self.track_containers = track_object_access, track_destructuring, track_containers

    def analyze(self, context: AnalysisContext | object, diff: Diff | None = None) -> Sequence[Finding]:
        ctx = context if isinstance(context, AnalysisContext) else AnalysisContext(context, diff) if diff is not None else (_ for _ in ()).throw(TypeError("analyze requires AnalysisContext or snapshot and Diff"))
        out = []
        for change in ctx.diff.files:
            if change.status not in {ChangeStatus.ADDED, ChangeStatus.MODIFIED, ChangeStatus.RENAMED, ChangeStatus.COPIED} or detect_language(change.path) not in _SUPPORTED: continue
            try: lines = (ctx.snapshot_path / change.path).read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeError): continue
            env: dict[str, tuple[str, int, str]] = {}
            changed = _changed_lines(change)
            for n, line in enumerate(lines, 1):
                if re.match(r"^\s*(?:def|async\s+def|function|class)\b", line): env = {}
                assignment = _assignment(line, self.track_destructuring)
                if assignment:
                    targets, expr = assignment
                    for target in targets:
                        if self.track_local_aliases:
                            env[target] = (expr, n, target)
                if n not in changed or not _is_sink(line, self.safe_wrappers, self.safe_loggers) or _COMMENT.match(line) or _SAFE.search(line): continue
                if any(re.search(p, line) for p in self.allow_patterns + tuple(ctx.allow_patterns)): continue
                expression = _sink_expression(line)
                source, depth, path, stopped = self._resolve(expression, env)
                expr_name = expression.strip()
                is_direct = self._contains_source(expression) and expr_name not in env
                if source or is_direct:
                    if self.ignore_placeholders and _PLACEHOLDER.search(expression): continue
                    conf = .93
                    if conf >= max(self.min_confidence, ctx.min_confidence):
                        if depth == 0:
                            flow = "direct"
                        elif re.search(r"(?:f[\"']|\$\{)", line):
                            flow = "interpolation"
                        elif re.search(r"\[\s*[\"']", expression) or any(re.search(r"\[\s*[\"']", env.get(p, ("",))[0]) for p in path):
                            flow = "dictionary_access"
                        elif "." in expression or any("." in env.get(p, ("",))[0] for p in path):
                            flow = "object_access"
                        else:
                            flow = "alias"
                        out.append(_finding(change.path, n, line, expression, depth, path, stopped, flow_type=flow))
        return tuple(out)

    def _contains_source(self, expr: str) -> bool:
        return any(w.lower().replace("-", "_") in self.identifiers for w in re.findall(r"[A-Za-z_][\w-]*", expr)) and not (self.ignore_placeholders and _PLACEHOLDER.search(expr))

    def _resolve(self, expr: str, env: dict[str, tuple[str, int, str]], depth: int = 0, path: list[str] | None = None):
        path = path or []; text = expr.strip()
        if depth >= self.max_alias_depth: return False, depth, path, "max_alias_depth"
        if _SAFE.search(text): return False, depth, path, "safe_sanitizer"
        if re.match(r"^\s*[A-Za-z_$][\w$]*\s*\(", text) and not re.search(r"(?:\.|\[)", text):
            return False, depth, path, "interprocedural_call"
        if text in env and text not in path:
            return self._resolve(env[text][0], env, depth + 1, path + [text])
        names = re.findall(_NAME, text)
        for name in names:
            if name in env and name not in path:
                sub, d, p, stop = self._resolve(env[name][0], env, depth + 1, path + [name])
                if sub: return True, d, p, stop
                if stop: return False, d, p, stop
        if self._contains_source(text):
            return True, depth, path, None
        if self.track_object_access and re.search(r"(?:\.|\[\s*[\"'])", text):
            base = re.match(_NAME, text)
            if base and base.group() in env and base.group() not in path:
                return self._resolve(env[base.group()][0], env, depth + 1, path + [base.group()])
        return False, depth, path, "ambiguous_expression" if re.search(r"=>|\b(?:eval|exec)\b", text) else None

def _changed_lines(change: FileChange) -> set[int]: return {n for r in change.new_ranges for n in range(r.start, r.end + 1)}
def _assignment(line: str, destructuring: bool = False):
    m = re.match(r"^\s*(?:const\s+|let\s+|var\s+)?(.+?)\s*(?<![=!<>])=(?!=)\s*(.+?)(?:;\s*)?$", line)
    if not m: return None
    target = m.group(1).strip(); expr = m.group(2).strip()
    if re.fullmatch(_NAME, target): return [target], expr
    # The caller gates this conservative support with track_destructuring by
    # treating the returned names as ordinary local bindings.
    names = re.findall(_NAME, target)
    if destructuring and target.startswith(("{", "[")) and names: return names, expr
    return None
def _is_sink(line, wrappers, loggers):
    return bool(_SINKS.search(line)) and not any(re.search(rf"\b{re.escape(x)}\s*\(", line) for x in wrappers + loggers)
def _sink_expression(line):
    m = re.search(r"\((.*)\)", line)
    return m.group(1) if m else line
def _finding(path, line, source, sink, depth, alias_path, stopped, flow_type="direct"):
    digest = hashlib.sha256(f"{path.replace(chr(92), '/')}/sensitive-data-exposure/{line}".encode()).hexdigest()[:16].upper()
    evidence = ["rule=sensitive-to-sink; redacted_match=<redacted>; line_kind=added"]
    prov = {"analyzer":"sensitive-data-exposure", "rule":"sensitive-to-sink", "flow_type":flow_type, "source_expression":"<redacted>", "sink_expression":"<redacted>", "alias_path":alias_path, "propagation_depth":depth, "flow_scope":"intraprocedural-local"}
    if stopped: prov["stopped_reason"] = stopped
    return Finding(id=f"CA-SENSITIVE-{digest}", file=path, start_line=line, end_line=line, severity="high", category="SENSITIVE_DATA_EXPOSURE", claim="A sensitive value may be exposed through an observable sink.", impact="Sensitive data may be disclosed through logs, errors, or responses.", evidence_strength="supported", confidence=.93, evidence=evidence, tools_consulted=["deterministic-sensitive-data-analyzer"], fixability="review_required", status="detected", limitations=["Bounded intraprocedural pattern-based flow detection requires human review."], provenance=prov)

__all__ = ["SensitiveDataExposureAnalyzer"]
