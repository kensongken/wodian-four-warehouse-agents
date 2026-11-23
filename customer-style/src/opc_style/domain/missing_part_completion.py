"""Fail-closed contracts for AI-simulated small jewelry repairs."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from string import hexdigits
from typing import Protocol, cast

from PIL import Image, ImageChops, ImageFilter, ImageStat, UnidentifiedImageError

from opc_style.domain.errors import DomainRuleError

SUPPORTED_MISSING_PARTS = frozenset(
    {
        "ear_hook",
        "fine_chain",
        "clasp",
        "bead_edge",
        "pendant_edge",
        "setting_edge",
    }
)
IDENTITY_CRITICAL_PARTS = frozenset(
    {"main_stone", "engraving", "hallmark", "unique_pattern", "unknown"}
)
MAX_MISSING_RATIO = 0.08
MAX_IMAGE_PIXELS = 16_000_000
MIN_MASK_CONTEXT_EDGE_MEAN = 8.0
MIN_MASK_CONTEXT_STRONG_EDGE_FRACTION = 0.04
MIN_MASK_CONTEXT_CHROMA_MEAN = 20.0
MIN_MASK_CONTEXT_STRONG_CHROMA_FRACTION = 0.10
MIN_MASK_CONTEXT_CHROMA_STDDEV = 8.0
MIN_CANDIDATE_CHANGE_MEAN = 2.0
OCCLUDED_SCENE_TAGS = frozenset({"hand_held", "occluded"})
MISSING_PART_EVIDENCE_KINDS = frozenset(
    {
        "none",
        "human_confirmed_mask",
        "clean_same_product_reference",
        "same_product_multiview_references",
    }
)
MIN_MULTIVIEW_REFERENCES = 2
MAX_MULTIVIEW_REFERENCES = 8
MIN_MULTIVIEW_DHASH_DISTANCE = 8

PART_REVIEW_CHECKLISTS = {
    "ear_hook": "确认耳钩只有一条连续曲线，粗细和连接点与原图一致",
    "fine_chain": "确认每个链节连续，没有凭空增加吊坠、扣头或分叉",
    "clasp": "确认扣头只连接原图可见的两个端点，数量和开合方向正确",
    "bead_edge": "确认只补珠子边缘，没有增加、删除或改变珠子顺序",
    "pendant_edge": "确认只延续吊坠轮廓，没有新增纹样、刻字或宝石",
    "setting_edge": "确认只延续原有镶口或隔片，没有把金属结构变成珠子",
}


@dataclass(frozen=True)
class MissingPartCompletionRequest:
    """One immutable source plus the exact region the model may simulate."""

    source_content: bytes
    source_sha256: str
    source_mime_type: str
    missing_mask_png: bytes
    category: str
    part_kind: str
    material_tags: tuple[str, ...]
    case_id: str
    scene_tags: tuple[str, ...] = ()
    evidence_kind: str = "none"
    evidence_sha256: str | None = None
    reference_content: bytes | None = None
    reference_mime_type: str | None = None
    reference_same_product_confirmed: bool = False
    reference_structure_confirmed: bool = False
    reference_contents: tuple[bytes, ...] = ()
    reference_mime_types: tuple[str, ...] = ()


@dataclass(frozen=True)
class ValidatedMissingPartRequest:
    request: MissingPartCompletionRequest
    width: int
    height: int
    missing_ratio: float
    request_fingerprint: str


@dataclass(frozen=True)
class MissingPartCandidate:
    """A review candidate; simulated pixels never become product truth by themselves."""

    content: bytes
    content_sha256: str
    mime_type: str
    width: int
    height: int
    model_version: str
    request_fingerprint: str
    simulated_region_sha256: str
    review_state: str
    review_reasons: tuple[str, ...]
    structural_review_checklist: str
    material_change_mean: float
    automatic_approval: bool = False
    publishable: bool = False
    requires_human_review: bool = True


@dataclass(frozen=True)
class MissingPartMaskAssessment:
    """Zero-cost evidence that a mask touches visible product structure."""

    state: str
    context_edge_mean: float
    strong_edge_fraction: float
    context_chroma_mean: float
    strong_chroma_fraction: float
    context_chroma_stddev: float
    reasons: tuple[str, ...]


class MissingPartCompleter(Protocol):
    model_version: str

    def complete(
        self,
        request: MissingPartCompletionRequest,
        *,
        learning_lessons: tuple[str, ...] = (),
    ) -> MissingPartCandidate:
        """Return one bounded, non-publishable completion candidate."""
        ...


def validate_missing_part_request(
    request: MissingPartCompletionRequest,
) -> ValidatedMissingPartRequest:
    if request.part_kind in IDENTITY_CRITICAL_PARTS:
        raise DomainRuleError("missing_part_identity_critical")
    if request.part_kind not in SUPPORTED_MISSING_PARTS:
        raise DomainRuleError("missing_part_kind_unsupported")
    if request.source_mime_type not in {"image/jpeg", "image/png"}:
        raise DomainRuleError("missing_part_source_invalid")
    if sha256(request.source_content).hexdigest() != request.source_sha256:
        raise DomainRuleError("missing_part_source_digest_mismatch")
    if not request.case_id or len(request.case_id) > 128:
        raise DomainRuleError("missing_part_case_invalid")
    reference_supplied = request.reference_content is not None
    multiview_supplied = bool(request.reference_contents)
    if (
        request.evidence_kind not in MISSING_PART_EVIDENCE_KINDS
        or any(not tag.strip() or len(tag) > 64 for tag in request.scene_tags)
        or (request.evidence_kind == "none" and request.evidence_sha256 is not None)
        or (
            request.evidence_kind != "none"
            and (
                request.evidence_sha256 is None
                or len(request.evidence_sha256) != 64
                or any(
                    character not in hexdigits for character in request.evidence_sha256
                )
            )
        )
        or (
            request.evidence_kind
            not in {
                "clean_same_product_reference",
                "same_product_multiview_references",
            }
            and (
                reference_supplied
                or multiview_supplied
                or request.reference_mime_type is not None
                or request.reference_mime_types
                or request.reference_same_product_confirmed
                or request.reference_structure_confirmed
            )
        )
        or (
            request.evidence_kind == "clean_same_product_reference"
            and (
                not reference_supplied
                or request.reference_mime_type not in {"image/jpeg", "image/png"}
                or not request.reference_same_product_confirmed
                or not request.reference_structure_confirmed
                or multiview_supplied
                or request.reference_mime_types
            )
        )
        or (
            request.evidence_kind == "same_product_multiview_references"
            and (
                reference_supplied
                or request.reference_mime_type is not None
                or not request.reference_same_product_confirmed
                or not request.reference_structure_confirmed
                or not (
                    MIN_MULTIVIEW_REFERENCES
                    <= len(request.reference_contents)
                    <= MAX_MULTIVIEW_REFERENCES
                )
                or len(request.reference_mime_types) != len(request.reference_contents)
                or any(
                    mime_type not in {"image/jpeg", "image/png"}
                    for mime_type in request.reference_mime_types
                )
            )
        )
    ):
        raise DomainRuleError("missing_part_evidence_invalid")
    if request.evidence_kind == "human_confirmed_mask" and (
        request.evidence_sha256 != sha256(request.missing_mask_png).hexdigest()
    ):
        raise DomainRuleError("missing_part_evidence_digest_mismatch")
    if request.evidence_kind == "clean_same_product_reference":
        assert request.reference_content is not None
        reference_sha256 = sha256(request.reference_content).hexdigest()
        if (
            request.evidence_sha256 != reference_sha256
            or reference_sha256 == request.source_sha256
        ):
            raise DomainRuleError("missing_part_evidence_digest_mismatch")
        try:
            with Image.open(BytesIO(request.reference_content)) as opened:
                opened.load()
                reference_pixels = opened.width * opened.height
        except (
            Image.DecompressionBombError,
            UnidentifiedImageError,
            OSError,
            ValueError,
        ):
            raise DomainRuleError("missing_part_reference_invalid") from None
        if reference_pixels > MAX_IMAGE_PIXELS:
            raise DomainRuleError("missing_part_reference_invalid")
    if request.evidence_kind == "same_product_multiview_references":
        reference_digests: list[str] = []
        reference_view_hashes: list[int] = []
        for content in request.reference_contents:
            reference_digest = sha256(content).hexdigest()
            if reference_digest == request.source_sha256:
                raise DomainRuleError("missing_part_evidence_digest_mismatch")
            reference_digests.append(reference_digest)
            try:
                with Image.open(BytesIO(content)) as opened:
                    opened.load()
                    reference_pixels = opened.width * opened.height
                    reference_view_hashes.append(reference_view_difference_hash(opened))
            except (
                Image.DecompressionBombError,
                UnidentifiedImageError,
                OSError,
                ValueError,
            ):
                raise DomainRuleError("missing_part_reference_invalid") from None
            if reference_pixels > MAX_IMAGE_PIXELS:
                raise DomainRuleError("missing_part_reference_invalid")
        if len(set(reference_digests)) != len(reference_digests) or (
            request.evidence_sha256
            != multiview_reference_digest(request.reference_contents)
        ):
            raise DomainRuleError("missing_part_evidence_digest_mismatch")
        maximum_view_distance = max(
            (left ^ right).bit_count()
            for index, left in enumerate(reference_view_hashes)
            for right in reference_view_hashes[index + 1 :]
        )
        if maximum_view_distance < MIN_MULTIVIEW_DHASH_DISTANCE:
            raise DomainRuleError("missing_part_multiview_not_diverse")
    try:
        with Image.open(BytesIO(request.source_content)) as opened:
            opened.load()
            source = opened.convert("RGB")
        with Image.open(BytesIO(request.missing_mask_png)) as opened:
            opened.load()
            if opened.format != "PNG":
                raise DomainRuleError("missing_part_mask_invalid")
            mask = opened.convert("L").point(
                lambda value: 255 if value >= 128 else 0, mode="L"
            )
    except DomainRuleError:
        raise
    except (Image.DecompressionBombError, UnidentifiedImageError, OSError, ValueError):
        raise DomainRuleError("missing_part_media_invalid") from None
    if source.size != mask.size or source.width * source.height > MAX_IMAGE_PIXELS:
        raise DomainRuleError("missing_part_dimensions_invalid")
    active = source.width * source.height - mask.histogram()[0]
    if active == 0:
        raise DomainRuleError("missing_part_region_empty")
    ratio = active / (source.width * source.height)
    if ratio > MAX_MISSING_RATIO:
        raise DomainRuleError("missing_part_region_too_large")
    fingerprint = sha256(
        b"\0".join(
            (
                request.source_sha256.encode("ascii"),
                sha256(request.missing_mask_png).hexdigest().encode("ascii"),
                request.category.encode("utf-8"),
                request.part_kind.encode("ascii"),
                "|".join(sorted(set(request.material_tags))).encode("utf-8"),
                "|".join(sorted(set(request.scene_tags))).encode("utf-8"),
                request.evidence_kind.encode("ascii"),
                (request.evidence_sha256 or "").encode("ascii"),
            )
        )
    ).hexdigest()
    return ValidatedMissingPartRequest(
        request=request,
        width=source.width,
        height=source.height,
        missing_ratio=ratio,
        request_fingerprint=fingerprint,
    )


def multiview_reference_digest(reference_contents: tuple[bytes, ...]) -> str:
    """Bind one request to the exact ordered set of existing reference views."""

    return sha256(
        b"\0".join(sha256(content).digest() for content in reference_contents)
    ).hexdigest()


def reference_view_difference_hash(image: Image.Image) -> int:
    """Return a small visual fingerprint used only to reject near-identical views."""

    grayscale = image.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    pixels = grayscale.load()
    assert pixels is not None
    value = 0
    for y in range(8):
        for x in range(8):
            left = cast(int, pixels[x, y])
            right = cast(int, pixels[x + 1, y])
            value = (value << 1) | int(left > right)
    return value


def require_missing_part_evidence(request: MissingPartCompletionRequest) -> None:
    """Block occluded completion unless a real, digest-bound authority exists."""

    validate_missing_part_request(request)
    if OCCLUDED_SCENE_TAGS.intersection(request.scene_tags) and (
        request.evidence_kind == "none" or request.evidence_sha256 is None
    ):
        raise DomainRuleError("missing_part_occlusion_evidence_required")


def measure_missing_part_mask(
    request: MissingPartCompletionRequest,
) -> MissingPartMaskAssessment:
    """Measure mask evidence without authorizing or calling a paid provider."""

    validate_missing_part_request(request)
    with Image.open(BytesIO(request.source_content)) as opened:
        source = opened.convert("RGB")
    with Image.open(BytesIO(request.missing_mask_png)) as opened:
        mask = opened.convert("L").point(
            lambda value: 255 if value >= 128 else 0, mode="L"
        )
    radius = max(2, min(15, round(min(source.size) * 0.005)))
    kernel = radius * 2 + 1
    bbox = mask.getbbox()
    if bbox is None:
        raise DomainRuleError("missing_part_region_empty")
    left, top, right, bottom = bbox
    padding = radius + 3
    context_box = (
        max(0, left - padding),
        max(0, top - padding),
        min(source.width, right + padding),
        min(source.height, bottom + padding),
    )
    local_mask = mask.crop(context_box)
    outside_ring = ImageChops.subtract(
        local_mask.filter(ImageFilter.MaxFilter(kernel)), local_mask
    )
    local_source = source.crop(context_box)
    edge_image = local_source.convert("L").filter(ImageFilter.FIND_EDGES)
    histogram = edge_image.histogram(mask=outside_ring)
    samples = sum(histogram)
    if samples == 0:
        raise DomainRuleError("missing_part_mask_unanchored")
    edge_mean = sum(value * count for value, count in enumerate(histogram)) / samples
    strong_fraction = sum(histogram[40:]) / samples
    red, green, blue = local_source.split()
    chroma = ImageChops.lighter(
        ImageChops.difference(red, green),
        ImageChops.lighter(
            ImageChops.difference(red, blue), ImageChops.difference(green, blue)
        ),
    )
    chroma_histogram = chroma.histogram(mask=outside_ring)
    chroma_mean = (
        sum(value * count for value, count in enumerate(chroma_histogram)) / samples
    )
    strong_chroma_fraction = sum(chroma_histogram[20:]) / samples
    chroma_stddev = ImageStat.Stat(chroma, mask=outside_ring).stddev[0]
    anchored = (
        edge_mean >= MIN_MASK_CONTEXT_EDGE_MEAN
        and strong_fraction >= MIN_MASK_CONTEXT_STRONG_EDGE_FRACTION
    ) or (
        chroma_stddev >= MIN_MASK_CONTEXT_CHROMA_STDDEV
        and chroma_mean >= MIN_MASK_CONTEXT_CHROMA_MEAN
        and strong_chroma_fraction >= MIN_MASK_CONTEXT_STRONG_CHROMA_FRACTION
    )
    return MissingPartMaskAssessment(
        state=("anchored_to_visible_structure" if anchored else "blocked_unanchored"),
        context_edge_mean=round(edge_mean, 4),
        strong_edge_fraction=round(strong_fraction, 6),
        context_chroma_mean=round(chroma_mean, 4),
        strong_chroma_fraction=round(strong_chroma_fraction, 6),
        context_chroma_stddev=round(chroma_stddev, 4),
        reasons=(
            ("visible_structure_touches_mask_context",)
            if anchored
            else ("visible_structure_not_detected_in_mask_context",)
        ),
    )


def assess_missing_part_mask(
    request: MissingPartCompletionRequest,
) -> MissingPartMaskAssessment:
    """Reject obvious background-only masks before any paid provider call."""

    assessment = measure_missing_part_mask(request)
    if assessment.state != "anchored_to_visible_structure":
        raise DomainRuleError("missing_part_mask_unanchored")
    return assessment


def compose_missing_part_candidate(
    request: MissingPartCompletionRequest,
    *,
    generated_content: bytes,
    model_version: str,
) -> MissingPartCandidate:
    """Copy generated pixels only inside the declared mask."""

    validated = validate_missing_part_request(request)
    try:
        with Image.open(BytesIO(request.source_content)) as opened:
            opened.load()
            source = opened.convert("RGB")
        with Image.open(BytesIO(request.missing_mask_png)) as opened:
            opened.load()
            mask = opened.convert("L").point(
                lambda value: 255 if value >= 128 else 0, mode="L"
            )
        with Image.open(BytesIO(generated_content)) as opened:
            opened.load()
            generated = opened.convert("RGB")
    except (Image.DecompressionBombError, UnidentifiedImageError, OSError, ValueError):
        raise DomainRuleError("missing_part_candidate_invalid") from None
    if generated.size != source.size:
        generated = generated.resize(source.size, Image.Resampling.LANCZOS)
    composed = Image.composite(generated, source, mask)
    difference = ImageChops.difference(composed, source)
    channel_means = ImageStat.Stat(difference, mask=mask).mean
    material_change_mean = sum(channel_means) / len(channel_means)
    review_reasons: tuple[str, ...]
    if material_change_mean < MIN_CANDIDATE_CHANGE_MEAN:
        review_state = "guardrail_rejected"
        review_reasons = ("candidate_has_no_meaningful_material_change",)
    else:
        review_state = "human_review_required"
        review_reasons = (
            "candidate_changed_only_declared_region",
            "part_structure_requires_human_confirmation",
        )
    output = BytesIO()
    composed.save(output, format="PNG", optimize=False)
    content = output.getvalue()
    return MissingPartCandidate(
        content=content,
        content_sha256=sha256(content).hexdigest(),
        mime_type="image/png",
        width=validated.width,
        height=validated.height,
        model_version=model_version,
        request_fingerprint=validated.request_fingerprint,
        simulated_region_sha256=sha256(request.missing_mask_png).hexdigest(),
        review_state=review_state,
        review_reasons=review_reasons,
        structural_review_checklist=PART_REVIEW_CHECKLISTS[request.part_kind],
        material_change_mean=round(material_change_mean, 4),
    )
