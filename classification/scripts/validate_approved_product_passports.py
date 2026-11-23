#!/usr/bin/env python3
"""Validate the mall-safe, human-confirmed ProductPassport package."""

import json
import sys
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import UUID

FIXED_FIELDS = {
    "product_passport_id",
    "sku_id",
    "approved_attributes",
    "evidence_refs",
    "taxonomy_version",
    "availability_status",
    "reviewer",
    "version",
}
REQUIRED_ATTRIBUTES = {
    "product_type",
    "title",
    "category",
    "form",
    "bead_id",
    "source_bead_id",
    "source_ticket_id",
    "material",
    "quality",
    "authenticity",
    "price_advisory",
    "selling_points",
    "description",
    "primary_image_url",
    "service_summary",
    "lineage_evidence_available",
}
SENSITIVE_FIELDS = ("material", "quality", "authenticity", "price_advisory")
ALLOWED_DECISION_SOURCES = {
    "merchant_declared",
    "human_confirmed",
    "warehouse_verified",
    "document_verified",
    "not_assessed",
}
FORBIDDEN_CLAIMS = (
    "保真",
    "真品",
    "正品",
    "纯天然",
    "已鉴定",
    "权威鉴定",
    "保证保值",
    "保证升值",
    "固定售价",
    "一口价",
)


class ValidationFailure(Exception):
    def __init__(self, code: str, record: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.record = record
        self.detail = detail


def fail(code: str, record: str, detail: object) -> None:
    raise ValidationFailure(code, record, str(detail))


def require(mapping: object, fields: set[str], label: str) -> None:
    if not isinstance(mapping, dict):
        fail("invalid_object", label, "expected object")
    for field in fields:
        if field not in mapping or mapping[field] in (None, "", []):
            fail("required_field_missing", label, field)


def require_keys(mapping: object, fields: set[str], label: str) -> None:
    if not isinstance(mapping, dict):
        fail("invalid_object", label, "expected object")
    for field in fields:
        if field not in mapping:
            fail("required_field_missing", label, field)


def validate_uuid(value: object, label: str, field: str) -> None:
    try:
        UUID(str(value))
    except (TypeError, ValueError):
        fail("invalid_uuid", label, field)


def validate_decision(field: str, decision: object, label: str) -> None:
    if not isinstance(decision, dict):
        fail("invalid_sensitive_decision", label, field)
    source = decision.get("source")
    if source == "ai_inferred":
        fail(f"automatic_{field}_decision_not_publishable", label, field)
    if source not in ALLOWED_DECISION_SOURCES:
        fail("invalid_decision_source", label, f"{field}:{source}")
    if "value" not in decision or "review_status" not in decision:
        fail("required_field_missing", label, field)
    if decision["value"] is not None and decision["review_status"] != "human_confirmed":
        fail("sensitive_decision_not_confirmed", label, field)


def validate_price(price: dict[str, object], label: str) -> None:
    require(
        price,
        {"currency", "minimum", "maximum", "basis", "source", "review_status"},
        label,
    )
    if price["currency"] != "CNY":
        fail("unsupported_currency", label, price["currency"])
    try:
        minimum = Decimal(str(price["minimum"]))
        maximum = Decimal(str(price["maximum"]))
    except InvalidOperation:
        fail("invalid_price_range", label, "not decimal")
    if minimum < 0 or maximum <= minimum:
        fail("invalid_or_fixed_price_range", label, f"{minimum}-{maximum}")


def validate_evidence(evidence_refs: object, label: str) -> None:
    if not isinstance(evidence_refs, list) or not evidence_refs:
        fail("required_field_missing", label, "evidence_refs")
    for evidence in evidence_refs:
        require(
            evidence,
            {"evidence_id", "evidence_type", "status", "visibility", "ref"},
            label,
        )
        validate_uuid(evidence["evidence_id"], label, "evidence_id")
        if evidence["status"] != "approved":
            fail("unapproved_evidence_not_publishable", label, evidence["evidence_id"])
        if evidence["visibility"] != "public":
            fail("private_evidence_not_publishable", label, evidence["evidence_id"])
        if not str(evidence["ref"]).startswith("https://"):
            fail("invalid_public_evidence_ref", label, evidence["evidence_id"])


def validate_record(record: object) -> None:
    label = record.get("sku_id", "unknown") if isinstance(record, dict) else "unknown"
    require(record, FIXED_FIELDS, label)
    if set(record) != FIXED_FIELDS:
        fail(
            "unexpected_product_passport_field",
            label,
            sorted(set(record) - FIXED_FIELDS),
        )
    validate_uuid(record["product_passport_id"], label, "product_passport_id")
    if record["taxonomy_version"] != "product-taxonomy-v0.1":
        fail("unsupported_taxonomy_version", label, record["taxonomy_version"])
    if record["availability_status"] != "available":
        fail(
            "unavailable_product_not_publishable", label, record["availability_status"]
        )
    if not isinstance(record["version"], int) or record["version"] < 1:
        fail("invalid_version", label, record["version"])

    reviewer = record["reviewer"]
    require(reviewer, {"reviewer_type", "reviewer_id", "reviewed_at"}, label)
    if reviewer["reviewer_type"] != "human":
        fail("reviewer_not_human", label, reviewer["reviewer_type"])

    attributes = record["approved_attributes"]
    require_keys(attributes, REQUIRED_ATTRIBUTES, label)
    for field in SENSITIVE_FIELDS:
        validate_decision(field, attributes[field], label)
    validate_price(attributes["price_advisory"], label)
    validate_evidence(record["evidence_refs"], label)

    content = json.dumps(attributes, ensure_ascii=False)
    for phrase in FORBIDDEN_CLAIMS:
        if phrase in content:
            fail("forbidden_unsupported_claim", label, phrase)


def validate_dataset(records: object) -> None:
    if not isinstance(records, list) or not records:
        fail("invalid_dataset", "dataset", "expected non-empty JSON array")
    for record in records:
        validate_record(record)
    for field in ("product_passport_id", "sku_id"):
        duplicates = [
            value
            for value, count in Counter(record[field] for record in records).items()
            if count > 1
        ]
        if duplicates:
            fail(f"duplicate_{field}", "dataset", duplicates[0])


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: validate_approved_product_passports.py <json>", file=sys.stderr)
        return 2
    try:
        records = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
        validate_dataset(records)
    except (OSError, json.JSONDecodeError) as error:
        print(f"INVALID code=invalid_input record=dataset detail={error}")
        return 1
    except ValidationFailure as error:
        print(f"INVALID code={error.code} record={error.record} detail={error.detail}")
        return 1
    print(f"VALID approved_product_passports={len(records)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
