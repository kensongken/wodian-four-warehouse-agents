import asyncio
import json
import os
import subprocess
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from product_engine.services.image_recognition import (
    MaterialCandidate,
    RecognitionFailure,
    VisionRecognitionResult,
)


class LocalVisionClassification(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    identifier: str
    confidence: float = Field(ge=0, le=1)


class LocalVisionObservation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    classifications: list[LocalVisionClassification]
    dominant_color: Literal[
        "green",
        "red",
        "orange",
        "white",
        "black",
        "blue",
        "brown",
        "multicolor",
        "unknown",
    ]
    brightness: Literal["light", "medium", "dark"]
    image_width: int = Field(gt=0)
    image_height: int = Field(gt=0)


VisionRunner = Callable[[bytes, str], Awaitable[LocalVisionObservation]]


def ensure_local_vision_executable(source: Path, output: Path) -> Path:
    if not source.is_file():
        raise RecognitionFailure("local_vision_unavailable")
    if output.is_file() and output.stat().st_mtime >= source.stat().st_mtime:
        return output

    output.parent.mkdir(parents=True, exist_ok=True)
    module_cache = output.parent / ".module-cache"
    module_cache.mkdir(parents=True, exist_ok=True)
    environment = {
        **os.environ,
        "CLANG_MODULE_CACHE_PATH": str(module_cache),
        "SWIFT_MODULECACHE_PATH": str(module_cache),
    }
    try:
        completed = subprocess.run(
            ["xcrun", "swiftc", "-O", str(source), "-o", str(output)],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            env=environment,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as error:
        raise RecognitionFailure("local_vision_unavailable") from error
    if completed.returncode != 0 or not output.is_file():
        raise RecognitionFailure("local_vision_unavailable")
    output.chmod(0o755)
    return output


async def run_local_vision(
    executable: Path,
    content: bytes,
    mime_type: str,
    *,
    timeout_seconds: float = 15,
) -> LocalVisionObservation:
    if not executable.is_file():
        raise RecognitionFailure("local_vision_unavailable")
    suffix = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/heic": ".heic",
        "image/heif": ".heif",
        "image/x-portable-pixmap": ".ppm",
    }.get(mime_type, ".image")
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as stream:
            stream.write(content)
            temporary_path = Path(stream.name)

        def execute() -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [str(executable), str(temporary_path)],
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )

        try:
            completed = await asyncio.to_thread(execute)
        except subprocess.TimeoutExpired as error:
            raise RecognitionFailure("local_vision_timeout") from error
        if completed.returncode != 0:
            raise RecognitionFailure("local_vision_unsupported_image")
        try:
            payload = json.loads(completed.stdout)
            return LocalVisionObservation.model_validate(payload)
        except (json.JSONDecodeError, ValueError) as error:
            raise RecognitionFailure("local_vision_invalid_response") from error
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


FORM_LABELS = {
    "bracelet": "手串",
    "bangle": "手镯",
    "necklace": "项链",
    "ring": "戒指",
    "earring": "耳饰",
    "earrings": "耳饰",
    "bead": "散珠",
    "beads": "散珠",
}

COLOR_LABELS = {
    "green": "绿色",
    "red": "红色",
    "orange": "橙红色",
    "white": "浅色",
    "black": "深色",
    "blue": "蓝色",
    "brown": "棕色",
    "multicolor": "多色",
}

COLOR_MATERIAL_CANDIDATES = {
    color: f"{label}宝石或矿物类候选" for color, label in COLOR_LABELS.items()
}

BRIGHTNESS_LABELS = {"light": "明亮", "medium": "中等", "dark": "偏暗"}


def _color_label(observation: LocalVisionObservation) -> str:
    return COLOR_LABELS.get(observation.dominant_color, "中性混合色")


def _composition_label(observation: LocalVisionObservation) -> str:
    ratio = observation.image_width / observation.image_height
    if ratio >= 1.15:
        return "横向近景构图"
    if ratio <= 0.87:
        return "竖向近景构图"
    return "方形近景构图"


class OnDeviceVisionProvider:
    provider: Literal["apple_vision"] = "apple_vision"
    model = "apple-vision-vnclassify-v1"

    def __init__(self, runner: VisionRunner) -> None:
        self._runner = runner

    @classmethod
    def from_executable(cls, executable: Path) -> "OnDeviceVisionProvider":
        async def executable_runner(
            content: bytes,
            mime_type: str,
        ) -> LocalVisionObservation:
            return await run_local_vision(executable, content, mime_type)

        return cls(executable_runner)

    @staticmethod
    def _form(observation: LocalVisionObservation) -> str:
        for classification in observation.classifications:
            normalized = classification.identifier.strip().lower()
            if normalized in FORM_LABELS:
                return FORM_LABELS[normalized]
        return "珠饰类商品候选"

    async def recognize(self, content: bytes, mime_type: str) -> VisionRecognitionResult:
        try:
            observation = await self._runner(content, mime_type)
        except RecognitionFailure:
            raise

        form = self._form(observation)
        color_label = _color_label(observation)
        brightness_label = BRIGHTNESS_LABELS[observation.brightness]
        composition_label = _composition_label(observation)
        material_label = f"{color_label}宝石或矿物类候选"
        visible_observations = [
            (
                f"{color_label}调明显"
                if observation.dominant_color != "unknown"
                else f"{color_label}调"
            ),
            f"{brightness_label}明暗",
            composition_label,
        ]
        if form in {"手串", "手镯", "戒指"}:
            visible_observations.append("本机视觉识别到手饰主体")
        else:
            visible_observations.append(f"本机视觉识别到{form}主体")

        selling_points = [
            f"{color_label}调在图片中较醒目，便于展示整体视觉效果",
            f"实拍图能够直观展示{form}的{composition_label}整体轮廓",
        ]

        return VisionRecognitionResult(
            material_candidates=[
                MaterialCandidate(
                    label=material_label,
                    confidence_level="low",
                    visible_evidence="仅根据图片颜色与外观形成宽泛候选；具体材质需复核",
                )
            ],
            product_form_candidate=form,
            visual_observations=visible_observations,
            selling_point_drafts=selling_points,
            specification_candidates=["商品实际尺寸待测量"],
        )
