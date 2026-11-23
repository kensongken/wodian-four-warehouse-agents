from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

CategoryCode = Literal[
    "agarwood",
    "turquoise",
    "nanhong",
    "hetian_jade",
    "colored_gemstone",
    "jadeite",
    "information_insufficient",
]
DecisionStatus = Literal[
    "candidate",
    "human_confirmed",
    "information_insufficient",
]
DecisionSource = Literal[
    "album_import",
    "apple_vision",
    "training_knowledge",
    "trainer_confirmed",
    "none",
]
ConfidenceLevel = Literal["low", "medium", "high"]


class ClassificationEvidenceRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_type: Literal["training_version", "album_event", "local_image"]
    reference_id: str = Field(min_length=1)


class ClassificationDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    category_code: CategoryCode
    category_label: str = Field(min_length=1)
    decision_status: DecisionStatus
    source: DecisionSource
    confidence_level: ConfidenceLevel | None
    explanations: list[str] = Field(min_length=1)
    evidence_refs: list[ClassificationEvidenceRef]
    review_required: bool
    mall_import_allowed: bool


_CATEGORY_ALIASES: dict[str, tuple[CategoryCode, str]] = {
    "沉香": ("agarwood", "沉香"),
    "绿松": ("turquoise", "绿松石"),
    "绿松石": ("turquoise", "绿松石"),
    "南红": ("nanhong", "南红"),
    "和田玉": ("hetian_jade", "和田玉"),
    "彩宝": ("colored_gemstone", "彩宝"),
    "彩色宝石": ("colored_gemstone", "彩宝"),
    "翡翠": ("jadeite", "翡翠"),
}


def _information_insufficient(reason: str) -> ClassificationDecision:
    return ClassificationDecision(
        category_code="information_insufficient",
        category_label="信息不足",
        decision_status="information_insufficient",
        source="none",
        confidence_level=None,
        explanations=[reason],
        evidence_refs=[],
        review_required=True,
        mall_import_allowed=False,
    )


def _confidence_level(confidence: float) -> ConfidenceLevel:
    if confidence >= 0.85:
        return "high"
    if confidence >= 0.60:
        return "medium"
    return "low"


def classify_album_category(
    category: str,
    classification_confidence: float,
) -> ClassificationDecision:
    normalized = category.strip()
    matched = _CATEGORY_ALIASES.get(normalized)
    if matched is None:
        return _information_insufficient(
            "相册整理传入的类目不在首批六品类内，需要训练员补充本件证据。"
        )
    confidence_level = _confidence_level(classification_confidence)
    if confidence_level == "low":
        return _information_insufficient("相册整理的分类置信度不足，不能据此确定首批六品类。")
    category_code, category_label = matched
    return ClassificationDecision(
        category_code=category_code,
        category_label=category_label,
        decision_status="candidate",
        source="album_import",
        confidence_level=confidence_level,
        explanations=[
            f"相册整理将本件标为“{normalized}”，仅作为待训练员复核的类目候选。",
            "该候选不代表真伪、产地、品质或价值结论。",
        ],
        evidence_refs=[],
        review_required=True,
        mall_import_allowed=False,
    )


def classify_trainer_label(
    material_label: str,
    *,
    version_id: UUID,
    template_code: str,
) -> ClassificationDecision:
    normalized = material_label.strip()
    matched = _CATEGORY_ALIASES.get(normalized)
    if matched is None:
        return _information_insufficient(
            "人工标签不在首批六品类内，商品资料保持信息不足并禁止商城导入。"
        )
    category_code, category_label = matched
    return ClassificationDecision(
        category_code=category_code,
        category_label=category_label,
        decision_status="human_confirmed",
        source="trainer_confirmed",
        confidence_level="high",
        explanations=[
            "训练员依据当前商品图确认材质类目。",
            "该确认仅用于商品类目，不构成真伪、产地、品质或价值承诺。",
        ],
        evidence_refs=[
            ClassificationEvidenceRef(
                evidence_type="training_version",
                reference_id=f"{template_code}:{version_id}",
            )
        ],
        review_required=False,
        mall_import_allowed=True,
    )
