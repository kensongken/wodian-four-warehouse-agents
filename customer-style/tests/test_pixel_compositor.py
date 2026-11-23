from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from io import BytesIO

import pytest
from PIL import Image

from opc_style.domain.errors import DomainRuleError
from opc_style.domain.pixel_compositor import PixelCompositor
from opc_style.domain.product_foreground import ForegroundTransform


def _png(mode: str, size: tuple[int, int], color) -> bytes:
    image = Image.new(mode, size, color)
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _foreground(*, soft_edge: bool = False) -> tuple[bytes, bytes]:
    foreground = Image.new("RGBA", (12, 12), (0, 0, 0, 0))
    mask = Image.new("L", (12, 12), 0)
    for x in range(2, 10):
        for y in range(2, 10):
            alpha = 128 if soft_edge and (x in {2, 9} or y in {2, 9}) else 255
            foreground.putpixel((x, y), (24, 120, 80, alpha))
            mask.putpixel((x, y), alpha)
    foreground_output, mask_output = BytesIO(), BytesIO()
    foreground.save(foreground_output, format="PNG")
    mask.save(mask_output, format="PNG")
    return foreground_output.getvalue(), mask_output.getvalue()


def _transform(**changes) -> ForegroundTransform:
    values = {
        "translate_x": 0,
        "translate_y": 0,
        "uniform_scale": Decimal(2),
        "rotation_degrees": Decimal(0),
    }
    values.update(changes)
    return ForegroundTransform(**values)


def test_composition_is_deterministic_and_verifiable() -> None:
    foreground, mask = _foreground()
    background = _png("RGB", (64, 64), (240, 232, 220))
    compositor = PixelCompositor()

    first = compositor.compose(foreground, mask, background, _transform(), (64, 64))
    second = compositor.compose(foreground, mask, background, _transform(), (64, 64))

    assert first == second
    assert first.verification_state == "verified"
    compositor.verify(first, foreground, mask, background)


def test_tabletop_recommendation_is_deterministic_and_stays_in_frame() -> None:
    foreground, mask = _foreground()
    background_image = Image.new("RGB", (64, 64), (244, 238, 230))
    for y in range(36, 64):
        for x in range(64):
            background_image.putpixel((x, y), (158, 112, 92))
    output = BytesIO()
    background_image.save(output, format="PNG")
    compositor = PixelCompositor()

    recommendation = compositor.recommend(foreground, mask, output.getvalue(), (64, 64))
    repeated = compositor.recommend(foreground, mask, output.getvalue(), (64, 64))

    assert recommendation == repeated
    assert 34 <= recommendation.horizon_y <= 38
    assert recommendation.publishable is False
    assert recommendation.tabletop_depth_ratio == Decimal("0.4375")
    assert recommendation.product_category == "legacy_unspecified"
    assert recommendation.category_profile == "legacy_bracelet_pilot_v1"
    assert recommendation.category_calibration_state == "legacy_pilot"
    assert recommendation.placement_state == "ready"
    assert recommendation.save_allowed is True
    assert recommendation.reason_codes == (
        "tabletop_horizon_detected",
        "foreground_mask_fitted",
    )
    compositor.compose(
        foreground,
        mask,
        output.getvalue(),
        ForegroundTransform(
            recommendation.translate_x,
            recommendation.translate_y,
            recommendation.uniform_scale,
            recommendation.rotation_degrees,
        ),
        (recommendation.output_width, recommendation.output_height),
    )


def test_tabletop_recommendation_detects_a_low_horizon() -> None:
    foreground, mask = _foreground()
    background_image = Image.new("RGB", (64, 64), (170, 70, 54))
    for y in range(52, 64):
        for x in range(64):
            background_image.putpixel((x, y), (238, 224, 206))
    output = BytesIO()
    background_image.save(output, format="PNG")

    recommendation = PixelCompositor().recommend(
        foreground, mask, output.getvalue(), (64, 64)
    )

    assert 50 <= recommendation.horizon_y <= 54
    assert recommendation.tabletop_depth_ratio < Decimal("0.2800")
    assert recommendation.placement_state == "background_unsuitable"
    assert recommendation.save_allowed is False
    assert "tabletop_depth_insufficient" in recommendation.reason_codes


@pytest.mark.parametrize("category", ("ring", "pendant"))
def test_uncalibrated_product_categories_fail_closed(category: str) -> None:
    foreground, mask = _foreground()
    background_image = Image.new("RGB", (64, 64), (244, 238, 230))
    for y in range(32, 64):
        for x in range(64):
            background_image.putpixel((x, y), (158, 112, 92))
    output = BytesIO()
    background_image.save(output, format="PNG")

    recommendation = PixelCompositor().recommend(
        foreground,
        mask,
        output.getvalue(),
        (64, 64),
        product_category=category,
    )

    assert recommendation.product_category == category
    assert recommendation.category_profile == f"{category}_unvalidated"
    assert recommendation.category_calibration_state == "required"
    assert recommendation.save_allowed is False
    assert "category_calibration_required" in recommendation.reason_codes


def test_bracelet_category_uses_the_calibrated_pilot_profile() -> None:
    foreground, mask = _foreground()
    background_image = Image.new("RGB", (64, 64), (244, 238, 230))
    for y in range(36, 64):
        for x in range(64):
            background_image.putpixel((x, y), (158, 112, 92))
    output = BytesIO()
    background_image.save(output, format="PNG")

    recommendation = PixelCompositor().recommend(
        foreground,
        mask,
        output.getvalue(),
        (64, 64),
        product_category="bracelet",
    )

    assert recommendation.category_profile == "bracelet_tabletop_v1"
    assert recommendation.category_calibration_state == "calibrated"
    assert recommendation.save_allowed is True


def test_bracelet_reports_scale_shortfall_separately_from_table_depth() -> None:
    foreground = Image.new("RGBA", (100, 300), (0, 0, 0, 0))
    mask = Image.new("L", (100, 300), 0)
    for x in range(10, 90):
        for y in range(10, 290):
            foreground.putpixel((x, y), (24, 120, 80, 255))
            mask.putpixel((x, y), 255)
    foreground_output, mask_output = BytesIO(), BytesIO()
    foreground.save(foreground_output, format="PNG")
    mask.save(mask_output, format="PNG")
    background_image = Image.new("RGB", (100, 100), (244, 238, 230))
    for y in range(35, 100):
        for x in range(100):
            background_image.putpixel((x, y), (158, 112, 92))
    background_output = BytesIO()
    background_image.save(background_output, format="PNG")

    recommendation = PixelCompositor().recommend(
        foreground_output.getvalue(),
        mask_output.getvalue(),
        background_output.getvalue(),
        (100, 100),
        product_category="bracelet",
    )

    assert recommendation.tabletop_depth_ratio >= Decimal("0.2800")
    assert recommendation.uniform_scale < Decimal("0.2800")
    assert recommendation.save_allowed is False
    assert "foreground_scale_insufficient" in recommendation.reason_codes
    assert "tabletop_depth_insufficient" not in recommendation.reason_codes


def test_bracelet_uses_minimum_display_scale_when_it_still_fits_tabletop() -> None:
    foreground = Image.new("RGBA", (100, 220), (0, 0, 0, 0))
    mask = Image.new("L", (100, 220), 0)
    for x in range(10, 90):
        for y in range(10, 210):
            foreground.putpixel((x, y), (24, 120, 80, 255))
            mask.putpixel((x, y), 255)
    foreground_output, mask_output = BytesIO(), BytesIO()
    foreground.save(foreground_output, format="PNG")
    mask.save(mask_output, format="PNG")
    background_image = Image.new("RGB", (100, 100), (244, 238, 230))
    for y in range(35, 100):
        for x in range(100):
            background_image.putpixel((x, y), (158, 112, 92))
    background_output = BytesIO()
    background_image.save(background_output, format="PNG")

    recommendation = PixelCompositor().recommend(
        foreground_output.getvalue(),
        mask_output.getvalue(),
        background_output.getvalue(),
        (100, 100),
        product_category="bracelet",
    )

    assert recommendation.uniform_scale == Decimal("0.2800")
    assert recommendation.save_allowed is True
    assert "minimum_display_scale_applied" in recommendation.reason_codes


def test_earring_category_uses_the_local_pilot_profile() -> None:
    foreground, mask = _foreground()
    background_image = Image.new("RGB", (64, 64), (244, 238, 230))
    for y in range(32, 64):
        for x in range(64):
            background_image.putpixel((x, y), (158, 112, 92))
    output = BytesIO()
    background_image.save(output, format="PNG")

    recommendation = PixelCompositor().recommend(
        foreground,
        mask,
        output.getvalue(),
        (64, 64),
        product_category="earring",
    )

    assert recommendation.category_profile == "earring_isolated_pilot_v1"
    assert recommendation.category_calibration_state == "pilot_calibrated"
    assert recommendation.save_allowed is True
    assert recommendation.publishable is False


def test_necklace_category_uses_the_local_pilot_profile() -> None:
    foreground, mask = _foreground()
    background_image = Image.new("RGB", (64, 64), (244, 238, 230))
    for y in range(32, 64):
        for x in range(64):
            background_image.putpixel((x, y), (158, 112, 92))
    output = BytesIO()
    background_image.save(output, format="PNG")

    recommendation = PixelCompositor().recommend(
        foreground,
        mask,
        output.getvalue(),
        (64, 64),
        product_category="necklace",
    )

    assert recommendation.category_profile == "necklace_tabletop_pilot_v1"
    assert recommendation.category_calibration_state == "pilot_calibrated"
    assert recommendation.save_allowed is True
    assert recommendation.publishable is False


def test_recommendation_ignores_transparent_padding_around_product() -> None:
    foreground = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    mask = Image.new("L", (100, 100), 0)
    for x in range(45, 55):
        for y in range(25, 75):
            foreground.putpixel((x, y), (180, 50, 30, 255))
            mask.putpixel((x, y), 255)
    foreground_output, mask_output = BytesIO(), BytesIO()
    foreground.save(foreground_output, format="PNG")
    mask.save(mask_output, format="PNG")
    background = Image.new("RGB", (100, 150), (244, 238, 230))
    for y in range(95, 150):
        for x in range(100):
            background.putpixel((x, y), (158, 112, 92))
    background_output = BytesIO()
    background.save(background_output, format="PNG")

    recommendation = PixelCompositor().recommend(
        foreground_output.getvalue(),
        mask_output.getvalue(),
        background_output.getvalue(),
        (100, 150),
        product_category="earring",
    )

    assert recommendation.uniform_scale > Decimal("0.5")
    PixelCompositor().compose(
        foreground_output.getvalue(),
        mask_output.getvalue(),
        background_output.getvalue(),
        ForegroundTransform(
            recommendation.translate_x,
            recommendation.translate_y,
            recommendation.uniform_scale,
            recommendation.rotation_degrees,
        ),
        (100, 150),
    )


def test_allowed_translation_scale_and_rotation_remain_inside_canvas() -> None:
    foreground, mask = _foreground()
    composition = PixelCompositor().compose(
        foreground,
        mask,
        _png("RGB", (80, 80), "white"),
        _transform(
            translate_x=8,
            translate_y=-6,
            uniform_scale=Decimal("2.5"),
            rotation_degrees=Decimal(5),
        ),
        (80, 80),
    )

    assert composition.transform_payload["rotation_degrees"] == "5"
    assert composition.transform_payload["uniform_scale"] == "2.5"


def test_shadow_is_separate_and_does_not_change_foreground_digest() -> None:
    foreground, mask = _foreground()
    background = _png("RGB", (64, 64), "white")
    compositor = PixelCompositor()

    with_shadow = compositor.compose(
        foreground, mask, background, _transform(), (64, 64), shadow=True
    )
    without_shadow = compositor.compose(
        foreground, mask, background, _transform(), (64, 64), shadow=False
    )

    assert with_shadow.output_sha256 != without_shadow.output_sha256
    assert with_shadow.foreground_rgb_sha256 == without_shadow.foreground_rgb_sha256
    assert (
        with_shadow.transformed_alpha_sha256 == without_shadow.transformed_alpha_sha256
    )


def test_alpha_edges_are_preserved_and_recomputable() -> None:
    foreground, mask = _foreground(soft_edge=True)
    composition = PixelCompositor().compose(
        foreground, mask, _png("RGB", (64, 64), "white"), _transform(), (64, 64)
    )

    PixelCompositor().verify(
        composition, foreground, mask, _png("RGB", (64, 64), "white")
    )


def test_nonuniform_scale_is_not_part_of_the_transform_contract() -> None:
    with pytest.raises(TypeError):
        ForegroundTransform(
            translate_x=0,
            translate_y=0,
            uniform_scale=Decimal(1),
            rotation_degrees=Decimal(0),
            scale_x=Decimal(2),
        )


def test_rotation_beyond_five_degrees_is_rejected() -> None:
    foreground, mask = _foreground()
    with pytest.raises(DomainRuleError) as exc_info:
        PixelCompositor().compose(
            foreground,
            mask,
            _png("RGB", (64, 64), "white"),
            _transform(rotation_degrees=Decimal("5.1")),
            (64, 64),
        )
    assert exc_info.value.code == "foreground_transform_invalid"


def test_crop_is_rejected() -> None:
    foreground, mask = _foreground()
    with pytest.raises(DomainRuleError) as exc_info:
        PixelCompositor().compose(
            foreground,
            mask,
            _png("RGB", (32, 32), "white"),
            _transform(translate_x=20),
            (32, 32),
        )
    assert exc_info.value.code == "foreground_out_of_frame"


def test_recolored_or_unrecomputable_valid_png_is_rejected() -> None:
    foreground, mask = _foreground()
    background = _png("RGB", (64, 64), "white")
    compositor = PixelCompositor()
    composition = compositor.compose(
        foreground, mask, background, _transform(), (64, 64)
    )
    image = Image.open(BytesIO(composition.png_bytes)).convert("RGBA")
    image.putpixel((32, 32), (255, 0, 255, 255))
    output = BytesIO()
    image.save(output, format="PNG")
    forged = replace(composition, png_bytes=output.getvalue())

    with pytest.raises(DomainRuleError) as exc_info:
        compositor.verify(forged, foreground, mask, background)
    assert exc_info.value.code == "pixel_preservation_verification_failed"
