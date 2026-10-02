import json

from codeatlas.evidence import EvidenceLogger
from codeatlas.findings import Finding, render_json, render_markdown
from codeatlas.orchestrator import ReviewRequest, RunManifest
from codeatlas.providers import FakeProvider


def sample_finding() -> Finding:
    return Finding(
        id="CA-TEST-1",
        file="src/example.py",
        start_line=3,
        end_line=4,
        severity="medium",
        category="correctness",
        claim="The branch can return an invalid value.",
        impact="The caller may fail to handle the result.",
        evidence_strength="supported",
        confidence=0.8,
        fixability="suggested",
        status="detected",
    )


def test_manifest_serializes_schema_fields_and_phase2_context():
    manifest = RunManifest(
        run_id="run-1",
        repository="repo",
        base_commit="abc",
        head_commit="def",
        base_ref="main",
        head_ref="feature",
        detected_languages=["python"],
    )
    payload = manifest.model_dump(mode="json")
    assert payload["base_ref"] == "main"
    assert payload["detected_languages"] == ["python"]
    assert RunManifest.model_validate_json(manifest.model_dump_json()) == manifest


def test_renderers_produce_json_and_markdown():
    finding = sample_finding()
    encoded = render_json([finding], indent=None)
    assert json.loads(encoded)[0]["id"] == "CA-TEST-1"
    markdown = render_markdown([finding])
    assert "CA-TEST-1" in markdown
    assert "src/example.py:3-4" in markdown
    assert "The branch can return" in markdown


def test_evidence_logger_writes_lifecycle_jsonl_without_sensitive_content(tmp_path):
    path = tmp_path / "evidence.jsonl"
    with EvidenceLogger(path) as logger:
        logger.run_started(run_id="run-1", repository="repo", source="SECRET SOURCE")
        logger.file_analyzed(file="src/example.py", language="python", content="SECRET SOURCE")
        logger.finding_emitted(finding_id="CA-TEST-1")
        logger.run_completed(run_id="run-1", token="secret-token")
    events = [json.loads(line) for line in path.read_text().splitlines()]
    assert [event["event"] for event in events] == [
        "run_started", "file_analyzed", "finding_emitted", "run_completed"
    ]
    assert all("SECRET SOURCE" not in line and "secret-token" not in line for line in path.read_text().splitlines())
    assert all("timestamp" in event for event in events)


def test_fake_provider_is_deterministic_and_does_not_fabricate_findings():
    result = FakeProvider().review(ReviewRequest(repository="repo", files=("a.py",)))
    assert result == ()
