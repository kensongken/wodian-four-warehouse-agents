"""Private, customer-scoped real-video editing with no identity or voice cloning."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from importlib import import_module
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import RLock
from typing import Literal
from uuid import UUID, uuid4

from opc_style.domain.errors import DomainRuleError

MAX_VIDEO_BYTES = 200 * 1024 * 1024
MAX_SUBTITLE_BYTES = 256 * 1024
VIDEO_PIPELINE_VERSION = "real-video-edit-v1"
VIDEO_PACKAGE_STYLES = {"original", "catalog-light", "catalog-dark"}


@dataclass(frozen=True)
class VideoArtifact:
    kind: Literal["video", "subtitles", "cover"]
    content_sha256: str
    mime_type: str
    size_bytes: int


@dataclass(frozen=True)
class VideoOutput:
    output_id: UUID
    customer_id: UUID
    idempotency_key: str
    source_sha256: str
    title: str
    edit_plan: dict[str, object]
    state: Literal["human_review_required", "customer_approved", "customer_rejected"]
    artifacts: tuple[VideoArtifact, ...]
    created_at: str
    updated_at: str
    publishable: Literal[False] = False
    voice_cloning_used: Literal[False] = False
    external_provider_calls: int = 0


class VideoOutputStore:
    """Persist immutable media and a small review manifest below one private root."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve(strict=False)
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._root.chmod(0o700)
        self._lock = RLock()

    def create(
        self,
        *,
        customer_id: UUID,
        source: bytes,
        source_mime_type: str,
        subtitles: str,
        title: str,
        idempotency_key: str,
        clip_start_seconds: float = 0.0,
        clip_end_seconds: float | None = None,
        denoise_audio: bool = False,
        normalize_audio: bool = True,
        package_style: str = "original",
    ) -> tuple[VideoOutput, bool]:
        if not source or len(source) > MAX_VIDEO_BYTES:
            raise DomainRuleError("video_source_size_invalid")
        if source_mime_type not in {"video/mp4", "video/quicktime"}:
            raise DomainRuleError("video_source_type_invalid")
        subtitle_bytes = subtitles.encode("utf-8")
        if len(subtitle_bytes) > MAX_SUBTITLE_BYTES:
            raise DomainRuleError("video_subtitles_too_large")
        normalized_title = title.strip()
        if not normalized_title or len(normalized_title) > 120:
            raise DomainRuleError("video_title_invalid")
        if not idempotency_key or len(idempotency_key) > 200:
            raise DomainRuleError("video_idempotency_key_invalid")
        if clip_start_seconds < 0 or clip_start_seconds > 86_400:
            raise DomainRuleError("video_clip_range_invalid")
        if clip_end_seconds is not None and (
            clip_end_seconds <= clip_start_seconds or clip_end_seconds > 86_400
        ):
            raise DomainRuleError("video_clip_range_invalid")
        if package_style not in VIDEO_PACKAGE_STYLES:
            raise DomainRuleError("video_package_style_invalid")
        edit_plan: dict[str, object] = {
            "clip_start_seconds": clip_start_seconds,
            "clip_end_seconds": clip_end_seconds,
            "denoise_audio": denoise_audio,
            "normalize_audio": normalize_audio,
            "package_style": package_style,
        }
        fingerprint = sha256(
            b"\0".join(
                (
                    source,
                    subtitle_bytes,
                    normalized_title.encode("utf-8"),
                    json.dumps(
                        edit_plan, sort_keys=True, separators=(",", ":")
                    ).encode("utf-8"),
                    VIDEO_PIPELINE_VERSION.encode("ascii"),
                )
            )
        ).hexdigest()
        with self._lock:
            replay = self._find_by_key(customer_id, idempotency_key)
            if replay is not None:
                record, stored_fingerprint = replay
                if stored_fingerprint != fingerprint:
                    raise DomainRuleError("video_idempotency_conflict")
                return record, False
            output_id = uuid4()
            customer_root = self._customer_root(customer_id)
            target = customer_root / str(output_id)
            with TemporaryDirectory(prefix="video-output-", dir=customer_root) as raw:
                staging = Path(raw)
                source_path = staging / (
                    "source.mov" if source_mime_type == "video/quicktime" else "source.mp4"
                )
                source_path.write_bytes(source)
                subtitle_path = staging / "subtitles.srt"
                subtitle_path.write_bytes(subtitle_bytes)
                output_path = staging / "edited.mp4"
                cover_path = staging / "cover.jpg"
                self._render(
                    source_path,
                    subtitle_path,
                    output_path,
                    cover_path,
                    clip_start_seconds=clip_start_seconds,
                    clip_end_seconds=clip_end_seconds,
                    denoise_audio=denoise_audio,
                    normalize_audio=normalize_audio,
                    package_style=package_style,
                )
                now = datetime.now(UTC).isoformat()
                record = VideoOutput(
                    output_id=output_id,
                    customer_id=customer_id,
                    idempotency_key=idempotency_key,
                    source_sha256=sha256(source).hexdigest(),
                    title=normalized_title,
                    edit_plan=edit_plan,
                    state="human_review_required",
                    artifacts=(
                        _artifact("video", output_path, "video/mp4"),
                        _artifact("subtitles", subtitle_path, "application/x-subrip"),
                        _artifact("cover", cover_path, "image/jpeg"),
                    ),
                    created_at=now,
                    updated_at=now,
                )
                (staging / "manifest.json").write_text(
                    json.dumps(
                        {**_serialize(record), "request_fingerprint": fingerprint},
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    encoding="utf-8",
                )
                os.replace(staging, target)
            return record, True

    def get(self, customer_id: UUID, output_id: UUID) -> VideoOutput:
        path = self._output_root(customer_id, output_id) / "manifest.json"
        if not path.is_file():
            raise DomainRuleError("video_output_not_found")
        return _deserialize(json.loads(path.read_text(encoding="utf-8")))

    def review(
        self,
        customer_id: UUID,
        output_id: UUID,
        *,
        decision: Literal["approved", "rejected"],
    ) -> VideoOutput:
        with self._lock:
            root = self._output_root(customer_id, output_id)
            path = root / "manifest.json"
            if not path.is_file():
                raise DomainRuleError("video_output_not_found")
            payload = json.loads(path.read_text(encoding="utf-8"))
            current = _deserialize(payload)
            target = "customer_approved" if decision == "approved" else "customer_rejected"
            if current.state != "human_review_required" and current.state != target:
                raise DomainRuleError("video_review_conflict")
            updated = VideoOutput(
                **{
                    **current.__dict__,
                    "state": target,
                    "updated_at": datetime.now(UTC).isoformat(),
                }
            )
            replacement = root / "manifest.json.tmp"
            replacement.write_text(
                json.dumps(
                    {**_serialize(updated), "request_fingerprint": payload["request_fingerprint"]},
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            os.replace(replacement, path)
            return updated

    def content(
        self, customer_id: UUID, output_id: UUID, kind: str
    ) -> tuple[bytes, str, str]:
        record = self.get(customer_id, output_id)
        names = {"video": "edited.mp4", "subtitles": "subtitles.srt", "cover": "cover.jpg"}
        if kind not in names:
            raise DomainRuleError("video_artifact_not_found")
        artifact = next((item for item in record.artifacts if item.kind == kind), None)
        if artifact is None:
            raise DomainRuleError("video_artifact_not_found")
        content = (self._output_root(customer_id, output_id) / names[kind]).read_bytes()
        if sha256(content).hexdigest() != artifact.content_sha256:
            raise DomainRuleError("video_artifact_integrity_failed")
        return content, artifact.mime_type, artifact.content_sha256

    def _render(
        self,
        source: Path,
        subtitles: Path,
        output: Path,
        cover: Path,
        *,
        clip_start_seconds: float,
        clip_end_seconds: float | None,
        denoise_audio: bool,
        normalize_audio: bool,
        package_style: str,
    ) -> None:
        ffmpeg = _ffmpeg_executable()
        command = [
            ffmpeg,
            "-nostdin",
            "-v",
            "error",
            "-y",
        ]
        if clip_start_seconds:
            command.extend(["-ss", _seconds(clip_start_seconds)])
        if clip_end_seconds is not None:
            command.extend(
                ["-t", _seconds(clip_end_seconds - clip_start_seconds)]
            )
        command.extend(
            [
            "-i",
            str(source),
            ]
        )
        if subtitles.stat().st_size:
            command.extend(["-i", str(subtitles), "-map", "0:v:0", "-map", "0:a?", "-map", "1:0"])
        else:
            command.extend(["-map", "0:v:0", "-map", "0:a?"])
        video_filter = "scale='min(1080,iw)':-2:flags=lanczos,fps=30"
        if package_style != "original":
            color = "f7f3ed" if package_style == "catalog-light" else "25221f"
            video_filter += (
                ",pad=iw:max(ih\\,ceil(iw*5/4/2)*2):(ow-iw)/2:(oh-ih)/2:"
                f"color=0x{color}"
            )
        command.extend(
            [
                "-vf",
                video_filter,
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
            ]
        )
        audio_filters = []
        if denoise_audio:
            audio_filters.append("afftdn=nf=-25")
        if normalize_audio:
            audio_filters.append("loudnorm=I=-16:TP=-1.5:LRA=11")
        if audio_filters:
            command.extend(["-af", ",".join(audio_filters)])
        if subtitles.stat().st_size:
            command.extend(["-c:s", "mov_text", "-metadata:s:s:0", "language=zho"])
        command.extend(["-map_metadata", "-1", "-movflags", "+faststart", str(output)])
        _run_ffmpeg(command)
        _run_ffmpeg(
            [
                ffmpeg,
                "-nostdin",
                "-v",
                "error",
                "-y",
                "-ss",
                "0",
                "-i",
                str(output),
                "-frames:v",
                "1",
                "-q:v",
                "2",
                str(cover),
            ]
        )

    def _find_by_key(
        self, customer_id: UUID, idempotency_key: str
    ) -> tuple[VideoOutput, str] | None:
        root = self._customer_root(customer_id)
        for manifest in root.glob("*/manifest.json"):
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            if payload.get("idempotency_key") == idempotency_key:
                return _deserialize(payload), str(payload["request_fingerprint"])
        return None

    def _customer_root(self, customer_id: UUID) -> Path:
        path = self._root / str(customer_id)
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        return path

    def _output_root(self, customer_id: UUID, output_id: UUID) -> Path:
        return self._customer_root(customer_id) / str(output_id)


def _ffmpeg_executable() -> str:
    """Resolve the video runtime only when a video is actually rendered."""
    try:
        imageio_ffmpeg = import_module("imageio_ffmpeg")
    except ModuleNotFoundError as exc:
        if exc.name != "imageio_ffmpeg":
            raise
        raise DomainRuleError("video_runtime_dependency_missing") from exc
    return str(imageio_ffmpeg.get_ffmpeg_exe())


def _run_ffmpeg(command: list[str]) -> None:
    try:
        subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise DomainRuleError("video_processing_failed") from error


def _seconds(value: float) -> str:
    return f"{value:.3f}"


def _artifact(kind: Literal["video", "subtitles", "cover"], path: Path, mime: str) -> VideoArtifact:
    content = path.read_bytes()
    return VideoArtifact(kind, sha256(content).hexdigest(), mime, len(content))


def _serialize(record: VideoOutput) -> dict[str, object]:
    return {
        "output_id": str(record.output_id),
        "customer_id": str(record.customer_id),
        "idempotency_key": record.idempotency_key,
        "source_sha256": record.source_sha256,
        "title": record.title,
        "edit_plan": record.edit_plan,
        "state": record.state,
        "artifacts": [item.__dict__ for item in record.artifacts],
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "publishable": False,
        "voice_cloning_used": False,
        "external_provider_calls": 0,
        "pipeline_version": VIDEO_PIPELINE_VERSION,
    }


def _deserialize(payload: dict[str, object]) -> VideoOutput:
    raw_artifacts = payload.get("artifacts")
    if not isinstance(raw_artifacts, list):
        raise DomainRuleError("video_manifest_invalid")
    raw_edit_plan = payload.get("edit_plan")
    if raw_edit_plan is None:
        raw_edit_plan = {}
    if not isinstance(raw_edit_plan, dict):
        raise DomainRuleError("video_manifest_invalid")
    return VideoOutput(
        output_id=UUID(str(payload["output_id"])),
        customer_id=UUID(str(payload["customer_id"])),
        idempotency_key=str(payload["idempotency_key"]),
        source_sha256=str(payload["source_sha256"]),
        title=str(payload["title"]),
        edit_plan=dict(raw_edit_plan),
        state=str(payload["state"]),  # type: ignore[arg-type]
        artifacts=tuple(
            VideoArtifact(
                kind=str(item["kind"]),  # type: ignore[arg-type]
                content_sha256=str(item["content_sha256"]),
                mime_type=str(item["mime_type"]),
                size_bytes=int(item["size_bytes"]),
            )
            for item in raw_artifacts
            if isinstance(item, dict)
        ),
        created_at=str(payload["created_at"]),
        updated_at=str(payload["updated_at"]),
    )
