"""Focused repository containment regressions without repository execution."""

from pathlib import Path

import pytest

from codeatlas.repository.scanner import scan_repository


def test_scanner_checks_resolved_file_containment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    link = root / "linked.py"
    link.write_text("# local placeholder\n", encoding="utf-8")
    outside = tmp_path / "outside.py"
    outside.write_text("# must not be read\n", encoding="utf-8")
    original_resolve = Path.resolve

    def resolve(path: Path, *args, **kwargs) -> Path:
        if path == link:
            return outside
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    assert "linked.py" not in scan_repository(root)


def test_scanner_excludes_external_symlink(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("# must not be read\n", encoding="utf-8")
    try:
        (root / "linked.py").symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"Symlink creation unavailable: {exc.errno}")
    assert "linked.py" not in scan_repository(root)


def test_scanner_preserves_regular_files(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    assert scan_repository(tmp_path)["app.py"].line_count == 1
