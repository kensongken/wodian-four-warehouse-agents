from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from hashlib import sha256
from io import BytesIO
from uuid import UUID

import pytest
from PIL import Image

from opc_style.domain.errors import DomainRuleError
from opc_style.domain.product_foreground import (
    EditedForeground,
    ExtractedForeground,
    ForegroundExtractionRequest,
    ForegroundStageReceipt,
    ForegroundTransform,
    MaskEditMetrics,
    compute_mask_edit_metrics,
    foreground_manifest_sha256,
    foreground_source_quality_flags,
    mask_edit_manifest_sha256,
    mask_pixels_sha256,
    normalize_edited_mask,
    rebuild_foreground_from_original,
    validate_extracted_foreground,
    validate_foreground_transform,
)


def _png(mode: str, size: tuple[int, int], *, box: tuple[int, int, int, int]) -> bytes:
    image = Image.new(mode, size, 0 if mode == "L" else (0, 0, 0, 0))
    fill = 255 if mode == "L" else (170, 40, 20, 255)
    for x in range(box[0], box[2]):
        for y in range(box[1], box[3]):
            image.putpixel((x, y), fill)
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _result(
    *,
    size: tuple[int, int] = (8, 8),
    foreground_size: tuple[int, int] | None = None,
    mask_box: tuple[int, int, int, int] = (2, 2, 6, 6),
) -> ExtractedForeground:
    foreground = _png("RGBA", foreground_size or size, box=mask_box)
    mask = _png("L", size, box=mask_box)
    return ExtractedForeground(
        foreground_png=foreground,
        foreground_sha256=sha256(foreground).hexdigest(),
        mask_png=mask,
        mask_sha256=sha256(mask).hexdigest(),
        width=size[0],
        height=size[1],
        extractor_version="fake-v1",
    )


def _request() -> ForegroundExtractionRequest:
    source = b"verified-source-image"
    return ForegroundExtractionRequest(
        customer_id=UUID("00000000-0000-0000-0000-000000000001"),
        asset_id=UUID("00000000-0000-0000-0000-000000000002"),
        product_id="bracelet-1",
        source_content=source,
        source_content_sha256=sha256(source).hexdigest(),
        mime_type="image/png",
        width=8,
        height=8,
    )


def test_valid_foreground_requires_matching_rgba_and_mask_dimensions() -> None:
    quality = validate_extracted_foreground(_result())

    assert quality.foreground_pixel_count == 16
    assert quality.total_pixel_count == 64
    assert quality.canvas_width == 8
    assert quality.canvas_height == 8
    assert quality.bounding_box == (2, 2, 6, 6)
    assert quality.touches_edge is False


def test_foreground_source_quality_flags_small_subject_for_primary_image() -> None:
    small = validate_extracted_foreground(
        _result(size=(100, 80), mask_box=(35, 25, 65, 55))
    )
    adequate = validate_extracted_foreground(
        _result(size=(100, 80), mask_box=(20, 10, 80, 70))
    )

    assert foreground_source_quality_flags(small) == (
        "subject_too_small_for_primary_image",
    )
    assert foreground_source_quality_flags(adequate) == ()

    with pytest.raises(ValueError):
        foreground_source_quality_flags(adequate, minimum_subject_span_ratio=1)

    with pytest.raises(DomainRuleError) as exc_info:
        validate_extracted_foreground(_result(foreground_size=(7, 8)))
    assert exc_info.value.code == "foreground_mask_invalid"


@pytest.mark.parametrize("mask_box", [(0, 0, 0, 0), (0, 0, 8, 8)])
def test_foreground_rejects_empty_or_full_mask(
    mask_box: tuple[int, int, int, int],
) -> None:
    with pytest.raises(DomainRuleError) as exc_info:
        validate_extracted_foreground(_result(mask_box=mask_box))
    assert exc_info.value.code == "foreground_mask_invalid"


def test_foreground_flags_subject_touching_canvas_edge_for_human_review() -> None:
    quality = validate_extracted_foreground(_result(mask_box=(0, 2, 6, 6)))

    assert quality.touches_edge is True
    assert quality.bounding_box == (0, 2, 6, 6)


def _pipeline_result(*, mismatched_support: bool = False) -> ExtractedForeground:
    size = (8, 8)
    coarse = _mask_png(size, {(x, y) for x in range(2, 6) for y in range(2, 6)})
    alpha_image = Image.new("L", size, 0)
    for y in range(2, 6):
        for x in range(2, 6):
            alpha_image.putpixel((x, y), 128 if x in {2, 5} else 255)
    alpha_output = BytesIO()
    alpha_image.save(alpha_output, format="PNG")
    alpha = alpha_output.getvalue()
    foreground = Image.new("RGBA", size, (170, 40, 20, 0))
    foreground.putalpha(alpha_image)
    if mismatched_support:
        foreground.putpixel((1, 1), (170, 40, 20, 1))
    foreground_output = BytesIO()
    foreground.save(foreground_output, format="PNG")
    foreground_bytes = foreground_output.getvalue()
    return ExtractedForeground(
        foreground_png=foreground_bytes,
        foreground_sha256=sha256(foreground_bytes).hexdigest(),
        mask_png=alpha,
        mask_sha256=sha256(alpha).hexdigest(),
        width=8,
        height=8,
        extractor_version="local-sam2-birefnet-v1:123456789abc",
        coarse_mask_png=coarse,
        coarse_mask_sha256=sha256(coarse).hexdigest(),
        stage_receipts=(
            ForegroundStageReceipt(
                role="coarse_segmenter",
                artifact_sha256="a" * 64,
                output_sha256=sha256(coarse).hexdigest(),
                parameters_sha256="b" * 64,
            ),
            ForegroundStageReceipt(
                role="alpha_refiner",
                artifact_sha256="c" * 64,
                output_sha256=sha256(alpha).hexdigest(),
                parameters_sha256="d" * 64,
            ),
        ),
        pipeline_manifest_sha256="e" * 64,
    )


def test_extracted_foreground_accepts_soft_alpha_bound_to_refined_support() -> None:
    quality = validate_extracted_foreground(_pipeline_result())

    assert quality.foreground_pixel_count == 16


def test_extracted_foreground_rejects_alpha_foreground_support_mismatch() -> None:
    with pytest.raises(DomainRuleError) as error:
        validate_extracted_foreground(_pipeline_result(mismatched_support=True))

    assert error.value.code == "foreground_mask_invalid"


def test_extracted_foreground_rejects_missing_stage_receipt() -> None:
    with pytest.raises(DomainRuleError) as error:
        validate_extracted_foreground(replace(_pipeline_result(), stage_receipts=()))

    assert error.value.code == "foreground_pipeline_evidence_invalid"


def test_transform_allows_only_uniform_scale_translation_and_five_degree_rotation() -> (
    None
):
    validate_foreground_transform(
        ForegroundTransform(
            translate_x=-12,
            translate_y=24,
            uniform_scale=Decimal(4),
            rotation_degrees=Decimal(-5),
        )
    )
    validate_foreground_transform(
        ForegroundTransform(
            translate_x=0,
            translate_y=0,
            uniform_scale=Decimal("0.01"),
            rotation_degrees=Decimal(5),
        )
    )

    for invalid in (
        ForegroundTransform(0, 0, Decimal(0), Decimal(0)),
        ForegroundTransform(0, 0, Decimal("4.01"), Decimal(0)),
        ForegroundTransform(0, 0, Decimal(1), Decimal("5.01")),
    ):
        with pytest.raises(DomainRuleError) as exc_info:
            validate_foreground_transform(invalid)
        assert exc_info.value.code == "foreground_transform_invalid"


def test_foreground_payload_hash_is_stable() -> None:
    request = _request()
    result = _result()

    first = foreground_manifest_sha256(request, result)
    second = foreground_manifest_sha256(request, result)

    assert first == second
    assert len(first) == 64
    changed = replace(result, extractor_version="fake-v2")
    assert foreground_manifest_sha256(request, changed) != first


def test_foreground_payload_hash_binds_pipeline_and_stage_evidence() -> None:
    request = _request()
    result = _pipeline_result()
    original = foreground_manifest_sha256(request, result)

    changed_pipeline = replace(result, pipeline_manifest_sha256="0" * 64)
    changed_stage = replace(
        result,
        stage_receipts=(
            replace(result.stage_receipts[0], parameters_sha256="0" * 64),
            result.stage_receipts[1],
        ),
    )

    assert foreground_manifest_sha256(request, changed_pipeline) != original
    assert foreground_manifest_sha256(request, changed_stage) != original


def test_foreground_request_rejects_unverified_source_digest() -> None:
    request = replace(_request(), source_content_sha256="0" * 64)

    with pytest.raises(DomainRuleError) as exc_info:
        foreground_manifest_sha256(request, _result())
    assert exc_info.value.code == "foreground_source_incomplete"


def _mask_png(size: tuple[int, int], points: set[tuple[int, int]]) -> bytes:
    image = Image.new("L", size, 0)
    for point in points:
        image.putpixel(point, 255)
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _rgb_source(size: tuple[int, int]) -> tuple[bytes, Image.Image]:
    image = Image.new("RGB", size)
    for y in range(size[1]):
        for x in range(size[0]):
            image.putpixel((x, y), (x * 17 + 3, y * 19 + 5, (x + y) * 11 + 7))
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue(), image


def test_edited_foreground_rgb_is_rebuilt_from_original_pixels() -> None:
    size = (8, 8)
    source_bytes, source = _rgb_source(size)
    edited_points = {(2, 2), (3, 2), (2, 3), (3, 3)}
    normalized = normalize_edited_mask(_mask_png(size, edited_points), *size)

    result = rebuild_foreground_from_original(source_bytes, normalized, *size)

    assert isinstance(result, EditedForeground)
    with Image.open(BytesIO(result.foreground_png)) as foreground:
        foreground.load()
        assert foreground.mode == "RGBA"
        for point in edited_points:
            assert foreground.getpixel(point)[:3] == source.getpixel(point)
            assert foreground.getpixel(point)[3] == 255
        assert foreground.getpixel((0, 0))[3] == 0
    with Image.open(BytesIO(result.mask_png)) as mask:
        mask.load()
        assert set(mask.get_flattened_data()) == {0, 255}


def test_mask_edit_metrics_recompute_added_removed_components_and_ratio() -> None:
    size = (10, 10)
    base = _mask_png(size, {(2, 2), (3, 3), (6, 6), (7, 6)})
    edited = _mask_png(
        size,
        {(2, 2), (3, 3), (4, 4), (7, 6), (7, 7), (8, 8)},
    )

    metrics = compute_mask_edit_metrics(base, edited, *size)

    assert isinstance(metrics, MaskEditMetrics)
    assert metrics.added_pixel_count == 3
    assert metrics.deleted_pixel_count == 1
    assert metrics.result_pixel_count == 6
    assert metrics.component_count == 2
    assert metrics.change_ratio == pytest.approx(1.0)
    assert metrics.large_change_warning is True
    assert metrics.touches_edge is False


@pytest.mark.parametrize(
    ("case", "mask_bytes", "width", "height", "base_bytes", "error_code"),
    [
        ("empty", _mask_png((8, 8), set()), 8, 8, None, "foreground_edit_invalid"),
        (
            "full",
            _mask_png((8, 8), {(x, y) for x in range(8) for y in range(8)}),
            8,
            8,
            None,
            "foreground_edit_invalid",
        ),
        (
            "wrong-size",
            _mask_png((7, 8), {(2, 2)}),
            8,
            8,
            None,
            "foreground_edit_invalid",
        ),
        (
            "unchanged",
            _mask_png((8, 8), {(2, 2), (3, 2)}),
            8,
            8,
            _mask_png((8, 8), {(2, 2), (3, 2)}),
            "foreground_edit_invalid",
        ),
        (
            "edge-touching",
            _mask_png((8, 8), {(0, 2), (1, 2)}),
            8,
            8,
            _mask_png((8, 8), {(2, 2), (3, 2)}),
            "foreground_edit_touches_edge",
        ),
    ],
)
def test_mask_edit_rejects_empty_full_unchanged_wrong_size_and_edge_touching_masks(
    case: str,
    mask_bytes: bytes,
    width: int,
    height: int,
    base_bytes: bytes | None,
    error_code: str,
) -> None:
    del case
    with pytest.raises(DomainRuleError) as exc_info:
        if base_bytes is None:
            normalize_edited_mask(mask_bytes, width, height)
        else:
            compute_mask_edit_metrics(base_bytes, mask_bytes, width, height)
    assert exc_info.value.code == error_code


def test_mask_edit_manifest_binds_base_mask_result_mask_editor_and_operation_counts() -> (
    None
):
    base = {
        "customer_id": "00000000-0000-0000-0000-000000000001",
        "asset_id": "00000000-0000-0000-0000-000000000002",
        "base_version_id": "00000000-0000-0000-0000-000000000003",
        "source_content_sha256": "a" * 64,
        "base_manifest_sha256": "b" * 64,
        "base_mask_sha256": "c" * 64,
        "result_mask_pixels_sha256": "d" * 64,
        "editor_version": "human-mask-editor-v1",
        "magic_wand_count": 1,
        "erase_stroke_count": 2,
        "restore_stroke_count": 3,
    }

    digest = mask_edit_manifest_sha256(**base)

    assert len(digest) == 64
    for key, changed in (
        ("base_mask_sha256", "e" * 64),
        ("result_mask_pixels_sha256", "f" * 64),
        ("erase_stroke_count", 4),
    ):
        assert mask_edit_manifest_sha256(**(base | {key: changed})) != digest
    with pytest.raises(DomainRuleError) as exc_info:
        mask_edit_manifest_sha256(**(base | {"editor_version": "human-mask-editor-v2"}))
    assert exc_info.value.code == "foreground_edit_manifest_mismatch"


def test_mask_pixel_digest_is_stable_across_equivalent_png_encodings() -> None:
    image = Image.new("L", (8, 8), 0)
    for y in range(2, 6):
        for x in range(2, 6):
            image.putpixel((x, y), 255)
    fast, small = BytesIO(), BytesIO()
    image.save(fast, format="PNG", compress_level=0)
    image.save(small, format="PNG", compress_level=9)

    assert fast.getvalue() != small.getvalue()
    assert mask_pixels_sha256(fast.getvalue(), 8, 8) == mask_pixels_sha256(
        small.getvalue(), 8, 8
    )
