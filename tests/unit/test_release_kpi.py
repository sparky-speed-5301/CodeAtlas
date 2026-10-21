from __future__ import annotations

from pathlib import Path

from test_release_evidence import _payload, _validate

from codeatlas.benchmark.product_validation import load_metric_catalog
from codeatlas.release_kpi import validate_kpi_catalog


CATALOG_PATH = Path("eval/product-validation/metrics-v1.json")


def _full_payload() -> dict[str, object]:
    payload = _payload()
    catalog = load_metric_catalog(CATALOG_PATH)
    metrics: list[dict[str, object]] = []
    for metric in catalog.metrics:
        if metric.unit == "rate":
            observation = {
                "status": "contract_only",
                "mode": "offline",
                "numerator": 1,
                "denominator": 1,
                "value": 1.0,
                "observation_count": 1,
            }
        else:
            observation = {
                "status": "contract_only",
                "mode": "offline",
                "numerator": 0,
                "denominator": 0,
                "value": 100.0,
                "observation_count": 1,
            }
        observation.update(
            {
                "unit": metric.unit,
                "evidence_source": "kpi-fixture",
                "exclusions": [],
                "limitations": [],
            }
        )
        metrics.append(
            {
                "metric_name": metric.name,
                "metric_group": metric.group,
                "observation": observation,
            }
        )
    payload["kpis"] = metrics
    return payload


def test_kpi_catalog_requires_every_metric_and_preserves_four_groups() -> None:
    evidence = _validate(_full_payload())
    report = validate_kpi_catalog(evidence)

    assert report.valid is True
    assert report.status == "contract_only"
    assert report.required_metrics == report.observed_metrics == len(load_metric_catalog(CATALOG_PATH).metrics)
    assert {group.group for group in report.groups} == {
        "review_usefulness",
        "autofix_usefulness",
        "reliability",
        "trust_and_safety",
    }
    assert report.production_ready is False


def test_kpi_catalog_reports_missing_and_unit_mismatch_as_blocked() -> None:
    payload = _full_payload()
    payload["kpis"] = payload["kpis"][:-1]  # type: ignore[index]
    report = validate_kpi_catalog(_validate(payload))
    assert report.valid is False
    assert report.status == "blocked"
    assert report.missing_metrics

    payload = _full_payload()
    for row in payload["kpis"]:  # type: ignore[union-attr]
        if row["metric_name"] == "time_to_understand_finding":
            row["observation"]["unit"] = "rate"  # type: ignore[index]
            row["observation"]["numerator"] = 1  # type: ignore[index]
            row["observation"]["denominator"] = 1  # type: ignore[index]
            row["observation"]["value"] = 1.0  # type: ignore[index]
            break
    report = validate_kpi_catalog(_validate(payload))
    assert report.valid is False
    assert report.status == "blocked"
    assert "time_to_understand_finding" in report.unexpected_metrics


def test_kpi_catalog_rejects_benchmark_drift_and_unlinked_catalog(tmp_path: Path) -> None:
    evidence = _validate(_full_payload())
    catalog_text = CATALOG_PATH.read_text(encoding="utf-8").replace(
        "wp7-controlled-beta-v1", "other-benchmark-v1"
    )
    drifted = tmp_path / "drifted-metrics.json"
    drifted.write_text(catalog_text, encoding="utf-8")
    report = validate_kpi_catalog(evidence, drifted)
    assert report.valid is False
    assert report.status == "blocked"
    assert "catalog_benchmark_version" in report.missing_metrics

    linked = tmp_path / "linked-metrics.json"
    linked.hardlink_to(drifted)
    try:
        validate_kpi_catalog(evidence, linked)
    except ValueError as error:
        assert "unlinked" in str(error)
    else:
        raise AssertionError("linked KPI catalog was accepted")
