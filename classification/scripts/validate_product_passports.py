#!/usr/bin/env python3
"""Validate OPC ProductPassport v0.1 JSON and mall-import CSV fixtures."""

import csv
import json
import re
import sys
import uuid
from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlparse


class DuplicateJsonKeyError(ValueError):
    """Raised when a signed contract object uses last-key-wins ambiguity."""


def strict_json_loads(raw_json):
    def reject_duplicate_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise DuplicateJsonKeyError(key)
            result[key] = value
        return result

    return json.loads(raw_json, object_pairs_hook=reject_duplicate_keys)


ALLOWED_TYPES = {"first_sale_bead", "legacy_material", "redesign_output"}
ALLOWED_STATUS = {"draft", "service_only", "publishable", "published", "retired"}
ALLOWED_EVIDENCE = {
    "image_observed",
    "merchant_declared",
    "warehouse_verified",
    "document_verified",
}
ALLOWED_VISIBILITY = {"public", "private"}
ALLOWED_AUTHORIZATION = {
    "merchant_owned",
    "customer_authorized",
    "warehouse_authorized",
    "ai_generated",
}
SELLABLE_TYPES = {"first_sale_bead", "redesign_output"}
ROOT = Path(__file__).resolve().parents[1]
PRODUCT_PASSPORT_SCHEMA = strict_json_loads(
    (ROOT / "contracts" / "v0.1" / "product-passport.schema.json").read_text(encoding="utf-8")
)
SALES_PROJECTION_TEXT_FIELDS = (
    "sku",
    "title",
    "category",
    "form",
    "description",
)
SALES_PROJECTION_LIST_FIELDS = ("selling_points", "service_summary")
SALES_PROJECTION_ANCHOR_FIELDS = (
    "product_id",
    "package_version",
    "evidence_level",
    "evidence_ids",
    "conservative_price_range",
    "validity",
    "forbidden_claims",
)
FORBIDDEN_VALUE_PROMISES = ("保证保值", "保证升值", "回购价保证", "投资收益")
FORBIDDEN_UNSUPPORTED_CLAIMS = (
    "保真",
    "真品",
    "正品",
    "纯天然",
    "已鉴定",
    "权威鉴定",
    "产自",
    "原产于",
    "保证保值",
    "保证升值",
    "回购价保证",
    "投资收益",
    "一口价",
    "固定售价",
)
BEAD_ID_PATTERN = re.compile(r"^B-\d{4}-\d{6}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
CANONICAL_UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
CANONICAL_DATETIME_PATTERN = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")


class ValidationFailure(Exception):
    def __init__(self, code, record, detail):
        super().__init__(detail)
        self.code = code
        self.record = record
        self.detail = detail


def fail(code, record, detail):
    raise ValidationFailure(code, record, detail)


def require(record, fields, label):
    for field in fields:
        if field not in record or record[field] in (None, "", []):
            fail("required_field_missing", label, field)


def resolve_schema(schema):
    if "$ref" not in schema:
        return schema
    reference = schema["$ref"]
    if not reference.startswith("#/$defs/"):
        raise ValueError(f"unsupported schema reference: {reference}")
    return PRODUCT_PASSPORT_SCHEMA["$defs"][reference.rsplit("/", 1)[1]]


def matches_schema(value, schema):
    try:
        validate_schema(value, schema, "schema-condition", "condition")
    except ValidationFailure:
        return False
    return True


def validate_schema(value, schema, label, path):
    schema = resolve_schema(schema)
    if "const" in schema and value != schema["const"]:
        fail("schema_validation_failed", label, path)
    if "enum" in schema and value not in schema["enum"]:
        fail("schema_validation_failed", label, path)
    if "minimum" in schema and (
        not isinstance(value, (int, float)) or isinstance(value, bool) or value < schema["minimum"]
    ):
        fail("schema_validation_failed", label, path)

    accepted_types = schema.get("type")
    if accepted_types:
        if not isinstance(accepted_types, list):
            accepted_types = [accepted_types]
        type_checks = {
            "object": lambda item: isinstance(item, dict),
            "array": lambda item: isinstance(item, list),
            "string": lambda item: isinstance(item, str),
            "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
            "null": lambda item: item is None,
        }
        if not any(type_checks[item_type](value) for item_type in accepted_types):
            fail("schema_validation_failed", label, path)

    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for required_field in schema.get("required", []):
            if required_field not in value:
                fail("schema_validation_failed", label, f"{path}.{required_field}")
        if schema.get("additionalProperties") is False:
            for field in value:
                if field not in properties:
                    fail("schema_validation_failed", label, f"{path}.{field}")
        for field, field_schema in properties.items():
            if field in value:
                validate_schema(value[field], field_schema, label, f"{path}.{field}")

    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            fail("schema_validation_failed", label, path)
        if "items" in schema:
            for index, item in enumerate(value):
                validate_schema(item, schema["items"], label, f"{path}[{index}]")

    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            fail("schema_validation_failed", label, path)
        if "pattern" in schema and not re.fullmatch(schema["pattern"], value):
            fail("schema_validation_failed", label, path)
        if schema.get("format") == "uuid":
            try:
                parsed_uuid = uuid.UUID(value)
            except ValueError:
                fail("schema_validation_failed", label, path)
            if not CANONICAL_UUID_PATTERN.fullmatch(value) or str(parsed_uuid) != value:
                fail("schema_validation_failed", label, path)
        if schema.get("format") == "date-time":
            try:
                datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
            except ValueError:
                fail("schema_validation_failed", label, path)
            if not CANONICAL_DATETIME_PATTERN.fullmatch(value):
                fail("schema_validation_failed", label, path)
        if schema.get("format") == "uri":
            parsed = urlparse(value)
            if not parsed.scheme:
                fail("schema_validation_failed", label, path)

    for component in schema.get("allOf", []):
        if "if" in component:
            if matches_schema(value, component["if"]) and "then" in component:
                validate_schema(value, component["then"], label, path)
        else:
            validate_schema(value, component, label, path)


def validate_price(record, label):
    price = record.get("conservative_price_range")
    if price is None:
        return
    require(price, ("currency", "minimum", "maximum", "basis", "evidence_level"), label)
    if price["currency"] != "CNY":
        fail("unsupported_currency", label, price["currency"])
    try:
        minimum = Decimal(str(price["minimum"]))
        maximum = Decimal(str(price["maximum"]))
    except InvalidOperation:
        fail("invalid_price_range", label, "price is not decimal")
    if minimum < 0 or maximum < minimum:
        fail("invalid_price_range", label, f"{minimum}-{maximum}")
    if minimum == maximum:
        fail("fixed_price_promise_not_supported", label, f"{minimum}-{maximum}")
    if price["evidence_level"] not in ALLOWED_EVIDENCE:
        fail("invalid_evidence_level", label, price["evidence_level"])


def validate_media(record, label):
    media_items = record.get("media")
    if not isinstance(media_items, list) or not media_items:
        fail("required_field_missing", label, "media")

    for media in media_items:
        require(
            media,
            (
                "media_id",
                "media_kind",
                "object_key",
                "sha256",
                "product_id",
                "evidence_level",
                "visibility",
                "authorization_status",
                "captured_at",
            ),
            label,
        )
        if not SHA256_PATTERN.fullmatch(str(media["sha256"])):
            fail("invalid_media_sha256", label, media.get("media_id"))
        public_url = media.get("public_url")
        if public_url is not None:
            parsed_url = urlparse(public_url) if isinstance(public_url, str) else None
            if (
                not isinstance(public_url, str)
                or parsed_url.scheme not in {"http", "https"}
                or not parsed_url.netloc
            ):
                fail("invalid_media_public_url", label, media.get("media_id"))
        if media["visibility"] not in ALLOWED_VISIBILITY:
            fail("invalid_media_visibility", label, media["visibility"])
        if media["authorization_status"] not in ALLOWED_AUTHORIZATION:
            if media["media_kind"] == "customer_return_photo":
                fail("unauthorized_customer_photo", label, media.get("media_id"))
            fail("invalid_media_authorization", label, media["authorization_status"])
        if media["media_kind"] == "customer_return_photo" and (
            media["authorization_status"] != "customer_authorized"
            or media["visibility"] != "private"
            or not media.get("ticket_id")
        ):
            fail("unauthorized_customer_photo", label, media.get("media_id"))
        if media["media_kind"] == "warehouse_acceptance_photo" and (
            media["authorization_status"] != "warehouse_authorized"
            or media["visibility"] != "private"
            or not media.get("ticket_id")
        ):
            fail("invalid_warehouse_evidence", label, media.get("media_id"))
        if (
            media["media_kind"]
            in {
                "customer_return_photo",
                "warehouse_acceptance_photo",
            }
            and media["visibility"] == "public"
        ):
            fail("private_evidence_exposed", label, media.get("media_id"))


def reject_forbidden_value_promises(content, label):
    for phrase in FORBIDDEN_VALUE_PROMISES:
        if phrase in content:
            fail("forbidden_value_promise", label, phrase)


def reject_forbidden_unsupported_claims(content, label):
    for phrase in FORBIDDEN_UNSUPPORTED_CLAIMS:
        if phrase in content:
            fail("forbidden_unsupported_claim", label, phrase)


def validate_claims(record, label):
    raw_content = " ".join(
        [str(record.get("description", ""))]
        + [str(item) for item in record.get("selling_points", [])]
        + [str(item) for item in record.get("claims", [])]
    )
    reject_forbidden_unsupported_claims(raw_content, label)

    if "已认证资产" in raw_content:
        warehouse_evidence = any(
            item.get("media_kind") == "warehouse_acceptance_photo"
            and item.get("evidence_level") == "warehouse_verified"
            for item in record.get("media", [])
        )
        if not warehouse_evidence:
            fail("uncertified_legacy_asset_claim", label, "已认证资产")


def validate_approved_sales_text(value, label, field):
    if not isinstance(value, dict):
        fail("invalid_sales_projection", label, field)
    if set(value) != {"text", "approval_status"}:
        fail("invalid_sales_projection", label, field)
    if not isinstance(value["text"], str) or not value["text"].strip():
        fail("invalid_sales_projection", label, field)
    if value["approval_status"] != "approved":
        fail("sales_projection_not_approved", label, field)


def validate_sales_projection_price(price, evidence_ids, label):
    validate_approved_sales_text(price["basis"], label, "conservative_price_range.basis")
    try:
        minimum = Decimal(price["minimum"])
        maximum = Decimal(price["maximum"])
    except InvalidOperation:
        fail("invalid_sales_projection", label, "conservative_price_range")
    if minimum < 0 or maximum < minimum:
        fail("invalid_sales_projection", label, "conservative_price_range")
    price_evidence_ids = price["evidence_ids"]
    if len(set(price_evidence_ids)) != len(price_evidence_ids):
        fail("invalid_sales_projection", label, "conservative_price_range.evidence_ids")
    if not set(price_evidence_ids).issubset(evidence_ids):
        fail("invalid_sales_projection", label, "conservative_price_range.evidence_ids")


def validate_sales_projection_validity(validity, label):
    valid_from = datetime.fromisoformat(validity["valid_from"].replace("Z", "+00:00"))
    valid_until = datetime.fromisoformat(validity["valid_until"].replace("Z", "+00:00"))
    if valid_until <= valid_from:
        fail("invalid_sales_projection", label, "validity")


def validate_sales_journey_projection(record, label):
    projection = record.get("sales_journey_projection")
    sellable = record.get("type") in SELLABLE_TYPES and record.get("status") == "published"
    if projection is None:
        if sellable:
            fail("sales_projection_missing", label, "sales_journey_projection")
        return
    if not isinstance(projection, dict):
        fail("invalid_sales_projection", label, "sales_journey_projection")

    expected_fields = set(
        SALES_PROJECTION_TEXT_FIELDS
        + SALES_PROJECTION_LIST_FIELDS
        + SALES_PROJECTION_ANCHOR_FIELDS
        + ("public_media",)
    )
    if set(projection) != expected_fields:
        fail("invalid_sales_projection", label, "sales_journey_projection")
    if projection["product_id"] != record["product_id"]:
        fail("invalid_sales_projection", label, "product_id")
    if projection["package_version"] != record["package_version"]:
        fail("invalid_sales_projection", label, "package_version")
    for field in SALES_PROJECTION_TEXT_FIELDS:
        validate_approved_sales_text(projection[field], label, field)
    for field in SALES_PROJECTION_LIST_FIELDS:
        values = projection[field]
        if not isinstance(values, list) or not values:
            fail("invalid_sales_projection", label, field)
        for value in values:
            validate_approved_sales_text(value, label, field)

    projected_customer_text = " ".join(
        [projection[field]["text"] for field in SALES_PROJECTION_TEXT_FIELDS]
        + [item["text"] for item in projection["selling_points"]]
        + [item["text"] for item in projection["service_summary"]]
        + [projection["conservative_price_range"]["basis"]["text"]]
    )
    reject_forbidden_value_promises(projected_customer_text, label)

    public_media = projection["public_media"]
    if not isinstance(public_media, list):
        fail("invalid_sales_projection", label, "public_media")
    raw_public_media = {
        item["media_id"]: item
        for item in record["media"]
        if item.get("visibility") == "public"
        and item.get("media_kind") in {"product_photo", "redesign_output_photo"}
    }
    evidence_ids = projection["evidence_ids"]
    evidence_id_set = set(evidence_ids)
    if len(evidence_id_set) != len(evidence_ids) or not evidence_id_set:
        fail("invalid_sales_projection", label, "evidence_ids")
    if not evidence_id_set.issubset(raw_public_media):
        fail("invalid_sales_projection", label, "evidence_ids")

    validate_sales_projection_price(projection["conservative_price_range"], evidence_id_set, label)
    validate_sales_projection_validity(projection["validity"], label)
    for forbidden_claim in projection["forbidden_claims"]:
        validate_approved_sales_text(forbidden_claim, label, "forbidden_claims")

    projection_media_ids = set()
    for item in public_media:
        if not isinstance(item, dict) or set(item) != {"media_id", "public_url"}:
            fail("invalid_sales_projection", label, "public_media")
        media_id = item["media_id"]
        if media_id not in raw_public_media or media_id in projection_media_ids:
            fail("invalid_sales_projection", label, "public_media")
        if media_id not in evidence_id_set:
            fail("invalid_sales_projection", label, "public_media")
        projection_media_ids.add(media_id)
        validate_approved_sales_text(item["public_url"], label, "public_media")
        projected_url = urlparse(item["public_url"]["text"])
        if projected_url.scheme not in {"http", "https"} or not projected_url.netloc:
            fail("invalid_sales_projection_url", label, media_id)
    if projection_media_ids != evidence_id_set:
        fail("invalid_sales_projection", label, "public_media")


def product_passport_label(record):
    if not isinstance(record, dict):
        return "unknown"
    return record.get("sku") or record.get("product_id") or "unknown"


def validate_product_passport(record):
    label = product_passport_label(record)
    validate_schema(record, PRODUCT_PASSPORT_SCHEMA, label, "product_passport")
    require(
        record,
        (
            "product_id",
            "sku",
            "type",
            "status",
            "title",
            "category",
            "form",
            "service_rules",
            "evidence_level",
            "selling_points",
            "description",
            "media",
            "package_version",
            "updated_at",
        ),
        label,
    )
    product_type = record["type"]
    if product_type not in ALLOWED_TYPES:
        fail("unsupported_product_type", label, product_type)
    if record["status"] not in ALLOWED_STATUS:
        fail("unsupported_product_status", label, record["status"])
    if record["evidence_level"] not in ALLOWED_EVIDENCE:
        fail("invalid_evidence_level", label, record["evidence_level"])
    bead_id = record.get("bead_id")
    if bead_id and not BEAD_ID_PATTERN.fullmatch(str(bead_id)):
        fail("invalid_bead_id", label, bead_id)

    validate_price(record, label)
    validate_media(record, label)
    validate_claims(record, label)
    validate_sales_journey_projection(record, label)

    if product_type == "first_sale_bead":
        if record.get("source_bead_id") or record.get("source_ticket_id"):
            fail("first_sale_has_source_lineage", label, "source fields are forbidden")
        if record["status"] in {"publishable", "published"} and not bead_id:
            fail("first_sale_bead_id_missing", label, "bead_id")
        public_product_photo = any(
            item.get("media_kind") == "product_photo"
            and item.get("product_id") == record["product_id"]
            and item.get("bead_id") == bead_id
            and item.get("visibility") == "public"
            for item in record["media"]
        )
        if record["status"] == "published" and not public_product_photo:
            fail("first_sale_public_photo_missing", label, "product_photo")

    if product_type == "legacy_material":
        if record["status"] != "service_only":
            fail("legacy_material_not_sellable", label, record["status"])
        if not record.get("source_ticket_id"):
            fail("legacy_ticket_missing", label, "source_ticket_id")
        customer_media = [
            item for item in record["media"] if item.get("media_kind") == "customer_return_photo"
        ]
        warehouse_media = [
            item
            for item in record["media"]
            if item.get("media_kind") == "warehouse_acceptance_photo"
        ]
        if not customer_media:
            fail("legacy_customer_media_missing", label, "customer_return_photo")
        if record["evidence_level"] == "warehouse_verified" and not warehouse_media:
            fail("legacy_warehouse_evidence_missing", label, "warehouse_acceptance_photo")
        for item in customer_media + warehouse_media:
            if item.get("product_id") != record["product_id"]:
                fail("legacy_media_product_mismatch", label, item.get("media_id"))
            if item.get("ticket_id") != record["source_ticket_id"]:
                fail("legacy_media_ticket_mismatch", label, item.get("media_id"))

    if product_type == "redesign_output":
        source_ticket_id = record.get("source_ticket_id")
        if not bead_id or not source_ticket_id:
            fail("redesign_lineage_incomplete", label, "bead_id/source_ticket_id")
        if isinstance(record.get("source_bead_id"), list):
            fail("redesign_many_to_one_not_supported", label, "source_bead_id")
        if record.get("source_bead_id") and bead_id == record["source_bead_id"]:
            fail("redesign_requires_new_bead_id", label, bead_id)
        output_media = [
            item for item in record["media"] if item.get("media_kind") == "redesign_output_photo"
        ]
        warehouse_media = [
            item
            for item in record["media"]
            if item.get("media_kind") == "warehouse_acceptance_photo"
        ]
        if not output_media or not warehouse_media:
            fail("redesign_lineage_incomplete", label, "output/warehouse media")
        for item in output_media:
            if (
                item.get("product_id") != record["product_id"]
                or item.get("bead_id") != bead_id
                or item.get("ticket_id") != source_ticket_id
            ):
                fail("redesign_output_media_mismatch", label, item.get("media_id"))
        for item in warehouse_media:
            if item.get("ticket_id") != source_ticket_id:
                fail("redesign_warehouse_ticket_mismatch", label, item.get("media_id"))
            if record.get("source_bead_id") and item.get("bead_id") != record["source_bead_id"]:
                fail("redesign_source_bead_mismatch", label, item.get("media_id"))


def validate_dataset(records):
    if not isinstance(records, list) or not records:
        fail("invalid_dataset", "dataset", "expected non-empty JSON array")
    for record in records:
        validate_product_passport(record)

    duplicate_product_ids = [
        value for value, count in Counter(r["product_id"] for r in records).items() if count > 1
    ]
    if duplicate_product_ids:
        fail("duplicate_product_id", "dataset", duplicate_product_ids[0])
    duplicate_skus = [
        value for value, count in Counter(r["sku"] for r in records).items() if count > 1
    ]
    if duplicate_skus:
        fail("duplicate_sku", "dataset", duplicate_skus[0])

    redesign_ticket_counts = Counter(
        r["source_ticket_id"]
        for r in records
        if r["type"] == "redesign_output" and r.get("source_ticket_id")
    )
    for ticket_id, count in redesign_ticket_counts.items():
        if count > 1:
            fail("redesign_one_to_many_not_supported", "dataset", ticket_id)


def validate_mall_import_records(records):
    if not isinstance(records, list) or not records:
        fail("invalid_dataset", "mall_import", "expected non-empty JSON array")
    required = (
        "sku",
        "product_id",
        "type",
        "status",
        "mall_visible",
        "title",
        "category",
        "form",
        "bead_id",
        "evidence_level",
        "price_currency",
        "price_min",
        "price_max",
        "price_basis",
        "selling_points",
        "description",
        "primary_image_url",
        "service_summary",
        "lineage_evidence_available",
        "package_version",
    )
    ticket_counts = Counter()
    for record in records:
        label = record.get("sku") or "unknown"
        require(record, required, label)
        if "lineage_image_refs" in record:
            fail("private_evidence_exposed", label, "lineage_image_refs")
        if not isinstance(record["lineage_evidence_available"], bool):
            fail("invalid_lineage_evidence_flag", label, "lineage_evidence_available")
        if record["type"] not in ALLOWED_TYPES:
            fail("unsupported_product_type", label, record["type"])
        if record["mall_visible"] is not True:
            fail("mall_import_product_hidden", label, record["mall_visible"])
        if record["type"] == "legacy_material" or record["status"] != "published":
            fail("non_sellable_product_exposed", label, record["type"])
        if record["price_currency"] != "CNY":
            fail("unsupported_currency", label, record["price_currency"])
        try:
            minimum = Decimal(str(record["price_min"]))
            maximum = Decimal(str(record["price_max"]))
            if minimum > maximum:
                fail("invalid_price_range", label, "minimum > maximum")
            if minimum == maximum:
                fail("fixed_price_promise_not_supported", label, "minimum = maximum")
        except InvalidOperation:
            fail("invalid_price_range", label, "not decimal")
        validate_claims(record, label)
        if record["type"] == "first_sale_bead" and (
            record.get("source_bead_id") or record.get("source_ticket_id")
        ):
            fail(
                "first_sale_has_source_lineage",
                label,
                "source fields are forbidden",
            )
        if record["type"] == "redesign_output":
            ticket_id = record.get("source_ticket_id")
            if not ticket_id or record["lineage_evidence_available"] is not True:
                fail("redesign_lineage_incomplete", label, "ticket/image lineage")
            if record.get("source_bead_id") and record["bead_id"] == record["source_bead_id"]:
                fail("redesign_requires_new_bead_id", label, record["bead_id"])
            ticket_counts[ticket_id] += 1
        elif record["lineage_evidence_available"] is not False:
            fail("unexpected_lineage_evidence", label, record["type"])
    for ticket_id, count in ticket_counts.items():
        if count > 1:
            fail("redesign_one_to_many_not_supported", "mall_import", ticket_id)


def validate_mall_csv(path):
    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        fail("invalid_dataset", path.name, "empty CSV")
    required = {
        "sku",
        "product_id",
        "type",
        "status",
        "title",
        "bead_id",
        "price_currency",
        "price_min",
        "price_max",
        "primary_image_url",
        "mall_visible",
        "lineage_evidence_available",
    }
    missing_columns = required - set(rows[0])
    if missing_columns:
        fail("csv_column_missing", path.name, min(missing_columns))
    for row in rows:
        label = row.get("sku") or "unknown"
        if row["type"] not in ALLOWED_TYPES:
            fail("unsupported_product_type", label, row["type"])
        if row["mall_visible"].lower() == "true":
            if row["type"] == "legacy_material" or row["status"] != "published":
                fail("non_sellable_product_exposed", label, row["type"])
            require(row, ("bead_id", "primary_image_url"), label)
        if row["type"] == "redesign_output":
            if not row.get("source_ticket_id"):
                fail("redesign_lineage_incomplete", label, "source_ticket_id")
            if row["lineage_evidence_available"].lower() != "true":
                fail("redesign_lineage_incomplete", label, "lineage evidence")
            if row.get("source_bead_id") and row["bead_id"] == row["source_bead_id"]:
                fail("redesign_requires_new_bead_id", label, row["bead_id"])
        elif (
            row["type"] == "first_sale_bead"
            and row["lineage_evidence_available"].lower() != "false"
        ):
            fail("unexpected_lineage_evidence", label, row["type"])
        try:
            minimum = Decimal(row["price_min"])
            maximum = Decimal(row["price_max"])
            if minimum > maximum:
                fail("invalid_price_range", label, "minimum > maximum")
            if minimum == maximum:
                fail("fixed_price_promise_not_supported", label, "minimum = maximum")
        except InvalidOperation:
            fail("invalid_price_range", label, "not decimal")
    return len(rows)


def main(argv):
    if len(argv) != 2:
        print("USAGE validate_product_passports.py <json-or-csv>")
        return 2
    path = Path(argv[1])
    try:
        if path.suffix.lower() == ".csv":
            count = validate_mall_csv(path)
        else:
            records = strict_json_loads(path.read_text(encoding="utf-8"))
            if records and isinstance(records, list) and "mall_visible" in records[0]:
                validate_mall_import_records(records)
            else:
                validate_dataset(records)
            count = len(records)
    except FileNotFoundError:
        print(f"INVALID code=file_not_found record={path} detail=missing")
        return 1
    except json.JSONDecodeError as error:
        print(f"INVALID code=invalid_json record={path.name} detail={error.msg}")
        return 1
    except DuplicateJsonKeyError as error:
        print(f"INVALID code=duplicate_json_key record={path.name} detail={error}")
        return 1
    except ValidationFailure as error:
        print(f"INVALID code={error.code} record={error.record} detail={error.detail}")
        return 1
    print(f"VALID records={count} file={path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
