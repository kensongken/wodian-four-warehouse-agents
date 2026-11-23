from pathlib import Path

import pytest

from product_engine.services.image_recognition import RecognitionFailure
from product_engine.services.on_device_vision import (
    LocalVisionClassification,
    LocalVisionObservation,
    OnDeviceVisionProvider,
    ensure_local_vision_executable,
    run_local_vision,
)


def observation(
    *,
    identifiers: tuple[tuple[str, float], ...] = (
        ("bracelet", 0.82),
        ("jewelry", 0.71),
    ),
    color: str = "green",
) -> LocalVisionObservation:
    return LocalVisionObservation(
        classifications=[
            LocalVisionClassification(identifier=identifier, confidence=confidence)
            for identifier, confidence in identifiers
        ],
        dominant_color=color,
        brightness="medium",
        image_width=1200,
        image_height=1200,
    )


@pytest.mark.asyncio
async def test_maps_local_bracelet_and_green_pixels_to_reviewable_candidates() -> None:
    async def fake_runner(_: bytes, __: str) -> LocalVisionObservation:
        return observation()

    result = await OnDeviceVisionProvider(runner=fake_runner).recognize(
        b"controlled-image", "image/jpeg"
    )

    assert result.product_form_candidate == "手串"
    assert result.material_candidates[0].label == "绿色宝石或矿物类候选"
    assert result.material_candidates[0].confidence_level == "low"
    assert result.material_candidates[0].visible_evidence == (
        "仅根据图片颜色与外观形成宽泛候选；具体材质需复核"
    )
    assert "绿色调明显" in result.visual_observations
    assert "本机视觉识别到手饰主体" in result.visual_observations
    assert result.specification_candidates == ["商品实际尺寸待测量"]


@pytest.mark.asyncio
async def test_unknown_form_and_red_pixels_do_not_become_specific_material() -> None:
    async def fake_runner(_: bytes, __: str) -> LocalVisionObservation:
        return observation(identifiers=(("object", 0.91),), color="red")

    result = await OnDeviceVisionProvider(runner=fake_runner).recognize(
        b"controlled-image", "image/png"
    )

    assert result.product_form_candidate == "珠饰类商品候选"
    assert result.material_candidates[0].label == "红色宝石或矿物类候选"
    assert result.material_candidates[0].confidence_level == "low"
    assert "南红" not in result.model_dump_json()
    assert "真品" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_unknown_visual_labels_still_generate_complete_editable_baseline() -> None:
    async def fake_runner(_: bytes, __: str) -> LocalVisionObservation:
        return LocalVisionObservation(
            classifications=[LocalVisionClassification(identifier="object", confidence=0.91)],
            dominant_color="unknown",
            brightness="medium",
            image_width=900,
            image_height=1200,
        )

    result = await OnDeviceVisionProvider(runner=fake_runner).recognize(
        b"controlled-image", "image/jpeg"
    )

    assert result.material_candidates[0].label == "中性混合色宝石或矿物类候选"
    assert result.product_form_candidate == "珠饰类商品候选"
    assert len(result.visual_observations) >= 3
    assert "中性混合色调" in result.visual_observations
    assert "中等明暗" in result.visual_observations
    assert "竖向近景构图" in result.visual_observations
    assert len(result.selling_point_drafts) >= 2
    assert result.specification_candidates == ["商品实际尺寸待测量"]
    assert "待训练员确认" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_generated_baseline_never_contains_verification_or_value_claims() -> None:
    async def fake_runner(_: bytes, __: str) -> LocalVisionObservation:
        return observation(identifiers=(("bracelet", 0.91),), color="green")

    result = await OnDeviceVisionProvider(runner=fake_runner).recognize(
        b"controlled-image", "image/jpeg"
    )

    serialized = result.model_dump_json()
    for forbidden in ("真品", "天然", "产地", "保值", "升值", "固定售价"):
        assert forbidden not in serialized


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_code",
    ["local_vision_timeout", "local_vision_invalid_response"],
)
async def test_preserves_stable_local_runner_failures(error_code: str) -> None:
    async def failing_runner(_: bytes, __: str) -> LocalVisionObservation:
        raise RecognitionFailure(error_code)

    provider = OnDeviceVisionProvider(runner=failing_runner)

    with pytest.raises(RecognitionFailure, match=error_code):
        await provider.recognize(b"controlled-image", "image/jpeg")


@pytest.mark.asyncio
async def test_runs_executable_and_validates_literal_observation(tmp_path: Path) -> None:
    executable = tmp_path / "local-vision"
    executable.write_text(
        "#!/bin/sh\n"
        'printf \'%s\' \'{"classifications":[{"identifier":"bracelet",'
        '"confidence":0.82}],"dominant_color":"green",'
        '"brightness":"medium","image_width":640,"image_height":480}\'\n',
        encoding="utf-8",
    )
    executable.chmod(0o755)

    result = await run_local_vision(executable, b"image-bytes", "image/jpeg")

    assert result == LocalVisionObservation(
        classifications=[LocalVisionClassification(identifier="bracelet", confidence=0.82)],
        dominant_color="green",
        brightness="medium",
        image_width=640,
        image_height=480,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("script", "timeout_seconds", "expected_code"),
    [
        ("#!/bin/sh\nprintf '%s' 'not-json'\n", 1.0, "local_vision_invalid_response"),
        ("#!/bin/sh\nexit 4\n", 1.0, "local_vision_unsupported_image"),
        ("#!/bin/sh\nsleep 1\n", 0.05, "local_vision_timeout"),
    ],
)
async def test_runner_converts_process_failures_to_stable_codes(
    tmp_path: Path,
    script: str,
    timeout_seconds: float,
    expected_code: str,
) -> None:
    executable = tmp_path / "local-vision"
    executable.write_text(script, encoding="utf-8")
    executable.chmod(0o755)

    with pytest.raises(RecognitionFailure, match=expected_code):
        await run_local_vision(
            executable,
            b"image-bytes",
            "image/png",
            timeout_seconds=timeout_seconds,
        )


def test_build_rejects_missing_swift_source(tmp_path: Path) -> None:
    with pytest.raises(RecognitionFailure, match="local_vision_unavailable"):
        ensure_local_vision_executable(
            tmp_path / "missing.swift",
            tmp_path / "bin" / "local-vision",
        )
