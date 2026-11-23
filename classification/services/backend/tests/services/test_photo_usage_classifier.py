import inspect
import json
from dataclasses import asdict

import pytest
from PIL import Image

from product_engine.services.photo_usage_classifier import (
    LOCAL_CLASSIFIER_VERSION,
    MAX_DECODED_PIXELS,
    LocalPhotoUsageClassifier,
    record_photo_usage_correction,
)


def generated_image(*, size: tuple[int, int], color: tuple[int, int, int]) -> Image.Image:
    """Build a deterministic, non-media fixture directly in memory."""

    return Image.new("RGB", size, color)


@pytest.mark.parametrize(
    ("safe_filename", "size", "color", "product_count_hint", "category", "confidence"),
    [
        ("studio-product.png", (800, 800), (120, 120, 120), 1, "real_photo", "high"),
        ("wrist-wear.png", (600, 1000), (200, 120, 90), 1, "wear_photo", "high"),
        ("lifestyle-scene.png", (1400, 800), (50, 150, 200), 1, "scene_photo", "high"),
        ("multi-set.png", (800, 800), (180, 180, 180), 3, "multi_product_photo", "low"),
    ],
)
def test_classifies_each_photo_use_automatically_from_local_evidence(
    safe_filename: str,
    size: tuple[int, int],
    color: tuple[int, int, int],
    product_count_hint: int,
    category: str,
    confidence: str,
) -> None:
    decision = LocalPhotoUsageClassifier().classify(
        image=generated_image(size=size, color=color),
        product_count_hint=product_count_hint,
        safe_filename=safe_filename,
    )

    assert decision.category == category
    assert decision.confidence == confidence
    assert decision.classifier_version == LOCAL_CLASSIFIER_VERSION


def test_conflicting_local_signals_still_choose_a_category_and_require_review() -> None:
    decision = LocalPhotoUsageClassifier().classify(
        image=generated_image(size=(600, 1000), color=(80, 80, 80)),
        product_count_hint=1,
        safe_filename="scene-shot.png",
    )

    assert decision.category == "scene_photo"
    assert decision.confidence == "low"
    assert decision.reason_codes == (
        "filename_hint_scene_photo",
        "geometry_portrait_wear_photo",
        "single_product_hint",
        "image_brightness_mid",
        "review_signal_disagreement",
    )


def test_classification_interface_has_no_caller_selected_category() -> None:
    parameters = inspect.signature(LocalPhotoUsageClassifier.classify).parameters

    assert tuple(parameters) == ("self", "image", "product_count_hint", "safe_filename")


def test_manual_correction_is_a_separate_record_that_keeps_the_automatic_decision() -> None:
    automatic = LocalPhotoUsageClassifier().classify(
        image=generated_image(size=(800, 800), color=(120, 120, 120)),
        product_count_hint=1,
        safe_filename="studio-product.png",
    )

    correction = record_photo_usage_correction(
        automatic_decision=automatic,
        corrected_category="wear_photo",
        reviewer_note="商品实际为佩戴展示，需人工改正。",
    )

    assert automatic.category == "real_photo"
    assert correction.automatic_decision == automatic
    assert correction.corrected_category == "wear_photo"


@pytest.mark.parametrize("corrected_category", ["video", "anything-else"])
def test_manual_correction_rejects_categories_outside_the_photo_contract(
    corrected_category: str,
) -> None:
    automatic = LocalPhotoUsageClassifier().classify(
        image=generated_image(size=(800, 800), color=(120, 120, 120)),
        product_count_hint=1,
        safe_filename="studio-product.png",
    )

    with pytest.raises(ValueError, match="invalid_corrected_category"):
        record_photo_usage_correction(
            automatic_decision=automatic,
            corrected_category=corrected_category,
            reviewer_note="无效分类不能写入人工更正。",
        )  # type: ignore[arg-type]


@pytest.mark.parametrize("product_count_hint", [0, -1])
def test_rejects_non_positive_product_count_hints(product_count_hint: int) -> None:
    with pytest.raises(ValueError, match="invalid_product_count_hint"):
        LocalPhotoUsageClassifier().classify(
            image=generated_image(size=(800, 800), color=(120, 120, 120)),
            product_count_hint=product_count_hint,
            safe_filename="studio-product.png",
        )


def unallocated_image_with_size(size: tuple[int, int]) -> Image.Image:
    """Construct only the metadata needed to test pre-decode size checks."""

    image = Image.Image()
    image._size = size
    return image


@pytest.mark.parametrize(
    ("size", "error_code"),
    [
        ((0, 1), "invalid_image_dimensions"),
        ((1, 0), "invalid_image_dimensions"),
        ((MAX_DECODED_PIXELS + 1, 1), "decoded_image_too_large"),
    ],
)
def test_rejects_invalid_or_bounded_out_decoded_image_sizes(
    size: tuple[int, int], error_code: str
) -> None:
    with pytest.raises(ValueError, match=error_code):
        LocalPhotoUsageClassifier().classify(
            image=unallocated_image_with_size(size),
            product_count_hint=1,
            safe_filename="studio-product.png",
        )


@pytest.mark.parametrize(
    ("size", "geometry_reason"),
    [
        ((1400, 800), "geometry_landscape_scene_photo"),
        ((600, 1000), "geometry_portrait_wear_photo"),
    ],
)
def test_multi_product_geometry_conflicts_require_low_confidence_review(
    size: tuple[int, int], geometry_reason: str
) -> None:
    decision = LocalPhotoUsageClassifier().classify(
        image=generated_image(size=size, color=(120, 120, 120)),
        product_count_hint=3,
        safe_filename="multi-set.png",
    )

    assert decision.category == "multi_product_photo"
    assert decision.confidence == "low"
    assert geometry_reason in decision.reason_codes
    assert "review_signal_disagreement" in decision.reason_codes


@pytest.mark.parametrize(
    ("safe_filename", "size", "color", "product_count_hint"),
    [
        ("studio-product.png", (800, 800), (120, 120, 120), 1),
        ("wrist-wear.png", (600, 1000), (200, 120, 90), 1),
        ("lifestyle-scene.png", (1400, 800), (50, 150, 200), 1),
        ("multi-set.png", (800, 800), (180, 180, 180), 3),
        ("scene-shot.png", (600, 1000), (80, 80, 80), 1),
    ],
)
def test_repeated_runs_serialize_to_the_same_decision(
    safe_filename: str,
    size: tuple[int, int],
    color: tuple[int, int, int],
    product_count_hint: int,
) -> None:
    classifier = LocalPhotoUsageClassifier()
    image = generated_image(size=size, color=color)

    serialized = {
        json.dumps(
            asdict(
                classifier.classify(
                    image=image,
                    product_count_hint=product_count_hint,
                    safe_filename=safe_filename,
                )
            ),
            sort_keys=True,
            separators=(",", ":"),
        )
        for _ in range(20)
    }

    assert len(serialized) == 1
