from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
SCHEMA_PATH = REPOSITORY_ROOT / "contracts/album-product-categorization-v1.schema.json"
FIXTURE_PATH = (
    REPOSITORY_ROOT / "contracts/fixtures/first-10-sku-categorization-events.json"
)

LEVEL_1 = {
    "南红",
    "沉香",
    "翡翠",
    "和田玉",
    "蜜蜡",
    "珍珠",
    "水晶",
    "彩宝",
    "贵金属",
    "其他",
    "无法判断",
}
LEVEL_2 = {
    "手串",
    "项链",
    "吊坠",
    "挂件",
    "戒指",
    "耳饰",
    "手镯",
    "珠子/散珠",
    "摆件",
    "配件",
    "其他",
    "无法判断",
}
FORBIDDEN_KEYS = {
    "customer_name",
    "customer_id",
    "phone",
    "mobile",
    "wechat",
    "address",
    "shipping_address",
    "chat_text",
    "authenticity",
    "origin_claim",
    "quality_claim",
    "warehouse_verification",
}


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _walk(value: Any) -> list[Any]:
    items = [value]
    if isinstance(value, dict):
        for key, child in value.items():
            items.extend(_walk(key))
            items.extend(_walk(child))
    elif isinstance(value, list):
        for child in value:
            items.extend(_walk(child))
    return items


def _validate_business_rules(payload: dict[str, Any]) -> None:
    events = payload["events"]
    if len(events) != 10:
        raise ValueError("first_10_sku_fixture_requires_exactly_10_events")

    album_product_ids = [event["album_product_id"] for event in events]
    if len(set(album_product_ids)) != len(album_product_ids):
        raise ValueError("album_product_id_must_be_unique")

    asset_ids: list[str] = []
    hashes: list[str] = []
    for event in events:
        images = event["images"]
        originals = {
            image["asset_id"] for image in images if image["role"] == "original"
        }
        if not originals:
            raise ValueError("each_sku_requires_an_original")
        for image in images:
            asset_ids.append(image["asset_id"])
            hashes.append(image["sha256"])
            if image["role"] == "crop" and image["source_asset_id"] not in originals:
                raise ValueError("crop_must_reference_same_event_original")

    if len(set(asset_ids)) != len(asset_ids):
        raise ValueError("asset_id_must_be_unique")
    if len(set(hashes)) != len(hashes):
        raise ValueError("fixture_sha256_must_be_unique")

    keys = {item for item in _walk(payload) if isinstance(item, str)}
    if keys & FORBIDDEN_KEYS:
        raise ValueError("privacy_or_forbidden_assertion_field")


def _validate(payload: dict[str, Any]) -> None:
    schema = _load_json(SCHEMA_PATH)
    Draft202012Validator(
        schema,
        format_checker=FormatChecker(),
    ).validate(payload)
    _validate_business_rules(payload)


def test_schema_is_valid_draft_2020_12() -> None:
    Draft202012Validator.check_schema(_load_json(SCHEMA_PATH))


def test_checked_in_fixture_contains_ten_valid_unique_skus() -> None:
    payload = _load_json(FIXTURE_PATH)

    _validate(payload)

    assert payload["data_classification"] == "synthetic_no_customer_data"
    assert len(payload["events"]) == 10
    assert len({event["event_id"] for event in payload["events"]}) == 10
    assert len({event["album_product_id"] for event in payload["events"]}) == 10


def test_same_contract_accepts_privacy_checked_opc_owned_product_material() -> None:
    payload = copy.deepcopy(_load_json(FIXTURE_PATH))
    payload["data_classification"] = "opc_owned_product_material_no_customer_data"

    _validate(payload)


def test_fixture_uses_locked_categories_and_keeps_business_ids_separate() -> None:
    payload = _load_json(FIXTURE_PATH)

    for event in payload["events"]:
        assert event["human_confirmation"]["status"] == "confirmed"
        assert set(event["allowed_consumers"]) == {
            "categorization",
            "mall_draft_export",
        }
        assert event["mall_product_id"] is None
        assert event["bead_id"] is None
        assert event["categorization_input"]["level_1_candidate"] in LEVEL_1
        assert event["categorization_input"]["level_2_candidate"] in LEVEL_2
        assert event["categorization_input"]["assertion_scope"] == "candidate_only"


def test_fixture_has_no_customer_privacy_or_secret_image_references() -> None:
    payload = _load_json(FIXTURE_PATH)
    strings = [item for item in _walk(payload) if isinstance(item, str)]

    assert not ({item.lower() for item in strings} & FORBIDDEN_KEYS)
    assert not any(re.search(r"[\w.+-]+@[\w.-]+", item) for item in strings)
    assert not any(re.search(r"(?<!\d)1[3-9]\d{9}(?!\d)", item) for item in strings)
    assert not any(
        item.startswith(("http://", "https://", "file:", "content:", "/"))
        or "X-Amz-" in item
        for item in strings
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload["events"][0]["human_confirmation"].update(
            status="pending"
        ),
        lambda payload: payload["events"][0]["images"][0].update(
            storage_ref="https://objects.example/signed?X-Amz-Signature=secret"
        ),
        lambda payload: payload["events"][0]["images"][0].update(
            storage_ref="evidence/synthetic-opc-001/../escape.jpg"
        ),
        lambda payload: payload["events"][0].update(bead_id="bead-auto-created"),
        lambda payload: payload["events"][0].update(
            mall_product_id="mall-product-auto-created"
        ),
        lambda payload: payload["events"][0].update(customer_name="Synthetic Person"),
        lambda payload: payload["events"][0]["categorization_input"].update(
            authenticity="genuine"
        ),
        lambda payload: payload["events"][0].update(
            images=[
                image
                for image in payload["events"][0]["images"]
                if image["role"] != "original"
            ]
        ),
    ],
    ids=[
        "unconfirmed_group",
        "signed_url",
        "storage_traversal",
        "automatic_bead_id",
        "automatic_mall_product_id",
        "customer_privacy",
        "authenticity_assertion",
        "missing_original",
    ],
)
def test_contract_rejects_unsafe_or_unowned_variants(mutate: Any) -> None:
    payload = copy.deepcopy(_load_json(FIXTURE_PATH))
    mutate(payload)

    with pytest.raises((ValidationError, ValueError)):
        _validate(payload)
