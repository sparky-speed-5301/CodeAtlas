"""Release scanner and platform evidence contract regressions."""

import json
import zipfile
from pathlib import Path

import pytest

from eval.eval_platform_matrix import FEATURES, platform_matrix
from eval.eval_release_evidence import (
    deterministic_vsix,
    deterministic_wheel,
    scan_archive,
)


def test_matrix_does_not_invent_platform_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "win32")
    matrix = platform_matrix({"runner_policy": "passed"})
    assert len(matrix["rows"]) == 3 * len(FEATURES)
    assert all(row["status"] == "not_run" for row in matrix["rows"] if row["os"] != "windows")
    assert matrix["production_ready"] is False
    with pytest.raises(ValueError):
        platform_matrix({"runner_policy": "supported"})


def test_matrix_records_required_environments_without_inventing_passes() -> None:
    matrix = platform_matrix()
    environments = {row["environment"]: row for row in matrix["environments"]}
    assert set(environments) == {
        "Python 3.11", "Python 3.12", "Python 3.14", "Linux", "Windows",
        "Go", "Java", "Rust", "VS Code host",
    }
    assert matrix["benchmark_version"] == "real-repositories-v1"
    assert matrix["timestamp"]
    for row in environments.values():
        assert {"availability", "version", "command", "result", "skipped_tests", "limitations"} <= row.keys()
        if row["availability"] == "unavailable":
            assert row["result"] == "unavailable"


@pytest.mark.parametrize("name", ["../outside", "/absolute", "C:/outside", "extension\\outside"])
def test_archive_rejects_unsafe_names(tmp_path: Path, name: str) -> None:
    artifact = tmp_path / "bad.vsix"
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr(name.replace("\\", "/"), b"data")
    if "\\" in name:
        artifact.write_bytes(artifact.read_bytes().replace(name.replace("\\", "/").encode(), name.encode()))
    assert scan_archive(artifact)["status"] == "failed"


def test_archive_scans_envelope_without_persisting_secret(tmp_path: Path) -> None:
    artifact = tmp_path / "bad.vsix"
    secret = "sk-" + "x" * 30
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr("extension.vsixmanifest", secret)
    result = scan_archive(artifact)
    assert result["status"] == "failed"
    assert secret not in json.dumps(result)


def test_wheel_archive_is_byte_reproducible_and_installable_shape(tmp_path: Path) -> None:
    source = tmp_path / "source"
    package = source / "src/codeatlas"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("__version__ = '0.1.0'\n", encoding="utf-8")
    (package / "cli.py").write_text("app = object()\n", encoding="utf-8")
    (source / "pyproject.toml").write_text(
        "[project]\nname='codeatlas'\nversion='0.1.0'\n"
        "description='test'\nrequires-python='>=3.11'\ndependencies=[]\n",
        encoding="utf-8",
    )
    first, second = tmp_path / "one.whl", tmp_path / "two.whl"
    deterministic_wheel(source, first)
    deterministic_wheel(source, second)
    assert first.read_bytes() == second.read_bytes()
    assert scan_archive(first)["status"] == "passed"
    with zipfile.ZipFile(first) as archive:
        assert "codeatlas/__init__.py" in archive.namelist()
        assert "codeatlas-0.1.0.dist-info/RECORD" in archive.namelist()


def test_vsix_archive_is_byte_reproducible(tmp_path: Path) -> None:
    extension = tmp_path / "extension"
    extension.mkdir()
    (extension / "package.json").write_text(json.dumps({"name": "codeatlas", "version": "0.1.0", "publisher": "codeatlas", "engines": {"vscode": "^1.85.0"}}), encoding="utf-8")
    first, second = tmp_path / "one.vsix", tmp_path / "two.vsix"
    deterministic_vsix(extension, first, ["package.json"])
    deterministic_vsix(extension, second, ["package.json"])
    assert first.read_bytes() == second.read_bytes()
    assert scan_archive(first)["status"] == "passed"
