"""Deterministic technical inspection and scene evidence for authorized local media."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import selectors
import subprocess
import sys
import time
from array import array
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from protocol import BINDING_KEYS, ProtocolError, validate_job


MINIMUM_SCENE_MS = 600
MAX_METRIC_FRAMES = 120
MAX_PROBE_BYTES = 64 * 1024
PROBE_READ_CHUNK_BYTES = 8 * 1024
PROBE_TIMEOUT_SECONDS = 30
FFMPEG_TIMEOUT_SECONDS = 60
MAX_PCM_BYTES = 8 * 1024 * 1024
MAX_AUDIO_ANALYSIS_MS = 300_000
REFERENCE_AUDIO_SAMPLE_RATE = 8_000
REFERENCE_AUDIO_WINDOW_MS = 50
REFERENCE_AUDIO_MINIMUM_PEAK_GAP_MS = 250
MAX_REFERENCE_BEAT_MARKERS = 512
MAX_TRANSITION_FRAMES_PER_BOUNDARY = 7
MAX_TRANSITION_BOUNDARIES = 512
CUT_THRESHOLD = 0.25
MAX_SAFE_INTEGER = 9_007_199_254_740_991
CANONICAL_DURATION_SECONDS = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")


class TechnicalConfigurationError(ValueError):
    """Safe error for invalid local binary configuration."""


class MediaAnalysisError(ValueError):
    """Safe media-analysis failure with a stable code only."""


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _validate_ffprobe_path(ffprobe_path: str) -> str:
    if not isinstance(ffprobe_path, str):
        raise TechnicalConfigurationError("studio-technical-ffprobe-path-invalid")
    configured = Path(ffprobe_path)
    if not configured.is_absolute() or not configured.is_file() or not os.access(configured, os.X_OK):
        raise TechnicalConfigurationError("studio-technical-ffprobe-path-invalid")
    return str(configured.resolve())


def _validate_ffmpeg_path(ffmpeg_path: str) -> str:
    if not isinstance(ffmpeg_path, str):
        raise TechnicalConfigurationError("studio-technical-ffmpeg-path-invalid")
    configured = Path(ffmpeg_path)
    if not configured.is_absolute() or not configured.is_file() or not os.access(configured, os.X_OK):
        raise TechnicalConfigurationError("studio-technical-ffmpeg-path-invalid")
    return str(configured.resolve())


def _source_digest(source_path: str) -> str:
    digest = hashlib.sha256()
    try:
        with open(source_path, "rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise MediaAnalysisError("studio-technical-media-unavailable") from error
    return digest.hexdigest()


def _run_bounded_ffprobe(arguments: list[str]) -> bytes:
    process: subprocess.Popen[bytes] | None = None
    selector = selectors.DefaultSelector()
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    deadline = time.monotonic() + PROBE_TIMEOUT_SECONDS
    try:
        process = subprocess.Popen(
            arguments,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if process.stdout is None or process.stderr is None:
            raise MediaAnalysisError("studio-technical-media-invalid")
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")

        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MediaAnalysisError("studio-technical-ffprobe-timeout")
            for key, _ in selector.select(min(remaining, 0.05)):
                stream_name = key.data
                chunk = os.read(key.fileobj.fileno(), PROBE_READ_CHUNK_BYTES)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                buffers[stream_name].extend(chunk)
                if len(buffers[stream_name]) > MAX_PROBE_BYTES:
                    raise MediaAnalysisError(f"studio-technical-ffprobe-{stream_name}-overflow")

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise MediaAnalysisError("studio-technical-ffprobe-timeout")
        try:
            return_code = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as error:
            raise MediaAnalysisError("studio-technical-ffprobe-timeout") from error
        if return_code != 0:
            raise MediaAnalysisError("studio-technical-media-invalid")
        return bytes(buffers["stdout"])
    except (OSError, subprocess.SubprocessError) as error:
        raise MediaAnalysisError("studio-technical-media-invalid") from error
    finally:
        selector.close()
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait()
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()


def _duration_ms(value: Any) -> int:
    if type(value) in (int, float):
        duration_seconds = float(value)
    elif isinstance(value, str) and CANONICAL_DURATION_SECONDS.fullmatch(value) is not None:
        duration_seconds = float(value)
    else:
        raise MediaAnalysisError("studio-technical-media-invalid")
    duration_ms = math.floor(duration_seconds * 1000 + 0.5)
    if (
        not math.isfinite(duration_seconds)
        or duration_seconds <= 0
        or duration_ms <= 0
        or duration_ms > MAX_SAFE_INTEGER
    ):
        raise MediaAnalysisError("studio-technical-media-invalid")
    return duration_ms


def _probe(job: dict[str, Any], ffprobe_path: str) -> dict[str, Any]:
    stdout = _run_bounded_ffprobe([
        ffprobe_path,
        "-v", "error",
        "-show_entries", "format=duration:stream=codec_type,codec_name,width,height",
        "-of", "json",
        job["sourcePath"],
    ])
    try:
        payload = json.loads(stdout.decode("utf-8", errors="strict"))
        streams = payload.get("streams", [])
        duration_ms = _duration_ms(payload.get("format", {}).get("duration"))
    except (UnicodeDecodeError, ValueError, TypeError, AttributeError, json.JSONDecodeError) as error:
        raise MediaAnalysisError("studio-technical-media-invalid") from error
    video = next((row for row in streams if row.get("codec_type") == "video"), None)
    audio = next((row for row in streams if row.get("codec_type") == "audio"), None)
    if job["sourceEndMs"] > duration_ms:
        raise MediaAnalysisError("studio-technical-source-range-invalid")
    return {
        "durationMs": duration_ms,
        "hasVideo": video is not None,
        "hasAudio": audio is not None,
        "width": int(video.get("width", 0)) if video else 0,
        "height": int(video.get("height", 0)) if video else 0,
        "videoCodec": str(video.get("codec_name")) if video and video.get("codec_name") else None,
        "audioCodec": str(audio.get("codec_name")) if audio and audio.get("codec_name") else None,
    }


def _load_cv2():
    try:
        import cv2  # type: ignore[import-not-found]
    except ImportError as error:
        raise MediaAnalysisError("studio-technical-dependency-unavailable") from error
    return cv2


def _load_numpy():
    try:
        import numpy  # type: ignore[import-not-found]
    except ImportError as error:
        raise MediaAnalysisError("studio-technical-dependency-unavailable") from error
    return numpy


def classify_reference_boundaries(boundaries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Classify already-bounded frame samples without inventing unsupported kinds."""
    numpy = _load_numpy()
    evidence: list[dict[str, Any]] = []
    for boundary in boundaries[:MAX_TRANSITION_BOUNDARIES]:
        at_ms = boundary.get("atMs")
        frames = boundary.get("frames")
        if type(at_ms) is not int or at_ms < 0 or not isinstance(frames, list) or len(frames) < 2:
            raise MediaAnalysisError("studio-technical-transition-evidence-invalid")
        grays = []
        for frame in frames[:MAX_TRANSITION_FRAMES_PER_BOUNDARY]:
            value = numpy.asarray(frame)
            if value.ndim == 3:
                value = _load_cv2().cvtColor(value, _load_cv2().COLOR_BGR2GRAY)
            if value.ndim != 2 or value.size == 0:
                raise MediaAnalysisError("studio-technical-transition-evidence-invalid")
            grays.append(value.astype(numpy.float32))
        lumas = [float(frame.mean()) for frame in grays]
        histograms = []
        for frame in grays:
            counts, _ = numpy.histogram(frame, bins=32, range=(0, 256))
            histograms.append(counts.astype(numpy.float64) / frame.size)
        histogram_changes = [
            float(numpy.abs(left - right).sum()) / 2.0
            for left, right in zip(histograms, histograms[1:])
        ]
        valley = min(range(len(lumas)), key=lumas.__getitem__)
        near_black_valley = lumas[valley] <= 16.0 and 0 < valley < len(lumas) - 1
        monotonic_luma_ramp = (
            all(lumas[index] >= lumas[index + 1] for index in range(valley))
            and all(lumas[index] <= lumas[index + 1] for index in range(valley, len(lumas) - 1))
        )
        endpoint_change = float(numpy.abs(grays[0] - grays[-1]).mean()) / 255.0
        middle = grays[len(grays) // 2]
        overlapping_structure = (
            len(grays) >= 3
            and endpoint_change >= CUT_THRESHOLD
            and float(numpy.abs(grays[0] - middle).mean()) / 255.0 < endpoint_change
            and float(numpy.abs(middle - grays[-1]).mean()) / 255.0 < endpoint_change
        )
        distributed_histogram_change = sum(
            change >= 0.2 for change in histogram_changes
        ) >= 2
        adjacent_histogram_jump = max(histogram_changes)
        if near_black_valley and monotonic_luma_ramp:
            kind = "fade"
            confidence = 0.9
        elif distributed_histogram_change and overlapping_structure:
            kind = "crossfade"
            confidence = 0.8
        elif adjacent_histogram_jump >= CUT_THRESHOLD:
            kind = "cut"
            confidence = min(1.0, round(0.6 + adjacent_histogram_jump * 0.4, 6))
        else:
            kind = "unsupported"
            confidence = min(0.59, round(adjacent_histogram_jump, 6))
        evidence.append({"atMs": at_ms, "kind": kind, "confidence": confidence})
    return evidence


def detect_audio_beat_evidence(pcm: bytes, *, sample_rate: int) -> tuple[list[int], bool]:
    """Return bounded 50 ms energy peaks from mono signed 16-bit PCM."""
    if not isinstance(pcm, bytes) or len(pcm) % 2 != 0:
        raise MediaAnalysisError("studio-technical-audio-evidence-invalid")
    if type(sample_rate) is not int or sample_rate <= 0:
        raise MediaAnalysisError("studio-technical-audio-evidence-invalid")
    samples = array("h")
    samples.frombytes(pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    window_samples = max(1, sample_rate * REFERENCE_AUDIO_WINDOW_MS // 1000)
    energies = []
    for start in range(0, len(samples), window_samples):
        window = samples[start:start + window_samples]
        if len(window) < window_samples:
            break
        energies.append(math.sqrt(sum(value * value for value in window) / len(window)))
    if not energies:
        return [], False
    ordered = sorted(energies)
    median = ordered[len(ordered) // 2]
    deviations = sorted(abs(value - median) for value in energies)
    median_deviation = deviations[len(deviations) // 2]
    threshold = max(256.0, median * 2.5, median + 6 * median_deviation)
    minimum_gap_windows = math.ceil(
        REFERENCE_AUDIO_MINIMUM_PEAK_GAP_MS / REFERENCE_AUDIO_WINDOW_MS
    )
    selected: list[int] = []
    truncated = False
    last_window = -minimum_gap_windows
    for index, energy in enumerate(energies):
        previous = energies[index - 1] if index > 0 else -1.0
        following = energies[index + 1] if index + 1 < len(energies) else -1.0
        if (
            energy >= threshold
            and energy >= previous
            and energy > following
            and index - last_window >= minimum_gap_windows
        ):
            if len(selected) == MAX_REFERENCE_BEAT_MARKERS:
                truncated = True
                break
            selected.append(index * REFERENCE_AUDIO_WINDOW_MS)
            last_window = index
    return selected, truncated


def detect_audio_beats(pcm: bytes, *, sample_rate: int) -> list[int]:
    beats, _ = detect_audio_beat_evidence(pcm, sample_rate=sample_rate)
    return beats


def _sample_metrics(job: dict[str, Any]) -> dict[str, float]:
    cv2 = _load_cv2()
    capture = cv2.VideoCapture(job["sourcePath"])
    if not capture.isOpened():
        capture.release()
        raise MediaAnalysisError("studio-technical-media-invalid")
    duration_ms = job["sourceEndMs"] - job["sourceStartMs"]
    fps = float(capture.get(cv2.CAP_PROP_FPS))
    frame_ms = max(1, math.ceil(1000 / fps)) if math.isfinite(fps) and fps > 0 else 40
    last_sample_ms = max(job["sourceStartMs"], job["sourceEndMs"] - frame_ms)
    sample_count = min(MAX_METRIC_FRAMES, max(2, math.ceil(duration_ms / 100)))
    timestamps = [
        job["sourceStartMs"]
        + round((last_sample_ms - job["sourceStartMs"]) * index / (sample_count - 1))
        for index in range(sample_count)
    ]
    black_values: list[float] = []
    blur_values: list[float] = []
    freeze_pairs = 0
    previous_gray = None
    try:
        for timestamp_ms in timestamps:
            capture.set(cv2.CAP_PROP_POS_MSEC, timestamp_ms)
            ok, frame = capture.read()
            if not ok or frame is None:
                raise MediaAnalysisError("studio-technical-frame-decode-failed")
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            black_values.append(float((gray <= 16).mean()))
            blur_values.append(float(cv2.Laplacian(gray, cv2.CV_64F).var()))
            if previous_gray is not None:
                difference = float(cv2.absdiff(previous_gray, gray).mean())
                if difference <= 1.0:
                    freeze_pairs += 1
            previous_gray = gray
    finally:
        capture.release()
    return {
        "blackRatio": round(sum(black_values) / len(black_values), 6),
        "freezeRatio": round(freeze_pairs / max(1, len(black_values) - 1), 6),
        "blurScore": round(sum(blur_values) / len(blur_values), 6),
    }


def _scene_intervals(job: dict[str, Any]) -> list[dict[str, Any]]:
    cv2 = _load_cv2()
    try:
        from scenedetect import AdaptiveDetector, SceneManager, open_video
    except ImportError as error:
        raise MediaAnalysisError("studio-technical-dependency-unavailable") from error
    try:
        video = open_video(job["sourcePath"])
        fps = float(video.frame_rate)
        minimum_frames = max(1, math.ceil(fps * MINIMUM_SCENE_MS / 1000))
        manager = SceneManager()
        manager.add_detector(AdaptiveDetector(min_scene_len=minimum_frames))
        start_time = video.base_timecode + round(job["sourceStartMs"] * fps / 1000)
        end_time = video.base_timecode + round(job["sourceEndMs"] * fps / 1000)
        video.seek(start_time)
        manager.detect_scenes(video, end_time=end_time)
        detected = manager.get_scene_list(start_in_scene=True)
    except Exception as error:
        raise MediaAnalysisError("studio-technical-scene-detection-failed") from error
    boundaries = [job["sourceStartMs"]]
    for _, end in detected[:-1]:
        boundary = round(end.seconds * 1000)
        if job["sourceStartMs"] < boundary < job["sourceEndMs"]:
            boundaries.append(boundary)
    boundaries.append(job["sourceEndMs"])
    boundaries = _merge_short_intervals(sorted(set(boundaries)))
    scenes: list[dict[str, Any]] = []
    capture = cv2.VideoCapture(job["sourcePath"])
    try:
        for start_ms, end_ms in zip(boundaries, boundaries[1:]):
            representative_ms = start_ms + (end_ms - start_ms) // 2
            capture.set(cv2.CAP_PROP_POS_MSEC, representative_ms)
            ok, frame = capture.read()
            if not ok or frame is None:
                raise MediaAnalysisError("studio-technical-frame-decode-failed")
            scenes.append({
                "startMs": start_ms,
                "endMs": end_ms,
                "representativeFrameMs": representative_ms,
                "representativeFrameDigest": hashlib.sha256(frame.tobytes()).hexdigest(),
            })
    finally:
        capture.release()
    return scenes


def _transition_evidence(
    job: dict[str, Any],
    scenes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    cv2 = _load_cv2()
    capture = cv2.VideoCapture(job["sourcePath"])
    if not capture.isOpened():
        capture.release()
        raise MediaAnalysisError("studio-technical-media-invalid")
    fps = float(capture.get(cv2.CAP_PROP_FPS))
    frame_ms = max(1, math.ceil(1000 / fps)) if math.isfinite(fps) and fps > 0 else 40
    half_window = MAX_TRANSITION_FRAMES_PER_BOUNDARY // 2
    samples: list[dict[str, Any]] = []
    try:
        for scene in scenes[:-1][:MAX_TRANSITION_BOUNDARIES]:
            boundary_ms = scene["endMs"]
            frames = []
            for offset in range(-half_window, half_window + 1):
                timestamp_ms = min(
                    job["sourceEndMs"] - 1,
                    max(job["sourceStartMs"], boundary_ms + offset * frame_ms),
                )
                capture.set(cv2.CAP_PROP_POS_MSEC, timestamp_ms)
                ok, frame = capture.read()
                if not ok or frame is None:
                    raise MediaAnalysisError("studio-technical-frame-decode-failed")
                frames.append(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
            samples.append({"atMs": boundary_ms, "frames": frames})
    finally:
        capture.release()
    return classify_reference_boundaries(samples)


def _run_bounded_ffmpeg(arguments: list[str]) -> bytes:
    process: subprocess.Popen[bytes] | None = None
    selector = selectors.DefaultSelector()
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    deadline = time.monotonic() + FFMPEG_TIMEOUT_SECONDS
    try:
        process = subprocess.Popen(
            arguments,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if process.stdout is None or process.stderr is None:
            raise MediaAnalysisError("studio-technical-ffmpeg-failed")
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MediaAnalysisError("studio-technical-ffmpeg-timeout")
            for key, _ in selector.select(min(remaining, 0.05)):
                stream_name = key.data
                chunk = os.read(key.fileobj.fileno(), PROBE_READ_CHUNK_BYTES)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                buffers[stream_name].extend(chunk)
                limit = MAX_PCM_BYTES if stream_name == "stdout" else MAX_PROBE_BYTES
                if len(buffers[stream_name]) > limit:
                    raise MediaAnalysisError(f"studio-technical-ffmpeg-{stream_name}-overflow")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise MediaAnalysisError("studio-technical-ffmpeg-timeout")
        try:
            return_code = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as error:
            raise MediaAnalysisError("studio-technical-ffmpeg-timeout") from error
        if return_code != 0:
            raise MediaAnalysisError("studio-technical-ffmpeg-failed")
        return bytes(buffers["stdout"])
    except (OSError, subprocess.SubprocessError) as error:
        raise MediaAnalysisError("studio-technical-ffmpeg-failed") from error
    finally:
        selector.close()
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait()
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()


def _audio_evidence(
    job: dict[str, Any],
    ffmpeg_path: str,
) -> tuple[list[int], bool]:
    ffmpeg = _validate_ffmpeg_path(ffmpeg_path)
    available_ms = job["sourceEndMs"] - job["sourceStartMs"]
    analyzed_ms = min(available_ms, MAX_AUDIO_ANALYSIS_MS)
    pcm = _run_bounded_ffmpeg([
        ffmpeg,
        "-hide_banner", "-loglevel", "error",
        "-ss", f"{job['sourceStartMs'] / 1000:.3f}",
        "-t", f"{analyzed_ms / 1000:.3f}",
        "-i", job["sourcePath"],
        "-vn", "-ac", "1", "-ar", str(REFERENCE_AUDIO_SAMPLE_RATE),
        "-acodec", "pcm_s16le", "-f", "s16le", "pipe:1",
    ])
    relative_beats, peak_cap_truncated = detect_audio_beat_evidence(
        pcm,
        sample_rate=REFERENCE_AUDIO_SAMPLE_RATE,
    )
    beats = [
        job["sourceStartMs"] + marker
        for marker in relative_beats
    ]
    return beats, analyzed_ms < available_ms or peak_cap_truncated


def _merge_short_intervals(boundaries: list[int]) -> list[int]:
    if len(boundaries) <= 2:
        return boundaries
    merged = [boundaries[0]]
    for boundary in boundaries[1:-1]:
        if boundary - merged[-1] >= MINIMUM_SCENE_MS:
            merged.append(boundary)
    if boundaries[-1] - merged[-1] < MINIMUM_SCENE_MS and len(merged) > 1:
        merged.pop()
    merged.append(boundaries[-1])
    return merged


def _receipt(
    job: dict[str, Any],
    *,
    status: str,
    quality_signals: dict[str, Any],
    observations: list[str],
    warnings: list[str],
    failure_code: str | None,
    started_at: str,
    runtime_ms: int,
) -> dict[str, Any]:
    canonical_job = json.dumps(job, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "schemaVersion": 1,
        **{key: job[key] for key in BINDING_KEYS},
        "adapterName": "ffprobe-opencv-technical",
        "modelName": "deterministic-technical-tools",
        "modelVersion": "v1",
        "checkpointDigest": None,
        "inputDigest": hashlib.sha256(canonical_job.encode("utf-8")).hexdigest(),
        "promptVersion": "studio-technical-v1",
        "observations": observations,
        "suggestedTags": [],
        "qualitySignals": quality_signals,
        "confidence": 1 if status == "succeeded" else 0,
        "warnings": warnings,
        "startedAt": started_at,
        "completedAt": _timestamp(),
        "runtimeMs": runtime_ms,
        "status": status,
        "failure": None if failure_code is None else {"code": failure_code},
        "synthetic": False,
        "providerMode": "local",
    }


def _analyze(
    job_input: Any,
    ffprobe_path: str,
    *,
    include_scenes: bool,
    ffmpeg_path: str | None = None,
) -> dict[str, Any]:
    ffprobe = _validate_ffprobe_path(ffprobe_path)
    started_at = _timestamp()
    started = time.monotonic_ns()
    try:
        job = validate_job(job_input)
        expected_layer = "scene" if include_scenes else "technical"
        if job["layer"] != expected_layer:
            raise ProtocolError("studio-model-worker-invalid-job")
        if _source_digest(job["sourcePath"]) != job["sourceDigest"]:
            raise MediaAnalysisError("studio-technical-source-digest-mismatch")
        signals = _probe(job, ffprobe)
        if not signals["hasVideo"]:
            raise MediaAnalysisError("studio-technical-video-stream-missing")
        if include_scenes and job["sourceEndMs"] - job["sourceStartMs"] < MINIMUM_SCENE_MS:
            raise MediaAnalysisError("studio-technical-scene-interval-too-short")
        signals.update(_sample_metrics(job))
        observations = [
            f"duration-ms:{signals['durationMs']}",
            f"video-codec:{signals['videoCodec']}",
            f"audio-stream:{'present' if signals['hasAudio'] else 'absent'}",
        ]
        if include_scenes:
            signals["scenes"] = _scene_intervals(job)
            signals["transitionEvidence"] = _transition_evidence(job, signals["scenes"])
            signals["transitionEvidenceTruncated"] = (
                len(signals["scenes"]) - 1 > MAX_TRANSITION_BOUNDARIES
            )
            if signals["hasAudio"]:
                signals["beatMarkers"], signals["audioEvidenceTruncated"] = _audio_evidence(
                    job,
                    ffmpeg_path,
                )
            else:
                signals["beatMarkers"] = []
                signals["audioEvidenceTruncated"] = False
            observations.append(f"scene-count:{len(signals['scenes'])}")
        return _receipt(
            job,
            status="succeeded",
            quality_signals=signals,
            observations=observations,
            warnings=[],
            failure_code=None,
            started_at=started_at,
            runtime_ms=max(0, (time.monotonic_ns() - started) // 1_000_000),
        )
    except (ProtocolError, MediaAnalysisError) as error:
        job = validate_job(job_input)
        failure_code = (
            str(error)
            if isinstance(error, MediaAnalysisError)
            else "studio-technical-job-invalid"
        )
        return _receipt(
            job,
            status="failed-manual-review",
            quality_signals={},
            observations=[],
            warnings=["technical-analysis-requires-human-review"],
            failure_code=failure_code,
            started_at=started_at,
            runtime_ms=max(0, (time.monotonic_ns() - started) // 1_000_000),
        )


def analyze_technical(job_input: Any, ffprobe_path: str) -> dict[str, Any]:
    return _analyze(job_input, ffprobe_path, include_scenes=False)


def analyze_scene(
    job_input: Any,
    ffprobe_path: str,
    ffmpeg_path: str | None = None,
) -> dict[str, Any]:
    return _analyze(
        job_input,
        ffprobe_path,
        include_scenes=True,
        ffmpeg_path=ffmpeg_path,
    )
