from __future__ import annotations

from copy import deepcopy
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

_SPEC = spec_from_file_location(
    "run_foreground_pipeline_acceptance",
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "run_foreground_pipeline_acceptance.py",
)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
AcceptanceManifestError = _MODULE.AcceptanceManifestError
evaluate_acceptance_manifest = _MODULE.evaluate_acceptance_manifest


def _manifest(*, products: int = 10, samples: int = 50) -> dict[str, object]:
    return {
        "schema_version": "foreground-pipeline-acceptance-v1",
        "pipeline_manifest_sha256": "a" * 64,
        "risk_config_sha256": "b" * 64,
        "sample_specific_rules": [],
        "samples": [
            {
                "sample_id": f"sample-{index:02d}",
                "product_id": f"product-{index % products:02d}",
                "source_sha256": f"{index + 1:064x}",
                "gold_mask_sha256": f"{index + 101:064x}",
                "predicted_mask_sha256": f"{index + 201:064x}",
                "stage_receipts_sha256": f"{index + 301:064x}",
                "split": "acceptance",
                "human_reviewed": True,
                "status": "passed",
                "failure_reasons": [],
                "elapsed_ms": index + 1,
            }
            for index in range(samples)
        ],
    }


def test_acceptance_rejects_sample_specific_rules() -> None:
    manifest = _manifest()
    manifest["sample_specific_rules"] = [{"sample_id": "sample-01", "threshold": 0.1}]

    with pytest.raises(AcceptanceManifestError, match="sample_specific_rules_forbidden"):
        evaluate_acceptance_manifest(manifest)


def test_acceptance_reports_all_failures_not_only_averages() -> None:
    manifest = _manifest()
    samples = manifest["samples"]
    assert isinstance(samples, list)
    samples[3]["status"] = "failed"
    samples[3]["failure_reasons"] = ["pendant_missing"]
    samples[41]["status"] = "failed"
    samples[41]["failure_reasons"] = ["background_contamination"]

    report = evaluate_acceptance_manifest(manifest)

    assert report["passed"] is False
    assert report["failed_sample_count"] == 2
    assert report["failures"] == [
        {"sample_id": "sample-03", "reasons": ["pendant_missing"]},
        {"sample_id": "sample-41", "reasons": ["background_contamination"]},
    ]


def test_acceptance_requires_ten_unique_products() -> None:
    report = evaluate_acceptance_manifest(_manifest(products=9))

    assert report["reuse_gate"] == {
        "passed": False,
        "required_unique_products": 10,
        "actual_unique_products": 9,
    }
    assert report["passed"] is False


def test_acceptance_requires_fifty_frozen_gold_samples() -> None:
    manifest = deepcopy(_manifest(samples=49))
    report = evaluate_acceptance_manifest(manifest)

    assert report["gold_gate"] == {
        "passed": False,
        "required_frozen_samples": 50,
        "actual_frozen_samples": 49,
    }
    assert report["passed"] is False


def test_acceptance_requires_stage_receipt_evidence_for_every_sample() -> None:
    manifest = _manifest()
    samples = manifest["samples"]
    assert isinstance(samples, list)
    del samples[0]["stage_receipts_sha256"]

    with pytest.raises(AcceptanceManifestError, match="sample_shape_invalid"):
        evaluate_acceptance_manifest(manifest)


def test_acceptance_report_is_path_free_and_digest_bound() -> None:
    report = evaluate_acceptance_manifest(_manifest())
    serialized = str(report)

    assert report["passed"] is True
    assert report["pipeline_manifest_sha256"] == "a" * 64
    assert report["risk_config_sha256"] == "b" * 64
    assert "/private/" not in serialized
    assert "source_content" not in serialized
    assert "mask_content" not in serialized
