"""Bounded, approval-gated Qwen3-VL suggestions over authorized representative frames."""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import math
import os
import re
import signal
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol, Sequence

from artifact_registry import ArtifactRegistry, ArtifactRegistryError
from protocol import BINDING_KEYS, ProtocolError, validate_job
from runtime_integrity import RuntimeExpectation, RuntimeIntegrityError, validate_runtime_process
from stage_telemetry import (
    DISABLED_STAGE_TELEMETRY,
    StageTelemetryError,
    StageTelemetryWriter,
)
from technical import MediaAnalysisError, _scene_intervals


MAX_FRAMES = 12
MAX_FRAME_EVIDENCE_WINDOW_MS = 4_000
HIGHLIGHT_FRAME_EVIDENCE_WINDOW_MS = 2_000
MAX_FRAME_LONG_EDGE = 448
MAX_FRAME_CHANNELS = 4
MAX_TOTAL_FRAME_BYTES = MAX_FRAMES * MAX_FRAME_LONG_EDGE * MAX_FRAME_LONG_EDGE * MAX_FRAME_CHANNELS
MAX_OUTPUT_TOKENS = 512
HIGHLIGHT_OUTPUT_TOKENS = 384
MAX_SUGGESTED_TAGS = 6
MAX_VISUAL_OBSERVATIONS = 6
MAX_DEMAND_EVIDENCE_CANDIDATES = 2
MAX_WARNING_ITEMS = 4
VISION_TIMEOUT_SECONDS = 300
VISION_RESTART_JOBS = 50
MAX_OUTPUT_BYTES = 64 * 1024
MAX_TRANSCRIPT_SPANS = 64
MAX_TRANSCRIPT_TEXT_BYTES = 16 * 1024
MAX_DEMAND_TEXT_BYTES = 4 * 1024
MAX_OUTPUT_TEXT_BYTES = 32 * 1024
PROMPT_VERSION = "studio-qwen3-vl-zh-json-v5"
STRUCTURED_OUTPUT_SCHEMA_VERSION = "studio-vision-json-schema-v2"
HIGHLIGHT_PROMPT_VERSION = "studio-qwen3-vl-highlight-pair-json-v19"
HIGHLIGHT_SCHEMA_VERSION = "studio-highlight-pair-json-schema-v8"
HIGHLIGHT_OPERATION_PREFIX = "editorial-highlight-"
VISUAL_EVIDENCE_KINDS = frozenset({
    "visible-person",
    "visible-product",
    "visible-action",
    "visible-scene",
})
HIGHLIGHT_SUBJECT_LABELS = {
    "bead-strands": "珠串陈列",
    "jewelry-display": "饰品陈列",
    "raw-stone": "原石",
    "person": "人物",
    "aircraft": "飞机",
    "building": "建筑",
    "natural-scene": "自然景物",
}
HIGHLIGHT_ADVANTAGE_TEXT = {
    "subject-larger": ("所选画面的主体占比更大", "主体细节更便于人工核对"),
    "less-occluded": ("所选画面的主体遮挡更少", "主体形态保留得更完整"),
    "sharper-outline": ("所选画面的主体轮廓更清楚", "主体边缘更便于人工核对"),
    "stronger-color-contrast": ("所选画面的主体色彩对比更明显", "主体与背景更容易区分"),
    "more-complete-composition": ("所选画面的主体构图更完整", "主体没有被画面边缘明显截断"),
}

VISION_OUTPUT_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "suggestedTags": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": MAX_SUGGESTED_TAGS,
        },
        "visualObservations": {
            "type": "array",
            "maxItems": MAX_VISUAL_OBSERVATIONS,
            "items": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": [
                            "visible-person",
                            "visible-product",
                            "visible-action",
                            "visible-scene",
                        ],
                    },
                    "text": {"type": "string"},
                    "frameId": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": ["kind", "text", "frameId", "confidence"],
                "additionalProperties": False,
            },
        },
        "demandEvidenceCandidates": {
            "type": "array",
            "maxItems": MAX_DEMAND_EVIDENCE_CANDIDATES,
            "items": {
                "type": "object",
                "properties": {
                    "demandText": {"type": "string"},
                    "observation": {"type": "string"},
                    "frameId": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": [
                    "demandText",
                    "observation",
                    "frameId",
                    "confidence",
                ],
                "additionalProperties": False,
            },
        },
        "highlightCandidates": {
            "type": "array",
            "maxItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string"},
                    "frameId": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": ["reason", "frameId", "confidence"],
                "additionalProperties": False,
            },
        },
        "forbiddenContentWarnings": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": MAX_WARNING_ITEMS,
        },
        "uncertainty": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": MAX_WARNING_ITEMS,
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "personIdentity": {"type": ["string", "number", "null"]},
        "productMaterial": {"type": ["string", "number", "null"]},
        "productPrice": {"type": ["string", "number", "null"]},
        "ownership": {"type": ["string", "number", "null"]},
        "permission": {"type": ["string", "number", "null"]},
    },
    "required": [
        "suggestedTags",
        "visualObservations",
        "demandEvidenceCandidates",
        "highlightCandidates",
        "forbiddenContentWarnings",
        "uncertainty",
        "confidence",
    ],
    "additionalProperties": False,
}

HIGHLIGHT_OUTPUT_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "highlightCandidates": {
            "type": "array",
            "maxItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "preferredFrame": {"type": "string", "enum": ["first", "second"]},
                    "subjectKind": {"type": "string", "enum": list(HIGHLIGHT_SUBJECT_LABELS)},
                    "visibleAdvantage": {"type": "string", "enum": list(HIGHLIGHT_ADVANTAGE_TEXT)},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": [
                    "preferredFrame", "subjectKind", "visibleAdvantage", "confidence"
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["highlightCandidates"],
    "additionalProperties": False,
}

PROMPT_TEMPLATE = """你是本机私有内容素材的视觉初筛助手。仅返回严格 JSON，不要 Markdown、代码围栏或解释。
所有标签、观察和需求证据都只是待人工复核的建议。禁止确认人物身份、商品材质、价格、所有权或使用权限；
若模型产生这些推测，只能放入相应的未确认声明字段，系统会降级为 pending。
输出必须严格包含 suggestedTags、visualObservations、demandEvidenceCandidates、highlightCandidates、forbiddenContentWarnings、
uncertainty、confidence；可选 personIdentity、productMaterial、productPrice、ownership、permission。
suggestedTags 输出 1—4 个简短且不重复的标签；每个代表帧时间窗最多输出 1 条 visualObservations，
总数最多 6 条，只保留直接可见且有用的观察；demandEvidenceCandidates 最多 2 条，uncertainty 最多 3 条。
visualObservations 的每一项必须严格包含 kind、text、frameId、confidence；kind 只能是
visible-person、visible-product、visible-action、visible-scene 之一，禁止输出情绪、动机、身份、材质、价格、归属或权限类型。
visible-product 只用于画面明确以展示或售卖为目的的独立商品；飞机、车辆、建筑及其部件作为环境或交通场景出现时，必须归为 visible-scene，不得归为 visible-product。
每个观察和证据只能填写 frames 中已经存在的 frameId；禁止自行输出或推算 startMs/endMs，服务器会把 frameId 换算为完整时间窗。
不能跨时间窗合并，也不能根据单张代表帧编造更精细的秒点。不要输出像素、路径、原文件或额外字段。
highlightCandidates 是你比较所有代表帧后真正推荐保留的高光时间窗，最多 3 条；每条严格包含 reason、frameId、confidence，
且 frameId 必须来自 frames。reason 只能写直接可见的相对优势，例如色彩更丰富、主体更清楚、构图变化更明显；
不能写观众情绪或商业效果。如果各时间窗没有足够可见差异，必须返回空数组，禁止为了凑数选择。
输入上下文：{context_json}
"""

HIGHLIGHT_PROMPT_TEMPLATE = """系统已固定两张代表帧供你二选一。比较两张画面直接可见的差异，选择更值得保留的一张，只返回严格 JSON。
无候选时返回：{{"highlightCandidates":[]}}。
有候选时严格返回：{{"highlightCandidates":[{{"preferredFrame":"first","subjectKind":"bead-strands","visibleAdvantage":"subject-larger","confidence":0.8}}]}}。
preferredFrame 只能是 first 或 second。
subjectKind 只能是 bead-strands、jewelry-display、raw-stone、person、aircraft、building、natural-scene。
visibleAdvantage 只能是 subject-larger、less-occluded、sharper-outline、stronger-color-contrast、more-complete-composition。
highlightCandidates 最多 1 条，禁止输出任何自由文本、说明、帧编号、秒数、Markdown 或额外字段；完整 JSON 必须闭合后再结束输出。
必须忽略视频上叠加的字幕、标题、水印和文案；它们不能作为主体、差异或保留理由。
只根据实物主体的占比、遮挡、轮廓、色彩对比或构图完整度选择。如果除文字外看不出明确优势，返回空数组。
禁止推测人物身份、地点、材质、价格、所有权、许可、情绪、动机或商业效果。
输入：{context_json}
"""


class VisionAnalysisError(ValueError):
    """Stable vision failure that never includes model output, media, or local paths."""

    def __init__(self, code: str, *, restart_required: bool = False):
        super().__init__(code)
        self.restart_required = restart_required


class VisionModel(Protocol):
    def generate(
        self,
        *,
        prompt: str,
        frames: Sequence[Any],
        max_tokens: int,
        temperature: float,
        timeout_seconds: float,
    ) -> str: ...


VisionContextProvider = Callable[[dict[str, Any]], dict[str, Any]]
VisionRuntimeValidator = Callable[[], None]
VisionModelLoader = Callable[[str, str, VisionRuntimeValidator], VisionModel]
_MODEL_CACHE: dict[tuple[str, ...], VisionModel] = {}
_MODEL_EXECUTION_LOCK = threading.Lock()


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _digest_file(path: str) -> str:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise VisionAnalysisError("studio-vision-media-unavailable") from error
    return digest.hexdigest()


def _pixel_bytes(pixels: Any) -> bytes:
    try:
        if isinstance(pixels, (bytes, bytearray, memoryview)):
            payload = bytes(pixels)
        else:
            converter = getattr(pixels, "tobytes", None)
            if not callable(converter):
                raise TypeError
            payload = converter()
        if not isinstance(payload, bytes) or not payload:
            raise TypeError
        return payload
    except Exception as error:
        raise ProtocolError("studio-vision-input-invalid") from error


def _select_scenes(scenes: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(scenes) <= MAX_FRAMES:
        return list(scenes)
    positions = {
        round(index * (len(scenes) - 1) / (MAX_FRAMES - 1))
        for index in range(MAX_FRAMES)
    }
    return [scenes[index] for index in sorted(positions)]


def _localized_scene_windows(
    scenes: Sequence[dict[str, Any]],
    maximum_window_ms: int = MAX_FRAME_EVIDENCE_WINDOW_MS,
) -> list[dict[str, Any]]:
    """Splits long scenes into bounded evidence windows without inventing frame precision."""
    windows: list[dict[str, Any]] = []
    for scene in scenes:
        start, end = scene["startMs"], scene["endMs"]
        duration = end - start
        count = max(1, math.ceil(duration / maximum_window_ms))
        for index in range(count):
            window_start = start + math.floor(duration * index / count)
            window_end = start + math.floor(duration * (index + 1) / count)
            representative = window_start + (window_end - window_start) // 2
            unchanged = count == 1 and representative == scene["representativeFrameMs"]
            windows.append({
                "startMs": window_start,
                "endMs": window_end,
                "representativeFrameMs": representative,
                "representativeFrameDigest": (
                    scene["representativeFrameDigest"] if unchanged else None
                ),
            })
    return windows


def _default_context(job: dict[str, Any]) -> dict[str, Any]:
    try:
        import cv2  # type: ignore[import-not-found]
    except ImportError as error:
        raise VisionAnalysisError("studio-vision-dependency-unavailable") from error
    selected = _select_scenes(_localized_scene_windows(
        _scene_intervals(job),
        HIGHLIGHT_FRAME_EVIDENCE_WINDOW_MS if _is_highlight_only(job)
        else MAX_FRAME_EVIDENCE_WINDOW_MS,
    ))
    capture = cv2.VideoCapture(job["sourcePath"])
    if not capture.isOpened():
        capture.release()
        raise VisionAnalysisError("studio-vision-media-invalid")
    frames = []
    try:
        for scene in selected:
            capture.set(cv2.CAP_PROP_POS_MSEC, scene["representativeFrameMs"])
            ok, original = capture.read()
            if not ok or original is None:
                raise VisionAnalysisError("studio-vision-frame-extraction-failed")
            original_bytes = _pixel_bytes(original)
            if (
                scene["representativeFrameDigest"] is not None
                and hashlib.sha256(original_bytes).hexdigest() != scene["representativeFrameDigest"]
            ):
                raise VisionAnalysisError("studio-vision-frame-digest-mismatch")
            try:
                height, width = int(original.shape[0]), int(original.shape[1])
            except (AttributeError, IndexError, TypeError, ValueError) as error:
                raise VisionAnalysisError("studio-vision-frame-invalid") from error
            long_edge = max(width, height)
            pixels = original
            if long_edge > MAX_FRAME_LONG_EDGE:
                scale = MAX_FRAME_LONG_EDGE / long_edge
                width = max(1, round(width * scale))
                height = max(1, round(height * scale))
                pixels = cv2.resize(original, (width, height), interpolation=cv2.INTER_AREA)
            pixels = cv2.cvtColor(pixels, cv2.COLOR_BGR2RGB)
            frames.append({
                "sourceDigest": job["sourceDigest"],
                "startMs": scene["startMs"],
                "endMs": scene["endMs"],
                "representativeFrameMs": scene["representativeFrameMs"],
                "representativeFrameDigest": hashlib.sha256(_pixel_bytes(pixels)).hexdigest(),
                "width": width,
                "height": height,
                "pixels": pixels,
            })
    finally:
        capture.release()
    return {
        "sourceAssetId": job["sourceAssetId"],
        "sourceDigest": job["sourceDigest"],
        "sourceStartMs": job["sourceStartMs"],
        "sourceEndMs": job["sourceEndMs"],
        "frames": frames,
        "transcriptSpans": [],
        "demand": None,
    }


def trusted_context_provider(job: dict[str, Any]) -> dict[str, Any]:
    """Adds server-resolved transcript/demand metadata to locally extracted frames."""
    base = _default_context(job)
    raw = os.environ.get("STUDIO_VISION_CONTEXT_JSON")
    if raw is None:
        return base
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError, UnicodeError) as error:
        raise VisionAnalysisError("studio-vision-context-binding-mismatch") from error
    expected = {
        "customerId", "sessionId", "sourceAssetId", "sourceDigest", "sourceStartMs",
        "sourceEndMs", "transcriptSpans", "demand",
    }
    if (
        not isinstance(value, dict)
        or set(value) != expected
        or value["customerId"] != job["customerId"]
        or value["sessionId"] != job["sessionId"]
        or any(value[key] != job[key] for key in (
            "sourceAssetId", "sourceDigest", "sourceStartMs", "sourceEndMs"
        ))
    ):
        raise VisionAnalysisError("studio-vision-context-binding-mismatch")
    demand = value["demand"]
    if demand is not None:
        expected_demand = {
            "demandId", "matchAssessmentId", "sourceDigest", "sourceStartMs", "sourceEndMs", "text",
        }
        if not isinstance(demand, dict) or set(demand) != expected_demand:
            raise VisionAnalysisError("studio-vision-context-binding-mismatch")
        demand = {
            key: demand[key]
            for key in ("sourceDigest", "sourceStartMs", "sourceEndMs", "text")
        }
    return {
        **base,
        "transcriptSpans": value["transcriptSpans"],
        "demand": demand,
    }


def _input_utf8_length(value: str) -> int:
    try:
        return len(value.encode("utf-8"))
    except UnicodeError as error:
        raise ProtocolError("studio-vision-input-invalid") from error


def _bounded_text(value: Any, *, maximum_bytes: int) -> str:
    if not isinstance(value, str):
        raise ProtocolError("studio-vision-input-invalid")
    normalized = value.strip()
    if (
        not normalized
        or _input_utf8_length(normalized) > maximum_bytes
        or any(ord(character) < 32 and character not in "\n\t" for character in normalized)
    ):
        raise ProtocolError("studio-vision-input-invalid")
    return normalized


def _validate_context(job: dict[str, Any], value: Any) -> dict[str, Any]:
    expected = {
        "sourceAssetId", "sourceDigest", "sourceStartMs", "sourceEndMs",
        "frames", "transcriptSpans", "demand",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise ProtocolError("studio-vision-input-invalid")
    if any(value[key] != job[key] for key in (
        "sourceAssetId", "sourceDigest", "sourceStartMs", "sourceEndMs"
    )):
        raise ProtocolError("studio-vision-input-invalid")
    frames_input = value["frames"]
    if not isinstance(frames_input, list) or not frames_input:
        raise ProtocolError("studio-vision-input-invalid")
    if len(frames_input) > MAX_FRAMES:
        raise ProtocolError("studio-vision-frame-limit")
    frames = []
    evidence = []
    total_frame_bytes = 0
    frame_keys = {
        "sourceDigest", "startMs", "endMs", "representativeFrameMs",
        "representativeFrameDigest", "width", "height", "pixels",
    }
    for frame_index, frame in enumerate(frames_input, start=1):
        if not isinstance(frame, dict) or set(frame) != frame_keys:
            raise ProtocolError("studio-vision-input-invalid")
        start, end, representative = frame["startMs"], frame["endMs"], frame["representativeFrameMs"]
        width, height = frame["width"], frame["height"]
        if (
            frame["sourceDigest"] != job["sourceDigest"]
            or type(start) is not int or type(end) is not int or type(representative) is not int
            or not job["sourceStartMs"] <= start < representative < end <= job["sourceEndMs"]
            or type(width) is not int or type(height) is not int or width <= 0 or height <= 0
        ):
            raise ProtocolError("studio-vision-input-invalid")
        if max(width, height) > MAX_FRAME_LONG_EDGE:
            raise ProtocolError("studio-vision-frame-size-limit")
        pixels = _pixel_bytes(frame["pixels"])
        if len(pixels) > width * height * MAX_FRAME_CHANNELS:
            raise ProtocolError("studio-vision-frame-byte-limit")
        total_frame_bytes += len(pixels)
        if total_frame_bytes > MAX_TOTAL_FRAME_BYTES:
            raise ProtocolError("studio-vision-frame-byte-limit")
        digest = hashlib.sha256(pixels).hexdigest()
        if frame["representativeFrameDigest"] != digest:
            raise ProtocolError("studio-vision-input-invalid")
        shape = getattr(frame["pixels"], "shape", None)
        if shape is not None and (len(shape) < 2 or int(shape[0]) != height or int(shape[1]) != width):
            raise ProtocolError("studio-vision-input-invalid")
        frames.append(frame["pixels"])
        evidence.append({
            "frameId": f"frame-{frame_index}",
            "sourceDigest": job["sourceDigest"],
            "startMs": start,
            "endMs": end,
            "representativeFrameMs": representative,
            "representativeFrameDigest": digest,
            "width": width,
            "height": height,
        })

    transcript_input = value["transcriptSpans"]
    if not isinstance(transcript_input, list) or len(transcript_input) > MAX_TRANSCRIPT_SPANS:
        raise ProtocolError("studio-vision-transcript-limit")
    transcript = []
    transcript_bytes = 0
    for span in transcript_input:
        if not isinstance(span, dict) or set(span) != {"sourceDigest", "startMs", "endMs", "text"}:
            raise ProtocolError("studio-vision-input-invalid")
        start, end = span["startMs"], span["endMs"]
        if (
            span["sourceDigest"] != job["sourceDigest"]
            or type(start) is not int or type(end) is not int
            or not job["sourceStartMs"] <= start < end <= job["sourceEndMs"]
        ):
            raise ProtocolError("studio-vision-input-invalid")
        text = _bounded_text(span["text"], maximum_bytes=2_000)
        transcript_bytes += _input_utf8_length(text)
        if transcript_bytes > MAX_TRANSCRIPT_TEXT_BYTES:
            raise ProtocolError("studio-vision-transcript-limit")
        transcript.append({
            "sourceDigest": job["sourceDigest"],
            "startMs": start,
            "endMs": end,
            "text": text,
        })

    demand_input = value["demand"]
    demand = None
    if demand_input is not None:
        if not isinstance(demand_input, dict) or set(demand_input) != {
            "sourceDigest", "sourceStartMs", "sourceEndMs", "text"
        }:
            raise ProtocolError("studio-vision-input-invalid")
        if (
            demand_input["sourceDigest"] != job["sourceDigest"]
            or demand_input["sourceStartMs"] != job["sourceStartMs"]
            or demand_input["sourceEndMs"] != job["sourceEndMs"]
        ):
            raise ProtocolError("studio-vision-input-invalid")
        demand = {
            "sourceDigest": job["sourceDigest"],
            "sourceStartMs": job["sourceStartMs"],
            "sourceEndMs": job["sourceEndMs"],
            "text": _bounded_text(demand_input["text"], maximum_bytes=MAX_DEMAND_TEXT_BYTES),
        }
    return {
        "sourceBinding": {
            "sourceAssetId": job["sourceAssetId"],
            "sourceDigest": job["sourceDigest"],
            "sourceStartMs": job["sourceStartMs"],
            "sourceEndMs": job["sourceEndMs"],
        },
        "frames": frames,
        "frameEvidence": evidence,
        "transcriptSpans": transcript,
        "demand": demand,
    }


class _MlxVisionModel:
    def __init__(
        self,
        model: Any,
        processor: Any,
        generate_function: Callable[..., Any],
        structured_output_factory: Callable[[Any, dict[str, Any]], Any] | None = None,
    ):
        self.model = model
        self.processor = processor
        self.generate_function = generate_function
        self.structured_output_factory = structured_output_factory

    def generate(self, **arguments: Any) -> str:
        stage_boundary = arguments.pop("stage_boundary", None)
        output_schema = arguments.pop("output_schema", VISION_OUTPUT_JSON_SCHEMA)
        frames = arguments["frames"]
        model_frames = []
        for frame in frames:
            shape = getattr(frame, "shape", None)
            if shape is None:
                model_frames.append(frame)
                continue
            transpose = getattr(frame, "transpose", None)
            if len(shape) != 3 or int(shape[2]) not in (1, 3, 4) or not callable(transpose):
                raise VisionAnalysisError("studio-vision-model-interface-invalid")
            converted = transpose(2, 0, 1)
            copy = getattr(converted, "copy", None)
            if not callable(copy):
                raise VisionAnalysisError("studio-vision-model-interface-invalid")
            model_frames.append(copy())
        apply_chat_template = getattr(self.processor, "apply_chat_template", None)
        if not callable(apply_chat_template):
            raise VisionAnalysisError("studio-vision-model-interface-invalid")
        prompt = apply_chat_template(
            [{
                "role": "user",
                "content": [
                    *({"type": "image"} for _frame in frames),
                    {"type": "text", "text": arguments["prompt"]},
                ],
            }],
            tokenize=False,
            add_generation_prompt=True,
        )
        if not isinstance(prompt, str) or not prompt:
            raise VisionAnalysisError("studio-vision-model-interface-invalid")
        tokenizer = getattr(self.processor, "tokenizer", self.processor)
        if not callable(self.structured_output_factory):
            raise VisionAnalysisError("studio-vision-structured-output-unavailable")
        try:
            structured_output_processor = self.structured_output_factory(
                tokenizer,
                output_schema,
            )
        except Exception as error:
            raise VisionAnalysisError("studio-vision-structured-output-unavailable") from error
        if not callable(structured_output_processor):
            raise VisionAnalysisError("studio-vision-structured-output-unavailable")
        if callable(stage_boundary):
            stage_boundary()
        result = self.generate_function(
            self.model,
            self.processor,
            prompt,
            model_frames,
            max_tokens=arguments["max_tokens"],
            temperature=arguments["temperature"],
            logits_processors=[structured_output_processor],
            verbose=False,
        )
        if isinstance(result, str):
            return result
        text = getattr(result, "text", None)
        if not isinstance(text, str):
            raise VisionAnalysisError("studio-vision-model-interface-invalid")
        return text


def _loaded_mlx_module_files() -> dict[str, Path]:
    loaded = {
        name: module
        for name, module in tuple(sys.modules.items())
        if name == "mlx_vlm" or name.startswith("mlx_vlm.")
    }
    if not loaded:
        return {}
    if "mlx_vlm" not in loaded:
        raise VisionAnalysisError("studio-vision-runtime-origin-invalid")
    files = {}
    for name, module in loaded.items():
        module_file = getattr(module, "__file__", None)
        if not isinstance(module_file, str):
            raise VisionAnalysisError("studio-vision-runtime-origin-invalid")
        files[name] = Path(module_file)
    return files


def _load_mlx_model(
    checkpoint_path: str,
    runtime_root: str,
    validate_runtime: VisionRuntimeValidator,
) -> VisionModel:
    runtime = Path(runtime_root).resolve(strict=True)
    package_root = runtime / "site-packages" if (runtime / "site-packages").is_dir() else runtime
    module_path = (package_root / "mlx_vlm/__init__.py").resolve(strict=True)
    imported_here = "mlx_vlm" not in sys.modules
    previous_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        validate_runtime()
        if imported_here:
            module_path.relative_to(runtime)
            spec = importlib.util.spec_from_file_location(
                "mlx_vlm",
                module_path,
                submodule_search_locations=[str(module_path.parent)],
            )
            if spec is None or spec.loader is None:
                raise ImportError
            module = importlib.util.module_from_spec(spec)
            sys.modules["mlx_vlm"] = module
            spec.loader.exec_module(module)
        else:
            module = sys.modules["mlx_vlm"]
        validate_runtime()
        generate = getattr(module, "generate")
        load = getattr(module, "load")
        structured_module = importlib.import_module("mlx_vlm.structured")
        structured_output_factory = getattr(
            structured_module,
            "build_json_schema_logits_processor",
        )
        if (
            not callable(generate)
            or not callable(load)
            or not callable(structured_output_factory)
        ):
            raise TypeError
        model, processor = load(str(Path(checkpoint_path).parent))
        validate_runtime()
    except Exception as error:
        if imported_here:
            for name in tuple(sys.modules):
                if name == "mlx_vlm" or name.startswith("mlx_vlm."):
                    sys.modules.pop(name, None)
        if isinstance(error, VisionAnalysisError):
            raise
        if isinstance(error, (AttributeError, ImportError, OSError, TypeError, ValueError)):
            raise VisionAnalysisError("studio-vision-dependency-unavailable") from error
        raise VisionAnalysisError("studio-vision-model-unavailable") from error
    finally:
        sys.dont_write_bytecode = previous_bytecode
    return _MlxVisionModel(model, processor, generate, structured_output_factory)


def _cached_model(
    artifact: dict[str, str],
    runtime: dict[str, str],
    loader: VisionModelLoader,
    validate_runtime: VisionRuntimeValidator,
) -> VisionModel:
    key = (
        artifact["id"], artifact["version"], artifact["sha256"], artifact["path"],
        runtime["id"], runtime["version"], runtime["sha256"], runtime["path"],
    )
    model = _MODEL_CACHE.get(key)
    if model is None:
        try:
            model = loader(artifact["path"], runtime["path"], validate_runtime)
            validate_runtime()
            if not callable(getattr(model, "generate", None)):
                raise VisionAnalysisError("studio-vision-model-invalid")
        except VisionAnalysisError:
            raise
        except Exception as error:
            raise VisionAnalysisError("studio-vision-model-unavailable") from error
        _MODEL_CACHE[key] = model
    return model


@contextmanager
def _deadline(seconds: float):
    if seconds <= 0:
        raise VisionAnalysisError("studio-vision-model-timeout")
    can_signal = threading.current_thread() is threading.main_thread() and hasattr(signal, "setitimer")
    if not can_signal:
        yield
        return
    previous_handler = signal.getsignal(signal.SIGALRM)

    def timed_out(_signum: int, _frame: Any) -> None:
        raise VisionAnalysisError("studio-vision-model-timeout")

    signal.signal(signal.SIGALRM, timed_out)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        signal.setitimer(signal.ITIMER_REAL, *previous_timer)


def _is_highlight_only(job: dict[str, Any]) -> bool:
    return str(job.get("operationKey", "")).startswith(HIGHLIGHT_OPERATION_PREFIX)


def _prompt_version(job: dict[str, Any]) -> str:
    return HIGHLIGHT_PROMPT_VERSION if _is_highlight_only(job) else PROMPT_VERSION


def _highlight_frame_score(pixels: Any) -> float:
    shape = getattr(pixels, "shape", None)
    reshape = getattr(pixels, "reshape", None)
    if shape is None or len(shape) != 3 or int(shape[2]) < 3 or not callable(reshape):
        return 0.0
    try:
        channels = int(shape[2])
        flattened = reshape(-1, channels)
        rgb = flattened[:, :3]
        means = rgb.mean(axis=0)
        brightness = rgb.mean(axis=1)
        contrast = float(brightness.std())
        color_spread = float(max(means) - min(means))
    except (AttributeError, IndexError, TypeError, ValueError, OverflowError):
        return 0.0
    if not math.isfinite(contrast) or not math.isfinite(color_spread):
        return 0.0
    return round(contrast + color_spread, 6)


def _prefilter_highlight_context(context: dict[str, Any], maximum: int = 5) -> dict[str, Any]:
    frames = context["frames"]
    evidence = context["frameEvidence"]
    if len(frames) != len(evidence):
        raise VisionAnalysisError("studio-vision-context-binding-mismatch")
    if len(frames) <= maximum:
        selected_indices = list(range(len(frames)))
    else:
        scores = [_highlight_frame_score(frame) for frame in frames]
        selected_indices = []
        for bucket in range(maximum):
            bucket_start = math.floor(len(frames) * bucket / maximum)
            bucket_end = math.floor(len(frames) * (bucket + 1) / maximum)
            selected_indices.append(max(
                range(bucket_start, bucket_end),
                key=lambda index: (scores[index], -index),
            ))
    selected_frames = [frames[index] for index in selected_indices]
    source_evidence = [evidence[index] for index in selected_indices]
    selected_evidence = [
        {**item, "frameId": f"frame-{position}"}
        for position, item in enumerate(source_evidence, start=1)
    ]
    return {
        **context,
        "frames": selected_frames,
        "frameEvidence": selected_evidence,
        "highlightPrefilter": {
            "algorithm": "temporal-bucket-salience-v2",
            "inputFrameCount": len(frames),
            "selectedFrameCount": len(selected_indices),
            "selectedFrameIds": [item["frameId"] for item in selected_evidence],
            "selectedSourceFrameIds": [item["frameId"] for item in source_evidence],
        },
    }


def _highlight_frame_signature(pixels: Any) -> tuple[float, ...]:
    shape = getattr(pixels, "shape", None)
    reshape = getattr(pixels, "reshape", None)
    if shape is None or len(shape) != 3 or int(shape[2]) < 3 or not callable(reshape):
        return (0.0,) * 54
    try:
        height, width, channels = (int(shape[0]), int(shape[1]), int(shape[2]))
        rgb = reshape(-1, channels)[:, :3]
        means = rgb.mean(axis=0)
        deviations = rgb.std(axis=0)
        samples = []
        for y_step in range(4):
            y = min(height - 1, round((height - 1) * y_step / 3))
            for x_step in range(4):
                x = min(width - 1, round((width - 1) * x_step / 3))
                samples.extend(float(value) for value in pixels[y, x, :3])
        signature = tuple(float(value) for value in (*means, *deviations, *samples))
    except (AttributeError, IndexError, TypeError, ValueError, OverflowError):
        return (0.0,) * 54
    return signature if all(math.isfinite(value) for value in signature) else (0.0,) * 6


def _select_highlight_pair_context(context: dict[str, Any]) -> dict[str, Any]:
    frames = context["frames"]
    evidence = context["frameEvidence"]
    if len(frames) != len(evidence):
        raise VisionAnalysisError("studio-vision-context-binding-mismatch")
    if len(frames) < 2:
        raise VisionAnalysisError("studio-highlight-comparison-frame-unavailable")
    signatures = [_highlight_frame_signature(frame) for frame in frames]
    pair = max(
        ((left, right) for left in range(len(frames)) for right in range(left + 1, len(frames))),
        key=lambda indices: (
            sum(
                (signatures[indices[0]][offset] - signatures[indices[1]][offset]) ** 2
                for offset in range(len(signatures[0]))
            ),
            -indices[0],
            -indices[1],
        ),
    )
    left, right = pair
    if _highlight_frame_score(frames[right]) > _highlight_frame_score(frames[left]):
        selected, comparison = right, left
    else:
        selected, comparison = left, right
    pair_indices = [selected, comparison]
    pair_frames = [frames[index] for index in pair_indices]
    pair_evidence = [evidence[index] for index in pair_indices]
    return {
        **context,
        "frames": pair_frames,
        "frameEvidence": pair_evidence,
        "highlightPair": {
            "algorithm": "maximum-visible-signature-distance-v1",
            "candidateFrameId": pair_evidence[0]["frameId"],
            "comparisonFrameId": pair_evidence[1]["frameId"],
            "candidateStartMs": pair_evidence[0]["startMs"],
            "candidateEndMs": pair_evidence[0]["endMs"],
            "candidateRepresentativeFrameMs": pair_evidence[0]["representativeFrameMs"],
            "comparisonStartMs": pair_evidence[1]["startMs"],
            "comparisonEndMs": pair_evidence[1]["endMs"],
            "comparisonRepresentativeFrameMs": pair_evidence[1]["representativeFrameMs"],
        },
    }


def _prompt(context: dict[str, Any], *, highlight_only: bool = False) -> str:
    if highlight_only:
        safe_context = {
            "candidateFrame": context["frameEvidence"][0],
            "comparisonFrame": context["frameEvidence"][1],
        }
        return HIGHLIGHT_PROMPT_TEMPLATE.format(context_json=json.dumps(
            safe_context, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ))
    safe_context = {
        "sourceBinding": context["sourceBinding"],
        "frames": context["frameEvidence"],
        "transcriptSpans": context["transcriptSpans"],
        "demand": context["demand"],
    }
    return PROMPT_TEMPLATE.format(context_json=json.dumps(
        safe_context, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ))


def _output_text(
    value: Any,
    *,
    schema_failure_code: str,
    maximum_bytes: int = 1_000,
) -> str:
    if not isinstance(value, str):
        raise VisionAnalysisError(schema_failure_code)
    result = value.strip()
    if (
        not result
        or _output_utf8_length(result, failure_code=schema_failure_code) > maximum_bytes
    ):
        raise VisionAnalysisError(schema_failure_code)
    return result


def _output_utf8_length(value: str, *, failure_code: str) -> int:
    try:
        return len(value.encode("utf-8"))
    except UnicodeError as error:
        raise VisionAnalysisError(failure_code) from error


def _output_number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise VisionAnalysisError(
            "studio-vision-output-schema-scalar-confidence-optional-claim-shape"
        )
    try:
        result = float(value)
    except OverflowError as error:
        raise VisionAnalysisError(
            "studio-vision-output-invalid-number",
            restart_required=True,
        ) from error
    if not math.isfinite(result) or not 0 <= result <= 1:
        raise VisionAnalysisError("studio-vision-output-invalid-number", restart_required=True)
    return result


def _output_interval(
    value: dict[str, Any],
    job: dict[str, Any],
    frames: Sequence[dict[str, Any]],
    *,
    schema_failure_code: str,
) -> tuple[int, int]:
    frame_id = value.get("frameId")
    if not isinstance(frame_id, str):
        raise VisionAnalysisError(schema_failure_code)
    matches = [frame for frame in frames if frame.get("frameId") == frame_id]
    if len(matches) != 1:
        raise VisionAnalysisError(schema_failure_code)
    frame = matches[0]
    start, end = frame["startMs"], frame["endMs"]
    if not job["sourceStartMs"] <= start < end <= job["sourceEndMs"]:
        raise VisionAnalysisError(schema_failure_code)
    return start, end


def _parse_model_output(raw: Any, job: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, str):
        raise VisionAnalysisError("studio-vision-output-invalid-json")
    if _output_utf8_length(
        raw,
        failure_code="studio-vision-output-invalid-json",
    ) > MAX_OUTPUT_BYTES:
        raise VisionAnalysisError("studio-vision-output-too-large")

    def invalid_constant(_value: str) -> None:
        raise VisionAnalysisError("studio-vision-output-invalid-number", restart_required=True)

    def bounded_integer(value: str) -> int:
        try:
            return int(value)
        except ValueError as error:
            raise VisionAnalysisError(
                "studio-vision-output-invalid-number",
                restart_required=True,
            ) from error

    try:
        value = json.loads(
            raw,
            parse_constant=invalid_constant,
            parse_int=bounded_integer,
        )
    except VisionAnalysisError:
        raise
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise VisionAnalysisError("studio-vision-output-invalid-json") from error
    required = {
        "suggestedTags", "visualObservations", "demandEvidenceCandidates", "highlightCandidates",
        "forbiddenContentWarnings", "uncertainty", "confidence",
    }
    claims = {"personIdentity", "productMaterial", "productPrice", "ownership", "permission"}
    if not isinstance(value, dict) or not required.issubset(value) or not set(value) <= required | claims:
        raise VisionAnalysisError("studio-vision-output-schema-top-level-object-keys")
    if not isinstance(value["suggestedTags"], list) or len(value["suggestedTags"]) > 32:
        raise VisionAnalysisError(
            "studio-vision-output-schema-suggested-tag-shape-content-duplication"
        )
    suggested_tags_raw = [
        _output_text(
            tag,
            schema_failure_code=(
                "studio-vision-output-schema-suggested-tag-shape-content-duplication"
            ),
            maximum_bytes=200,
        )
        for tag in value["suggestedTags"]
    ]
    # Tags are untrusted suggestions. Exact duplicates add no evidence, so
    # collapse them deterministically and retain a warning instead of losing
    # every otherwise valid visual observation in the receipt.
    suggested_tags = list(dict.fromkeys(suggested_tags_raw))
    normalization_warnings = (
        ["duplicate-suggested-tags-deduplicated"]
        if len(suggested_tags) != len(suggested_tags_raw)
        else []
    )

    observations = []
    rows = value["visualObservations"]
    if not isinstance(rows, list) or len(rows) > 64:
        raise VisionAnalysisError("studio-vision-output-schema-visual-observation-shape-kind")
    for row in rows:
        if (
            not isinstance(row, dict)
            or set(row) != {"kind", "text", "frameId", "confidence"}
            or not isinstance(row["kind"], str)
            or row["kind"] not in VISUAL_EVIDENCE_KINDS
        ):
            raise VisionAnalysisError(
                "studio-vision-output-schema-visual-observation-shape-kind"
            )
        start, end = _output_interval(
            row,
            job,
            context["frameEvidence"],
            schema_failure_code=(
                "studio-vision-output-schema-visual-observation-interval"
            ),
        )
        observations.append({
            "kind": row["kind"],
            "text": _output_text(
                row["text"],
                schema_failure_code=(
                    "studio-vision-output-schema-visual-observation-shape-kind"
                ),
            ),
            "startMs": start,
            "endMs": end,
            "confidence": _output_number(row["confidence"]),
            "status": "pending-human-confirmation",
        })

    demand_candidates = []
    rows = value["demandEvidenceCandidates"]
    if not isinstance(rows, list) or len(rows) > 32:
        raise VisionAnalysisError(
            "studio-vision-output-schema-demand-evidence-shape-trusted-text"
        )
    demand_warnings = []
    if context["demand"] is None:
        if rows:
            demand_warnings.append("untrusted-demand-evidence-discarded")
        rows = []
    for row in rows:
        expected = {"demandText", "observation", "frameId", "confidence"}
        if (
            not isinstance(row, dict)
            or set(row) != expected
            or row["demandText"] != context["demand"]["text"]
        ):
            demand_warnings.append("untrusted-demand-evidence-discarded")
            continue
        try:
            start, end = _output_interval(
                row,
                job,
                context["frameEvidence"],
                schema_failure_code=(
                    "studio-vision-output-schema-demand-evidence-shape-trusted-text"
                ),
            )
            observation = _output_text(
                row["observation"],
                schema_failure_code=(
                    "studio-vision-output-schema-demand-evidence-shape-trusted-text"
                ),
            )
            candidate_confidence = _output_number(row["confidence"])
        except VisionAnalysisError:
            demand_warnings.append("untrusted-demand-evidence-discarded")
            continue
        demand_candidates.append({
            "demandText": context["demand"]["text"],
            "observation": observation,
            "startMs": start,
            "endMs": end,
            "confidence": candidate_confidence,
            "status": "pending-human-confirmation",
        })

    highlight_candidates = []
    rows = value["highlightCandidates"]
    if not isinstance(rows, list) or len(rows) > 3:
        raise VisionAnalysisError("studio-vision-output-schema-highlight-candidate-shape")
    seen_highlight_windows = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"reason", "frameId", "confidence"}:
            raise VisionAnalysisError("studio-vision-output-schema-highlight-candidate-shape")
        start, end = _output_interval(
            row,
            job,
            context["frameEvidence"],
            schema_failure_code="studio-vision-output-schema-highlight-candidate-interval",
        )
        window = (start, end)
        if window in seen_highlight_windows:
            if "duplicate-highlight-candidates-deduplicated" not in normalization_warnings:
                normalization_warnings.append("duplicate-highlight-candidates-deduplicated")
            continue
        seen_highlight_windows.add(window)
        highlight_candidates.append({
            "reason": _output_text(
                row["reason"],
                schema_failure_code="studio-vision-output-schema-highlight-candidate-shape",
                maximum_bytes=500,
            ),
            "startMs": start,
            "endMs": end,
            "confidence": _output_number(row["confidence"]),
            "status": "pending-human-confirmation",
        })

    def text_array(name: str, maximum: int) -> list[str]:
        rows = value[name]
        if not isinstance(rows, list) or len(rows) > maximum:
            raise VisionAnalysisError(
                "studio-vision-output-schema-warning-uncertainty-text-arrays"
            )
        return [
            _output_text(
                row,
                schema_failure_code=(
                    "studio-vision-output-schema-warning-uncertainty-text-arrays"
                ),
                maximum_bytes=500,
            )
            for row in rows
        ]

    forbidden = text_array("forbiddenContentWarnings", 32)
    uncertainty = text_array("uncertainty", 32)
    confidence = _output_number(value["confidence"])
    pending = []
    claim_map = {
        "personIdentity": ("identity", "unsupported-identity-claim"),
        "productMaterial": ("material", "unsupported-material-claim"),
        "productPrice": ("price", "unsupported-price-claim"),
        "ownership": ("ownership", "unsupported-ownership-claim"),
        "permission": ("permission", "unsupported-permission-claim"),
    }
    claim_warnings = []
    for field, (kind, warning) in claim_map.items():
        if field not in value or value[field] is None:
            continue
        claim = value[field]
        if isinstance(claim, bool) or not isinstance(claim, (str, int, float)):
            raise VisionAnalysisError(
                "studio-vision-output-schema-scalar-confidence-optional-claim-shape"
            )
        if isinstance(claim, float) and not math.isfinite(claim):
            raise VisionAnalysisError("studio-vision-output-invalid-number", restart_required=True)
        pending.append({
            "kind": kind,
            "value": _output_text(
                str(claim),
                schema_failure_code=(
                    "studio-vision-output-schema-scalar-confidence-optional-claim-shape"
                ),
                maximum_bytes=500,
            ),
            "status": "pending-human-confirmation",
        })
        claim_warnings.append(warning)
    total_text = sum(_output_utf8_length(
        item,
        failure_code="studio-vision-output-invalid-json",
    ) for item in (
        suggested_tags
        + [row["text"] for row in observations]
        + [row["demandText"] for row in demand_candidates]
        + [row["observation"] for row in demand_candidates]
        + [row["reason"] for row in highlight_candidates]
        + forbidden + uncertainty
        + [row["value"] for row in pending]
    ))
    if total_text > MAX_OUTPUT_TEXT_BYTES:
        raise VisionAnalysisError("studio-vision-output-too-large")
    return {
        "suggestedTags": suggested_tags,
        "visualObservations": observations,
        "demandEvidenceCandidates": demand_candidates,
        "highlightCandidates": highlight_candidates,
        "forbiddenContentWarnings": forbidden,
        "uncertainty": uncertainty,
        "confidence": confidence,
        "pendingObservations": pending,
        "claimWarnings": claim_warnings,
        "demandWarnings": demand_warnings,
        "normalizationWarnings": normalization_warnings,
    }


def _decode_highlight_json(raw: str) -> Any:
    candidate = raw.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*\n?(.*?)\n?```", candidate, flags=re.DOTALL | re.IGNORECASE)
    if fenced is not None:
        candidate = fenced.group(1).strip()
    try:
        return json.loads(candidate)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise VisionAnalysisError("studio-vision-output-invalid-json") from error


def _parse_highlight_output(raw: Any, job: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, str) or _output_utf8_length(
        raw,
        failure_code="studio-vision-output-invalid-json",
    ) > MAX_OUTPUT_BYTES:
        raise VisionAnalysisError("studio-vision-output-invalid-json")
    value = _decode_highlight_json(raw)
    if not isinstance(value, dict) or set(value) != {"highlightCandidates"}:
        raise VisionAnalysisError("studio-highlight-output-schema-top-level")
    rows = value["highlightCandidates"]
    if not isinstance(rows, list) or len(rows) > 1:
        raise VisionAnalysisError("studio-vision-output-schema-highlight-candidate-shape")
    candidates = []
    for row in rows:
        expected_fields = {
            "preferredFrame", "subjectKind", "visibleAdvantage", "confidence"
        }
        if not isinstance(row, dict) or set(row) != expected_fields:
            raise VisionAnalysisError("studio-vision-output-schema-highlight-candidate-shape")
        subject_kind = row["subjectKind"]
        visible_advantage = row["visibleAdvantage"]
        if subject_kind not in HIGHLIGHT_SUBJECT_LABELS or visible_advantage not in HIGHLIGHT_ADVANTAGE_TEXT:
            raise VisionAnalysisError("studio-vision-output-schema-highlight-candidate-shape")
        subject = HIGHLIGHT_SUBJECT_LABELS[subject_kind]
        visual_change, retention_value = HIGHLIGHT_ADVANTAGE_TEXT[visible_advantage]
        explanation = {
            "subject": subject,
            "action": f"画面中可见{subject}",
            "visualChange": visual_change,
            "retentionValue": retention_value,
        }
        preference = row["preferredFrame"]
        if preference not in ("first", "second"):
            raise VisionAnalysisError("studio-vision-output-schema-highlight-candidate-shape")
        first_evidence, second_evidence = context["frameEvidence"]
        candidate_evidence, comparison_evidence = (
            (first_evidence, second_evidence)
            if preference == "first"
            else (second_evidence, first_evidence)
        )
        context["highlightPair"] = {
            **context["highlightPair"],
            "candidateFrameId": candidate_evidence["frameId"],
            "comparisonFrameId": comparison_evidence["frameId"],
            "candidateStartMs": candidate_evidence["startMs"],
            "candidateEndMs": candidate_evidence["endMs"],
            "candidateRepresentativeFrameMs": candidate_evidence["representativeFrameMs"],
            "comparisonStartMs": comparison_evidence["startMs"],
            "comparisonEndMs": comparison_evidence["endMs"],
            "comparisonRepresentativeFrameMs": comparison_evidence["representativeFrameMs"],
            "modelPreferredFrame": preference,
        }
        candidates.append({
            **explanation,
            "subjectKind": subject_kind,
            "visibleAdvantage": visible_advantage,
            "modelPreferredFrame": preference,
            "reason": "；".join((
                f"主体：{explanation['subject']}",
                f"动作：{explanation['action']}",
                f"变化：{explanation['visualChange']}",
                f"保留：{explanation['retentionValue']}",
            )),
            "startMs": candidate_evidence["startMs"],
            "endMs": candidate_evidence["endMs"],
            "comparisonStartMs": comparison_evidence["startMs"],
            "comparisonEndMs": comparison_evidence["endMs"],
            "confidence": _output_number(row["confidence"]),
            "status": "pending-human-confirmation",
        })
    return {
        "suggestedTags": [],
        "visualObservations": [],
        "demandEvidenceCandidates": [],
        "highlightCandidates": candidates,
        "forbiddenContentWarnings": [],
        "uncertainty": [],
        "pendingObservations": [],
        "claimWarnings": [],
        "demandWarnings": [],
        "normalizationWarnings": [],
        "confidence": max((item["confidence"] for item in candidates), default=0),
    }


def _specific_highlight_text(value: Any) -> str:
    result = _output_text(
        value,
        schema_failure_code="studio-highlight-output-generic-explanation",
        maximum_bytes=120,
    )
    normalized = "".join(result.lower().split()).strip("。！？!?,，；;：:")
    generic_values = {
        "标题", "标题文字", "文字", "画面", "画面内容", "内容", "主体",
        "动作", "变化", "很好", "精彩", "有吸引力", "好看", "不确定",
        "未知", "无", "没有", "none", "unknown", "n/a",
    }
    if normalized in generic_values or len(normalized) < 2:
        raise VisionAnalysisError("studio-highlight-output-generic-explanation")
    return result


def _reject_highlight_overlay_text(explanation: dict[str, str]) -> None:
    overlay_terms = (
        "字幕", "标题文字", "标题", "文案", "水印", "屏幕文字", "画面文字",
        "subtitle", "caption", "watermark", "on-screen text", "overlay text",
    )
    normalized = "\n".join(explanation.values()).lower()
    if any(term in normalized for term in overlay_terms):
        raise VisionAnalysisError("studio-highlight-output-overlay-text-dominant")


def _reject_highlight_candidate_direction(explanation: dict[str, str]) -> None:
    comparison_terms = (
        "第一张", "第二张", "候选帧", "对比帧",
        "first frame", "second frame", "candidate frame", "comparison frame",
    )
    candidate_only = "\n".join((explanation["action"], explanation["retentionValue"])).lower()
    if any(term in candidate_only for term in comparison_terms):
        raise VisionAnalysisError("studio-highlight-output-candidate-direction-invalid")


def _reject_highlight_temporal_claim(explanation: dict[str, str]) -> None:
    combined = "".join(explanation.values()).replace(" ", "")
    prohibited = (
        "正在夹取并放置", "过渡", "切换", "变成", "转为", "先做完再展示",
        "随后", "接着", "即将", "已经完成", "开始到结束",
    )
    if any(value in combined for value in prohibited):
        raise VisionAnalysisError("studio-highlight-output-temporal-overclaim")
    if "先" in combined and "后" in combined:
        raise VisionAnalysisError("studio-highlight-output-temporal-overclaim")


def _restart_signal(reason: str, count: int) -> dict[str, Any]:
    return {"required": True, "reason": reason, "completedVisionJobs": count}


def _receipt(
    job: dict[str, Any],
    *,
    artifact_id: str,
    model_version: str,
    checkpoint_digest: str | None,
    status: str,
    quality_signals: dict[str, Any],
    observations: list[str],
    suggested_tags: list[str],
    confidence: float,
    warnings: list[str],
    failure_code: str | None,
    started_at: str,
    runtime_ms: int,
    artifact_binding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    canonical_job = json.dumps(job, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "schemaVersion": 1,
        **{key: job[key] for key in BINDING_KEYS},
        "adapterName": "mlx-vlm-local",
        "modelName": artifact_id,
        "modelVersion": model_version,
        "checkpointDigest": checkpoint_digest,
        "inputDigest": hashlib.sha256(canonical_job.encode("utf-8")).hexdigest(),
        "promptVersion": _prompt_version(job),
        "observations": observations,
        "suggestedTags": suggested_tags,
        "qualitySignals": quality_signals,
        "confidence": confidence,
        "warnings": warnings,
        "startedAt": started_at,
        "completedAt": _timestamp(),
        "runtimeMs": runtime_ms,
        "status": status,
        "failure": None if failure_code is None else {"code": failure_code},
        "synthetic": artifact_id.startswith("synthetic-"),
        "providerMode": "local",
        "inputBinding": {
            "sourceDigest": job["sourceDigest"],
            "sourceStartMs": job["sourceStartMs"],
            "sourceEndMs": job["sourceEndMs"],
        },
        "artifactBinding": artifact_binding,
        "runtimeMetrics": {"runtimeMs": runtime_ms},
    }


def analyze_vision(
    job_input: Any,
    *,
    model_lock_path: str,
    model_root: str,
    runtime_artifact_id: str | None = None,
    runtime_package_path: str | None = None,
    runtime_expectation: RuntimeExpectation | None = None,
    qwen_artifact_id: str,
    checkpoint_path: str,
    context_provider: VisionContextProvider = _default_context,
    model_loader: VisionModelLoader = _load_mlx_model,
    vision_job_count: int = 1,
    inference_timeout_seconds: float = VISION_TIMEOUT_SECONDS,
    telemetry: StageTelemetryWriter = DISABLED_STAGE_TELEMETRY,
    runtime_preaccept_started: bool = False,
) -> dict[str, Any]:
    started_at = _timestamp()
    started = time.monotonic_ns()
    job = validate_job(job_input)
    highlight_only = _is_highlight_only(job)
    if job["layer"] != "vision":
        raise ProtocolError("studio-model-worker-invalid-job")
    if type(vision_job_count) is not int or vision_job_count <= 0:
        raise ProtocolError("studio-vision-job-count-invalid")
    if not runtime_preaccept_started:
        telemetry.started("runtime-preaccept")
    model_version = "unavailable"
    checkpoint_digest = None
    artifact_id = qwen_artifact_id
    try:
        registry = ArtifactRegistry(model_lock_path, model_root)
        if runtime_expectation is None:
            if runtime_artifact_id is None or runtime_package_path is None:
                raise ArtifactRegistryError("studio-runtime-artifact-unapproved")
            runtime = registry.resolve_ready_runtime_tree(
                runtime_artifact_id,
                runtime_package_path,
                package_name="mlx_vlm",
            )
        else:
            runtime = None
        artifact = registry.resolve_ready_artifact(
            qwen_artifact_id,
            checkpoint_path,
            executable=False,
            required_source_kind="weights",
        )
        artifact_id = artifact["id"]
        model_version = artifact["version"]
        checkpoint_digest = artifact["sha256"]

        formal_expectation = None
        if runtime_expectation is not None:
            module_files = runtime_expectation.module_files
            if module_files is not None:
                module_files = {**module_files, **_loaded_mlx_module_files()}
            formal_expectation = replace(
                runtime_expectation,
                registry=registry,
                model_sha256=artifact["sha256"],
                module_files=module_files,
            )
            identity = validate_runtime_process(formal_expectation)
            runtime = {
                "id": identity.artifact_id,
                "version": identity.version,
                "path": identity.root,
                "sha256": identity.tree_sha256,
            }

        def validate_runtime() -> None:
            try:
                if formal_expectation is not None:
                    module_files = formal_expectation.module_files
                    if module_files is not None:
                        module_files = {**module_files, **_loaded_mlx_module_files()}
                    current_identity = validate_runtime_process(replace(
                        formal_expectation,
                        module_files=module_files,
                    ))
                    current = {
                        "id": current_identity.artifact_id,
                        "version": current_identity.version,
                        "path": current_identity.root,
                        "sha256": current_identity.tree_sha256,
                    }
                else:
                    current = registry.validate_ready_runtime_modules(
                        runtime_artifact_id,
                        runtime_package_path,
                        package_name="mlx_vlm",
                        module_files=tuple(
                            str(path) for path in _loaded_mlx_module_files().values()
                        ),
                    )
            except (ArtifactRegistryError, RuntimeIntegrityError) as error:
                raise VisionAnalysisError(str(error)) from error
            if current != runtime:
                raise VisionAnalysisError("studio-model-artifact-digest-mismatch")

        def validate_runtime_and_model() -> None:
            validate_runtime()
            try:
                current_artifact = registry.resolve_ready_artifact(
                    qwen_artifact_id,
                    checkpoint_path,
                    executable=False,
                    required_source_kind="weights",
                )
            except ArtifactRegistryError as error:
                raise VisionAnalysisError(str(error)) from error
            if current_artifact != artifact:
                raise VisionAnalysisError("studio-model-artifact-digest-mismatch")

        validate_runtime()
        if _digest_file(job["sourcePath"]) != job["sourceDigest"]:
            raise VisionAnalysisError("studio-vision-source-digest-mismatch")
        telemetry.completed("runtime-preaccept")
        telemetry.started("frontstage-extraction")
    except (ArtifactRegistryError, RuntimeIntegrityError, VisionAnalysisError) as error:
        return _receipt(
            job,
            artifact_id=artifact_id,
            model_version=model_version,
            checkpoint_digest=checkpoint_digest,
            status="failed-manual-review",
            quality_signals={}, observations=[], suggested_tags=[], confidence=0,
            warnings=["vision-requires-human-review"], failure_code=str(error),
            started_at=started_at,
            runtime_ms=max(0, (time.monotonic_ns() - started) // 1_000_000),
        )

    try:
        raw_context = context_provider(job)
    except (VisionAnalysisError, MediaAnalysisError) as error:
        return _receipt(
            job,
            artifact_id=artifact_id,
            model_version=model_version,
            checkpoint_digest=checkpoint_digest,
            status="failed-manual-review",
            quality_signals={}, observations=[], suggested_tags=[], confidence=0,
            warnings=["vision-requires-human-review"], failure_code=str(error),
            started_at=started_at,
            runtime_ms=max(0, (time.monotonic_ns() - started) // 1_000_000),
        )
    except Exception:
        return _receipt(
            job,
            artifact_id=artifact_id,
            model_version=model_version,
            checkpoint_digest=checkpoint_digest,
            status="failed-manual-review",
            quality_signals={}, observations=[], suggested_tags=[], confidence=0,
            warnings=["vision-requires-human-review"],
            failure_code="studio-vision-frame-extraction-failed",
            started_at=started_at,
            runtime_ms=max(0, (time.monotonic_ns() - started) // 1_000_000),
        )
    context = _validate_context(job, raw_context)
    if highlight_only:
        context = _prefilter_highlight_context(context)
        try:
            context = _select_highlight_pair_context(context)
        except VisionAnalysisError as error:
            return _receipt(
                job,
                artifact_id=artifact_id,
                model_version=model_version,
                checkpoint_digest=checkpoint_digest,
                status="failed-manual-review",
                quality_signals={"highlightPrefilter": context["highlightPrefilter"]},
                observations=[], suggested_tags=[], confidence=0,
                warnings=["vision-output-requires-human-review"], failure_code=str(error),
                started_at=started_at,
                runtime_ms=max(0, (time.monotonic_ns() - started) // 1_000_000),
            )
    telemetry.completed("frontstage-extraction")
    telemetry.started("model-load")
    try:
        validate_runtime_and_model()
    except (ArtifactRegistryError, VisionAnalysisError) as error:
        return _receipt(
            job,
            artifact_id=artifact_id,
            model_version=model_version,
            checkpoint_digest=checkpoint_digest,
            status="failed-manual-review",
            quality_signals={}, observations=[], suggested_tags=[], confidence=0,
            warnings=["vision-requires-human-review"], failure_code=str(error),
            started_at=started_at,
            runtime_ms=max(0, (time.monotonic_ns() - started) // 1_000_000),
        )
    if _digest_file(job["sourcePath"]) != job["sourceDigest"]:
        return _receipt(
            job,
            artifact_id=artifact_id,
            model_version=model_version,
            checkpoint_digest=checkpoint_digest,
            status="failed-manual-review",
            quality_signals={}, observations=[], suggested_tags=[], confidence=0,
            warnings=["vision-requires-human-review"],
            failure_code="studio-vision-source-digest-mismatch",
            started_at=started_at,
            runtime_ms=max(0, (time.monotonic_ns() - started) // 1_000_000),
        )
    prompt = _prompt(context, highlight_only=highlight_only)
    try:
        acquired = _MODEL_EXECUTION_LOCK.acquire(timeout=inference_timeout_seconds)
        if not acquired:
            raise VisionAnalysisError("studio-vision-model-timeout")
        try:
            with _deadline(inference_timeout_seconds):
                model = _cached_model(artifact, runtime, model_loader, validate_runtime)
                telemetry.completed("model-load")
                telemetry.started("processor-prepare")
                generation_started = False

                def mark_generation_started() -> None:
                    nonlocal generation_started
                    if generation_started:
                        raise StageTelemetryError("studio-model-worker-telemetry-invalid")
                    telemetry.completed("processor-prepare")
                    telemetry.started("model-generation")
                    generation_started = True

                generation_arguments = {
                    "prompt": prompt,
                    "frames": context["frames"],
                    "max_tokens": HIGHLIGHT_OUTPUT_TOKENS if highlight_only else MAX_OUTPUT_TOKENS,
                    "temperature": 0,
                    "timeout_seconds": inference_timeout_seconds,
                }
                if highlight_only:
                    generation_arguments["output_schema"] = HIGHLIGHT_OUTPUT_JSON_SCHEMA
                telemetry_enabled = (
                    telemetry is not DISABLED_STAGE_TELEMETRY
                    and getattr(telemetry, "enabled", True)
                )
                if telemetry_enabled:
                    generation_arguments["stage_boundary"] = mark_generation_started
                raw_output = model.generate(**generation_arguments)
                if not generation_started:
                    mark_generation_started()
                telemetry.completed("model-generation")
                telemetry.started("output-validation")
                validate_runtime_and_model()
        finally:
            _MODEL_EXECUTION_LOCK.release()
        parsed = (
            _parse_highlight_output(raw_output, job, context)
            if highlight_only
            else _parse_model_output(raw_output, job, context)
        )
        telemetry.completed("output-validation")
    except StageTelemetryError:
        raise
    except VisionAnalysisError as error:
        restart = (
            _restart_signal("invalid-numeric-output", vision_job_count)
            if error.restart_required else None
        )
        quality = {"workerRestart": restart} if restart is not None else {}
        if highlight_only:
            quality["highlightPrefilter"] = context["highlightPrefilter"]
            quality["highlightPair"] = context["highlightPair"]
        warnings = ["vision-output-requires-human-review"]
        if restart is not None:
            warnings.append("vision-worker-restart-required")
        status = "failed-retryable" if str(error) == "studio-vision-model-timeout" else "failed-manual-review"
        return _receipt(
            job,
            artifact_id=artifact_id,
            model_version=model_version,
            checkpoint_digest=checkpoint_digest,
            status=status,
            quality_signals=quality, observations=[], suggested_tags=[], confidence=0,
            warnings=warnings, failure_code=str(error), started_at=started_at,
            runtime_ms=max(0, (time.monotonic_ns() - started) // 1_000_000),
        )
    except Exception:
        return _receipt(
            job,
            artifact_id=artifact_id,
            model_version=model_version,
            checkpoint_digest=checkpoint_digest,
            status="failed-manual-review",
            quality_signals={}, observations=[], suggested_tags=[], confidence=0,
            warnings=["vision-output-requires-human-review"],
            failure_code="studio-vision-model-failed", started_at=started_at,
            runtime_ms=max(0, (time.monotonic_ns() - started) // 1_000_000),
        )

    quality_signals = {
        "visualObservations": parsed["visualObservations"],
        "demandEvidenceCandidates": parsed["demandEvidenceCandidates"],
        "highlightCandidates": parsed["highlightCandidates"],
        "uncertainty": parsed["uncertainty"],
        "pendingObservations": parsed["pendingObservations"],
        "representativeFrames": context["frameEvidence"],
        **({"highlightPrefilter": context["highlightPrefilter"]} if highlight_only else {}),
        **({"highlightPair": context["highlightPair"]} if highlight_only else {}),
        "artifact": {
            "id": artifact["id"], "version": artifact["version"], "sha256": artifact["sha256"],
        },
        "runtimeArtifact": {
            "id": runtime["id"], "version": runtime["version"], "sha256": runtime["sha256"],
        },
        "inference": {
            "frameCount": len(context["frames"]),
            "maxLongEdge": MAX_FRAME_LONG_EDGE,
            "maxOutputTokens": HIGHLIGHT_OUTPUT_TOKENS if highlight_only else MAX_OUTPUT_TOKENS,
            "temperature": 0,
            "timeoutSeconds": VISION_TIMEOUT_SECONDS,
            "structuredOutputSchemaVersion": (
                HIGHLIGHT_SCHEMA_VERSION if highlight_only else STRUCTURED_OUTPUT_SCHEMA_VERSION
            ),
        },
    }
    warnings = [
        "vision-output-is-model-suggestion",
        *parsed["normalizationWarnings"],
        *parsed["demandWarnings"],
        *parsed["claimWarnings"],
    ]
    warnings.extend(f"forbidden-content-warning:{warning}" for warning in parsed["forbiddenContentWarnings"])
    observations = [
        f"pending-visual-observation:{item['text']}" for item in parsed["visualObservations"]
    ] + [
        f"pending-{item['kind']}-claim:{item['value']}" for item in parsed["pendingObservations"]
    ]
    return _receipt(
        job,
        artifact_id=artifact_id,
        model_version=model_version,
        checkpoint_digest=checkpoint_digest,
        status="succeeded",
        quality_signals=quality_signals,
        observations=observations,
        suggested_tags=parsed["suggestedTags"],
        confidence=parsed["confidence"],
        warnings=warnings,
        failure_code=None,
        started_at=started_at,
        runtime_ms=max(0, (time.monotonic_ns() - started) // 1_000_000),
        artifact_binding={
            "adapter": {
                "id": runtime["id"],
                "version": runtime["version"],
                "sha256": runtime["sha256"],
            },
            "model": {
                "id": artifact["id"],
                "version": artifact["version"],
                "sha256": artifact["sha256"],
            },
        },
    )
