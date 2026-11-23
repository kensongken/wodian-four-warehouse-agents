"""Deterministic local scaffolding for automatic photo-use classification.

This module deliberately uses only decoded image properties and already-safe local
metadata.  It is not an image model and never makes a network request.
"""

import re
from dataclasses import dataclass
from math import sqrt
from typing import Final, Literal, Protocol

from PIL import Image

from product_engine.contracts.style_test_bridge import PhotoCategory

LOCAL_CLASSIFIER_VERSION: Final[Literal["local-photo-usage-rules-v1"]] = (
    "local-photo-usage-rules-v1"
)
MAX_DECODED_PIXELS: Final = 16_000_000

_ALLOWED_PHOTO_CATEGORIES: Final[frozenset[PhotoCategory]] = frozenset(
    {
        "real_photo",
        "wear_photo",
        "scene_photo",
        "multi_product_photo",
    }
)

_FILENAME_HINTS: Final[dict[PhotoCategory, frozenset[str]]] = {
    "real_photo": frozenset({"catalog", "detail", "product", "real", "studio"}),
    "wear_photo": frozenset({"hand", "model", "wear", "worn", "wrist"}),
    "scene_photo": frozenset({"ambient", "lifestyle", "scene"}),
    "multi_product_photo": frozenset({"group", "multi", "set"}),
}


@dataclass(frozen=True)
class PhotoUsageDecision:
    category: PhotoCategory
    confidence: Literal["high", "medium", "low"]
    reason_codes: tuple[str, ...]
    classifier_version: Literal["local-photo-usage-rules-v1"] = LOCAL_CLASSIFIER_VERSION


class PhotoUsageClassifier(Protocol):
    def classify(
        self,
        *,
        image: Image.Image,
        product_count_hint: int | None,
        safe_filename: str,
    ) -> PhotoUsageDecision: ...


@dataclass(frozen=True)
class PhotoUsageCorrectionEvent:
    """A human correction that references, but cannot replace, the automatic decision."""

    automatic_decision: PhotoUsageDecision
    corrected_category: PhotoCategory
    reviewer_note: str


def _filename_categories(safe_filename: str) -> tuple[PhotoCategory, ...]:
    tokens = frozenset(re.findall(r"[a-z0-9]+", safe_filename.casefold()))
    return tuple(
        category for category, hints in _FILENAME_HINTS.items() if tokens.intersection(hints)
    )


def _geometry_category(image: Image.Image) -> tuple[PhotoCategory, str]:
    width, height = image.size
    ratio = width / height
    if ratio >= 1.3:
        return "scene_photo", "geometry_landscape_scene_photo"
    if ratio <= 0.77:
        return "wear_photo", "geometry_portrait_wear_photo"
    return "real_photo", "geometry_square_real_photo"


def _validate_image_size(image: Image.Image) -> None:
    width, height = image.size
    if width <= 0 or height <= 0:
        raise ValueError("invalid_image_dimensions")
    if width * height > MAX_DECODED_PIXELS:
        raise ValueError("decoded_image_too_large")


def prepare_classification_image(image: Image.Image) -> Image.Image:
    """Return a bounded classifier copy while leaving source bytes untouched."""
    width, height = image.size
    if width <= 0 or height <= 0:
        raise ValueError("invalid_image_dimensions")
    if width * height <= MAX_DECODED_PIXELS:
        return image
    scale = sqrt(MAX_DECODED_PIXELS / (width * height))
    target = (max(1, int(width * scale)), max(1, int(height * scale)))
    return image.resize(target, Image.Resampling.LANCZOS)


def _brightness_reason(image: Image.Image) -> str:
    rgb = image.convert("RGB")
    histogram = rgb.histogram()
    channel_total = image.width * image.height * 3
    mean = sum((index % 256) * count for index, count in enumerate(histogram)) / channel_total
    if mean < 64:
        return "image_brightness_dark"
    if mean >= 192:
        return "image_brightness_bright"
    return "image_brightness_mid"


class LocalPhotoUsageClassifier:
    """Assign a photo-use category before any optional human correction exists."""

    def classify(
        self,
        *,
        image: Image.Image,
        product_count_hint: int | None,
        safe_filename: str,
    ) -> PhotoUsageDecision:
        _validate_image_size(image)
        if product_count_hint is not None and product_count_hint <= 0:
            raise ValueError("invalid_product_count_hint")
        filename_categories = _filename_categories(safe_filename)
        geometry_category, geometry_reason = _geometry_category(image)
        reasons = [
            *(f"filename_hint_{category}" for category in filename_categories),
            geometry_reason,
        ]

        if product_count_hint is not None and product_count_hint > 1:
            category: PhotoCategory = "multi_product_photo"
            evidence_categories = (category, *filename_categories, geometry_category)
            reasons.append("multiple_product_hint")
        else:
            category = filename_categories[0] if filename_categories else geometry_category
            evidence_categories = (*filename_categories, geometry_category)
            if product_count_hint == 1:
                reasons.append("single_product_hint")
            else:
                reasons.append("product_count_hint_unavailable")

        reasons.append(_brightness_reason(image))
        if len(set(evidence_categories)) > 1:
            confidence: Literal["high", "medium", "low"] = "low"
            reasons.append("review_signal_disagreement")
        elif filename_categories or product_count_hint is not None:
            confidence = "high"
        else:
            confidence = "medium"

        return PhotoUsageDecision(
            category=category,
            confidence=confidence,
            reason_codes=tuple(reasons),
        )


def record_photo_usage_correction(
    *,
    automatic_decision: PhotoUsageDecision,
    corrected_category: PhotoCategory,
    reviewer_note: str,
) -> PhotoUsageCorrectionEvent:
    """Record a review separately so the original automatic decision remains auditable."""

    cleaned_note = reviewer_note.strip()
    if not cleaned_note:
        raise ValueError("reviewer_note_required")
    if corrected_category not in _ALLOWED_PHOTO_CATEGORIES:
        raise ValueError("invalid_corrected_category")
    return PhotoUsageCorrectionEvent(
        automatic_decision=automatic_decision,
        corrected_category=corrected_category,
        reviewer_note=cleaned_note,
    )
