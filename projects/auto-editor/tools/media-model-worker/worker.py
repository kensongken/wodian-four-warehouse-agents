"""One-process JSON-lines entry point. No network listener is created."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

WORKER_ROOT = str(Path(__file__).resolve(strict=True).parent)
if WORKER_ROOT not in sys.path:
    sys.path.insert(0, WORKER_ROOT)

from artifact_registry import ArtifactRegistry, ArtifactRegistryError
from embedding import analyze_embedding
from protocol import ProtocolError, build_unconfigured_receipt, validate_job
from runtime_integrity import RuntimeExpectation, RuntimeIntegrityError, validate_runtime_process
from stage_telemetry import StageTelemetryError, StageTelemetryWriter
from technical import TechnicalConfigurationError, analyze_scene, analyze_technical
from transcription import analyze_transcript
from vision import VISION_RESTART_JOBS, analyze_vision, trusted_context_provider


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--ffprobe-path")
    parser.add_argument("--ffmpeg-path")
    parser.add_argument("--whisper-path")
    parser.add_argument("--whisper-model-path")
    parser.add_argument("--model-lock-path")
    parser.add_argument("--model-root")
    parser.add_argument("--whisper-binary-artifact-id")
    parser.add_argument("--whisper-model-artifact-id")
    parser.add_argument("--openclip-checkpoint-path")
    parser.add_argument("--openclip-artifact-id")
    parser.add_argument("--qwen-checkpoint-path")
    parser.add_argument("--qwen-artifact-id")
    parser.add_argument("--mlx-vlm-package-path")
    parser.add_argument("--mlx-vlm-artifact-id")
    parser.add_argument("--worker-runtime-profile-id")
    parser.add_argument("--worker-runtime-root")
    parser.add_argument("--worker-runtime-artifact-id")
    parser.add_argument("--worker-runtime-tree-sha256")
    parser.add_argument("--telemetry-fd")
    return parser.parse_args()


def _stage_telemetry(arguments: argparse.Namespace) -> StageTelemetryWriter:
    raw_value = getattr(arguments, "telemetry_fd", None)
    if raw_value is None:
        return StageTelemetryWriter.from_fd(None)
    if (type(raw_value) is int and raw_value == 3) or raw_value == "3":
        return StageTelemetryWriter.from_fd(3)
    raise StageTelemetryError("studio-model-worker-telemetry-invalid")


def _runtime_expectation(
    arguments: argparse.Namespace,
    *,
    registry: ArtifactRegistry | None = None,
    model_sha256: str | None = None,
) -> RuntimeExpectation:
    return RuntimeExpectation(
        registry=registry or ArtifactRegistry(arguments.model_lock_path, arguments.model_root),
        artifact_id=arguments.worker_runtime_artifact_id,
        runtime_root=arguments.worker_runtime_root,
        tree_sha256=arguments.worker_runtime_tree_sha256,
        model_sha256=model_sha256,
    )


def _preaccept_formal_vision(arguments: argparse.Namespace) -> RuntimeExpectation:
    registry = ArtifactRegistry(arguments.model_lock_path, arguments.model_root)
    model = registry.resolve_ready_artifact(
        arguments.qwen_artifact_id,
        arguments.qwen_checkpoint_path,
        executable=False,
        required_source_kind="weights",
    )
    expectation = _runtime_expectation(
        arguments,
        registry=registry,
        model_sha256=model["sha256"],
    )
    validate_runtime_process(expectation)
    return expectation


def _runtime_failure_receipt(
    job: dict,
    *,
    code: str,
    started_at: str,
    completed_at: str,
    runtime_ms: int,
) -> dict:
    receipt = build_unconfigured_receipt(
        job,
        started_at=started_at,
        completed_at=completed_at,
        runtime_ms=runtime_ms,
    )
    return {
        **receipt,
        "status": "failed-manual-review",
        "failure": {"code": code},
        "warnings": ["vision-requires-human-review"],
    }


def _vision_runtime_mode(arguments: argparse.Namespace) -> str | None:
    formal = (
        arguments.worker_runtime_root,
        arguments.worker_runtime_artifact_id,
        arguments.worker_runtime_tree_sha256,
    )
    if all(formal):
        return "formal"
    if any(formal):
        return None
    if all((arguments.mlx_vlm_package_path, arguments.mlx_vlm_artifact_id)):
        return "legacy-test"
    return None


def _apply_scheduled_vision_restart(receipt: dict, completed_vision_jobs: int) -> dict:
    if completed_vision_jobs % VISION_RESTART_JOBS != 0:
        return receipt
    quality = dict(receipt.get("qualitySignals", {}))
    existing = quality.get("workerRestart")
    restart = {
        "required": True,
        "reason": "scheduled-job-limit",
        "completedVisionJobs": completed_vision_jobs,
    }
    if isinstance(existing, dict) and existing.get("reason") != "scheduled-job-limit":
        restart["additionalReasons"] = [existing.get("reason", "unspecified")]
    quality["workerRestart"] = restart
    warnings = list(receipt.get("warnings", []))
    if "vision-worker-restart-required" not in warnings:
        warnings.append("vision-worker-restart-required")
    return {**receipt, "qualitySignals": quality, "warnings": warnings}


def main() -> int:
    arguments = _arguments()
    try:
        profile=getattr(arguments,"worker_runtime_profile_id",None)
        runtime_id=getattr(arguments,"worker_runtime_artifact_id",None)
        if profile is not None or (isinstance(runtime_id,str) and runtime_id.startswith("phone-editorial-")):
            ArtifactRegistry(arguments.model_lock_path,arguments.model_root).require_runtime_profile(runtime_id,profile)
        _stage_telemetry(arguments)
    except ArtifactRegistryError:
        sys.stderr.write("studio-runtime-profile-mismatch\n")
        return 2
    except StageTelemetryError:
        sys.stderr.write("studio-model-worker-telemetry-invalid\n")
        return 2
    completed_vision_jobs = 0
    for line in sys.stdin:
        telemetry = _stage_telemetry(arguments)
        started = time.monotonic_ns()
        started_at = _timestamp()
        try:
            job = validate_job(json.loads(line))
        except (json.JSONDecodeError, ProtocolError, TypeError, ValueError):
            sys.stderr.write("studio-model-worker-invalid-job\n")
            return 2
        is_vision = job["layer"] == "vision"
        if is_vision:
            completed_vision_jobs += 1
        completed_at = _timestamp()
        runtime_ms = max(0, (time.monotonic_ns() - started) // 1_000_000)
        try:
            if job["layer"] == "technical" and arguments.ffprobe_path:
                receipt = analyze_technical(job, arguments.ffprobe_path)
            elif job["layer"] == "scene" and arguments.ffprobe_path:
                receipt = analyze_scene(job, arguments.ffprobe_path, arguments.ffmpeg_path)
            elif job["layer"] == "transcript" and all((
                arguments.ffprobe_path,
                arguments.ffmpeg_path,
                arguments.whisper_path,
                arguments.whisper_model_path,
                arguments.model_lock_path,
                arguments.model_root,
                arguments.whisper_binary_artifact_id,
                arguments.whisper_model_artifact_id,
            )):
                receipt = analyze_transcript(
                    job,
                    arguments.ffprobe_path,
                    arguments.ffmpeg_path,
                    arguments.whisper_path,
                    arguments.whisper_model_path,
                    model_lock_path=arguments.model_lock_path,
                    model_root=arguments.model_root,
                    whisper_binary_artifact_id=arguments.whisper_binary_artifact_id,
                    whisper_model_artifact_id=arguments.whisper_model_artifact_id,
                )
            elif job["layer"] == "embedding" and all((
                arguments.model_lock_path,
                arguments.model_root,
                arguments.openclip_checkpoint_path,
                arguments.openclip_artifact_id,
            )):
                receipt = analyze_embedding(
                    job,
                    model_lock_path=arguments.model_lock_path,
                    model_root=arguments.model_root,
                    openclip_artifact_id=arguments.openclip_artifact_id,
                    checkpoint_path=arguments.openclip_checkpoint_path,
                )
            elif job["layer"] == "vision" and all((
                arguments.model_lock_path,
                arguments.model_root,
                arguments.qwen_checkpoint_path,
                arguments.qwen_artifact_id,
            )) and _vision_runtime_mode(arguments) is not None:
                runtime_mode = _vision_runtime_mode(arguments)
                if runtime_mode == "formal":
                    try:
                        telemetry.started("runtime-preaccept")
                        runtime_expectation = _preaccept_formal_vision(arguments)
                    except (ArtifactRegistryError, RuntimeIntegrityError) as error:
                        receipt = _runtime_failure_receipt(
                            job,
                            code=str(error),
                            started_at=started_at,
                            completed_at=completed_at,
                            runtime_ms=runtime_ms,
                        )
                        runtime_arguments = None
                    else:
                        runtime_arguments = {"runtime_expectation": runtime_expectation}
                else:
                    runtime_arguments = {
                        "runtime_artifact_id": arguments.mlx_vlm_artifact_id,
                        "runtime_package_path": arguments.mlx_vlm_package_path,
                    }
                if runtime_arguments is not None:
                    receipt = analyze_vision(
                        job,
                        model_lock_path=arguments.model_lock_path,
                        model_root=arguments.model_root,
                        qwen_artifact_id=arguments.qwen_artifact_id,
                        checkpoint_path=arguments.qwen_checkpoint_path,
                        context_provider=trusted_context_provider,
                        vision_job_count=completed_vision_jobs,
                        telemetry=telemetry,
                        runtime_preaccept_started=runtime_mode == "formal",
                        **runtime_arguments,
                    )
            else:
                receipt = build_unconfigured_receipt(
                    job,
                    started_at=started_at,
                    completed_at=completed_at,
                    runtime_ms=runtime_ms,
                )
        except StageTelemetryError:
            sys.stderr.write("studio-model-worker-telemetry-invalid\n")
            return 2
        except TechnicalConfigurationError:
            receipt = build_unconfigured_receipt(
                job,
                started_at=started_at,
                completed_at=completed_at,
                runtime_ms=runtime_ms,
            )
        if is_vision:
            receipt = _apply_scheduled_vision_restart(receipt, completed_vision_jobs)
        sys.stdout.write(json.dumps(receipt, ensure_ascii=False, separators=(",", ":")) + "\n")
        sys.stdout.flush()
        restart = receipt.get("qualitySignals", {}).get("workerRestart", {})
        if (
            is_vision
            and completed_vision_jobs % VISION_RESTART_JOBS == 0
        ) or restart.get("required") is True:
            return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
