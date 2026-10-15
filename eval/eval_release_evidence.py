"""Offline release evidence with isolated builds and fail-closed archive scans."""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from email.parser import BytesParser
from pathlib import Path, PurePosixPath

if __package__:
    from eval.eval_platform_matrix import platform_matrix
else:
    from eval_platform_matrix import platform_matrix

SECRET_PATTERNS = {
    "api_key": re.compile(rb"sk-[A-Za-z0-9_-]{20,}"),
    "github_token": re.compile(rb"gh[pousr]_[A-Za-z0-9]{36,}"),
    "aws_key": re.compile(rb"AKIA[0-9A-Z]{16}"),
    "private_key": re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "approval_token": re.compile(rb"appr_[A-Za-z0-9_-]{16,}"),
}
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024


def scan_archive(path: Path) -> dict:
    errors = []
    inventory = []
    if path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ValueError("Archive exceeds compressed size limit")
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        if len(infos) > 1000 or sum(info.file_size for info in infos) > MAX_ARCHIVE_BYTES:
            raise ValueError("Archive exceeds expanded size or entry limit")
        seen = set()
        for info in infos:
            name = info.orig_filename
            parts = PurePosixPath(name).parts
            if name.casefold() in seen or "\\" in name or ":" in name or name.startswith("/") or ".." in parts:
                errors.append({"file": name, "reason": "unsafe_or_duplicate_path"})
                continue
            seen.add(name.casefold())
            if stat.S_ISLNK(info.external_attr >> 16):
                errors.append({"file": name, "reason": "symlink"})
                continue
            data = archive.read(info)
            inventory.append({"file": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
            for label, pattern in SECRET_PATTERNS.items():
                if pattern.search(data):
                    errors.append({"file": name, "reason": label})
            if path.suffix == ".whl" and not (name.startswith("codeatlas/") or re.match(r"codeatlas-[^/]+\.dist-info/", name)):
                errors.append({"file": name, "reason": "wheel_boundary"})
            if path.suffix == ".whl" and any(part in {"tests", "__pycache__", ".env", "node_modules"} for part in parts):
                errors.append({"file": name, "reason": "development_artifact"})
    return {"status": "failed" if errors else "passed", "errors": errors, "inventory": inventory, "scope": "bounded archive/path and credential-pattern scan, not vulnerability scanning"}


def deterministic_vsix(extension: Path, output: Path, allowlist: list[str]) -> None:
    manifest = json.loads((extension / "package.json").read_text(encoding="utf-8"))
    version = manifest["version"]
    publisher = manifest["publisher"]
    name = manifest["name"]
    engine = manifest["engines"]["vscode"]
    envelope = f'''<?xml version="1.0" encoding="utf-8"?>
<PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011"><Metadata><Identity Language="en-US" Id="{name}" Version="{version}" Publisher="{publisher}"/><DisplayName>CodeAtlas</DisplayName><Description xml:space="preserve">Evidence-first repository review</Description><Tags/><Categories>Linters</Categories><GalleryFlags>Public</GalleryFlags><Properties><Property Id="Microsoft.VisualStudio.Code.Engine" Value="{engine}"/></Properties></Metadata><Installation><InstallationTarget Id="Microsoft.VisualStudio.Code"/></Installation><Dependencies/><Assets><Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json" Addressable="true"/></Assets></PackageManifest>'''
    types = '''<?xml version="1.0" encoding="utf-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="json" ContentType="application/json"/><Default Extension="js" ContentType="application/javascript"/><Default Extension="xml" ContentType="text/xml"/><Default Extension="vsixmanifest" ContentType="text/xml"/><Default Extension="md" ContentType="text/markdown"/><Default Extension="svg" ContentType="image/svg+xml"/><Default Extension="ts" ContentType="text/plain"/><Default Extension="map" ContentType="application/json"/><Default Extension="" ContentType="text/plain"/></Types>'''
    files = {"extension.vsixmanifest": envelope.encode(), "[Content_Types].xml": types.encode()}
    for relative in sorted(allowlist):
        source = extension / relative
        if source.is_symlink() or not source.is_file():
            raise ValueError(f"Missing or linked allowlisted entry: {relative}")
        source.resolve().relative_to(extension.resolve())
        files[f"extension/{relative}"] = source.read_bytes()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)


def deterministic_wheel(source: Path, output: Path) -> None:
    """Assemble a deterministic pure-Python wheel without network or build dependencies."""
    project = tomllib.loads((source / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    normalized = project["name"].replace("-", "_")
    dist_info = f"{normalized}-{project['version']}.dist-info"
    files: dict[str, bytes] = {}
    for path in sorted((source / "src" / "codeatlas").rglob("*.py")):
        relative = path.relative_to(source / "src").as_posix()
        files[relative] = path.read_bytes()
    dependencies = "".join(f"Requires-Dist: {item}\n" for item in project.get("dependencies", ()))
    files[f"{dist_info}/METADATA"] = (
        "Metadata-Version: 2.3\n"
        f"Name: {project['name']}\n"
        f"Version: {project['version']}\n"
        f"Summary: {project['description']}\n"
        f"Requires-Python: {project['requires-python']}\n"
        f"{dependencies}\n"
    ).encode()
    files[f"{dist_info}/WHEEL"] = (
        b"Wheel-Version: 1.0\nGenerator: codeatlas-release-evidence\n"
        b"Root-Is-Purelib: true\nTag: py3-none-any\n"
    )
    files[f"{dist_info}/entry_points.txt"] = b"[console_scripts]\ncodeatlas = codeatlas.cli:app\n"
    record_name = f"{dist_info}/RECORD"
    record_stream = io.StringIO(newline="")
    writer = csv.writer(record_stream, lineterminator="\n")
    for name, data in sorted(files.items()):
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        writer.writerow((name, f"sha256={digest}", len(data)))
    writer.writerow((record_name, "", ""))
    files[record_name] = record_stream.getvalue().encode()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data, compresslevel=9)


def command_evidence(command: list[str], cwd: Path) -> dict:
    environment = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR", "PATHEXT", "LOCALAPPDATA", "APPDATA", "USERPROFILE", "TEMP", "TMP") if key in os.environ}
    environment.update({"PIP_NO_INDEX": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PYTHONDONTWRITEBYTECODE": "1", "SOURCE_DATE_EPOCH": "315532800"})
    try:
        proc = subprocess.run(command, cwd=cwd, env=environment, capture_output=True, timeout=120, check=False)
        return {"status": "passed" if proc.returncode == 0 else "failed", "exit_code": proc.returncode, "stdout_sha256": hashlib.sha256(proc.stdout).hexdigest(), "stderr_sha256": hashlib.sha256(proc.stderr).hexdigest()}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "blocked", "reason": type(exc).__name__}


def wheel_install_check(wheel: Path, directory: Path) -> dict:
    environment = directory / "venv"
    steps = {"create": command_evidence([sys.executable, "-m", "venv", str(environment)], directory)}
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if steps["create"]["status"] != "passed":
        return {"status": "blocked", "steps": steps}
    steps["install"] = command_evidence([str(python), "-m", "pip", "install", "--no-index", "--no-deps", str(wheel)], directory)
    steps["metadata"] = command_evidence([str(python), "-I", "-c", "import importlib.metadata as m; d=m.distribution('codeatlas'); assert d.version == '0.1.0'; assert any(e.name == 'codeatlas' for e in d.entry_points); import codeatlas"], directory)
    steps["uninstall"] = command_evidence([str(python), "-m", "pip", "uninstall", "-y", "codeatlas"], directory)
    steps["absent"] = command_evidence([str(python), "-I", "-c", "import importlib.util; assert importlib.util.find_spec('codeatlas') is None"], directory)
    return {"status": "passed" if all(row["status"] == "passed" for row in steps.values()) else "failed", "steps": steps, "scope": "no-deps packaging smoke, not dependency-complete runtime installation"}


def vsix_install_check(vsix: Path, directory: Path) -> dict:
    executable = shutil.which("code") or shutil.which("code.cmd")
    if not executable:
        return {"status": "blocked", "reason": "VS Code CLI unavailable"}
    arguments = [executable, "--user-data-dir", str(directory / "profile"), "--extensions-dir", str(directory / "extensions")]
    steps = {"install": command_evidence([*arguments, "--install-extension", str(vsix), "--force"], directory)}
    installed = directory / "extensions" / "codeatlas.codeatlas-0.1.0" / "package.json"
    steps["present"] = {"status": "passed" if installed.is_file() else "failed"}
    steps["uninstall"] = command_evidence([*arguments, "--uninstall-extension", "codeatlas.codeatlas"], directory)
    # VS Code may retain removed directories until the next process starts.
    listing = directory / "extensions" / "extensions.json"
    absent = listing.exists() and "codeatlas.codeatlas" not in listing.read_text(encoding="utf-8").lower()
    steps["unregistered"] = {"status": "passed" if absent else "failed"}
    return {"status": "passed" if all(row["status"] == "passed" for row in steps.values()) else "failed", "steps": steps, "scope": "disposable CLI profile; no GUI activation smoke"}


def run_release(root: Path, output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    report = {"schema_version": 1, "production_ready": False, "artifacts": [], "checks": {}, "gaps": ["Offline credential-pattern scans do not replace dependency CVE scanning.", "Build inputs are the working tree, not a signed/tagged or immutable release source.", "Custom deterministic wheel assembly is not setuptools backend certification.", "Custom deterministic VSIX builder is not Marketplace/vsce certification."]}
    checks = report["checks"]
    with tempfile.TemporaryDirectory(prefix="codeatlas-release-") as temporary:
        stage = Path(temporary)
        extension = stage / "extension"
        shutil.copytree(root / "extension", extension, ignore=shutil.ignore_patterns("out", "*.vsix"))
        node = shutil.which("node")
        compiler = root / "extension/node_modules/typescript/bin/tsc"
        checks["compile"] = command_evidence([node, str(compiler), "-p", str(extension)], stage) if node and compiler.is_file() else {"status": "blocked", "reason": "Local TypeScript compiler unavailable"}
        if node and checks["compile"]["status"] == "passed":
            allowlist_proc = subprocess.run([node, "-e", "console.log(JSON.stringify([...require('./package').PACKAGED_FILES_ALLOWLIST]))"], cwd=extension, capture_output=True, check=True)
            allowlist = json.loads(allowlist_proc.stdout)
            first = output / "codeatlas-0.1.0.vsix"
            second = stage / "repeat.vsix"
            deterministic_vsix(extension, first, allowlist)
            deterministic_vsix(extension, second, allowlist)
            checks["vsix_reproducible"] = {"status": "passed" if first.read_bytes() == second.read_bytes() else "failed", "scope": "two archive assemblies of the same freshly compiled inputs"}
            scan = scan_archive(first)
            checks["vsix_scan"] = {"status": scan["status"], "scope": scan["scope"]}
            with zipfile.ZipFile(first) as archive:
                expected = {f"extension/{item}" for item in allowlist} | {"extension.vsixmanifest", "[Content_Types].xml"}
                checks["vsix_boundary"] = {"status": "passed" if set(archive.namelist()) == expected else "failed"}
                manifest = json.loads(archive.read("extension/package.json"))
                source_manifest = json.loads((root / "extension/package.json").read_text(encoding="utf-8"))
                required = {"name", "version", "publisher", "engines", "license", "repository", "main", "contributes", "capabilities"}
                checks["vsix_metadata"] = {"status": "passed" if required <= manifest.keys() and manifest == source_manifest and manifest["main"] == "./out/extension.js" else "failed", "version": manifest.get("version"), "license": manifest.get("license"), "vscode_engine": manifest.get("engines", {}).get("vscode"), "commands": len(manifest.get("contributes", {}).get("commands", []))}
            report["artifacts"].append({"file": first.name, "sha256": hashlib.sha256(first.read_bytes()).hexdigest(), "scan": scan})
            checks["vsix_install_uninstall"] = vsix_install_check(first, stage) if scan["status"] == "passed" else {"status": "blocked", "reason": "Artifact scan failed"}
        source = stage / "python-source"
        source.mkdir()
        shutil.copytree(root / "src/codeatlas", source / "src/codeatlas", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for name in ("pyproject.toml", "README.md"):
            shutil.copy2(root / name, source / name)
        first_wheel = output / "codeatlas-0.1.0-py3-none-any.whl"
        repeated_wheel = stage / "codeatlas-0.1.0-py3-none-any.whl"
        try:
            deterministic_wheel(source, first_wheel)
            deterministic_wheel(source, repeated_wheel)
            checks["wheel_build"] = {
                "status": "passed",
                "scope": "network-free deterministic assembly of the declared pure-Python package",
            }
        except (OSError, KeyError, TypeError, ValueError, zipfile.BadZipFile) as exc:
            checks["wheel_build"] = {"status": "failed", "reason": type(exc).__name__}
        if checks["wheel_build"]["status"] == "passed":
            checks["wheel_reproducible"] = {
                "status": "passed" if repeated_wheel.read_bytes() == first_wheel.read_bytes() else "failed",
                "scope": "two independent network-free archive assemblies of the same source inputs",
            }
            scan = scan_archive(first_wheel)
            checks["wheel_scan"] = {"status": scan["status"], "scope": scan["scope"]}
            with zipfile.ZipFile(first_wheel) as archive:
                metadata = BytesParser().parsebytes(archive.read("codeatlas-0.1.0.dist-info/METADATA"))
                project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
                checks["wheel_metadata"] = {"status": "passed" if metadata["Name"] == project["name"] and metadata["Version"] == project["version"] and metadata["Requires-Python"] == project["requires-python"] else "failed"}
            report["artifacts"].append({"file": first_wheel.name, "sha256": hashlib.sha256(first_wheel.read_bytes()).hexdigest(), "scan": scan})
            checks["wheel_install_uninstall"] = wheel_install_check(first_wheel, stage) if scan["status"] == "passed" else {"status": "blocked", "reason": "Artifact scan failed"}
        else:
            checks["wheel_install_uninstall"] = {"status": "blocked", "reason": "No wheel produced"}
    matrix = platform_matrix({name: checks[name]["status"] for name in ("wheel_install_uninstall", "vsix_install_uninstall") if name in checks})
    (output / "platform-matrix.json").write_text(json.dumps(matrix, indent=2) + "\n", encoding="utf-8")
    (output / "SHA256SUMS").write_text("".join(f"{row['sha256']}  {row['file']}\n" for row in report["artifacts"]), encoding="utf-8")
    (output / "release-evidence.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    destination = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else root / "eval/results/release"
    result = run_release(root, destination)
    print(json.dumps(result["checks"], indent=2))
    raise SystemExit(1 if any(row["status"] != "passed" for row in result["checks"].values()) else 0)
