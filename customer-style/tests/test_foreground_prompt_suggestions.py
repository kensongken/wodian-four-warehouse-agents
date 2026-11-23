from __future__ import annotations

from io import BytesIO

import pytest
from PIL import Image, ImageDraw

from opc_style.domain.errors import DomainRuleError
from opc_style.services.foreground_prompt_suggestions import suggest_flat_lay_prompt


def _image_bytes(*, saturated_background: bool = False) -> bytes:
    background = (20, 80, 180) if saturated_background else (205, 205, 202)
    image = Image.new("RGB", (600, 600), background)
    draw = ImageDraw.Draw(image)
    if not saturated_background:
        draw.ellipse((115, 235, 175, 295), fill=(190, 35, 25))
        draw.line((170, 265, 270, 200), fill=(210, 155, 25), width=10)
        draw.ellipse((310, 370, 370, 430), fill=(190, 35, 25))
        draw.line((365, 400, 470, 335), fill=(210, 155, 25), width=10)
        draw.ellipse((460, -80, 680, 150), fill=(245, 245, 242))
    output = BytesIO()
    image.save(output, format="JPEG", quality=95)
    return output.getvalue()


def test_flat_lay_suggestion_finds_two_products_without_the_white_prop() -> None:
    suggestion = suggest_flat_lay_prompt(_image_bytes())

    assert suggestion.method == "flat-lay-color-components-v1"
    assert suggestion.component_count == 2
    assert suggestion.review_required is True
    assert suggestion.hints.negative_points == ()
    assert suggestion.hints.box is not None
    left, top, right, bottom = suggestion.hints.box
    assert 0.1 < left < 0.25
    assert 0.25 < top < 0.4
    assert 0.7 < right < 0.85
    assert 0.7 < bottom < 0.8
    assert len(suggestion.hints.positive_points) == 2
    assert (
        suggestion.hints.positive_points[0][1] < suggestion.hints.positive_points[1][1]
    )


def test_invalid_image_has_no_prompt_suggestion() -> None:
    with pytest.raises(
        DomainRuleError,
        match="foreground_benchmark_prompt_suggestion_unavailable",
    ):
        suggest_flat_lay_prompt(b"not-an-image")


def test_saturated_background_is_rejected_instead_of_becoming_a_full_frame_box() -> (
    None
):
    with pytest.raises(
        DomainRuleError,
        match="foreground_benchmark_prompt_suggestion_unavailable",
    ):
        suggest_flat_lay_prompt(_image_bytes(saturated_background=True))
