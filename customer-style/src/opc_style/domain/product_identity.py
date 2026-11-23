"""Provider-neutral contracts for visible product identity observations."""

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from typing import Literal, Protocol
from uuid import UUID

from opc_style.domain.errors import DomainRuleError

Certainty = Literal["observed", "unclear"]

PRODUCT_IDENTITY_FIELDS = (
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
)
PRODUCT_CATEGORY_VALUES = (
    "bracelet",
    "necklace",
    "ring",
    "earrings",
    "pendant",
    "anklet",
    "brooch",
    "other",
)
PRIMARY_COLOR_VALUES = (
    "black",
    "white",
    "gray",
    "red",
    "orange",
    "yellow",
    "green",
    "blue",
    "purple",
    "pink",
    "brown",
    "gold_tone",
    "silver_tone",
    "multicolor",
)
VISIBLE_SURFACE_APPEARANCE_VALUES = (
    "transparent",
    "translucent",
    "opaque",
    "glossy",
    "matte",
    "metallic_luster",
    "faceted",
    "smooth",
    "textured",
)
COMPONENT_SHAPE_VALUES = (
    "round_bead",
    "square_bead",
    "oval",
    "teardrop",
    "rectangular",
    "irregular",
    "chain_link",
    "cabochon",
    "faceted",
    "other",
)
COMPONENT_ARRANGEMENT_VALUES = (
    "single_strand",
    "multi_strand",
    "continuous_chain",
    "symmetric",
    "asymmetric",
    "clustered",
    "solitaire",
    "other",
)
CORE_ACCESSORY_KIND_VALUES = (
    "pendant",
    "clasp",
    "extension_chain",
    "ear_hook",
    "ear_post",
    "ring_setting",
    "spacer",
    "charm",
    "connector",
    "other",
)
OVERALL_FORM_VALUES = (
    "closed_loop",
    "open_chain",
    "circular_band",
    "drop",
    "stud",
    "hoop",
    "solitaire",
    "clustered",
    "other",
)
VISIBILITY_LIMIT_VALUES = (
    "partially_occluded",
    "cropped",
    "low_resolution",
    "glare",
    "shadow",
    "overlapping_components",
    "back_side_hidden",
    "clasp_hidden",
)

MAX_PRIMARY_COLORS = 4
MAX_VISIBLE_SURFACE_APPEARANCES = 4
MAX_COMPONENT_SHAPES = 4
MAX_CORE_ACCESSORIES = 6
MAX_VISIBILITY_LIMITS = 6
MAX_PRODUCT_IDENTITY_TEXT_LENGTH = 160
MAX_VISIBLE_COMPONENT_COUNT = 999
MAX_ACCESSORY_VISIBLE_COUNT = 99

# Text fields are for visible spatial facts only. These bounded patterns prevent
# them from becoming a second, free-form channel for authoritative product claims.
FORBIDDEN_IDENTITY_CLAIM_PATTERNS = (
    r"(?:材质|材料|黄金|白金|铂金|纯银|翡翠|和田玉|钻石|宝石|珍珠|\d{1,2}\s*k(?:金)?|(?<![A-Za-z])(?:material|made\s+of|gold|silver|platinum|diamond|jade)(?![A-Za-z]))",
    r"(?:正品|真品|假货|仿品|保真|鉴定|证书|(?<![A-Za-z])(?:authentic|genuine|counterfeit)(?![A-Za-z]))",
    r"(?:品牌|牌子|商标|(?<![A-Za-z])(?:logo|trademark|brand)(?![A-Za-z]))",
    r"(?:产自|产地|原产|来自|制造于|(?<![A-Za-z])(?:made\s+in|country\s+of\s+origin|origin)(?![A-Za-z]))",
    r"(?:价格|售价|价值|估价|市价|人民币|美元|\d+(?:\.\d+)?\s*元|(?<![A-Za-z])(?:rmb|usd)(?![A-Za-z])|[$¥￥])",
    r"(?:所有权|归属|属于|归.{0,20}所有|(?<![A-Za-z])(?:owned\s+by|belongs\s+to|ownership)(?![A-Za-z]))",
)

_OBSERVATION_KEYS = frozenset({"value", "certainty"})
_INVALID_DRAFT = "product_identity_draft_invalid"


@dataclass(frozen=True)
class IdentityObservation:
    """One visible fact or an explicit statement that the image is unclear."""

    value: object
    certainty: Certainty


@dataclass(frozen=True)
class CoreAccessory:
    """A visible accessory kind and the number visible in the source image."""

    kind: str
    visible_count: int


@dataclass(frozen=True)
class ProductIdentityDraft:
    """Non-authoritative AI proposal containing only visible product facts."""

    category: IdentityObservation
    primary_colors: IdentityObservation
    visible_surface_appearance: IdentityObservation
    component_shapes: IdentityObservation
    visible_component_count: IdentityObservation
    component_arrangement: IdentityObservation
    core_accessories: IdentityObservation
    overall_form: IdentityObservation
    relative_proportions: IdentityObservation
    distinguishing_marks: IdentityObservation
    visibility_limits: IdentityObservation


@dataclass(frozen=True)
class ProductIdentityImage:
    """Existing product asset bytes presented to an analyzer without new ownership."""

    asset_id: UUID
    product_id: str
    content: bytes
    content_sha256: str
    mime_type: str


class ProductIdentityAnalyzer(Protocol):
    """Provider-neutral boundary that can only return an identity-card proposal."""

    analysis_mode: str
    analyzer_version: str

    def analyze(self, image: ProductIdentityImage) -> ProductIdentityDraft:
        """Describe visible facts in one existing product image."""
        ...


def empty_manual_identity_draft() -> ProductIdentityDraft:
    """Return a valid blank card whose visible facts must be filled by a person."""
    return normalize_identity_draft(
        {
            field: {"value": None, "certainty": "unclear"}
            for field in PRODUCT_IDENTITY_FIELDS
        }
    )


def normalize_identity_draft(payload: Mapping[str, object]) -> ProductIdentityDraft:
    """Validate untrusted provider or user JSON into the frozen v1 contract."""
    if not isinstance(payload, Mapping) or set(payload) != set(PRODUCT_IDENTITY_FIELDS):
        raise DomainRuleError(_INVALID_DRAFT)

    return ProductIdentityDraft(
        category=_normalize_observation(
            payload["category"],
            lambda value: _normalize_single(value, PRODUCT_CATEGORY_VALUES),
        ),
        primary_colors=_normalize_observation(
            payload["primary_colors"],
            lambda value: _normalize_vocabulary_list(
                value,
                allowed=PRIMARY_COLOR_VALUES,
                minimum=1,
                maximum=MAX_PRIMARY_COLORS,
            ),
        ),
        visible_surface_appearance=_normalize_observation(
            payload["visible_surface_appearance"],
            lambda value: _normalize_vocabulary_list(
                value,
                allowed=VISIBLE_SURFACE_APPEARANCE_VALUES,
                minimum=1,
                maximum=MAX_VISIBLE_SURFACE_APPEARANCES,
            ),
        ),
        component_shapes=_normalize_observation(
            payload["component_shapes"],
            lambda value: _normalize_vocabulary_list(
                value,
                allowed=COMPONENT_SHAPE_VALUES,
                minimum=1,
                maximum=MAX_COMPONENT_SHAPES,
            ),
        ),
        visible_component_count=_normalize_observation(
            payload["visible_component_count"],
            lambda value: _normalize_integer(
                value, minimum=1, maximum=MAX_VISIBLE_COMPONENT_COUNT
            ),
        ),
        component_arrangement=_normalize_observation(
            payload["component_arrangement"],
            lambda value: _normalize_single(value, COMPONENT_ARRANGEMENT_VALUES),
        ),
        core_accessories=_normalize_observation(
            payload["core_accessories"], _normalize_accessories
        ),
        overall_form=_normalize_observation(
            payload["overall_form"],
            lambda value: _normalize_single(value, OVERALL_FORM_VALUES),
        ),
        relative_proportions=_normalize_observation(
            payload["relative_proportions"], _normalize_text
        ),
        distinguishing_marks=_normalize_observation(
            payload["distinguishing_marks"], _normalize_text
        ),
        visibility_limits=_normalize_observation(
            payload["visibility_limits"],
            lambda value: _normalize_vocabulary_list(
                value,
                allowed=VISIBILITY_LIMIT_VALUES,
                minimum=0,
                maximum=MAX_VISIBILITY_LIMITS,
            ),
        ),
    )


def identity_card_payload(draft: ProductIdentityDraft) -> dict[str, object]:
    """Return the stable JSON-compatible form used for persistence and hashing."""
    if not isinstance(draft, ProductIdentityDraft):
        raise DomainRuleError(_INVALID_DRAFT)
    payload: dict[str, object] = {
        field: _observation_payload(getattr(draft, field))
        for field in PRODUCT_IDENTITY_FIELDS
    }
    if normalize_identity_draft(payload) != draft:
        raise DomainRuleError(_INVALID_DRAFT)
    return payload


def identity_card_sha256(draft: ProductIdentityDraft) -> str:
    """Hash canonical UTF-8 JSON so equivalent cards have one stable identity."""
    encoded = json.dumps(
        identity_card_payload(draft),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _normalize_observation(
    raw: object, normalizer: Callable[[object], object]
) -> IdentityObservation:
    if not isinstance(raw, Mapping) or set(raw) != _OBSERVATION_KEYS:
        raise DomainRuleError(_INVALID_DRAFT)
    certainty = raw["certainty"]
    value = raw["value"]
    if certainty == "unclear":
        if value is not None:
            raise DomainRuleError(_INVALID_DRAFT)
        return IdentityObservation(value=None, certainty="unclear")
    if certainty != "observed":
        raise DomainRuleError(_INVALID_DRAFT)
    return IdentityObservation(value=normalizer(value), certainty="observed")


def _normalize_single(value: object, allowed: tuple[str, ...]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise DomainRuleError(_INVALID_DRAFT)
    return value


def _normalize_vocabulary_list(
    value: object,
    *,
    allowed: tuple[str, ...],
    minimum: int,
    maximum: int,
) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not minimum <= len(value) <= maximum:
        raise DomainRuleError(_INVALID_DRAFT)
    if any(not isinstance(item, str) or item not in allowed for item in value):
        raise DomainRuleError(_INVALID_DRAFT)
    if len(set(value)) != len(value):
        raise DomainRuleError(_INVALID_DRAFT)
    return tuple(value)


def _normalize_integer(value: object, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DomainRuleError(_INVALID_DRAFT)
    if not minimum <= value <= maximum:
        raise DomainRuleError(_INVALID_DRAFT)
    return value


def _normalize_accessories(value: object) -> tuple[CoreAccessory, ...]:
    if not isinstance(value, (list, tuple)) or len(value) > MAX_CORE_ACCESSORIES:
        raise DomainRuleError(_INVALID_DRAFT)
    accessories: list[CoreAccessory] = []
    kinds: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {"kind", "visible_count"}:
            raise DomainRuleError(_INVALID_DRAFT)
        kind = _normalize_single(item["kind"], CORE_ACCESSORY_KIND_VALUES)
        visible_count = _normalize_integer(
            item["visible_count"], minimum=1, maximum=MAX_ACCESSORY_VISIBLE_COUNT
        )
        if kind in kinds:
            raise DomainRuleError(_INVALID_DRAFT)
        kinds.add(kind)
        accessories.append(CoreAccessory(kind=kind, visible_count=visible_count))
    return tuple(accessories)


def _normalize_text(value: object) -> str:
    if not isinstance(value, str):
        raise DomainRuleError(_INVALID_DRAFT)
    normalized = value.strip()
    if not 1 <= len(normalized) <= MAX_PRODUCT_IDENTITY_TEXT_LENGTH:
        raise DomainRuleError(_INVALID_DRAFT)
    if any(
        re.search(pattern, normalized, flags=re.IGNORECASE)
        for pattern in FORBIDDEN_IDENTITY_CLAIM_PATTERNS
    ):
        raise DomainRuleError(_INVALID_DRAFT)
    return normalized


def _observation_payload(observation: IdentityObservation) -> dict[str, object]:
    if not isinstance(observation, IdentityObservation):
        raise DomainRuleError(_INVALID_DRAFT)
    value = observation.value
    if isinstance(value, tuple):
        serialized: object = [
            {"kind": item.kind, "visible_count": item.visible_count}
            if isinstance(item, CoreAccessory)
            else item
            for item in value
        ]
    else:
        serialized = value
    return {"certainty": observation.certainty, "value": serialized}
