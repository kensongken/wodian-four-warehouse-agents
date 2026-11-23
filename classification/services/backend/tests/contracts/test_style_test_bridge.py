import hashlib
from copy import deepcopy

import pytest
from pydantic import ValidationError

from product_engine.contracts import style_test_bridge as bridge_contract
from product_engine.contracts.style_test_bridge import (
    ClassificationStyleHandoffV1,
    canonical_handoff_bytes,
)

GOLDEN_DIGEST = "79c78959f33be8a95454d5d5f6c489921045d31a067d076be3d14af234781449"
STRING_CHANNELS = (
    "schema_version",
    "customer_test_id",
    "product_id",
    "product_category",
    "product_form",
    "classification_version",
    "idempotency_key",
    "source",
    "photo.content_sha256",
    "photo.mime_type",
    "photo.automatic_category",
    "photo.confidence",
    "photo.confirmed_category",
)
PROHIBITED_STRING_CONTENT = (
    "/tmp/customer-photo.jpg",
    "https://example.invalid/customer-photo.jpg",
    "data:image/jpeg;base64,AAAA",
    "aGVsbG8gd29ybGQ=",
    "aGVsbG8td29ybGQ",
    "Bearer test-secret",
    "sk_live_51Jfakecredential",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.signature",
)
FREE_STRING_LIMITS = (
    ("customer_test_id", 128),
    ("product_id", 128),
    ("product_category", 64),
    ("product_form", 64),
    ("classification_version", 64),
    ("idempotency_key", 255),
)
STRICT_WIRE_CASES = (
    *(("transform_permission", value) for value in (1, 0, "true", "false")),
    *(("style_generation_allowed", value) for value in (1, 0, "true", "false")),
    *(("desensitized_test_only", value) for value in (1, 0, "true", "false")),
    *(("publishable", value) for value in (1, 0, "true", "false")),
    *(("production_eligible", value) for value in (1, 0, "true", "false")),
    *(("confirmation_version", value) for value in (True, False, 3.0, "3")),
    *(("photo.human_corrected", value) for value in (1, 0, "true", "false")),
)
FRESH_GRAMMAR_BYPASSES = (
    ("product_form", "QUFBQUFBQUFBQUFB"),
    ("product_id", "ffd8ffe000104a464946000101"),
    ("idempotency_key", "password:secret123"),
    ("idempotency_key", "https:example.com"),
    ("idempotency_key", "c:customer.jpg"),
)
INVALID_PROVENANCE_VALUES = (
    ("customer_test_id", "customer-001"),
    ("product_id", "product-001"),
    ("classification_version", "v7"),
    ("product_category", "information_insufficient"),
    ("idempotency_key", "style-test:test-product-999:v3"),
    ("idempotency_key", "style-test:test-product-001:v4"),
)
APPROVED_PRODUCT_FORMS = (
    ("bangle", "手镯"),
    ("bead", "单珠"),
    ("bracelet", "手串"),
    ("brooch", "胸针"),
    ("earring", "耳饰"),
    ("necklace", "项链"),
    ("pendant", "吊坠"),
    ("ring", "戒指"),
    ("strand", "串珠"),
    ("round_bead_bracelet", "方圆珠手串"),
    ("BraceletModel2026", "手串型号2026"),
)
REJECTED_PRODUCT_FORM_TEXT = (
    "待确认",
    "待识别",
    "客户姓名张三",
    "客户住址台北市",
    "密码测试密钥",
    "手串",
    "方圆珠手串",
)


@pytest.fixture
def valid_handoff_payload() -> dict[str, object]:
    return {
        "schema_version": "classification-style-handoff-v1",
        "handoff_id": "11111111-1111-4111-8111-111111111111",
        "customer_test_id": "test-customer-001",
        "product_id": "test-product-001",
        "product_category": "turquoise",
        "product_form": "bracelet",
        "classification_version": "classification-v7",
        "confirmation_version": 3,
        "main_photo_id": "21111111-1111-4111-8111-111111111111",
        "transform_permission": True,
        "style_generation_allowed": True,
        "photos": [
            {
                "photo_id": "21111111-1111-4111-8111-111111111111",
                "content_sha256": "a" * 64,
                "mime_type": "image/jpeg",
                "width": 1200,
                "height": 900,
                "automatic_category": "real_photo",
                "confidence": "high",
                "human_corrected": False,
                "confirmed_category": "real_photo",
                "media_receipt_id": "31111111-1111-4111-8111-111111111111",
            },
            {
                "photo_id": "22222222-2222-4222-8222-222222222222",
                "content_sha256": "b" * 64,
                "mime_type": "image/png",
                "width": 1080,
                "height": 1440,
                "automatic_category": "wear_photo",
                "confidence": "medium",
                "human_corrected": True,
                "confirmed_category": "wear_photo",
                "media_receipt_id": "32222222-2222-4222-8222-222222222222",
            },
            {
                "photo_id": "23333333-3333-4333-8333-333333333333",
                "content_sha256": "c" * 64,
                "mime_type": "image/heic",
                "width": 1600,
                "height": 1200,
                "automatic_category": "scene_photo",
                "confidence": "low",
                "human_corrected": False,
                "confirmed_category": "scene_photo",
                "media_receipt_id": "33333333-3333-4333-8333-333333333333",
            },
            {
                "photo_id": "24444444-4444-4444-8444-444444444444",
                "content_sha256": "d" * 64,
                "mime_type": "image/jpeg",
                "width": 2048,
                "height": 1536,
                "automatic_category": "multi_product_photo",
                "confidence": "high",
                "human_corrected": False,
                "confirmed_category": "multi_product_photo",
                "media_receipt_id": "34444444-4444-4444-8444-444444444444",
            },
        ],
        "idempotency_key": "style-test:test-product-001:v3",
        "desensitized_test_only": True,
        "source": "classification_local_page",
        "publishable": False,
        "production_eligible": False,
    }


def replace_payload_channel(payload: dict[str, object], channel: str, value: object) -> None:
    if channel.startswith("photo."):
        photo_field = channel.removeprefix("photo.")
        payload["photos"][0][photo_field] = value  # type: ignore[index]
        return
    payload[channel] = value


def test_accepts_all_four_photo_categories_and_has_stable_digest(
    valid_handoff_payload: dict[str, object],
) -> None:
    handoff = ClassificationStyleHandoffV1.model_validate(valid_handoff_payload)

    assert {photo.confirmed_category for photo in handoff.photos} == {
        "real_photo",
        "wear_photo",
        "scene_photo",
        "multi_product_photo",
    }
    assert hashlib.sha256(canonical_handoff_bytes(handoff)).hexdigest() == GOLDEN_DIGEST


@pytest.mark.parametrize(("channel", "bypass_value"), FRESH_GRAMMAR_BYPASSES)
def test_rejects_fresh_field_grammar_bypasses(
    valid_handoff_payload: dict[str, object], channel: str, bypass_value: str
) -> None:
    payload = deepcopy(valid_handoff_payload)
    replace_payload_channel(payload, channel, bypass_value)

    with pytest.raises(ValidationError):
        ClassificationStyleHandoffV1.model_validate(payload)


@pytest.mark.parametrize(("channel", "invalid_value"), INVALID_PROVENANCE_VALUES)
def test_requires_field_specific_provenance_grammars(
    valid_handoff_payload: dict[str, object], channel: str, invalid_value: str
) -> None:
    payload = deepcopy(valid_handoff_payload)
    replace_payload_channel(payload, channel, invalid_value)

    with pytest.raises(ValidationError):
        ClassificationStyleHandoffV1.model_validate(payload)


@pytest.mark.parametrize(("product_form", "display_label"), APPROVED_PRODUCT_FORMS)
def test_accepts_only_approved_product_form_codes_and_derives_display_labels(
    valid_handoff_payload: dict[str, object], product_form: str, display_label: str
) -> None:
    payload = {**valid_handoff_payload, "product_form": product_form}

    assert ClassificationStyleHandoffV1.model_validate(payload).product_form == product_form
    assert bridge_contract.product_form_display_label_zh(product_form) == display_label


@pytest.mark.parametrize("product_form", REJECTED_PRODUCT_FORM_TEXT)
def test_rejects_unapproved_product_form_transport_text(
    valid_handoff_payload: dict[str, object], product_form: str
) -> None:
    payload = {**valid_handoff_payload, "product_form": product_form}

    with pytest.raises(ValidationError):
        ClassificationStyleHandoffV1.model_validate(payload)


@pytest.mark.parametrize("forbidden_field", ["path", "url", "bytes", "credential"])
def test_rejects_unknown_media_or_secret_fields(
    valid_handoff_payload: dict[str, object], forbidden_field: str
) -> None:
    payload = {**valid_handoff_payload, forbidden_field: "must-not-cross-the-contract"}

    with pytest.raises(ValidationError):
        ClassificationStyleHandoffV1.model_validate(payload)


def test_rejects_unknown_photo_fields(valid_handoff_payload: dict[str, object]) -> None:
    payload = deepcopy(valid_handoff_payload)
    payload["photos"][0]["local_path"] = "/tmp/customer-photo.jpg"  # type: ignore[index]

    with pytest.raises(ValidationError):
        ClassificationStyleHandoffV1.model_validate(payload)


@pytest.mark.parametrize("channel", STRING_CHANNELS)
@pytest.mark.parametrize("prohibited_content", PROHIBITED_STRING_CONTENT)
def test_rejects_prohibited_content_in_every_string_channel(
    valid_handoff_payload: dict[str, object],
    channel: str,
    prohibited_content: str,
) -> None:
    payload = deepcopy(valid_handoff_payload)
    replace_payload_channel(payload, channel, prohibited_content)

    with pytest.raises(ValidationError):
        ClassificationStyleHandoffV1.model_validate(payload)


@pytest.mark.parametrize("channel", STRING_CHANNELS)
@pytest.mark.parametrize("invalid_type", [b"safe-token", 123])
def test_rejects_bytes_and_non_strings_in_every_string_channel(
    valid_handoff_payload: dict[str, object], channel: str, invalid_type: object
) -> None:
    payload = deepcopy(valid_handoff_payload)
    replace_payload_channel(payload, channel, invalid_type)

    with pytest.raises(ValidationError):
        ClassificationStyleHandoffV1.model_validate(payload)


@pytest.mark.parametrize(("channel", "maximum_length"), FREE_STRING_LIMITS)
@pytest.mark.parametrize("invalid_value_kind", ["empty", "overlong"])
def test_rejects_unbounded_free_string_channels(
    valid_handoff_payload: dict[str, object],
    channel: str,
    maximum_length: int,
    invalid_value_kind: str,
) -> None:
    payload = deepcopy(valid_handoff_payload)
    invalid_value = "" if invalid_value_kind == "empty" else "a" * (maximum_length + 1)
    replace_payload_channel(payload, channel, invalid_value)

    with pytest.raises(ValidationError):
        ClassificationStyleHandoffV1.model_validate(payload)


@pytest.mark.parametrize(("channel", "lookalike"), STRICT_WIRE_CASES)
def test_rejects_bool_and_integer_wire_type_lookalikes(
    valid_handoff_payload: dict[str, object], channel: str, lookalike: object
) -> None:
    payload = deepcopy(valid_handoff_payload)
    replace_payload_channel(payload, channel, lookalike)

    with pytest.raises(ValidationError):
        ClassificationStyleHandoffV1.model_validate(payload)


@pytest.mark.parametrize("invalid_hash", ["A" * 64, "a" * 63, "not-a-sha256"])
def test_rejects_non_lowercase_or_non_64_character_sha256(
    valid_handoff_payload: dict[str, object], invalid_hash: str
) -> None:
    payload = deepcopy(valid_handoff_payload)
    payload["photos"][0]["content_sha256"] = invalid_hash  # type: ignore[index]

    with pytest.raises(ValidationError):
        ClassificationStyleHandoffV1.model_validate(payload)


@pytest.mark.parametrize(("dimension", "invalid_value"), [("width", 0), ("height", -1)])
def test_rejects_non_positive_dimensions(
    valid_handoff_payload: dict[str, object], dimension: str, invalid_value: int
) -> None:
    payload = deepcopy(valid_handoff_payload)
    payload["photos"][0][dimension] = invalid_value  # type: ignore[index]

    with pytest.raises(ValidationError):
        ClassificationStyleHandoffV1.model_validate(payload)


def test_rejects_duplicate_photo_ids(valid_handoff_payload: dict[str, object]) -> None:
    payload = deepcopy(valid_handoff_payload)
    payload["photos"][1]["photo_id"] = payload["photos"][0]["photo_id"]  # type: ignore[index]

    with pytest.raises(ValidationError, match="duplicate photo_id"):
        ClassificationStyleHandoffV1.model_validate(payload)


def test_rejects_handoff_without_real_photo(valid_handoff_payload: dict[str, object]) -> None:
    payload = deepcopy(valid_handoff_payload)
    payload["photos"][0]["automatic_category"] = "scene_photo"  # type: ignore[index]
    payload["photos"][0]["confirmed_category"] = "scene_photo"  # type: ignore[index]

    with pytest.raises(ValidationError, match="real_photo"):
        ClassificationStyleHandoffV1.model_validate(payload)


def test_rejects_false_transform_permission(valid_handoff_payload: dict[str, object]) -> None:
    payload = {**valid_handoff_payload, "transform_permission": False}

    with pytest.raises(ValidationError):
        ClassificationStyleHandoffV1.model_validate(payload)


def test_rejects_video_category(valid_handoff_payload: dict[str, object]) -> None:
    payload = deepcopy(valid_handoff_payload)
    payload["photos"][1]["confirmed_category"] = "video"  # type: ignore[index]

    with pytest.raises(ValidationError):
        ClassificationStyleHandoffV1.model_validate(payload)


def test_rejects_main_photo_absent_from_photos(valid_handoff_payload: dict[str, object]) -> None:
    payload = {
        **valid_handoff_payload,
        "main_photo_id": "29999999-9999-4999-8999-999999999999",
    }

    with pytest.raises(ValidationError, match="main_photo_id"):
        ClassificationStyleHandoffV1.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("desensitized_test_only", False),
        ("source", "production_upload"),
        ("publishable", True),
        ("production_eligible", True),
    ],
)
def test_rejects_non_test_boundary_labels(
    valid_handoff_payload: dict[str, object], field: str, invalid_value: object
) -> None:
    payload = {**valid_handoff_payload, field: invalid_value}

    with pytest.raises(ValidationError):
        ClassificationStyleHandoffV1.model_validate(payload)
