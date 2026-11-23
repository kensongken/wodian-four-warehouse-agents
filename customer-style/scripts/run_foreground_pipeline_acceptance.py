"""Evaluate sealed, offline foreground-pipeline acceptance receipts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from string import hexdigits
from typing import Any


class AcceptanceManifestError(ValueError):
    """A stable failure that never includes private manifest values."""


_SAMPLE_KEYS = {
    "elapsed_ms",
    "failure_reasons",
    "gold_mask_sha256",
    "human_reviewed",
    "predicted_mask_sha256",
    "product_id",
    "sample_id",
    "source_sha256",
    "stage_receipts_sha256",
    "split",
    "status",
}


def _sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in hexdigits for character in value)
    )


def _validated_samples(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw_samples = payload.get("samples")
    if not isinstance(raw_samples, list):
        raise AcceptanceManifestError("samples_invalid")
    samples: list[dict[str, Any]] = []
    sample_ids: set[str] = set()
    source_digests: set[str] = set()
    for raw in raw_samples:
        if not isinstance(raw, dict) or set(raw) != _SAMPLE_KEYS:
            raise AcceptanceManifestError("sample_shape_invalid")
        sample_id = raw.get("sample_id")
        product_id = raw.get("product_id")
        reasons = raw.get("failure_reasons")
        elapsed_ms = raw.get("elapsed_ms")
        if (
            not isinstance(sample_id, str)
            or not sample_id.strip()
            or sample_id in sample_ids
            or not isinstance(product_id, str)
            or not product_id.strip()
            or raw.get("split") != "acceptance"
            or raw.get("human_reviewed") is not True
            or raw.get("status") not in {"passed", "failed"}
            or not isinstance(reasons, list)
            or not all(isinstance(reason, str) and reason.strip() for reason in reasons)
            or not isinstance(elapsed_ms, int)
            or isinstance(elapsed_ms, bool)
            or elapsed_ms < 0
            or not all(
                _sha256(raw.get(key))
                for key in (
                    "source_sha256",
                    "gold_mask_sha256",
                    "predicted_mask_sha256",
                    "stage_receipts_sha256",
                )
            )
            or raw["source_sha256"] in source_digests
        ):
            raise AcceptanceManifestError("sample_evidence_invalid")
        if (raw["status"] == "failed") != bool(reasons):
            raise AcceptanceManifestError("sample_failure_evidence_invalid")
        sample_ids.add(sample_id)
        source_digests.add(raw["source_sha256"])
        samples.append(raw)
    return samples


def evaluate_acceptance_manifest(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a path-free gate report from already sealed local receipts."""
    if payload.get("schema_version") != "foreground-pipeline-acceptance-v1":
        raise AcceptanceManifestError("schema_version_invalid")
    if not _sha256(payload.get("pipeline_manifest_sha256")) or not _sha256(
        payload.get("risk_config_sha256")
    ):
        raise AcceptanceManifestError("acceptance_digest_invalid")
    if payload.get("sample_specific_rules") not in ([], None):
        raise AcceptanceManifestError("sample_specific_rules_forbidden")
    samples = _validated_samples(payload)
    products = {sample["product_id"] for sample in samples}
    failures = [
        {"sample_id": sample["sample_id"], "reasons": sample["failure_reasons"]}
        for sample in samples
        if sample["status"] == "failed"
    ]
    reuse_gate = {
        "passed": len(products) >= 10,
        "required_unique_products": 10,
        "actual_unique_products": len(products),
    }
    gold_gate = {
        "passed": len(samples) >= 50,
        "required_frozen_samples": 50,
        "actual_frozen_samples": len(samples),
    }
    passed = bool(reuse_gate["passed"] and gold_gate["passed"] and not failures)
    return {
        "schema_version": "foreground-pipeline-acceptance-report-v1",
        "passed": passed,
        "publishable": False,
        "pipeline_manifest_sha256": payload["pipeline_manifest_sha256"],
        "risk_config_sha256": payload["risk_config_sha256"],
        "sample_count": len(samples),
        "failed_sample_count": len(failures),
        "failures": failures,
        "reuse_gate": reuse_gate,
        "gold_gate": gold_gate,
        "timing": {
            "total_elapsed_ms": sum(sample["elapsed_ms"] for sample in samples),
            "maximum_elapsed_ms": max(
                (sample["elapsed_ms"] for sample in samples), default=0
            ),
        },
        "samples": [
            {
                "sample_id": sample["sample_id"],
                "product_id": sample["product_id"],
                "status": sample["status"],
                "failure_reasons": sample["failure_reasons"],
                "elapsed_ms": sample["elapsed_ms"],
                "stage_receipts_sha256": sample["stage_receipts_sha256"],
            }
            for sample in samples
        ],
    }


def _markdown(report: dict[str, Any]) -> str:
    status = "PASS" if report["passed"] else "FAIL"
    lines = [
        "# Foreground pipeline acceptance",
        "",
        f"- Status: {status}",
        f"- Pipeline: `{report['pipeline_manifest_sha256']}`",
        f"- Risk config: `{report['risk_config_sha256']}`",
        f"- Unique products: {report['reuse_gate']['actual_unique_products']} / 10",
        f"- Frozen gold samples: {report['gold_gate']['actual_frozen_samples']} / 50",
        f"- Failed samples: {report['failed_sample_count']}",
        "- Publishable: false",
        "",
        "## Failures",
        "",
    ]
    if report["failures"]:
        lines.extend(
            f"- `{failure['sample_id']}`: {', '.join(failure['reasons'])}"
            for failure in report["failures"]
        )
    else:
        lines.append("- None")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--json-report", type=Path, required=True)
    parser.add_argument("--markdown-report", type=Path, required=True)
    args = parser.parse_args()
    try:
        payload = json.loads(args.manifest.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise AcceptanceManifestError("manifest_invalid")
        report = evaluate_acceptance_manifest(payload)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise SystemExit("foreground_acceptance_manifest_unavailable") from None
    except AcceptanceManifestError as error:
        raise SystemExit(str(error)) from None
    args.json_report.write_text(
        json.dumps(report, ensure_ascii=True, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    args.markdown_report.write_text(_markdown(report), encoding="utf-8")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
