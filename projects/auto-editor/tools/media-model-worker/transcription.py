"""Bounded local whisper.cpp transcription for an authorized media interval."""

from __future__ import annotations

import hashlib
import json
import math
import os
import selectors
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from artifact_registry import ArtifactRegistry, ArtifactRegistryError
from protocol import BINDING_KEYS, ProtocolError, validate_job
from technical import (
    MediaAnalysisError,
    TechnicalConfigurationError,
    _probe,
    _timestamp,
    _validate_ffprobe_path,
)


COMMAND_TIMEOUT_SECONDS = 30
MAX_COMMAND_BYTES = 64 * 1024
COMMAND_READ_CHUNK_BYTES = 8 * 1024
MAX_TRANSCRIPT_SPANS = 800
MAX_TRANSCRIPT_TEXT_BYTES = 32 * 1024


def _validate_executable(path: str) -> str:
    if not isinstance(path, str):
        raise TechnicalConfigurationError("studio-transcription-config-invalid")
    configured = Path(path)
    if not configured.is_absolute() or not configured.is_file() or not os.access(configured, os.X_OK):
        raise TechnicalConfigurationError("studio-transcription-config-invalid")
    return str(configured.resolve())


def _validate_model(path: str) -> str:
    if not isinstance(path, str):
        raise TechnicalConfigurationError("studio-transcription-config-invalid")
    configured = Path(path)
    if not configured.is_absolute() or not configured.is_file():
        raise TechnicalConfigurationError("studio-transcription-config-invalid")
    return str(configured.resolve())


def _digest_file(path: str, failure_code: str) -> str:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise MediaAnalysisError(failure_code) from error
    return digest.hexdigest()


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (AttributeError, OSError):
        try:
            process.kill()
        except OSError:
            pass


def _run_bounded_command(arguments: list[str], label: str) -> bytes:
    process: subprocess.Popen[bytes] | None = None
    selector = selectors.DefaultSelector()
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    deadline = time.monotonic() + COMMAND_TIMEOUT_SECONDS
    try:
        process = subprocess.Popen(
            arguments,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if process.stdout is None or process.stderr is None:
            raise MediaAnalysisError(f"studio-transcription-{label}-failed")
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")

        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MediaAnalysisError(f"studio-transcription-{label}-timeout")
            for key, _ in selector.select(min(remaining, 0.05)):
                stream_name = key.data
                chunk = os.read(key.fileobj.fileno(), COMMAND_READ_CHUNK_BYTES)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                buffers[stream_name].extend(chunk)
                if len(buffers[stream_name]) > MAX_COMMAND_BYTES:
                    raise MediaAnalysisError(
                        f"studio-transcription-{label}-{stream_name}-overflow"
                    )

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise MediaAnalysisError(f"studio-transcription-{label}-timeout")
        try:
            return_code = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as error:
            raise MediaAnalysisError(f"studio-transcription-{label}-timeout") from error
        if return_code != 0:
            raise MediaAnalysisError(f"studio-transcription-{label}-failed")
        return bytes(buffers["stdout"])
    except MediaAnalysisError:
        raise
    except (OSError, subprocess.SubprocessError) as error:
        raise MediaAnalysisError(f"studio-transcription-{label}-failed") from error
    finally:
        selector.close()
        if process is not None:
            _terminate_process(process)
            try:
                process.wait()
            except OSError:
                pass
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()


def _probability(value: Any) -> float | None:
    if value is None:
        return None
    if type(value) not in (int, float):
        raise MediaAnalysisError("studio-transcription-output-invalid")
    result = float(value)
    if not math.isfinite(result) or result < 0 or result > 1:
        raise MediaAnalysisError("studio-transcription-output-invalid")
    return result


def _normalize_output(
    stdout: bytes,
    *,
    source_start_ms: int,
    source_end_ms: int,
    media_duration_ms: int,
) -> dict[str, Any]:
    try:
        payload = json.loads(stdout.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MediaAnalysisError("studio-transcription-output-invalid") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("transcription"), list):
        raise MediaAnalysisError("studio-transcription-output-invalid")

    language_value = (
        payload.get("result", {}).get("language")
        if isinstance(payload.get("result"), dict)
        else None
    )
    if language_value is None:
        language_value = payload.get("language")
    if language_value is not None:
        if not isinstance(language_value, str) or not language_value.strip():
            raise MediaAnalysisError("studio-transcription-output-invalid")
        language_value = language_value.strip()
        if len(language_value.encode("utf-8")) > 100:
            raise MediaAnalysisError("studio-transcription-output-invalid")

    interval_ms = source_end_ms - source_start_ms
    spans: list[dict[str, Any]] = []
    probabilities: list[float] = []
    text_bytes = 0
    for row in payload["transcription"]:
        if not isinstance(row, dict) or not isinstance(row.get("offsets"), dict):
            raise MediaAnalysisError("studio-transcription-output-invalid")
        offsets = row["offsets"]
        relative_start = offsets.get("from")
        relative_end = offsets.get("to")
        if type(relative_start) is not int or type(relative_end) is not int:
            raise MediaAnalysisError("studio-transcription-output-invalid")
        if relative_start < 0 or relative_end <= relative_start or relative_end > interval_ms:
            raise MediaAnalysisError("studio-transcription-span-range-invalid")
        absolute_start = source_start_ms + relative_start
        absolute_end = source_start_ms + relative_end
        if (
            absolute_start < source_start_ms
            or absolute_end > source_end_ms
            or absolute_end > media_duration_ms
        ):
            raise MediaAnalysisError("studio-transcription-span-range-invalid")
        text = row.get("text")
        if not isinstance(text, str) or not text.strip():
            raise MediaAnalysisError("studio-transcription-output-invalid")
        text = text.strip()
        encoded_length = len(text.encode("utf-8"))
        text_bytes += encoded_length
        if encoded_length > 4000 or text_bytes > MAX_TRANSCRIPT_TEXT_BYTES:
            raise MediaAnalysisError("studio-transcription-output-too-large")
        no_speech_probability = _probability(
            row.get("no_speech_prob", row.get("noSpeechProbability"))
        )
        span = {
            "startMs": absolute_start,
            "endMs": absolute_end,
            "text": text,
        }
        if no_speech_probability is not None:
            span["noSpeechProbability"] = no_speech_probability
            probabilities.append(no_speech_probability)
        spans.append(span)
        if len(spans) > MAX_TRANSCRIPT_SPANS:
            raise MediaAnalysisError("studio-transcription-output-too-large")

    overall_probability = _probability(
        payload.get("no_speech_prob", payload.get("noSpeechProbability"))
    )
    if overall_probability is None and probabilities:
        overall_probability = sum(probabilities) / len(probabilities)
    return {
        "detectedLanguage": language_value,
        "noSpeechProbability": overall_probability,
        "spans": spans,
    }


def _receipt(
    job: dict[str, Any],
    *,
    adapter_name: str,
    model_name: str,
    model_version: str,
    checkpoint_digest: str | None,
    status: str,
    quality_signals: dict[str, Any],
    observations: list[str],
    warnings: list[str],
    failure_code: str | None,
    started_at: str,
    runtime_ms: int,
    artifact_binding: dict[str, Any] | None = None,
    speech_state: str | None = None,
) -> dict[str, Any]:
    canonical_job = json.dumps(job, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "schemaVersion": 1,
        **{key: job[key] for key in BINDING_KEYS},
        "adapterName": adapter_name,
        "modelName": model_name,
        "modelVersion": model_version,
        "checkpointDigest": checkpoint_digest,
        "inputDigest": hashlib.sha256(canonical_job.encode("utf-8")).hexdigest(),
        "promptVersion": "studio-transcript-zh-v1",
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
        "inputBinding": {
            "sourceDigest": job["sourceDigest"],
            "sourceStartMs": job["sourceStartMs"],
            "sourceEndMs": job["sourceEndMs"],
        },
        "artifactBinding": artifact_binding,
        "runtimeMetrics": {"runtimeMs": runtime_ms},
        "speechState": speech_state,
    }


def analyze_transcript(
    job_input: Any,
    ffprobe_path: str,
    ffmpeg_path: str,
    whisper_path: str,
    whisper_model_path: str,
    *,
    model_lock_path: str,
    model_root: str,
    whisper_binary_artifact_id: str,
    whisper_model_artifact_id: str,
) -> dict[str, Any]:
    started_at = _timestamp()
    started = time.monotonic_ns()
    adapter_name = whisper_binary_artifact_id
    model_name = whisper_model_artifact_id
    model_version = "unavailable"
    checkpoint_digest = None
    try:
        job = validate_job(job_input)
        if job["layer"] != "transcript":
            raise ProtocolError("studio-model-worker-invalid-job")
        registry = ArtifactRegistry(model_lock_path, model_root)
        binary_artifact = registry.resolve_ready_artifact(
            whisper_binary_artifact_id,
            whisper_path,
            executable=True,
            required_source_kind="code",
        )
        model_artifact = registry.resolve_ready_artifact(
            whisper_model_artifact_id,
            whisper_model_path,
            executable=False,
            required_source_kind="weights",
        )
        adapter_name = binary_artifact["id"]
        model_name = model_artifact["id"]
        model_version = model_artifact["version"]
        checkpoint_digest = model_artifact["sha256"]
        artifact_signals = {
            "binary": {
                "id": binary_artifact["id"],
                "version": binary_artifact["version"],
                "sha256": binary_artifact["sha256"],
            },
            "model": {
                "id": model_artifact["id"],
                "version": model_artifact["version"],
                "sha256": model_artifact["sha256"],
            },
        }
        artifact_binding = {
            "adapter": artifact_signals["binary"],
            "model": artifact_signals["model"],
        }
        ffprobe = _validate_ffprobe_path(ffprobe_path)
        ffmpeg = _validate_executable(ffmpeg_path)
        whisper = _validate_executable(binary_artifact["path"])
        model = _validate_model(model_artifact["path"])
        if (
            _digest_file(job["sourcePath"], "studio-transcription-media-unavailable")
            != job["sourceDigest"]
        ):
            raise MediaAnalysisError("studio-transcription-source-digest-mismatch")
        media = _probe(job, ffprobe)
        if not media["hasAudio"]:
            return _receipt(
                job,
                adapter_name=adapter_name,
                model_name=model_name,
                model_version=model_version,
                checkpoint_digest=checkpoint_digest,
                status="succeeded",
                quality_signals={
                    "detectedLanguage": None,
                    "noSpeechProbability": None,
                    "spans": [],
                    "artifacts": artifact_signals,
                },
                observations=["audio-stream:absent", "transcript-span-count:0"],
                warnings=["no-audio-track"],
                failure_code=None,
                started_at=started_at,
                runtime_ms=max(0, (time.monotonic_ns() - started) // 1_000_000),
                artifact_binding=artifact_binding,
                speech_state="no-audio",
            )

        temporary_directory = tempfile.mkdtemp(prefix="studio-transcription-")
        try:
            wav_path = str(Path(temporary_directory, "authorized-segment.wav"))
            interval_ms = job["sourceEndMs"] - job["sourceStartMs"]
            _run_bounded_command([
                ffmpeg,
                "-v", "error",
                "-nostdin",
                "-ss", f"{job['sourceStartMs'] / 1000:.3f}",
                "-t", f"{interval_ms / 1000:.3f}",
                "-i", job["sourcePath"],
                "-map", "0:a:0",
                "-ac", "1",
                "-ar", "16000",
                "-c:a", "pcm_s16le",
                "-y",
                wav_path,
            ], "ffmpeg")
            output = _run_bounded_command([
                whisper,
                "-m", model,
                "-f", wav_path,
                "-oj",
                "-of", "-",
                "-np",
                "-l", "auto",
            ], "whisper")
            quality_signals = _normalize_output(
                output,
                source_start_ms=job["sourceStartMs"],
                source_end_ms=job["sourceEndMs"],
                media_duration_ms=media["durationMs"],
            )
            quality_signals["artifacts"] = artifact_signals
        finally:
            shutil.rmtree(temporary_directory, ignore_errors=True)
        observations = [
            f"detected-language:{quality_signals['detectedLanguage'] or 'unknown'}",
            f"transcript-span-count:{len(quality_signals['spans'])}",
        ]
        return _receipt(
            job,
            adapter_name=adapter_name,
            model_name=model_name,
            model_version=model_version,
            checkpoint_digest=checkpoint_digest,
            status="succeeded",
            quality_signals=quality_signals,
            observations=observations,
            warnings=["transcript-is-model-suggestion"],
            failure_code=None,
            started_at=started_at,
            runtime_ms=max(0, (time.monotonic_ns() - started) // 1_000_000),
            artifact_binding=artifact_binding,
            speech_state="speech" if quality_signals["spans"] else "no-speech",
        )
    except (ProtocolError, MediaAnalysisError, ArtifactRegistryError) as error:
        job = validate_job(job_input)
        failure_code = (
            str(error)
            if isinstance(error, (MediaAnalysisError, ArtifactRegistryError))
            else "studio-transcription-job-invalid"
        )
        return _receipt(
            job,
            adapter_name=adapter_name,
            model_name=model_name,
            model_version=model_version,
            checkpoint_digest=checkpoint_digest,
            status="failed-manual-review",
            quality_signals={},
            observations=[],
            warnings=["transcription-requires-human-review"],
            failure_code=failure_code,
            started_at=started_at,
            runtime_ms=max(0, (time.monotonic_ns() - started) // 1_000_000),
            speech_state=None,
        )
