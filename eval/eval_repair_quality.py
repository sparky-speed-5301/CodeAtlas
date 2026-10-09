"""Phase 12D deterministic and real-repository repair-quality benchmark."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from codeatlas.benchmark import (
    RepairBenchmarkCase,
    load_repository_cases,
    run_deterministic_benchmark,
    run_repository_benchmark,
)


def _fixtures() -> tuple[list[RepairBenchmarkCase], dict[str, dict[str, object]]]:
    cases = [
        RepairBenchmarkCase(
            case_id="python-zero-divisor",
            language="python",
            target_file="src/calc.py",
            source="def compute(value):\n    return value // 0\n",
            changed_line_range=(2, 2),
            expected_source="def compute(value):\n    return value // 2\n",
        ),
        RepairBenchmarkCase(
            case_id="javascript-zero-divisor",
            language="javascript",
            target_file="src/calc.js",
            source="export function compute(value) {\n    return value / 0;\n}\n",
            changed_line_range=(2, 2),
            expected_source="export function compute(value) {\n    return value / 2;\n}\n",
        ),
        RepairBenchmarkCase(
            case_id="typescript-zero-divisor",
            language="typescript",
            target_file="src/calc.ts",
            source="export function compute(value: number): number {\n    return value / 0;\n}\n",
            changed_line_range=(2, 2),
            expected_source="export function compute(value: number): number {\n    return value / 2;\n}\n",
        ),
        RepairBenchmarkCase(
            case_id="go-zero-divisor",
            language="go",
            target_file="src/calc.go",
            source="package calc\n\nfunc Compute(value int) int {\n    return value / 0\n}\n",
            changed_line_range=(4, 4),
            expected_source="package calc\n\nfunc Compute(value int) int {\n    return value / 2\n}\n",
        ),
        RepairBenchmarkCase(
            case_id="java-zero-divisor",
            language="java",
            target_file="src/Calc.java",
            source="public class Calc {\n    public static int compute(int value) {\n        return value / 0;\n    }\n}\n",
            changed_line_range=(3, 3),
            expected_source="public class Calc {\n    public static int compute(int value) {\n        return value / 2;\n    }\n}\n",
        ),
        RepairBenchmarkCase(
            case_id="rust-zero-divisor",
            language="rust",
            target_file="src/calc.rs",
            source="pub fn compute(value: i32) -> i32 {\n    value / 0\n}\n",
            changed_line_range=(2, 2),
            expected_source="pub fn compute(value: i32) -> i32 {\n    value / 2\n}\n",
        ),
    ]
    candidates = {
        "python-zero-divisor": {
            "diagnosis": "The divisor is the literal zero.",
            "patch": "--- a/src/calc.py\n+++ b/src/calc.py\n@@ -2 +2 @@\n-    return value // 0\n+    return value // 2\n",
            "target_files": ["src/calc.py"],
            "explanation": "Use the reviewed nonzero divisor.",
            "assumptions": ["The intended divisor is two."],
            "expected_behavior_change": "Avoid division by zero.",
            "risk_level": "medium",
            "confidence": 0.8,
            "tests_to_run": [],
            "limitations": ["The semantic intent requires review."],
        },
        "javascript-zero-divisor": {
            "diagnosis": "The divisor is the literal zero.",
            "patch": "--- a/src/calc.js\n+++ b/src/calc.js\n@@ -2 +2 @@\n-    return value / 0;\n+    return value / 2;\n",
            "target_files": ["src/calc.js"],
            "explanation": "Use the reviewed nonzero divisor.",
            "assumptions": [],
            "expected_behavior_change": "Avoid division by zero.",
            "risk_level": "medium",
            "confidence": 0.8,
            "tests_to_run": [],
            "limitations": [],
        },
        "typescript-zero-divisor": {
            "diagnosis": "The divisor is the literal zero.",
            "patch": "--- a/src/calc.ts\n+++ b/src/calc.ts\n@@ -2 +2 @@\n-    return value / 0;\n+    return value / 2;\n",
            "target_files": ["src/calc.ts"],
            "explanation": "Use the reviewed nonzero divisor.",
            "assumptions": [],
            "expected_behavior_change": "Avoid division by zero.",
            "risk_level": "medium",
            "confidence": 0.8,
            "tests_to_run": [],
            "limitations": ["TypeScript compiler checks are not inferred."],
        },
        "go-zero-divisor": {
            "diagnosis": "The divisor is the literal zero.",
            "patch": "--- a/src/calc.go\n+++ b/src/calc.go\n@@ -4 +4 @@\n-    return value / 0\n+    return value / 2\n",
            "target_files": ["src/calc.go"],
            "explanation": "Use the reviewed nonzero divisor.",
            "assumptions": [],
            "expected_behavior_change": "Avoid division by zero.",
            "risk_level": "medium",
            "confidence": 0.8,
            "tests_to_run": [],
            "limitations": ["Go compiler checks are capability-dependent."],
        },
        "java-zero-divisor": {
            "diagnosis": "The divisor is the literal zero.",
            "patch": "--- a/src/Calc.java\n+++ b/src/Calc.java\n@@ -3 +3 @@\n-        return value / 0;\n+        return value / 2;\n",
            "target_files": ["src/Calc.java"],
            "explanation": "Use the reviewed nonzero divisor.",
            "assumptions": [],
            "expected_behavior_change": "Avoid division by zero.",
            "risk_level": "medium",
            "confidence": 0.8,
            "tests_to_run": [],
            "limitations": ["Java compiler checks are capability-dependent."],
        },
        "rust-zero-divisor": {
            "diagnosis": "The divisor is the literal zero.",
            "patch": "--- a/src/calc.rs\n+++ b/src/calc.rs\n@@ -2 +2 @@\n-    value / 0\n+    value / 2\n",
            "target_files": ["src/calc.rs"],
            "explanation": "Use the reviewed nonzero divisor.",
            "assumptions": [],
            "expected_behavior_change": "Avoid division by zero.",
            "risk_level": "medium",
            "confidence": 0.8,
            "tests_to_run": [],
            "limitations": ["Rust compiler checks are capability-dependent."],
        },
    }
    return cases, candidates


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path)
    parser.add_argument("--cases", type=Path, help="JSON case declarations for --repository")
    parser.add_argument("--candidates", type=Path, help="JSON object mapping case IDs to structured repair output")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    if args.repository:
        if not args.cases or not args.candidates:
            parser.error("--repository requires --cases and --candidates")
        cases = load_repository_cases(args.repository, args.cases)
        candidates = json.loads(args.candidates.read_text(encoding="utf-8"))
        summary = run_repository_benchmark(args.repository, cases, candidates)
        label = "real repository"
    else:
        cases, candidates = _fixtures()
        summary = run_deterministic_benchmark(cases, candidates)
        label = "deterministic fixtures"

    serialized = summary.model_dump(mode="json")
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(serialized, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"benchmark": label, **serialized}, sort_keys=True))
    print(f"Phase 12D repair quality: {summary.useful}/{summary.total} useful, {summary.safe}/{summary.total} safe")
    return 0 if summary.useful == summary.total and summary.safe == summary.total else 1


if __name__ == "__main__":
    raise SystemExit(main())
