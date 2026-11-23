"""用合成资料验证商城候选包的批量通过与拒绝门禁。"""

import argparse
import hashlib
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BACKEND_SOURCE = REPOSITORY_ROOT / "services" / "backend" / "src"
sys.path.insert(0, str(BACKEND_SOURCE))

from product_engine.services.mall_candidate_imports import (
    MallCandidateImportFailure,
    build_mall_candidate_import,
)
from product_engine.services.mall_classification_handoff import (
    MallClassificationHandoffFailure,
)
from product_engine.services.product_passport_drafts import (
    ProductPassportDraftFailure,
    build_product_passport_draft,
)
from product_engine.services.training_label_versions import (
    TrainingLabelFailure,
    TrainingLabelInput,
    TrainingLabelVersionStore,
)


class CandidateBatchValidationFailure(ValueError):
    pass


def _merge_label(case: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    label = {**defaults, **case.get("label", {})}
    template_code = str(case["template_code"])
    if "main_image_template_code" not in label:
        label["main_image_template_code"] = template_code
    label["sha256"] = hashlib.sha256(case["case_id"].encode("utf-8")).hexdigest()
    return label


def _candidate_projection(candidate: Any) -> dict[str, Any]:
    return {
        "template_code": candidate.template_code,
        "source_version": candidate.source_version,
        "category_code": candidate.material_classification.category_code,
        "category_label": candidate.material_classification.category_label,
        "main_image": candidate.main_image.model_dump(mode="json"),
        "price_advisory": candidate.price_advisory.model_dump(mode="json"),
        "selling_point_candidates": candidate.approved_attributes.selling_points,
        "candidate_only": candidate.candidate_only,
        "automatic_publication_allowed": candidate.automatic_publication_allowed,
        "verification_boundaries": candidate.verification_boundaries.model_dump(
            mode="json"
        ),
    }


def _evaluate_case(case: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    case_id = str(case["case_id"])
    template_code = str(case["template_code"])
    try:
        values = TrainingLabelInput.model_validate(_merge_label(case, defaults))
        with tempfile.TemporaryDirectory(prefix="candidate-batch-") as temp_dir:
            root = Path(temp_dir)
            store = TrainingLabelVersionStore(
                root / "training-label-versions.jsonl",
                root / "training-labels.jsonl",
            )
            initial = store.register_initial(template_code, values)
            saved = store.save(template_code, initial.version, values)
            expected_source_version = int(
                case.get("expected_source_version", saved.version)
            )
            if saved.version != expected_source_version:
                raise CandidateBatchValidationFailure("source_version_mismatch")
            draft = build_product_passport_draft(saved)
            candidate = build_mall_candidate_import(draft)
        return {
            "case_id": case_id,
            "outcome": "passed",
            "candidate": _candidate_projection(candidate),
        }
    except MallCandidateImportFailure as error:
        error_code = (
            error.remaining_blockers[0] if error.remaining_blockers else str(error)
        )
    except (
        CandidateBatchValidationFailure,
        MallClassificationHandoffFailure,
        ProductPassportDraftFailure,
        TrainingLabelFailure,
    ) as error:
        error_code = str(error)
    return {
        "case_id": case_id,
        "outcome": "rejected",
        "error_code": error_code,
    }


def validate_batch(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("evidence_scope") != "synthetic_technical_readiness_only":
        raise CandidateBatchValidationFailure("synthetic_scope_required")
    cases = payload.get("cases")
    if not isinstance(cases, list) or len(cases) != 10:
        raise CandidateBatchValidationFailure("exactly_ten_cases_required")
    defaults = payload.get("label_defaults")
    if not isinstance(defaults, dict):
        raise CandidateBatchValidationFailure("label_defaults_required")

    results = [_evaluate_case(case, defaults) for case in cases]
    for case, result in zip(cases, results, strict=True):
        if result["outcome"] != case["expected_outcome"]:
            raise CandidateBatchValidationFailure(
                f"unexpected_outcome:{case['case_id']}:{result['outcome']}"
            )
        expected_error = case.get("expected_error_code")
        if expected_error is not None and result.get("error_code") != expected_error:
            raise CandidateBatchValidationFailure(
                f"unexpected_error:{case['case_id']}:{result.get('error_code')}"
            )

    passed = sum(item["outcome"] == "passed" for item in results)
    rejected = len(results) - passed
    return {
        "schema_version": "0.1",
        "evidence_scope": "synthetic_technical_readiness_only",
        "operating_results": 0,
        "summary": {"total": len(results), "passed": passed, "rejected": rejected},
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        payload = json.loads(args.input.read_text(encoding="utf-8"))
        output = validate_batch(payload)
    except (CandidateBatchValidationFailure, OSError, ValueError) as error:
        print(f"INVALID error={error}")
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    summary = output["summary"]
    print(
        "VALID "
        f"synthetic_cases={summary['total']} "
        f"passed={summary['passed']} rejected={summary['rejected']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
