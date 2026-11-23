"""把合成商品候选投影为商城和销售支持的只读资料视图。"""

import argparse
import copy
import json
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


class CandidateConsumerViewFailure(ValueError):
    pass


EXPECTED_BOUNDARIES = {
    "authenticity": "not_verified",
    "origin": "not_verified",
    "quality": "not_verified",
    "warehouse": "pending_warehouse_verification",
}

FORBIDDEN_FIELDS = {
    "material_conclusion": "material_conclusion_forbidden",
    "material_verified": "material_conclusion_forbidden",
    "category_conclusion": "material_conclusion_forbidden",
    "category_conclusion_allowed": "material_conclusion_forbidden",
    "fixed_price": "fixed_or_final_price_forbidden",
    "final_price": "fixed_or_final_price_forbidden",
    "price_conclusion": "fixed_or_final_price_forbidden",
    "authenticity_conclusion": "verification_conclusion_forbidden",
    "authenticity_verified": "verification_conclusion_forbidden",
    "quality_conclusion": "verification_conclusion_forbidden",
    "quality_verified": "verification_conclusion_forbidden",
    "warehouse_conclusion": "verification_conclusion_forbidden",
    "warehouse_verified": "verification_conclusion_forbidden",
}

TEXT_RULES = (
    (
        "customer_contact_forbidden",
        re.compile(
            r"联系(?:微信|wx|电话|手机)|"
            r"微信(?:号|id)?[:：]|"
            r"(?:电话|手机号|手机|邮箱|email)[:：]"
        ),
    ),
    (
        "fixed_or_final_price_forbidden",
        re.compile(r"固定价(?:格)?|最终价(?:格)?|一口价|售价\d|价格\d"),
    ),
    (
        "material_conclusion_forbidden",
        re.compile(r"材质(?:确认|结论|为|是)"),
    ),
    (
        "verification_conclusion_forbidden",
        re.compile(
            r"(?:经)?鉴定为|天然a货|保真|真品|正品|"
            r"品质(?:顶级|确认|结论|为)|"
            r"(?:已)?通过仓库验收|仓验(?:通过|合格|完成)"
        ),
    ),
)


def _fail(code: str) -> None:
    raise CandidateConsumerViewFailure(code)


def _normalized_text(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value)).lower()


def _reject_forbidden_fields(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            error_code = FORBIDDEN_FIELDS.get(str(key))
            if error_code is not None:
                _fail(error_code)
            _reject_forbidden_fields(item)
    elif isinstance(value, list):
        for item in value:
            _reject_forbidden_fields(item)


def _reject_unsafe_customer_text(candidate: dict[str, Any]) -> None:
    price = candidate.get("price_advisory")
    texts: list[str] = []
    if isinstance(price, dict) and isinstance(price.get("basis"), str):
        texts.append(price["basis"])
    selling_points = candidate.get("selling_point_candidates")
    if isinstance(selling_points, list):
        texts.extend(item for item in selling_points if isinstance(item, str))
    for text in texts:
        normalized = _normalized_text(text)
        for error_code, pattern in TEXT_RULES:
            if pattern.search(normalized):
                _fail(error_code)


def _required_string(value: Any, error_code: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(error_code)
    return value


def _validated_price(candidate: dict[str, Any]) -> dict[str, Any]:
    price = candidate.get("price_advisory")
    if not isinstance(price, dict):
        _fail("price_advisory_required")
    if price.get("semantics") != "advisory_range_not_fixed_price":
        _fail("fixed_or_final_price_forbidden")
    try:
        minimum = Decimal(str(price["minimum"]))
        maximum = Decimal(str(price["maximum"]))
    except (InvalidOperation, KeyError, TypeError, ValueError):
        _fail("invalid_price_range")
    if minimum < 0 or maximum <= minimum:
        _fail("invalid_price_range")
    return {
        "basis": _required_string(price.get("basis"), "price_basis_required"),
        "currency": _required_string(price.get("currency"), "price_currency_required"),
        "final_price_allowed": False,
        "human_review_required": True,
        "maximum": str(price["maximum"]),
        "minimum": str(price["minimum"]),
        "semantics": "advisory_range_not_fixed_price",
    }


def _validated_main_image(
    candidate: dict[str, Any], template_code: str
) -> dict[str, Any]:
    image = candidate.get("main_image")
    if not isinstance(image, dict):
        _fail("main_image_required")
    if (
        image.get("template_code") != template_code
        or image.get("role") != "main"
        or image.get("source") != "album_classified_image"
        or image.get("review_state") != "human_confirmed"
    ):
        _fail("main_image_invalid")
    return copy.deepcopy(image)


def _validated_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    _reject_forbidden_fields(candidate)
    _reject_unsafe_customer_text(candidate)
    if candidate.get("candidate_only") is not True:
        _fail("candidate_flag_required")
    if candidate.get("automatic_publication_allowed") is not False:
        _fail("automatic_publication_forbidden")
    source_version = candidate.get("source_version")
    if (
        isinstance(source_version, bool)
        or not isinstance(source_version, int)
        or source_version < 1
    ):
        _fail("source_version_invalid")
    if candidate.get("verification_boundaries") != EXPECTED_BOUNDARIES:
        _fail("verification_conclusion_forbidden")

    template_code = _required_string(
        candidate.get("template_code"), "template_code_required"
    )
    category_code = _required_string(
        candidate.get("category_code"), "category_candidate_required"
    )
    category_label = _required_string(
        candidate.get("category_label"), "category_candidate_required"
    )
    selling_points = candidate.get("selling_point_candidates")
    if (
        not isinstance(selling_points, list)
        or not selling_points
        or any(not isinstance(item, str) or not item.strip() for item in selling_points)
    ):
        _fail("selling_point_candidates_required")

    return {
        "automatic_publication_allowed": False,
        "candidate_only": True,
        "category_candidate": {
            "code": category_code,
            "conclusion_allowed": False,
            "human_review_required": True,
            "label": category_label,
            "semantics": "catalog_candidate_not_material_conclusion",
        },
        "main_image": _validated_main_image(candidate, template_code),
        "price_advisory": _validated_price(candidate),
        "selling_point_candidates": copy.deepcopy(selling_points),
        "source_version": source_version,
        "template_code": template_code,
        "verification_boundaries": copy.deepcopy(EXPECTED_BOUNDARIES),
    }


def build_consumer_views(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("evidence_scope") != "synthetic_technical_readiness_only":
        _fail("synthetic_scope_required")
    operating_results = payload.get("operating_results")
    if isinstance(operating_results, bool) or operating_results != 0:
        _fail("non_operating_evidence_required")
    results = payload.get("results")
    if not isinstance(results, list):
        _fail("candidate_results_required")

    candidates: list[dict[str, Any]] = []
    for item in results:
        if not isinstance(item, dict) or item.get("outcome") != "passed":
            continue
        candidate = item.get("candidate")
        if not isinstance(candidate, dict):
            _fail("passed_candidate_required")
        candidates.append(_validated_candidate(candidate))
    if not candidates:
        _fail("passed_candidate_required")
    template_codes = [item["template_code"] for item in candidates]
    if len(template_codes) != len(set(template_codes)):
        _fail("duplicate_template_code")

    mall_views = [copy.deepcopy(item) for item in candidates]
    sales_views = []
    for item in candidates:
        sales_view = copy.deepcopy(item)
        sales_view.update(
            {
                "conclusions_allowed": {
                    "authenticity": False,
                    "final_price": False,
                    "material": False,
                    "quality": False,
                    "warehouse": False,
                },
                "human_review_required": True,
                "usage": "draft_support_only",
            }
        )
        sales_views.append(sales_view)

    return {
        "evidence_scope": "synthetic_technical_readiness_only",
        "mall_views": mall_views,
        "operating_results": 0,
        "sales_views": sales_views,
        "schema_version": "0.1",
        "source_contract": "candidate-batch-0.1",
        "summary": {
            "items": len(candidates),
            "mall_views": len(mall_views),
            "sales_views": len(sales_views),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        payload = json.loads(args.input.read_text(encoding="utf-8"))
        output = build_consumer_views(payload)
    except (CandidateConsumerViewFailure, OSError, ValueError) as error:
        print(f"INVALID error={error}")
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        "VALID "
        f"items={output['summary']['items']} "
        f"mall_views={output['summary']['mall_views']} "
        f"sales_views={output['summary']['sales_views']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
