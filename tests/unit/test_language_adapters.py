"""Work Package 4 capability and adapter contract tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from codeatlas.adapters import (
    GoAdapter,
    JavaAdapter,
    RustAdapter,
    language_capability_reports,
    select_language_adapter,
)
from codeatlas.adapters.base import LanguageCapabilityReport
from codeatlas.repository.index import build_repository_index
from codeatlas.verification.command_policy import validate_check_command, validate_test_command
from codeatlas.verification.discovery import discover_test_plan


@pytest.mark.parametrize(
    ("path", "adapter_type"),
    [("src/main.go", GoAdapter), ("src/Main.java", JavaAdapter), ("src/lib.rs", RustAdapter)],
)
def test_compiled_language_selection_and_common_contract(path, adapter_type):
    adapter = select_language_adapter(path)
    assert isinstance(adapter, adapter_type)
    assert adapter.detect_language(path)
    assert adapter.language_version() is None
    report = adapter.capability_report()
    assert isinstance(report, LanguageCapabilityReport)
    assert report.language == adapter.language
    assert report.parser == "available"
    assert report.syntax_validation == "available"
    assert report.protected_generated_files == "available"
    assert set(report.__dataclass_fields__) >= {
        "language", "language_version", "parser", "syntax_validation", "formatter",
        "linter", "type_checker", "test_runner", "limitations",
    }


@pytest.mark.parametrize(
    ("adapter", "path", "source", "symbol"),
    [
        (GoAdapter(), "src/main.go", "package main\n\nfunc Compute(value int) int {\n return value\n}\n", "Compute"),
        (JavaAdapter(), "src/Main.java", "public class Main {\n public int compute(int value) {\n  return value;\n }\n}\n", "Main"),
        (RustAdapter(), "src/lib.rs", "pub fn compute(value: i32) -> i32 {\n value\n}\n", "compute"),
    ],
)
def test_compiled_static_parsing_and_syntax(adapter, path, source, symbol):
    parsed = adapter.syntax_check(path, source)
    assert parsed.syntax_valid is True
    assert any(item.name == symbol for item in parsed.symbols)
    broken = adapter.syntax_check(path, source.replace("}\n", "", 1))
    assert broken.syntax_valid is False
    assert broken.errors
    assert adapter.validate_patch_constraints(
        type("Patch", (), {"path": path, "operation": "modify", "hunks": [type("Hunk", (), {"lines": ["-x", "+y"]})()]})(),
        source,
    ) == ()


@pytest.mark.parametrize("adapter,path", [
    (GoAdapter(), "src/main.go"), (JavaAdapter(), "src/Main.java"), (RustAdapter(), "src/lib.rs"),
])
def test_compiled_adapter_protected_generated_and_command_gates(adapter, path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _name: None)
    tools = adapter.report_tool_availability()
    assert tools["parser"] is True
    assert tools["formatter"] is False
    assert adapter.produce_allowlisted_commands([], tools) == ()
    assert adapter.identify_protected_generated_files("target/generated.rs")
    assert adapter.identify_protected_generated_files("tests/MainTest.java")
    assert adapter.identify_protected_generated_files("vendor/main.go")
    assert adapter.allowlisted_check_commands("../escape" + Path(path).suffix, tools) == ()


@pytest.mark.parametrize(
    ("language", "changed", "test_file", "config", "runner"),
    [
        ("go", ["src/main.go"], "src/main_test.go", {}, "go"),
        ("java", ["src/Main.java"], "src/MainTest.java", {"pom.xml": "<project/>"}, "maven"),
        ("rust", ["src/lib.rs"], "tests/lib_test.rs", {"Cargo.toml": "[package]\nname='x'\nversion='0.1.0'\n"}, "cargo"),
    ],
)
def test_compiled_test_discovery_is_bounded_and_allowlisted(tmp_path, language, changed, test_file, config, runner, monkeypatch):
    for relative, content in config.items():
        (tmp_path / relative).write_text(content, encoding="utf-8")
    target = tmp_path / test_file
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("test", encoding="utf-8")
    monkeypatch.setattr("shutil.which", lambda name: name in {"go", "mvn", "cargo"})
    plan, blocked = discover_test_plan(
        tmp_path, changed, full_suite=False, runner_preference=runner,
    )
    assert blocked is None
    assert plan.language == language
    assert plan.test_files == [test_file]
    assert plan.exact_command
    assert validate_test_command(plan.exact_command).allowed
    assert all("install" not in item for item in plan.exact_command)


def test_repository_index_parses_go_java_rust_symbols(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/main.go").write_text("package main\nfunc Compute() {}\n", encoding="utf-8")
    (tmp_path / "src/Main.java").write_text("class Main { void compute() {} }\n", encoding="utf-8")
    (tmp_path / "src/lib.rs").write_text("pub fn compute() {}\n", encoding="utf-8")
    index = build_repository_index(tmp_path)
    assert {item.language for item in index.files.values()} >= {"go", "java", "rust"}
    assert {item.name for item in index.symbols} >= {"Compute", "Main", "compute"}
    assert all(index.files[path].parse_status == "parsed" for path in ("src/main.go", "src/Main.java", "src/lib.rs"))


def test_all_language_capability_reports_have_explicit_statuses():
    reports = language_capability_reports()
    assert {item.language for item in reports} == {"python", "javascript", "typescript", "go", "java", "rust"}
    allowed = {"available", "unavailable", "unsupported", "not_run", "passed", "failed", "blocked"}
    for report in reports:
        assert {report.parser, report.syntax_validation, report.formatter, report.linter,
                report.type_checker, report.test_runner} <= allowed


def test_command_policy_accepts_only_offline_compiled_templates():
    assert validate_test_command(["go", "test", "-mod=readonly", "./src"]).allowed
    assert validate_test_command(["cargo", "test", "--offline"]).allowed
    assert validate_test_command(["mvn", "-o", "test"]).allowed
    assert validate_test_command(["gradle", "--offline", "test"]).allowed
    assert not validate_test_command(["go", "test", "-mod=mod", "./src"]).allowed
    assert not validate_test_command(["cargo", "test", "--manifest-path", "../Cargo.toml"]).allowed
    assert validate_check_command(["gofmt", "-d", "src/main.go"]).allowed
    assert validate_check_command(["rustfmt", "--check", "src/lib.rs"]).allowed
    assert validate_check_command(["cargo", "check", "--offline"]).allowed
