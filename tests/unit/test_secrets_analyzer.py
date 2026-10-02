from dataclasses import dataclass
from pathlib import Path

import pytest

from codeatlas.analyzers import AnalysisContext, AnalyzerRegistry, HardcodedSecretAnalyzer, default_registry
from codeatlas.git.models import ChangeStatus, Diff, FileChange, LineRange


@dataclass(frozen=True)
class Snapshot:
    path: Path


def run(tmp_path: Path, name: str, content: str, *, status=ChangeStatus.MODIFIED, start=1, count=None, **options):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    count = count if count is not None else len(content.splitlines())
    diff = Diff("base", "head", (FileChange(status, name, new_ranges=(LineRange(start, count),)),))
    return HardcodedSecretAnalyzer(**options).analyze(Snapshot(tmp_path), diff)


@pytest.mark.parametrize(
    ("name", "content", "rule"),
    [
        ("a.py", 'KEY = "-----BEGIN RSA PRIVATE KEY-----"\n', "private-key"),
        ("a.py", 'KEY = "AKIA1234567890ABCDEF"\n', "aws-access-key"),
        ("a.js", 'const token = "ghp_123456789012345678901234567890123456";\n', "github-token"),
        ("a.ts", 'const apiKey = "live-api-key-value";\n', "api-key-assignment"),
        ("a.ts", 'const password = "correct horse battery staple";\n', "sensitive-assignment"),
        ("a.py", 'url = "postgres://app:real-password@db.example/app"\n', "database-url"),
    ],
)
def test_detects_each_rule_for_supported_languages(tmp_path, name, content, rule):
    findings = run(tmp_path, name, content)
    assert [finding.provenance["rule"] for finding in findings] == [rule]
    assert findings[0].category == "HARD_CODED_SECRET"


def test_private_key_is_blocker(tmp_path):
    finding = run(tmp_path, "key.py", 'KEY = "-----BEGIN RSA PRIVATE KEY-----"\n')[0]
    assert finding.severity == "blocker"


def test_findings_redact_secret_values_and_ids_are_stable(tmp_path):
    content = 'API_KEY = "super-secret-value"\n'
    first = run(tmp_path, "config.py", content)[0]
    second = run(tmp_path, "config.py", content)[0]
    assert first == second
    assert "super-secret-value" not in first.model_dump_json()
    changed = run(tmp_path, "config.py", 'API_KEY = "different-secret-value"\n')[0]
    assert changed.id == first.id


@pytest.mark.parametrize("value", ["changeme", "${API_KEY}", "your-api-key", "dummy"])
def test_skips_placeholders_and_environment_references(tmp_path, value):
    assert run(tmp_path, "config.py", f'api_key = "{value}"\n') == ()


def test_allow_patterns_and_confidence_filter(tmp_path):
    content = 'token = "real-value"\n'
    assert run(tmp_path, "a.py", content, allow_patterns=(r"real-value",)) == ()
    assert run(tmp_path, "a.py", content, min_confidence=0.9) == ()
    assert run(tmp_path, "a.py", content, min_confidence=0.88)


def test_only_changed_added_lines_and_non_deleted_files_are_analyzed(tmp_path):
    path = tmp_path / "a.py"
    path.write_text('safe = True\nsecret = "real-value"\n', encoding="utf-8")
    diff = Diff("base", "head", (FileChange(ChangeStatus.MODIFIED, "a.py", new_ranges=(LineRange(1, 1),)),))
    assert HardcodedSecretAnalyzer().analyze(Snapshot(tmp_path), diff) == ()
    assert run(tmp_path, "a.py", 'secret = "real-value"\n', status=ChangeStatus.DELETED) == ()


def test_renamed_file_scans_new_path(tmp_path):
    path = tmp_path / "new.py"
    path.write_text('token = "real-value"\n', encoding="utf-8")
    diff = Diff("base", "head", (FileChange(ChangeStatus.RENAMED, "new.py", old_path="old.py", new_ranges=(LineRange(1),)),))
    findings = HardcodedSecretAnalyzer().analyze(Snapshot(tmp_path), diff)
    assert len(findings) == 1
    assert findings[0].file == "new.py"


def test_unsupported_language_is_skipped(tmp_path):
    assert run(tmp_path, "a.go", 'token = "real-value"\n') == ()


def test_registry_contains_default_secret_analyzer(tmp_path):
    registry = default_registry()
    assert registry.get("hardcoded-secrets").name == "hardcoded-secrets"
    content = 'token = "real-value"\n'
    path = tmp_path / "a.py"
    path.write_text(content, encoding="utf-8")
    context = AnalysisContext(Snapshot(tmp_path), Diff("b", "h", (FileChange(ChangeStatus.ADDED, "a.py", new_ranges=(LineRange(1),)),)))
    assert registry.analyze(context)
    assert len(tuple(AnalyzerRegistry())) == 0
