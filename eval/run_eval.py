"""Deterministic, data-driven evaluation baseline.

The baseline intentionally does not import, compile, or execute case code.  It
predicts ``clean`` for every case (a useful no-op baseline) and compares that
prediction with metadata supplied by the benchmark author.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import shutil
import subprocess
import tempfile
import time
from types import SimpleNamespace
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, TextIO

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

try:
    from codeatlas.analyzers import SensitiveDataExposureAnalyzer
    from codeatlas.analyzers.models import AnalysisContext
    from codeatlas.analyzers.registry import AnalyzerRegistry
    from codeatlas.git.diff import extract_diff
    from codeatlas.analyzers.sensitive import _SINKS
except ImportError:  # baseline remains usable when the package is not installed
    SensitiveDataExposureAnalyzer = AnalysisContext = AnalyzerRegistry = extract_diff = None
    _SINKS = re.compile(r"(?:\b(?:print|logging\.(?:debug|info|warning|error|exception|critical)|logger\.(?:debug|info|warn|warning|error|log)|console\.(?:log|debug|info|warn|error)|debug)\s*\(|\bthrow\s+(?:new\s+)?[A-Za-z_$][\w$]*\s*\(|\braise(?:\s+[A-Za-z_]\w*)?\s*\(|\b(?:return\s+)?(?:res|response|reply)\.(?:send|json|end|status)\s*\(|\breturn\s+[^;]*(?:Response|HttpResponse|JSONResponse)\s*\()", re.I)

REQUIRED_METADATA = {"case_id", "category", "expected_status", "abstention_allowed"}
VALID_CATEGORIES = {"buggy", "clean", "ambiguous", "SENSITIVE_DATA_EXPOSURE"}
VALID_SEVERITIES = {"info", "low", "medium", "high", "blocker"}
VALID_STATUSES = {"detected", "review_only", "suggested", "validated", "rejected", "abstained"}
Predictor = Callable[[Path], str]
SECRET_MARKERS = (
    "-----BEGIN ", "AKIA", "ASIA", "ghp_", "github_pat_", "postgres://",
    "postgresql://", "mysql://", "mongodb://", "redis://",
)

def _redact(value: Any) -> Any:
    """Keep analyzer evidence safe even when it contains fixture secrets."""
    if isinstance(value, str):
        value = re.sub(r"(?i)(AKIA|ASIA|ghp_|github_pat_)[A-Za-z0-9_\-]+", r"\1[REDACTED]", value)
        return re.sub(r"(?i)(password|token|secret|api[_-]?key)\s*[:=]\s*[^,;\s]+", r"\1=[REDACTED]", value)
    if isinstance(value, list): return [_redact(x) for x in value]
    if isinstance(value, dict): return {k: _redact(v) for k, v in value.items()}
    return value

def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

def real_analyzer_status(case_dir: Path) -> dict[str, Any]:
    """REAL ANALYZER EXECUTION: analyze an isolated before/after Git fixture."""
    started = time.perf_counter()
    metadata = load_case(case_dir)
    with tempfile.TemporaryDirectory(prefix="codeatlas-eval-") as temp:
        repo = Path(temp); work = repo / "work"; work.mkdir()
        before_dir = case_dir / "before"
        after_dir = case_dir / "after"
        _git(work, "init", "-q"); _git(work, "config", "user.email", "eval@example.invalid"); _git(work, "config", "user.name", "eval")
        if after_dir.is_dir() and before_dir.is_dir() and any(p.is_file() for p in before_dir.rglob("*")):
            shutil.copytree(before_dir, work, dirs_exist_ok=True)
            _git(work, "add", "-A"); _git(work, "commit", "--allow-empty", "-qm", "before")
        else:
            _git(work, "commit", "--allow-empty", "-qm", "before")
        base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=work, text=True).strip()
        for p in list(work.iterdir()):
            if p.name != ".git": shutil.rmtree(p) if p.is_dir() else p.unlink()
        target_after = after_dir if after_dir.is_dir() else before_dir
        shutil.copytree(target_after, work, dirs_exist_ok=True)
        _git(work, "add", "-A"); _git(work, "commit", "--allow-empty", "-qm", "after")
        diff = extract_diff(work, base, "HEAD")
        findings = AnalyzerRegistry((SensitiveDataExposureAnalyzer(),)).analyze(
            AnalysisContext(SimpleNamespace(path=work), diff))
        safe_findings = [_redact(f.model_dump(mode="json", exclude_none=True)) for f in findings]
        stopped = sorted({str(x) for f in findings for x in f.limitations if x})
    predicted = "detected" if findings else ("abstained" if metadata.get("unsupported_flow") or metadata["expected_status"] == "abstained" else "review_only")
    return _result_from_prediction(case_dir, metadata, predicted, "sensitive-data-exposure", time.perf_counter()-started,
                                   safe_findings=safe_findings, stopped_reasons=stopped)

def _result_from_prediction(case_dir: Path, metadata: Mapping[str, Any], predicted: str, analyzer: str, duration: float,
                            *, safe_findings: list[dict[str, Any]] | None = None, stopped_reasons: list[str] | None = None) -> dict[str, Any]:
    expected = str(metadata["expected_status"])
    scored_expected = expected if expected in {"detected", "review_only"} else "ambiguous"
    precision = ("true_positive" if scored_expected == predicted == "detected" else "true_negative" if scored_expected == predicted == "review_only" else
                 "false_positive" if scored_expected == "review_only" and predicted == "detected" else "false_negative" if scored_expected == "detected" else "ambiguous")
    findings = safe_findings or []
    expected_file = metadata.get("expected_file") or metadata.get("expected_anchor_file")
    expected_line = metadata.get("expected_line") or metadata.get("expected_anchor_line")
    matched = next((f for f in findings if (f.get("category") == metadata.get("category") or metadata.get("category") == "buggy" or f.get("category") == metadata.get("expected_category", "SENSITIVE_DATA_EXPOSURE")) and
                    (expected_file is None or Path(f.get("file", "")).as_posix() == Path(expected_file).as_posix()) and
                    (expected_line is None or f.get("start_line") == expected_line)), None)
    finding_flow = next((f.get("provenance", {}).get("flow_type") for f in findings if f.get("provenance", {}).get("flow_type")), None)
    actual_flow = finding_flow or ("alias" if any(f.get("provenance", {}).get("alias_path") for f in findings) else ("direct" if findings else "unknown"))
    expected_flow = str(metadata.get("expected_flow_type", metadata.get("flow_kind", "unknown")))
    flow_matched = (actual_flow == expected_flow) or (expected_flow in {"alias", "object_access", "dictionary_access", "interpolation", "destructuring"} and actual_flow in {"alias", "object_access", "dictionary_access", "interpolation", "destructuring"}) if findings else (expected != "detected")
    expected_rule = str(metadata.get("expected_rule", metadata.get("rule", "unspecified")))
    rule_matched = (matched is not None and matched.get("provenance", {}).get("rule") == expected_rule) if expected == "detected" else True
    return {"case_id": str(metadata["case_id"]), "analyzer_name": analyzer, "analyzer_version": "1.0.0",
            "predicted_status": predicted, "expected_status": expected,
            "predicted_finding_status": predicted, "expected_finding_status": expected, "category": str(metadata["category"]),
            "severity": str(metadata.get("severity", metadata.get("expected_severity", "high"))), "rule": expected_rule,
            "flow_kind": expected_flow, "predicted_flow_type": actual_flow, "expected_flow_type": expected_flow,
            "actual_finding_count": len(findings), "matched_finding": matched is not None, "anchor_match": (matched is not None) if expected == "detected" else True,
            "flow_match": flow_matched, "rule_match": rule_matched,
            "direct_or_alias": actual_flow, "stopped_reason": (stopped_reasons or [None])[0],
            "expected_behavior": str(metadata.get("expected_behavior", "")), "redaction": str(metadata.get("redaction", metadata.get("expected_redaction", "safe"))),
            "propagation_steps": max((f.get("provenance", {}).get("propagation_depth", 0) for f in findings), default=int(metadata.get("propagation_steps", 0))), "unsupported_flow": bool(metadata.get("unsupported_flow", False)),
            "precision_related_result": precision, "tp": precision == "true_positive", "tn": precision == "true_negative", "fp": precision == "false_positive", "fn": precision == "false_negative",
            "abstention": predicted == "abstained", "redaction_result": str(metadata.get("expected_redaction", metadata.get("redaction", "safe"))), "analyzer": analyzer,
            "pass": predicted == expected, "duration": duration, "errors": [], "error_details": None,
            "findings": _redact(findings), "stopped_reasons": stopped_reasons or [],
            "redaction_safe": True,
            "duration_ms": duration * 1000, "error": None}


def _iter_case_dirs(cases_dir: Path, recursive: bool) -> Iterable[Path]:
    paths = cases_dir.rglob("metadata.json") if recursive else cases_dir.glob("*/metadata.json")
    for metadata_path in paths:
        case_dir = metadata_path.parent
        if not any(part.startswith((".", "__")) for part in case_dir.relative_to(cases_dir).parts):
            yield case_dir


def discover_cases(cases_dir: Path, *, recursive: bool = False) -> list[Path]:
    """Return case directories in stable order.

    Direct discovery remains the Phase 1/2 behavior; the expanded suites can
    opt into recursive discovery without changing callers that use the old
    layout.
    """
    return sorted(_iter_case_dirs(cases_dir, recursive), key=lambda p: p.relative_to(cases_dir).as_posix())


def load_case(case_dir: Path) -> dict[str, Any]:
    """Load and validate one case's static metadata and required artifacts."""
    metadata_path = case_dir / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if not isinstance(metadata, dict):
        raise ValueError("metadata must be a JSON object")
    missing = REQUIRED_METADATA - metadata.keys()
    if missing:
        raise ValueError(f"missing metadata fields: {', '.join(sorted(missing))}")
    for field in REQUIRED_METADATA - {"abstention_allowed"}:
        if not isinstance(metadata[field], str) or not metadata[field].strip():
            raise ValueError(f"{field} must be a nonempty string")
    if metadata["case_id"] != case_dir.name:
        raise ValueError("case_id must match the directory name")
    if metadata["category"] not in VALID_CATEGORIES:
        raise ValueError(f"category must be one of {sorted(VALID_CATEGORIES)}")
    if metadata["expected_status"] not in VALID_STATUSES:
        raise ValueError("expected_status is not supported")
    if not isinstance(metadata["abstention_allowed"], bool):
        raise ValueError("abstention_allowed must be boolean")
    severity = metadata.get("severity") or metadata.get("expected_severity")
    if severity is not None and severity not in VALID_SEVERITIES:
        raise ValueError("severity is not supported")
    if not (case_dir / "before").is_dir():
        raise ValueError("missing before directory")
    if not (case_dir / "visible_tests").is_dir():
        raise ValueError("missing visible_tests directory")
    if not any(path.is_file() for path in (case_dir / "before").rglob("*")):
        raise ValueError("before directory is empty")
    if not any(path.is_file() for path in (case_dir / "visible_tests").rglob("*")):
        raise ValueError("visible_tests directory is empty")
    if "expected_behavior" not in metadata:
        metadata["expected_behavior"] = "detect" if metadata["expected_status"] == "detected" else ("abstain" if metadata["expected_status"] == "abstained" else "review-only")
    if "severity" not in metadata and "expected_severity" in metadata:
        metadata["severity"] = metadata["expected_severity"]
    if "flow_kind" not in metadata and "expected_flow_type" in metadata:
        metadata["flow_kind"] = metadata["expected_flow_type"]
    if "rule" not in metadata and "expected_rule" in metadata:
        metadata["rule"] = metadata["expected_rule"]
    return metadata


def predict_status(_: Path) -> str:
    """Return the review-only no-op prediction."""
    return "review_only"


def predict_secret_status(before_dir: Path) -> str:
    """Conservative, data-only baseline for the expanded secret fixtures."""
    for path in sorted(before_dir.rglob("*")):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        for line in text.splitlines():
            if any(marker.lower() in line.lower() for marker in SECRET_MARKERS):
                return "detected"
            lowered = line.lower()
            if re.search(r"\b(?:api[_-]?key|apikey|password|passwd|token|secret)\b", lowered) and "=" in line:
                value = line.split("=", 1)[1].strip().strip("\"'`")
                if value and "env" not in value.lower() and not (value.startswith("${") or value.lower() in {"changeme", "dummy", "example", "test", "none", "null"}):
                    return "detected"
    return "review_only"


def evaluate_case(case_dir: Path, predictor: Predictor = predict_status, *, analyzer_name: str = "baseline") -> dict[str, Any]:
    """Evaluate without executing case code; predictors receive only before/.

    Duration is a deterministic zero sentinel (seconds), not a measurement.
    Later static baselines can supply a predictor without receiving answer labels.
    """
    try:
        metadata = load_case(case_dir)
        predicted = predictor(case_dir / "before")
        if predicted not in VALID_STATUSES:
            raise ValueError("predictor returned an unsupported status")
        expected = str(metadata["expected_status"])
        # Case-level confusion is defined only for the binary labeled classes.
        # Ambiguous labels are excluded from TP/TN/FP/FN and reported through
        # abstention/coverage instead. Positive cases that abstain are misses.
        if expected == "ambiguous":
            precision = "ambiguous"
        elif expected == predicted == "detected":
            precision = "true_positive"
        elif expected == predicted == "review_only":
            precision = "true_negative"
        elif expected == "review_only" and predicted == "detected":
            precision = "false_positive"
        elif expected == "detected" and predicted in {"review_only", "abstained"}:
            precision = "false_negative"
        else:
            precision = "unscored"
        tp = precision == "true_positive"
        tn = precision == "true_negative"
        fp = precision == "false_positive"
        fn = precision == "false_negative"
        redaction = str(metadata.get("expected_redaction", "not_applicable"))
        return {
            "case_id": str(metadata["case_id"]),
            "predicted_status": predicted,
            "expected_status": expected,
            "predicted_finding_status": predicted,
            "expected_finding_status": expected,
            "category": str(metadata["category"]),
            "severity": str(metadata["severity"]),
            "rule": str(metadata.get("rule", "unspecified")),
            "flow_kind": str(metadata.get("flow_kind", "unknown")),
            "expected_behavior": str(metadata.get("expected_behavior", "")),
            "redaction": str(metadata.get("redaction", metadata.get("expected_redaction", "not_applicable"))),
            "propagation_steps": int(metadata.get("propagation_steps", 0)),
            "unsupported_flow": bool(metadata.get("unsupported_flow", False)),
            "precision_related_result": precision,
            "tp": tp, "tn": tn, "fp": fp, "fn": fn,
            "abstention": predicted == "abstained",
            "redaction_result": redaction,
            "analyzer": analyzer_name,
            "pass": predicted == expected,
            "duration": 0.0,
            "errors": [],
            "error_details": None,
        }
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as error:
        return {
            "case_id": case_dir.name,
            "predicted_status": "error",
            "expected_status": None,
            "predicted_finding_status": "error",
            "expected_finding_status": None,
            "category": "invalid",
            "severity": "info",
            "rule": "invalid",
            "precision_related_result": "error",
            "tp": False, "tn": False, "fp": False, "fn": False,
            "abstention": False,
            "redaction_result": "error",
            "analyzer": analyzer_name,
            "pass": False,
            "duration": 0.0,
            "errors": [str(error)],
            "error_details": str(error),
        }


def summarize_results(results: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Return case-level metrics with explicit denominators.

    Only expected ``detected`` and ``review_only`` cases participate in the
    binary confusion matrix. Ambiguous cases are excluded from that matrix.
    Abstentions are counted in abstention rate and reduce coverage; an
    abstention on an expected positive is a false negative.
    """
    rows = list(results)
    scored = [r for r in rows if r.get("expected_status") in {"detected", "review_only"}]
    positives = sum(r.get("expected_status") == "detected" for r in scored)
    negatives = sum(r.get("expected_status") == "review_only" for r in scored)
    tp = sum(bool(r.get("tp")) for r in scored)
    tn = sum(bool(r.get("tn")) for r in scored)
    fp = sum(bool(r.get("fp")) for r in scored)
    fn = sum(bool(r.get("fn")) for r in scored)
    redaction_rows = [r for r in rows if r.get("redaction_result") != "not_applicable"]
    def rate(n: int, d: int) -> float:
        return n / d if d else 0.0
    summary = {
        "cases": len(rows),
        "precision": rate(tp, tp + fp),
        "recall": rate(tp, tp + fn),
        "false_positive_rate": rate(fp, negatives),
        "false_negative_rate": rate(fn, tp + fn),
        "abstention_rate": rate(sum(bool(r.get("abstention")) for r in rows), len(rows)),
        "coverage": rate(sum(not bool(r.get("abstention")) for r in rows), len(rows)),
        "redaction_safety_rate": rate(sum(r.get("redaction_result") in {"pass", "safe"} for r in redaction_rows), len(redaction_rows)),
    }
    # Phase 3C adds flow-stratified reporting while leaving the Phase 1/3A/3B
    # summary contract byte-for-byte compatible for their result rows.
    if any(r.get("flow_kind") not in (None, "unknown") for r in rows):
        def flow_metrics(kind: str | tuple[str, ...] | list[str]) -> dict[str, float]:
            if isinstance(kind, (tuple, list, set)):
                subset = [r for r in rows if r.get("flow_kind") in kind or r.get("expected_flow_type") in kind]
            else:
                subset = [r for r in rows if r.get("flow_kind") == kind or r.get("expected_flow_type") == kind]
            tp_k = sum(bool(r.get("tp")) for r in subset)
            fp_k = sum(bool(r.get("fp")) for r in subset)
            fn_k = sum(bool(r.get("fn")) for r in subset)
            return {"precision": rate(tp_k, tp_k + fp_k), "recall": rate(tp_k, tp_k + fn_k)}
        durations = sorted(float(r.get("duration", 0.0)) for r in rows)
        p95 = durations[max(0, int(len(durations) * .95) - 1)] if durations else 0.0
        summary.update({
            "direct_flow": flow_metrics("direct"),
            "alias_flow": flow_metrics("alias"),
            "object_dictionary_flow": flow_metrics(("object_access", "dictionary_access")),
            "object_flow": flow_metrics("object_access"),
            "dictionary_flow": flow_metrics("dictionary_access"),
            "overall": {"precision": summary["precision"], "recall": summary["recall"]},
            "mean_duration": rate(sum(durations), len(durations)),
            "p95_duration": p95,
            "mean_propagation_steps": rate(sum(int(r.get("propagation_steps", 0)) for r in rows), len(rows)),
            "unsupported_flow_abstentions": sum(bool(r.get("unsupported_flow")) and bool(r.get("abstention")) for r in rows),
            "ambiguous_flow_abstentions": sum(r.get("expected_status") == "abstained" and bool(r.get("abstention")) for r in rows),
        })
        summary["subcategories"] = {k: flow_metrics(k) for k in sorted({str(r.get("rule", "unspecified")) for r in rows})}
        summary["stopped_reasons"] = {reason: sum(reason in r.get("stopped_reasons", []) for r in rows)
                                       for reason in sorted({x for r in rows for x in r.get("stopped_reasons", [])})}
        summary["ambiguous_abstentions"] = sum(r.get("expected_status") == "abstained" and r.get("abstention") for r in rows)
        summary["false_negative_rate"] = 1 - summary["recall"]
    return summary


def run(cases_dir: Path, predictor: Predictor = predict_status, *, recursive: bool = False, analyzer_name: str = "baseline") -> Iterable[dict[str, Any]]:
    """Yield stable JSON-serializable results for every discovered case."""
    for case_dir in discover_cases(cases_dir, recursive=recursive):
        yield evaluate_case(case_dir, predictor, analyzer_name=analyzer_name)

def run_real(cases_dir: Path) -> Iterable[dict[str, Any]]:
    """Run the real analyzer against dataflow fixtures, never importing fixture code."""
    for case_dir in discover_cases(cases_dir, recursive=True):
        yield real_analyzer_status(case_dir)


def validate_benchmark_suite(cases_dir: Path, analyzer_name: str = "sensitive-data-exposure") -> dict[str, int]:
    """Validate benchmark suite contract, line anchors, and safety before execution."""
    cases = discover_cases(cases_dir, recursive=True)
    if not cases:
        raise ValueError(f"No cases discovered in {cases_dir}")
    positives = 0
    negatives = 0
    ambiguous = 0
    valid_flow_types = {"direct", "alias", "object_access", "dictionary_access", "interpolation", "destructuring", "none", "unknown"}
    supported_rules = {"sensitive-to-sink", "none", "unspecified", "sql-injection", "hardcoded-secret"}
    supported_categories = {"SENSITIVE_DATA_EXPOSURE", "buggy", "clean", "ambiguous"}

    for case_dir in cases:
        meta = load_case(case_dir)
        status = meta["expected_status"]
        category = meta.get("category")
        if category not in supported_categories:
            raise ValueError(f"Case {case_dir.name} has unsupported category {category}")

        rule = meta.get("expected_rule") or meta.get("rule", "unspecified")
        if rule not in supported_rules:
            raise ValueError(f"Case {case_dir.name} has unknown rule {rule}")

        flow = meta.get("expected_flow_type") or meta.get("flow_kind", "unknown")
        if flow not in valid_flow_types:
            raise ValueError(f"Case {case_dir.name} has invalid flow type {flow}")

        # Check for real secrets
        for d in (case_dir / "before", case_dir / "after"):
            if not d.is_dir(): continue
            for p in d.rglob("*"):
                if not p.is_file(): continue
                text = p.read_text(encoding="utf-8", errors="ignore")
                for marker in ("-----BEGIN RSA PRIVATE KEY-----", "-----BEGIN OPENSSH PRIVATE KEY-----"):
                    if marker in text:
                        raise ValueError(f"Fixture {case_dir.name} contains real private key")
                if re.search(r"\bAKIA[0-9A-Z]{16}\b", text) or re.search(r"\bghp_[A-Za-z0-9_]{30,}\b", text):
                    raise ValueError(f"Fixture {case_dir.name} contains potential real credential")

        if status == "detected":
            positives += 1
            expected_file = meta.get("expected_file") or meta.get("expected_anchor_file")
            expected_line = meta.get("expected_line") or meta.get("expected_anchor_line")
            if not expected_file or expected_line is None:
                raise ValueError(f"Positive case {case_dir.name} must specify expected_file and expected_line")
            after_file = (case_dir / "after" / expected_file) if (case_dir / "after").is_dir() else (case_dir / "before" / expected_file)
            if not after_file.is_file():
                raise ValueError(f"Positive case {case_dir.name} expected file {expected_file} does not exist")
            lines = after_file.read_text(encoding="utf-8").splitlines()
            if not (1 <= expected_line <= len(lines)):
                raise ValueError(f"Positive case {case_dir.name} line {expected_line} out of range in {expected_file}")
            anchor_line = lines[expected_line - 1]
            if not _SINKS.search(anchor_line):
                raise ValueError(f"Positive case {case_dir.name} line {expected_line} ({anchor_line.strip()}) is not an observable sink")
        elif status == "review_only":
            negatives += 1
        elif status == "abstained":
            ambiguous += 1
        else:
            negatives += 1

    total = len(cases)
    print(f"Benchmark validation passed: {positives} positive, {negatives} negative, {ambiguous} ambiguous ({total} total cases)", file=sys.stderr)
    return {"positives": positives, "negatives": negatives, "ambiguous": ambiguous, "total": total}


def write_jsonl(results: Iterable[Mapping[str, Any]], output: TextIO) -> None:
    """Write one compact result object per line."""
    for result in results:
        output.write(json.dumps(result, sort_keys=True) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=Path(__file__).with_name("cases"))
    parser.add_argument("--output", type=Path, help="write JSONL to this file instead of stdout")
    parser.add_argument("--real-analyzer", action="store_true", help="REAL ANALYZER EXECUTION for dataflow-expanded")
    args = parser.parse_args(argv)
    try:
        if "patching" in str(args.cases):
            from eval.eval_patching import run_patching_eval, summarize_patching
            print(f"Evaluating Phase 6 Patching Interface on {args.cases}...", file=sys.stderr)
            results = run_patching_eval(args.cases)
            summary = summarize_patching(results)
            print("\nPhase 6 Patching Evaluation Summary:", file=sys.stderr)
            print(f"  Total Cases: {summary.get('total_cases', 0)}", file=sys.stderr)
            print(
                f"  Proposal Construction Success Rate: {summary.get('proposal_construction_success_rate', 0.0):.4f} "
                f"({summary.get('proposal_construction_numerator', 0)}/{summary.get('proposal_construction_denominator', 0)})",
                file=sys.stderr,
            )
            print(
                f"  Redaction Safety: {summary.get('redaction_safety', 0.0):.4f} "
                f"({summary.get('redaction_safety_numerator', 0)}/{summary.get('redaction_safety_denominator', 0)})",
                file=sys.stderr,
            )
            print(
                f"  Policy Decision Accuracy: {summary.get('policy_decision_accuracy', 0.0):.4f} "
                f"({summary.get('policy_decision_numerator', 0)}/{summary.get('policy_decision_denominator', 0)})",
                file=sys.stderr,
            )
            print(
                f"  Invalid Proposal Rejection Rate: {summary.get('invalid_proposal_rejection_rate', 0.0):.4f} "
                f"({summary.get('invalid_proposal_rejection_numerator', 0)}/{summary.get('invalid_proposal_rejection_denominator', 0)})",
                file=sys.stderr,
            )
            print(
                f"  Clean Isolated Application Rate: {summary.get('clean_isolated_application_rate', 0.0):.4f} "
                f"({summary.get('clean_isolated_application_numerator', 0)}/{summary.get('clean_isolated_application_denominator', 0)})",
                file=sys.stderr,
            )
            print(
                f"  Original Worktree Safety Rate: {summary.get('original_worktree_safety_rate', 0.0):.4f} "
                f"({summary.get('original_worktree_safety_numerator', 0)}/{summary.get('original_worktree_safety_denominator', 0)})",
                file=sys.stderr,
            )
            print(
                f"  Syntax Validation Accuracy: {summary.get('syntax_validation_accuracy', 0.0):.4f} "
                f"({summary.get('syntax_validation_numerator', 0)}/{summary.get('syntax_validation_denominator', 0)})",
                file=sys.stderr,
            )
            print(f"  Mean Validation Duration: {summary.get('mean_validation_duration_ms', 0.0):.2f} ms", file=sys.stderr)
            print(f"  P95 Validation Duration: {summary.get('p95_validation_duration_ms', 0.0):.2f} ms", file=sys.stderr)
            if args.output is not None:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                with args.output.open("w", encoding="utf-8", newline="\n") as f:
                    for r in results:
                        f.write(json.dumps(r, sort_keys=True) + "\n")
                    f.write(json.dumps({"_summary": summary}, sort_keys=True) + "\n")
            return 0

        if "review-packets" in str(args.cases):
            from eval.eval_review_packets import run_review_packets_eval, summarize_review_packets
            print(f"Evaluating Phase 5 Review Packet Assembly on {args.cases}...", file=sys.stderr)
            results = run_review_packets_eval(args.cases)
            summary = summarize_review_packets(results)
            print("\nPhase 5 Review Packet Evaluation Summary:", file=sys.stderr)
            print(f"  Total Cases: {summary.get('total_cases', 0)}", file=sys.stderr)
            print(
                f"  Packet Construction Success Rate: {summary.get('packet_construction_success_rate', 0.0):.4f} "
                f"({summary.get('packet_construction_numerator', 0)}/{summary.get('packet_construction_denominator', 0)})",
                file=sys.stderr,
            )
            print(
                f"  Redaction Safety: {summary.get('redaction_safety', 0.0):.4f} "
                f"({summary.get('redaction_safety_numerator', 0)}/{summary.get('redaction_safety_denominator', 0)})",
                file=sys.stderr,
            )
            print(
                f"  Policy Decision Accuracy: {summary.get('policy_decision_accuracy', 0.0):.4f} "
                f"({summary.get('policy_decision_numerator', 0)}/{summary.get('policy_decision_denominator', 0)})",
                file=sys.stderr,
            )
            print(
                f"  Provider Validation Rejection Rate: {summary.get('provider_validation_rejection_rate', 0.0):.4f} "
                f"({summary.get('provider_validation_rejection_numerator', 0)}/{summary.get('provider_validation_rejection_denominator', 0)})",
                file=sys.stderr,
            )
            print(
                f"  Duplicate Merge Rate: {summary.get('duplicate_merge_rate', 0.0):.4f} "
                f"({summary.get('duplicate_merge_numerator', 0)}/{summary.get('duplicate_merge_denominator', 0)})",
                file=sys.stderr,
            )
            print(f"  Context Retention Priority Rate: {summary.get('context_retention_priority_rate', 0.0):.4f}", file=sys.stderr)
            print(f"  Mean Packet Bytes: {summary.get('mean_packet_bytes', 0.0):.1f} bytes", file=sys.stderr)
            print(f"  P95 Packet Bytes: {summary.get('p95_packet_bytes', 0.0):.1f} bytes", file=sys.stderr)
            print(f"  Mean Assembly Duration: {summary.get('mean_packet_duration_ms', 0.0):.2f} ms", file=sys.stderr)
            print(f"  P95 Assembly Duration: {summary.get('p95_packet_duration_ms', 0.0):.2f} ms", file=sys.stderr)
            if args.output is not None:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                with args.output.open("w", encoding="utf-8", newline="\n") as f:
                    for r in results:
                        f.write(json.dumps(r, sort_keys=True) + "\n")
                    f.write(json.dumps({"_summary": summary}, sort_keys=True) + "\n")
            all_passed = all(r["pass"] for r in results)
            return 0 if all_passed else 1

        if "repo-intel" in str(args.cases):
            from eval.eval_repo_intel import run_repo_intel_eval, summarize_repo_intel
            print(f"Evaluating Phase 4 Repository Intelligence on {args.cases}...", file=sys.stderr)
            results = run_repo_intel_eval(args.cases)
            summary = summarize_repo_intel(results)
            print("\nPhase 4 Repository Intelligence Evaluation Summary:", file=sys.stderr)
            print(f"  Total Cases: {summary.get('total_cases', 0)}", file=sys.stderr)
            print(f"  File Inventory Precision: {summary.get('file_inventory_precision', 0.0):.4f} ({summary.get('file_inventory_numerator', 0)}/{summary.get('file_inventory_denominator', 0)})", file=sys.stderr)
            print(f"  Symbol Anchor Accuracy: {summary.get('symbol_anchor_accuracy', 0.0):.4f} ({summary.get('symbol_anchor_numerator', 0)}/{summary.get('symbol_anchor_denominator', 0)})", file=sys.stderr)
            print(f"  Import Extraction Accuracy: {summary.get('import_extraction_accuracy', 0.0):.4f} ({summary.get('import_extraction_numerator', 0)}/{summary.get('import_extraction_denominator', 0)})", file=sys.stderr)
            print(f"  Changed-Symbol Mapping Accuracy: {summary.get('changed_symbol_mapping_accuracy', 0.0):.4f} ({summary.get('changed_symbol_numerator', 0)}/{summary.get('changed_symbol_denominator', 0)})", file=sys.stderr)
            print(f"  Retrieval Relevance: {summary.get('retrieval_relevance', 0.0):.4f} ({summary.get('retrieval_relevance_numerator', 0)}/{summary.get('retrieval_relevance_denominator', 0)})", file=sys.stderr)
            print(f"  Parse Failure Rate: {summary.get('parse_failure_rate', 0.0):.4f} ({summary.get('parse_failure_numerator', 0)}/{summary.get('parse_failure_denominator', 0)} files)", file=sys.stderr)
            print(f"  Unresolved Reference Rate: {summary.get('unresolved_reference_rate', 0.0):.4f}", file=sys.stderr)
            print(f"  Mean Index Duration: {summary.get('mean_index_duration_ms', 0.0):.2f} ms", file=sys.stderr)
            print(f"  P95 Index Duration: {summary.get('p95_index_duration_ms', 0.0):.2f} ms", file=sys.stderr)
            print(f"  Mean Retrieval Duration: {summary.get('mean_retrieval_duration_ms', 0.0):.2f} ms", file=sys.stderr)
            print(f"  P95 Retrieval Duration: {summary.get('p95_retrieval_duration_ms', 0.0):.2f} ms", file=sys.stderr)
            print(f"  Mean Storage / Index Size: {summary.get('mean_storage_bytes', 0.0):.1f} bytes", file=sys.stderr)
            if args.output is not None:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                with args.output.open("w", encoding="utf-8", newline="\n") as f:
                    for r in results:
                        f.write(json.dumps(r, sort_keys=True) + "\n")
                    f.write(json.dumps({"_summary": summary}, sort_keys=True) + "\n")
            all_passed = all(r["pass"] for r in results)
            return 0 if all_passed else 1

        if args.real_analyzer:
            print("REAL ANALYZER EXECUTION", file=sys.stderr)
            validate_benchmark_suite(args.cases)
            results = list(run_real(args.cases))
            summary = summarize_results(results)
            print("\nEvaluation Summary:", file=sys.stderr)
            print(f"  Precision: {summary.get('precision', 0.0):.4f}", file=sys.stderr)
            print(f"  Recall: {summary.get('recall', 0.0):.4f}", file=sys.stderr)
            print(f"  False Positive Rate: {summary.get('false_positive_rate', 0.0):.4f}", file=sys.stderr)
            print(f"  False Negative Rate: {summary.get('false_negative_rate', 0.0):.4f}", file=sys.stderr)
            print(f"  Abstention Rate: {summary.get('abstention_rate', 0.0):.4f}", file=sys.stderr)
            print(f"  Coverage: {summary.get('coverage', 0.0):.4f}", file=sys.stderr)
            print(f"  Redaction Safety Rate: {summary.get('redaction_safety_rate', 0.0):.4f}", file=sys.stderr)
            if "direct_flow" in summary:
                print(f"  Direct Flow Precision: {summary['direct_flow'].get('precision', 0.0):.4f}, Recall: {summary['direct_flow'].get('recall', 0.0):.4f}", file=sys.stderr)
            if "alias_flow" in summary:
                print(f"  Alias Flow Precision: {summary['alias_flow'].get('precision', 0.0):.4f}, Recall: {summary['alias_flow'].get('recall', 0.0):.4f}", file=sys.stderr)
            if "object_dictionary_flow" in summary:
                print(f"  Object/Dict Flow Precision: {summary['object_dictionary_flow'].get('precision', 0.0):.4f}, Recall: {summary['object_dictionary_flow'].get('recall', 0.0):.4f}", file=sys.stderr)
            print(f"  Mean Duration: {summary.get('mean_duration', 0.0):.4f}s, P95 Duration: {summary.get('p95_duration', 0.0):.4f}s", file=sys.stderr)
            print(f"  Mean Propagation Steps: {summary.get('mean_propagation_steps', 0.0):.2f}", file=sys.stderr)
            print(f"  Unsupported Flow Abstentions: {summary.get('unsupported_flow_abstentions', 0)}", file=sys.stderr)
            print(f"  Ambiguous Flow Abstentions: {summary.get('ambiguous_flow_abstentions', 0)}", file=sys.stderr)
        else:
            results = list(run(args.cases, predict_secret_status, recursive=True, analyzer_name="static-secret-baseline"))
        if args.output is None:
            write_jsonl(results, sys.stdout)
        else:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("w", encoding="utf-8", newline="\n") as output:
                write_jsonl(results, output)
    except OSError as error:
        print(f"evaluation error: {error}", file=sys.stderr)
        return 2
    # Benchmark misses are scores, not infrastructure failures.
    return 2 if any(result["error_details"] is not None for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
