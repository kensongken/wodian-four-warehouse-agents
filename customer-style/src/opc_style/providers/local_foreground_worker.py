"""Restricted subprocess adapter for an explicitly installed local candidate."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path
from threading import Lock

from opc_style.domain.errors import DomainRuleError
from opc_style.domain.foreground_benchmark import (
    LocalForegroundPipelineManifest,
    LocalModelArtifactManifest,
    LocalModelManifest,
    model_manifest_sha256,
    pipeline_manifest_sha256,
)
from opc_style.domain.product_foreground import (
    ExtractedForeground,
    ForegroundExtractionRequest,
    ForegroundStageReceipt,
    validate_extracted_foreground,
)

Runner = Callable[..., subprocess.CompletedProcess[bytes]]

_UNAVAILABLE = "foreground_candidate_unavailable"
_BUSY = "foreground_candidate_busy"
_FAILED = "foreground_candidate_failed"
_MANIFEST_MISMATCH = "foreground_candidate_manifest_mismatch"
_LICENSE_INVALID = "foreground_candidate_license_invalid"
_MODEL_FILENAMES = (
    "config.json",
    "model.safetensors",
    "preprocessor_config.json",
    "processor_config.json",
    "worker.py",
)
_PIPELINE_ARTIFACT_FILENAMES = (*_MODEL_FILENAMES, "LICENSE")
_OFFICIAL_PIPELINE_FILENAMES = {
    "coarse_segmenter": (
        "LICENSE",
        "config.json",
        "model.safetensors",
        "preprocessor_config.json",
        "processor_config.json",
        "worker.py",
    ),
    "alpha_refiner": (
        "BiRefNet_config.py",
        "LICENSE",
        "birefnet.py",
        "config.json",
        "model.safetensors",
    ),
}
_OFFICIAL_PIPELINE_CODE_FILENAMES = {
    "coarse_segmenter": (
        "config.json",
        "preprocessor_config.json",
        "processor_config.json",
        "worker.py",
    ),
    "alpha_refiner": ("BiRefNet_config.py", "birefnet.py", "config.json"),
}


class LocalForegroundWorker:
    """Execute a sealed local worker without adding model dependencies to the app."""

    def __init__(
        self,
        *,
        executable_path: Path,
        model_root: Path,
        manifest_path: Path,
        expected_model_manifest_sha256: str,
        timeout_seconds: int = 20,
        max_output_bytes: int = 20 * 1024 * 1024,
        runner: Runner = subprocess.run,
    ) -> None:
        self._executable_path = executable_path
        self._model_root = model_root
        self._manifest_path = manifest_path
        self._expected_model_manifest_sha256 = expected_model_manifest_sha256
        self._timeout_seconds = timeout_seconds
        self._max_output_bytes = max_output_bytes
        self._runner = runner
        self._execution_lock = Lock()
        self._startup_attestation_digest: str | None = None

    def extract(self, request: ForegroundExtractionRequest) -> ExtractedForeground:
        """Run one local model job at a time and fail clearly when it is busy."""
        if not self._execution_lock.acquire(blocking=False):
            raise DomainRuleError(_BUSY)
        try:
            return self._extract_locked(request)
        finally:
            self._execution_lock.release()

    def _extract_locked(
        self, request: ForegroundExtractionRequest
    ) -> ExtractedForeground:
        """Return a sealed candidate or a sanitized, fail-closed domain error."""
        manifest, manifest_digest = self._validated_installation()
        if (
            request.width <= 0
            or request.height <= 0
            or sha256(request.source_content).hexdigest()
            != request.source_content_sha256
        ):
            raise DomainRuleError("foreground_source_incomplete")
        prompt_payload = self._prompt_payload(request)
        is_pipeline = isinstance(manifest, LocalForegroundPipelineManifest)
        pipeline_manifest: LocalForegroundPipelineManifest | None = None
        if isinstance(manifest, LocalForegroundPipelineManifest):
            pipeline_manifest = manifest
        coarse: bytes | None = None
        stage_receipts: tuple[ForegroundStageReceipt, ...] = ()

        try:
            with tempfile.TemporaryDirectory(prefix="opc-local-foreground-") as directory:
                root = Path(directory).resolve(strict=True)
                input_path = root / "source-image"
                mask_path = root / "mask.png"
                coarse_mask_path = root / "coarse-mask.png"
                foreground_path = root / "foreground.png"
                receipt_path = root / "receipt.json"
                prompt_path = root / "prompt.json"
                input_path.write_bytes(request.source_content)
                prompt_path.write_text(
                    json.dumps(prompt_payload, sort_keys=True, separators=(",", ":")),
                    encoding="utf-8",
                )
                input_path.chmod(0o600)
                prompt_path.chmod(0o600)
                command = [
                    os.fspath(self._executable_path),
                    "--input",
                    os.fspath(input_path),
                ]
                if is_pipeline:
                    command.extend(
                        ["--coarse-mask-output", os.fspath(coarse_mask_path)]
                    )
                command.extend([
                    "--mask-output",
                    os.fspath(mask_path),
                    "--foreground-output",
                    os.fspath(foreground_path),
                    "--receipt-output",
                    os.fspath(receipt_path),
                    "--prompt-json",
                    os.fspath(prompt_path),
                    "--model-root",
                    os.fspath(self._model_root),
                    "--model-manifest-sha256",
                    manifest_digest,
                ])
                completed = self._runner(
                    command,
                    check=False,
                    capture_output=True,
                    timeout=self._timeout_seconds,
                    env={
                        "LANG": "C",
                        "PATH": "/usr/bin:/bin",
                        "HF_HUB_OFFLINE": "1",
                        "TRANSFORMERS_OFFLINE": "1",
                        "NO_PROXY": "*",
                    },
                    shell=False,
                )
                if completed.returncode != 0:
                    raise DomainRuleError(_FAILED)
                mask = self._read_output(root, mask_path, self._max_output_bytes)
                if is_pipeline:
                    coarse = self._read_output(
                        root, coarse_mask_path, self._max_output_bytes
                    )
                foreground = self._read_output(
                    root, foreground_path, self._max_output_bytes
                )
                receipt_content = self._read_output(root, receipt_path, 64 * 1024)
                stage_receipts = self._verify_receipt(
                    receipt_content,
                    source_sha256=request.source_content_sha256,
                    prompt_content=prompt_path.read_bytes(),
                    coarse_mask=coarse,
                    mask=mask,
                    foreground=foreground,
                    model_manifest_sha256=manifest_digest,
                    pipeline_manifest=pipeline_manifest,
                )
        except DomainRuleError:
            raise
        except (OSError, subprocess.SubprocessError, ValueError, TypeError):
            raise DomainRuleError(_FAILED) from None
        except Exception:  # noqa: BLE001 - never leak worker internals or private paths
            raise DomainRuleError(_FAILED) from None

        version = (
            manifest.pipeline_version
            if isinstance(manifest, LocalForegroundPipelineManifest)
            else manifest.model_name
        )
        result = ExtractedForeground(
            foreground_png=foreground,
            foreground_sha256=sha256(foreground).hexdigest(),
            mask_png=mask,
            mask_sha256=sha256(mask).hexdigest(),
            width=request.width,
            height=request.height,
            extractor_version=f"local-{version}:{manifest_digest[:12]}",
            coarse_mask_png=coarse,
            coarse_mask_sha256=(sha256(coarse).hexdigest() if coarse else None),
            stage_receipts=stage_receipts,
            pipeline_manifest_sha256=manifest_digest if is_pipeline else None,
        )
        try:
            validate_extracted_foreground(result)
        except DomainRuleError:
            raise DomainRuleError(_FAILED) from None
        return result

    def verify_installation(self) -> str:
        """Verify the complete sealed installation during application startup."""
        _, digest = self._validated_installation()
        self._startup_attestation_digest = digest
        return digest

    def installation_manifest(self) -> LocalForegroundPipelineManifest:
        """Return the verified pipeline identity used by local benchmark evidence."""
        manifest, _ = self._validated_installation()
        if not isinstance(manifest, LocalForegroundPipelineManifest):
            raise DomainRuleError(_MANIFEST_MISMATCH)
        return manifest

    def startup_attestation_digest(self) -> str:
        """Return the startup proof without re-hashing model weights per HTTP request."""
        if self._startup_attestation_digest is None:
            return self.verify_installation()
        return self._startup_attestation_digest

    def _validated_installation(
        self,
    ) -> tuple[LocalModelManifest | LocalForegroundPipelineManifest, str]:
        paths = (self._executable_path, self._model_root, self._manifest_path)
        if (
            any(not path.is_absolute() or path.is_symlink() for path in paths)
            or not self._executable_path.is_file()
            or not os.access(self._executable_path, os.X_OK)
            or not self._model_root.is_dir()
            or not self._manifest_path.is_file()
            or self._timeout_seconds <= 0
            or self._max_output_bytes <= 0
        ):
            raise DomainRuleError(_UNAVAILABLE)
        try:
            resolved_root = self._model_root.resolve(strict=True)
            if self._manifest_path.resolve(strict=True).parent != resolved_root:
                raise DomainRuleError(_UNAVAILABLE)
            payload = json.loads(self._manifest_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise DomainRuleError(_MANIFEST_MISMATCH)
            versions = payload.get("runtime_versions")
            if not isinstance(versions, list):
                raise DomainRuleError(_MANIFEST_MISMATCH)
            payload["runtime_versions"] = tuple(
                tuple(item) if isinstance(item, list) else item for item in versions
            )
            manifest: LocalModelManifest | LocalForegroundPipelineManifest
            if "artifacts" in payload:
                raw_artifacts = payload.get("artifacts")
                if not isinstance(raw_artifacts, list):
                    raise DomainRuleError(_MANIFEST_MISMATCH)
                payload["artifacts"] = tuple(
                    LocalModelArtifactManifest(
                        **{
                            **item,
                            "files_sha256": tuple(
                                tuple(entry) for entry in item.get("files_sha256", ())
                            ),
                        }
                    )
                    for item in raw_artifacts
                    if isinstance(item, dict)
                )
                if len(payload["artifacts"]) != len(raw_artifacts):
                    raise DomainRuleError(_MANIFEST_MISMATCH)
                manifest = LocalForegroundPipelineManifest(**payload)
            else:
                manifest = LocalModelManifest(**payload)
        except DomainRuleError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError):
            raise DomainRuleError(_MANIFEST_MISMATCH) from None
        if isinstance(manifest, LocalForegroundPipelineManifest):
            if any(
                artifact.license_id
                != {
                    "coarse_segmenter": "Apache-2.0",
                    "alpha_refiner": "MIT",
                }[artifact.role]
                for artifact in manifest.artifacts
            ):
                raise DomainRuleError(_LICENSE_INVALID)
            digest = pipeline_manifest_sha256(manifest)
            self._validate_pipeline_files(manifest, digest)
            return manifest, digest
        if manifest.license_id != "Apache-2.0":
            raise DomainRuleError(_LICENSE_INVALID)
        digest = model_manifest_sha256(manifest)
        if any(
            not (self._model_root / filename).is_file()
            for filename in _MODEL_FILENAMES
        ):
            raise DomainRuleError(_UNAVAILABLE)
        if (
            digest != self._expected_model_manifest_sha256
            or sha256(self._executable_path.read_bytes()).hexdigest()
            != manifest.executable_sha256
            or sha256((self._model_root / "model.safetensors").read_bytes()).hexdigest()
            != manifest.weights_sha256
            or self._model_tree_sha256() != manifest.model_tree_sha256
            or sum((self._model_root / filename).stat().st_size for filename in _MODEL_FILENAMES)
            != manifest.total_bytes
        ):
            raise DomainRuleError(_MANIFEST_MISMATCH)
        return manifest, digest

    def _validate_pipeline_files(
        self, manifest: LocalForegroundPipelineManifest, digest: str
    ) -> None:
        directories = {
            "coarse_segmenter": self._model_root / "sam2",
            "alpha_refiner": self._model_root / "birefnet",
        }
        worker_path = self._model_root / "worker.py"
        total_bytes = self._executable_path.stat().st_size
        if (
            digest != self._expected_model_manifest_sha256
            or worker_path.is_symlink()
            or not worker_path.is_file()
            or sha256(self._executable_path.read_bytes()).hexdigest()
            != manifest.executable_sha256
            or sha256(worker_path.read_bytes()).hexdigest() != manifest.worker_sha256
        ):
            raise DomainRuleError(_MANIFEST_MISMATCH)
        total_bytes += worker_path.stat().st_size
        for artifact in manifest.artifacts:
            directory = directories[artifact.role]
            strict = manifest.pipeline_version == "sam2-birefnet-official-v1"
            filenames = (
                _OFFICIAL_PIPELINE_FILENAMES[artifact.role]
                if strict
                else _PIPELINE_ARTIFACT_FILENAMES
            )
            paths = tuple(directory / name for name in filenames)
            if (
                not directory.is_absolute()
                or directory.is_symlink()
                or not directory.is_dir()
                or any(path.is_symlink() or not path.is_file() for path in paths)
                or sha256((directory / "model.safetensors").read_bytes()).hexdigest()
                != artifact.weights_sha256
                or sha256((directory / "LICENSE").read_bytes()).hexdigest()
                != artifact.license_sha256
                or self._pipeline_code_tree_sha256(directory, artifact.role, strict)
                != artifact.code_tree_sha256
                or (
                    strict
                    and tuple(
                        (name, sha256((directory / name).read_bytes()).hexdigest())
                        for name in filenames
                    )
                    != artifact.files_sha256
                )
            ):
                raise DomainRuleError(_MANIFEST_MISMATCH)
            if strict and tuple(sorted(path.name for path in directory.iterdir())) != filenames:
                raise DomainRuleError(_MANIFEST_MISMATCH)
            total_bytes += sum(path.stat().st_size for path in paths)
        if total_bytes != manifest.total_bytes:
            raise DomainRuleError(_MANIFEST_MISMATCH)

    @staticmethod
    def _pipeline_code_tree_sha256(
        directory: Path, role: str, strict: bool
    ) -> str:
        filenames = (
            _OFFICIAL_PIPELINE_CODE_FILENAMES[role]
            if strict
            else ("config.json", "preprocessor_config.json", "processor_config.json", "worker.py")
        )
        payload = "".join(
            f"{filename}\0{sha256((directory / filename).read_bytes()).hexdigest()}\n"
            for filename in filenames
        ).encode()
        return sha256(payload).hexdigest()

    def _model_tree_sha256(self) -> str:
        payload = "".join(
            f"{filename}\0{sha256((self._model_root / filename).read_bytes()).hexdigest()}\n"
            for filename in _MODEL_FILENAMES
        ).encode()
        return sha256(payload).hexdigest()

    @staticmethod
    def _prompt_payload(request: ForegroundExtractionRequest) -> dict[str, object]:
        hints = request.prompt_hints
        if hints is None:
            return {
                "box": [0.0, 0.0, 1.0, 1.0],
                "negative_points": [],
                "positive_points": [],
            }
        values = [
            *(hints.box or ()),
            *(coordinate for point in hints.positive_points for coordinate in point),
            *(coordinate for point in hints.negative_points for coordinate in point),
        ]
        if any(value < 0 or value > 1 for value in values):
            raise DomainRuleError("foreground_prompt_invalid")
        if hints.box is not None and (
            hints.box[0] >= hints.box[2] or hints.box[1] >= hints.box[3]
        ):
            raise DomainRuleError("foreground_prompt_invalid")
        return {
            "box": list(hints.box) if hints.box is not None else None,
            "negative_points": [list(point) for point in hints.negative_points],
            "positive_points": [list(point) for point in hints.positive_points],
        }

    @staticmethod
    def _read_output(root: Path, path: Path, limit: int) -> bytes:
        if path.is_symlink() or not path.is_file():
            raise DomainRuleError(_FAILED)
        try:
            resolved = path.resolve(strict=True)
        except OSError:
            raise DomainRuleError(_FAILED) from None
        if resolved.parent != root or resolved.stat().st_size <= 0 or resolved.stat().st_size > limit:
            raise DomainRuleError(_FAILED)
        return resolved.read_bytes()

    @staticmethod
    def _verify_receipt(
        content: bytes,
        *,
        source_sha256: str,
        prompt_content: bytes,
        coarse_mask: bytes | None,
        mask: bytes,
        foreground: bytes,
        model_manifest_sha256: str,
        pipeline_manifest: LocalForegroundPipelineManifest | None,
    ) -> tuple[ForegroundStageReceipt, ...]:
        try:
            receipt = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise DomainRuleError(_FAILED) from None
        expected: dict[str, object] = {
            "source_sha256": source_sha256,
            "mask_sha256": sha256(mask).hexdigest(),
            "foreground_sha256": sha256(foreground).hexdigest(),
            "model_manifest_sha256": model_manifest_sha256,
        }
        if pipeline_manifest is None:
            if receipt != expected:
                raise DomainRuleError(_FAILED)
            return ()
        if coarse_mask is None:
            raise DomainRuleError(_FAILED)
        expected["coarse_mask_sha256"] = sha256(coarse_mask).hexdigest()
        raw_stages = receipt.pop("stage_receipts", None)
        if receipt != expected or not isinstance(raw_stages, list):
            raise DomainRuleError(_FAILED)
        try:
            stages = tuple(ForegroundStageReceipt(**stage) for stage in raw_stages)
        except (TypeError, ValueError):
            raise DomainRuleError(_FAILED) from None
        prompt_sha = sha256(prompt_content).hexdigest()
        refined_parameters_sha = sha256(
            prompt_content + sha256(coarse_mask).hexdigest().encode()
        ).hexdigest()
        expected_artifacts = tuple(
            artifact.weights_sha256 for artifact in pipeline_manifest.artifacts
        )
        if (
            tuple(stage.role for stage in stages)
            != ("coarse_segmenter", "alpha_refiner")
            or tuple(stage.artifact_sha256 for stage in stages)
            != expected_artifacts
            or tuple(stage.output_sha256 for stage in stages)
            != (sha256(coarse_mask).hexdigest(), sha256(mask).hexdigest())
            or tuple(stage.parameters_sha256 for stage in stages)
            != (prompt_sha, refined_parameters_sha)
        ):
            raise DomainRuleError(_FAILED)
        return stages
