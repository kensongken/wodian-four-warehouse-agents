"""Approved-artifact-gated OpenCLIP embeddings over Task 4 representative frames."""

from __future__ import annotations

import hashlib
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol, Sequence

from artifact_registry import ArtifactRegistry, ArtifactRegistryError
from protocol import BINDING_KEYS, ProtocolError, validate_job
from technical import MediaAnalysisError, _scene_intervals


MIN_VECTOR_DIMENSION = 2
MAX_VECTOR_DIMENSION = 2_048
MAX_REPRESENTATIVE_FRAMES = 64
DIGEST_LENGTH = 64


class EmbeddingAnalysisError(ValueError):
    """Stable embedding failure that never includes media or local paths."""


class EmbeddingEncoder(Protocol):
    def encode_images(self, frames: Sequence[Any]) -> Sequence[float]: ...


FrameProvider = Callable[[dict[str, Any]], Iterable[dict[str, Any]]]
ModelLoader = Callable[[str], EmbeddingEncoder]
_MODEL_CACHE: dict[tuple[str, str, str, str], EmbeddingEncoder] = {}


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def normalize_vector(vector_input: Sequence[float]) -> list[float]:
    if (
        isinstance(vector_input, (str, bytes, bytearray))
        or not isinstance(vector_input, Sequence)
        or not MIN_VECTOR_DIMENSION <= len(vector_input) <= MAX_VECTOR_DIMENSION
    ):
        raise EmbeddingAnalysisError("studio-embedding-vector-dimension-invalid")
    vector: list[float] = []
    for value in vector_input:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise EmbeddingAnalysisError("studio-embedding-vector-invalid")
        number = float(value)
        if not math.isfinite(number):
            raise EmbeddingAnalysisError("studio-embedding-vector-invalid")
        vector.append(number)
    norm = math.hypot(*vector)
    if not math.isfinite(norm) or norm <= float.fromhex("0x1.0p-52"):
        raise EmbeddingAnalysisError("studio-embedding-vector-zero")
    return [value / norm for value in vector]


def _source_digest(source_path: str) -> str:
    digest = hashlib.sha256()
    try:
        with open(source_path, "rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise EmbeddingAnalysisError("studio-embedding-media-unavailable") from error
    return digest.hexdigest()


def _is_digest(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == DIGEST_LENGTH
        and all(character in "0123456789abcdef" for character in value)
    )


def _pixel_digest(pixels: Any) -> str:
    try:
        if isinstance(pixels, (bytes, bytearray, memoryview)):
            payload = bytes(pixels)
        else:
            to_bytes = getattr(pixels, "tobytes", None)
            if not callable(to_bytes):
                raise TypeError
            payload = to_bytes()
        if not isinstance(payload, bytes):
            raise TypeError
    except Exception as error:
        raise EmbeddingAnalysisError("studio-embedding-frame-invalid") from error
    return hashlib.sha256(payload).hexdigest()


def _verify_source_digest(job: dict[str, Any]) -> None:
    if _source_digest(job["sourcePath"]) != job["sourceDigest"]:
        raise EmbeddingAnalysisError("studio-embedding-source-digest-mismatch")


def _validated_frames(
    job: dict[str, Any],
    provider: FrameProvider,
) -> tuple[list[Any], list[dict[str, Any]]]:
    try:
        frames = list(provider(job))
    except (EmbeddingAnalysisError, MediaAnalysisError):
        raise
    except Exception as error:
        raise EmbeddingAnalysisError("studio-embedding-frame-extraction-failed") from error
    if not frames or len(frames) > MAX_REPRESENTATIVE_FRAMES:
        raise EmbeddingAnalysisError("studio-embedding-frame-count-invalid")
    pixels: list[Any] = []
    evidence: list[dict[str, Any]] = []
    expected_keys = {
        "startMs",
        "endMs",
        "representativeFrameMs",
        "representativeFrameDigest",
        "pixels",
    }
    for frame in frames:
        if not isinstance(frame, dict) or set(frame) != expected_keys:
            raise EmbeddingAnalysisError("studio-embedding-frame-invalid")
        start = frame["startMs"]
        end = frame["endMs"]
        representative = frame["representativeFrameMs"]
        if (
            type(start) is not int
            or type(end) is not int
            or type(representative) is not int
            or not job["sourceStartMs"] <= start < representative < end <= job["sourceEndMs"]
            or not _is_digest(frame["representativeFrameDigest"])
            or frame["pixels"] is None
        ):
            raise EmbeddingAnalysisError("studio-embedding-frame-invalid")
        if _pixel_digest(frame["pixels"]) != frame["representativeFrameDigest"]:
            raise EmbeddingAnalysisError("studio-embedding-frame-digest-mismatch")
        pixels.append(frame["pixels"])
        evidence.append({
            "startMs": start,
            "endMs": end,
            "representativeFrameMs": representative,
            "representativeFrameDigest": frame["representativeFrameDigest"],
        })
    return pixels, evidence


def _task4_representative_frames(job: dict[str, Any]) -> Iterable[dict[str, Any]]:
    try:
        import cv2  # type: ignore[import-not-found]
    except ImportError as error:
        raise EmbeddingAnalysisError("studio-embedding-dependency-unavailable") from error
    scenes = _scene_intervals(job)
    if len(scenes) > MAX_REPRESENTATIVE_FRAMES:
        raise EmbeddingAnalysisError("studio-embedding-frame-count-invalid")
    capture = cv2.VideoCapture(job["sourcePath"])
    if not capture.isOpened():
        capture.release()
        raise EmbeddingAnalysisError("studio-embedding-media-invalid")
    try:
        for scene in scenes:
            capture.set(cv2.CAP_PROP_POS_MSEC, scene["representativeFrameMs"])
            ok, pixels = capture.read()
            if not ok or pixels is None:
                raise EmbeddingAnalysisError("studio-embedding-frame-extraction-failed")
            yield {**scene, "pixels": pixels}
    finally:
        capture.release()


class _OpenClipEncoder:
    def __init__(self, model: Any, preprocess: Any, torch_module: Any):
        self.model = model
        self.preprocess = preprocess
        self.torch = torch_module

    def encode_images(self, frames: Sequence[Any]) -> Sequence[float]:
        try:
            from PIL import Image  # type: ignore[import-not-found]

            tensors = []
            for frame in frames:
                rgb = frame[:, :, ::-1]
                tensors.append(self.preprocess(Image.fromarray(rgb)))
            batch = self.torch.stack(tensors).to("cpu")
            with self.torch.inference_mode():
                encoded = self.model.encode_image(batch).float().mean(dim=0)
            return encoded.detach().cpu().tolist()
        except Exception as error:
            raise EmbeddingAnalysisError("studio-embedding-model-failed") from error


def _load_openclip_model(checkpoint_path: str) -> EmbeddingEncoder:
    try:
        import open_clip  # type: ignore[import-not-found]
        import torch  # type: ignore[import-not-found]
    except ImportError as error:
        raise EmbeddingAnalysisError("studio-embedding-dependency-unavailable") from error
    try:
        model, _, preprocess = open_clip.create_model_and_transforms(
            "ViT-B-32",
            pretrained=checkpoint_path,
            device="cpu",
        )
        model.eval()
    except Exception as error:
        raise EmbeddingAnalysisError("studio-embedding-model-unavailable") from error
    return _OpenClipEncoder(model, preprocess, torch)


def _cached_model(
    artifact: dict[str, str],
    loader: ModelLoader,
) -> EmbeddingEncoder:
    key = (artifact["id"], artifact["version"], artifact["sha256"], artifact["path"])
    model = _MODEL_CACHE.get(key)
    if model is None:
        try:
            model = loader(artifact["path"])
            if not callable(getattr(model, "encode_images", None)):
                raise EmbeddingAnalysisError("studio-embedding-model-invalid")
        except EmbeddingAnalysisError:
            raise
        except Exception as error:
            raise EmbeddingAnalysisError("studio-embedding-model-unavailable") from error
        _MODEL_CACHE[key] = model
    return model


def _receipt(
    job: dict[str, Any],
    *,
    artifact_id: str,
    model_version: str,
    checkpoint_digest: str | None,
    quality_signals: dict[str, Any],
    status: str,
    failure_code: str | None,
    started_at: str,
    runtime_ms: int,
) -> dict[str, Any]:
    canonical_job = json.dumps(job, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "schemaVersion": 1,
        **{key: job[key] for key in BINDING_KEYS},
        "adapterName": "open-clip-local",
        "modelName": artifact_id,
        "modelVersion": model_version,
        "checkpointDigest": checkpoint_digest,
        "inputDigest": hashlib.sha256(canonical_job.encode("utf-8")).hexdigest(),
        "promptVersion": "studio-embedding-v1",
        "observations": (
            [f"embedding-dimension:{quality_signals['dimension']}"]
            if status == "succeeded"
            else []
        ),
        "suggestedTags": [],
        "qualitySignals": quality_signals,
        "confidence": 1 if status == "succeeded" else 0,
        "warnings": ["embedding-is-similarity-suggestion"],
        "startedAt": started_at,
        "completedAt": _timestamp(),
        "runtimeMs": runtime_ms,
        "status": status,
        "failure": None if failure_code is None else {"code": failure_code},
        "synthetic": False,
        "providerMode": "local",
    }


def analyze_embedding(
    job_input: Any,
    *,
    model_lock_path: str,
    model_root: str,
    openclip_artifact_id: str,
    checkpoint_path: str,
    frame_provider: FrameProvider = _task4_representative_frames,
    model_loader: ModelLoader = _load_openclip_model,
) -> dict[str, Any]:
    started_at = _timestamp()
    started = time.monotonic_ns()
    job = validate_job(job_input)
    if job["layer"] != "embedding":
        raise ProtocolError("studio-model-worker-invalid-job")
    model_version = "unavailable"
    verified_digest = None
    try:
        registry = ArtifactRegistry(model_lock_path, model_root)
        artifact = registry.resolve_ready_artifact(
            openclip_artifact_id,
            checkpoint_path,
            executable=False,
            required_source_kind="weights",
        )
        model_version = artifact["version"]
        verified_digest = artifact["sha256"]
        _verify_source_digest(job)
        pixels, frame_evidence = _validated_frames(job, frame_provider)
        _verify_source_digest(job)
        model = _cached_model(artifact, model_loader)
        _verify_source_digest(job)
        try:
            vector = normalize_vector(model.encode_images(pixels))
        except EmbeddingAnalysisError:
            raise
        except Exception as error:
            raise EmbeddingAnalysisError("studio-embedding-model-failed") from error
        return _receipt(
            job,
            artifact_id=artifact["id"],
            model_version=model_version,
            checkpoint_digest=verified_digest,
            quality_signals={
                "embedding": vector,
                "dimension": len(vector),
                "representativeFrames": frame_evidence,
                "artifact": {
                    "id": artifact["id"],
                    "version": artifact["version"],
                    "sha256": artifact["sha256"],
                },
            },
            status="succeeded",
            failure_code=None,
            started_at=started_at,
            runtime_ms=max(0, (time.monotonic_ns() - started) // 1_000_000),
        )
    except (ArtifactRegistryError, EmbeddingAnalysisError, MediaAnalysisError) as error:
        failure_code = str(error)
    except Exception:
        failure_code = "studio-embedding-analysis-failed"
    return _receipt(
        job,
        artifact_id=openclip_artifact_id,
        model_version=model_version,
        checkpoint_digest=verified_digest,
        quality_signals={},
        status="failed-manual-review",
        failure_code=failure_code,
        started_at=started_at,
        runtime_ms=max(0, (time.monotonic_ns() - started) // 1_000_000),
    )
