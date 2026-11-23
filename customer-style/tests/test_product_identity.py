"""Restricted, provider-neutral product identity card contracts."""

from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from hashlib import sha256
from uuid import UUID

import pytest

from opc_style.domain.errors import DomainRuleError
from opc_style.domain.product_identity import (
    CoreAccessory,
    IdentityObservation,
    ProductIdentityDraft,
    ProductIdentityImage,
    identity_card_payload,
    identity_card_sha256,
    normalize_identity_draft,
)


def observation(value: object, certainty: str = "observed") -> dict[str, object]:
    return {"value": value, "certainty": certainty}


def valid_payload(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "category": observation("bracelet"),
        "primary_colors": observation(["green", "gold_tone"]),
        "visible_surface_appearance": observation(["glossy", "opaque"]),
        "component_shapes": observation(["round_bead"]),
        "visible_component_count": observation(18),
        "component_arrangement": observation("single_strand"),
        "core_accessories": observation(
            [{"kind": "clasp", "visible_count": 1}]
        ),
        "overall_form": observation("closed_loop"),
        "relative_proportions": observation("主体珠明显大于扣头"),
        "distinguishing_marks": observation("中心珠两侧各有一枚小隔片"),
        "visibility_limits": observation([]),
    }
    for field, value in overrides.items():
        values[field] = observation(value)
    return values


def test_identity_draft_rejects_inferred_material_and_duplicate_values() -> None:
    with pytest.raises(DomainRuleError, match="product_identity_draft_invalid"):
        normalize_identity_draft(valid_payload(primary_colors=["green", "green"]))
    with pytest.raises(DomainRuleError, match="product_identity_draft_invalid"):
        normalize_identity_draft(
            valid_payload(visible_surface_appearance=["18k_gold"])
        )


def test_unclear_count_cannot_claim_an_observed_number() -> None:
    payload = valid_payload()
    payload["visible_component_count"] = observation(18, "unclear")

    with pytest.raises(DomainRuleError, match="product_identity_draft_invalid"):
        normalize_identity_draft(payload)


def test_normalizes_every_supported_field_into_frozen_contracts() -> None:
    draft = normalize_identity_draft(valid_payload())

    assert draft == ProductIdentityDraft(
        category=IdentityObservation(value="bracelet", certainty="observed"),
        primary_colors=IdentityObservation(
            value=("green", "gold_tone"), certainty="observed"
        ),
        visible_surface_appearance=IdentityObservation(
            value=("glossy", "opaque"), certainty="observed"
        ),
        component_shapes=IdentityObservation(
            value=("round_bead",), certainty="observed"
        ),
        visible_component_count=IdentityObservation(value=18, certainty="observed"),
        component_arrangement=IdentityObservation(
            value="single_strand", certainty="observed"
        ),
        core_accessories=IdentityObservation(
            value=(CoreAccessory(kind="clasp", visible_count=1),),
            certainty="observed",
        ),
        overall_form=IdentityObservation(value="closed_loop", certainty="observed"),
        relative_proportions=IdentityObservation(
            value="主体珠明显大于扣头", certainty="observed"
        ),
        distinguishing_marks=IdentityObservation(
            value="中心珠两侧各有一枚小隔片", certainty="observed"
        ),
        visibility_limits=IdentityObservation(value=(), certainty="observed"),
    )
    with pytest.raises(FrozenInstanceError):
        draft.category = IdentityObservation(value=None, certainty="unclear")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("category", "watch"),
        ("primary_colors", ["turquoise"]),
        ("visible_surface_appearance", ["jade"]),
        ("component_shapes", ["heart"]),
        ("component_arrangement", "braided"),
        ("overall_form", "square"),
        ("visibility_limits", ["unknown"]),
    ],
)
def test_rejects_values_outside_visible_fact_vocabularies(
    field: str, value: object
) -> None:
    with pytest.raises(DomainRuleError, match="product_identity_draft_invalid"):
        normalize_identity_draft(valid_payload(**{field: value}))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("primary_colors", []),
        ("primary_colors", ["black", "white", "gray", "red", "orange"]),
        (
            "visible_surface_appearance",
            ["transparent", "translucent", "opaque", "glossy", "matte"],
        ),
        (
            "component_shapes",
            ["round_bead", "square_bead", "oval", "teardrop", "rectangular"],
        ),
        (
            "visibility_limits",
            [
                "partially_occluded",
                "cropped",
                "low_resolution",
                "glare",
                "shadow",
                "overlapping_components",
                "back_side_hidden",
            ],
        ),
    ],
)
def test_rejects_list_values_outside_v1_size_limits(
    field: str, value: object
) -> None:
    with pytest.raises(DomainRuleError, match="product_identity_draft_invalid"):
        normalize_identity_draft(valid_payload(**{field: value}))


@pytest.mark.parametrize("count", [False, 0, 1000, 1.5, "18"])
def test_observed_component_count_is_an_integer_from_1_to_999(count: object) -> None:
    with pytest.raises(DomainRuleError, match="product_identity_draft_invalid"):
        normalize_identity_draft(valid_payload(visible_component_count=count))


@pytest.mark.parametrize("visible_count", [False, 0, 100, 1.5, "1"])
def test_accessory_visible_count_is_an_integer_from_1_to_99(
    visible_count: object,
) -> None:
    payload = valid_payload(
        core_accessories=[{"kind": "clasp", "visible_count": visible_count}]
    )

    with pytest.raises(DomainRuleError, match="product_identity_draft_invalid"):
        normalize_identity_draft(payload)


def test_accessories_are_bounded_structures_with_unique_controlled_kinds() -> None:
    invalid_accessories = (
        [{"kind": "gemstone", "visible_count": 1}],
        [
            {"kind": "clasp", "visible_count": 1},
            {"kind": "clasp", "visible_count": 2},
        ],
        [{"kind": "clasp", "visible_count": 1, "material": "gold"}],
        [
            {"kind": kind, "visible_count": 1}
            for kind in (
                "pendant",
                "clasp",
                "extension_chain",
                "ear_hook",
                "ear_post",
                "ring_setting",
                "spacer",
            )
        ],
    )

    for accessories in invalid_accessories:
        with pytest.raises(DomainRuleError, match="product_identity_draft_invalid"):
            normalize_identity_draft(valid_payload(core_accessories=accessories))


def test_rejects_unknown_or_missing_contract_keys() -> None:
    unknown_top_level = valid_payload()
    unknown_top_level["material"] = observation("18k_gold")
    missing_top_level = valid_payload()
    del missing_top_level["category"]
    unknown_observation = valid_payload()
    unknown_observation["category"] = {
        "value": "bracelet",
        "certainty": "observed",
        "confidence": 0.99,
    }

    for payload in (unknown_top_level, missing_top_level, unknown_observation):
        with pytest.raises(DomainRuleError, match="product_identity_draft_invalid"):
            normalize_identity_draft(payload)


@pytest.mark.parametrize(
    "field",
    [
        "category",
        "primary_colors",
        "visible_surface_appearance",
        "component_shapes",
        "visible_component_count",
        "component_arrangement",
        "core_accessories",
        "overall_form",
        "relative_proportions",
        "distinguishing_marks",
        "visibility_limits",
    ],
)
def test_unclear_fields_accept_only_none(field: str) -> None:
    payload = valid_payload()
    payload[field] = observation(None, "unclear")
    assert normalize_identity_draft(payload)

    for invalid_value in ((), [], "", 0):
        invalid = valid_payload()
        invalid[field] = observation(invalid_value, "unclear")
        with pytest.raises(DomainRuleError, match="product_identity_draft_invalid"):
            normalize_identity_draft(invalid)


@pytest.mark.parametrize(
    "field",
    [
        "category",
        "primary_colors",
        "visible_surface_appearance",
        "component_shapes",
        "component_arrangement",
        "overall_form",
        "relative_proportions",
        "distinguishing_marks",
    ],
)
def test_observed_fields_reject_empty_values(field: str) -> None:
    payload = valid_payload()
    payload[field] = observation([] if field.endswith("s") else "")

    with pytest.raises(DomainRuleError, match="product_identity_draft_invalid"):
        normalize_identity_draft(payload)


def test_observed_accessories_and_visibility_limits_may_be_explicitly_empty() -> None:
    draft = normalize_identity_draft(
        valid_payload(core_accessories=[], visibility_limits=[])
    )

    assert draft.core_accessories.value == ()
    assert draft.visibility_limits.value == ()


@pytest.mark.parametrize("field", ["relative_proportions", "distinguishing_marks"])
def test_text_observations_are_trimmed_and_limited_to_160_characters(
    field: str,
) -> None:
    draft = normalize_identity_draft(valid_payload(**{field: "  可见特征  "}))
    assert getattr(draft, field).value == "可见特征"

    for value in ("   ", "x" * 161):
        with pytest.raises(DomainRuleError, match="product_identity_draft_invalid"):
            normalize_identity_draft(valid_payload(**{field: value}))


@pytest.mark.parametrize(
    "claim",
    [
        "材质是18K金",
        "这是正品",
        "品牌是某某",
        "产自法国",
        "价值5000元",
        "归某人所有",
        "material is gold",
        "country of origin is France",
        "owned by Alice",
        "origin是France",
        "gold色中心珠",
        "owned by张三",
        "material是gold",
    ],
)
@pytest.mark.parametrize(
    "field", ["relative_proportions", "distinguishing_marks"]
)
def test_text_observations_reject_non_visual_product_claims(
    field: str, claim: str
) -> None:
    with pytest.raises(DomainRuleError, match="product_identity_draft_invalid"):
        normalize_identity_draft(valid_payload(**{field: claim}))


@pytest.mark.parametrize(
    "description",
    [
        "components follow a golden ratio",
        "center bead is materially larger than the clasp",
        "original oval is larger than adjacent beads",
    ],
)
@pytest.mark.parametrize(
    "field", ["relative_proportions", "distinguishing_marks"]
)
def test_text_observations_allow_legitimate_english_spatial_descriptions(
    field: str, description: str
) -> None:
    draft = normalize_identity_draft(valid_payload(**{field: description}))

    assert getattr(draft, field).value == description


@pytest.mark.parametrize("serialize", [identity_card_payload, identity_card_sha256])
def test_serialization_rejects_directly_constructed_invalid_drafts(
    serialize: object,
) -> None:
    draft = normalize_identity_draft(valid_payload())
    invalid_drafts = (
        replace(
            draft,
            category=IdentityObservation(value="watch", certainty="observed"),
        ),
        replace(
            draft,
            primary_colors=IdentityObservation(
                value=("green", "green"), certainty="observed"
            ),
        ),
        replace(
            draft,
            distinguishing_marks=IdentityObservation(
                value="这是正品", certainty="observed"
            ),
        ),
        replace(
            draft,
            visible_component_count=IdentityObservation(
                value=18, certainty="unclear"
            ),
        ),
    )

    for invalid in invalid_drafts:
        with pytest.raises(DomainRuleError, match="product_identity_draft_invalid"):
            serialize(invalid)  # type: ignore[operator]


def test_payload_round_trips_and_hashes_canonical_utf8_json() -> None:
    draft = normalize_identity_draft(valid_payload())
    payload = identity_card_payload(draft)

    assert normalize_identity_draft(deepcopy(payload)) == draft
    assert payload["core_accessories"] == {
        "certainty": "observed",
        "value": [{"kind": "clasp", "visible_count": 1}],
    }
    assert identity_card_sha256(draft) == sha256(
        (
            '{"category":{"certainty":"observed","value":"bracelet"},'
            '"component_arrangement":{"certainty":"observed","value":"single_strand"},'
            '"component_shapes":{"certainty":"observed","value":["round_bead"]},'
            '"core_accessories":{"certainty":"observed","value":[{"kind":"clasp",'
            '"visible_count":1}]},"distinguishing_marks":{"certainty":"observed",'
            '"value":"中心珠两侧各有一枚小隔片"},"overall_form":{"certainty":'
            '"observed","value":"closed_loop"},"primary_colors":{"certainty":'
            '"observed","value":["green","gold_tone"]},"relative_proportions":'
            '{"certainty":"observed","value":"主体珠明显大于扣头"},'
            '"visibility_limits":{"certainty":"observed","value":[]},'
            '"visible_component_count":{"certainty":"observed","value":18},'
            '"visible_surface_appearance":{"certainty":"observed","value":'
            '["glossy","opaque"]}}'
        ).encode()
    ).hexdigest()


def test_product_identity_image_is_a_frozen_provider_neutral_value() -> None:
    image = ProductIdentityImage(
        asset_id=UUID("00000000-0000-0000-0000-000000000001"),
        product_id="product-1",
        content=b"image-bytes",
        content_sha256="f" * 64,
        mime_type="image/jpeg",
    )

    assert image.product_id == "product-1"
    with pytest.raises(FrozenInstanceError):
        image.product_id = "changed"
