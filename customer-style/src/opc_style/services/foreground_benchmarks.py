"""Validate and freeze repository-external foreground benchmark datasets."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import UUID, uuid4

from PIL import Image, UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.orm import Session

from opc_style.domain.errors import DomainRuleError
from opc_style.domain.foreground_benchmark import (
    CriticalComponent,
    GoldSampleManifest,
    LocalForegroundPipelineManifest,
    LocalModelManifest,
    benchmark_manifest_sha256,
    model_manifest_sha256,
    pipeline_manifest_sha256,
    score_foreground_mask,
)
from opc_style.models import (
    ForegroundBenchmarkRun,
    ForegroundBenchmarkSampleResult,
    ForegroundShadowDecision,
)

_FILE_INVALID = "foreground_benchmark_file_invalid"
_MANIFEST_INVALID = "foreground_benchmark_manifest_invalid"
_REQUIRED_DIFFICULTIES = {"easy", "medium", "hard"}
_REQUIRED_SCENES = {"complex_background", "glove", "tray", "bust"}
_REQUIRED_MATERIALS = {"fine_chain", "transparent", "reflective"}
_REQUIRED_COMPONENTS = {"clasp", "pendant"}
_BENCHMARK_POLICY = {
    "background_contamination_strictly_below": 0.02,
    "minimum_auto_eligible_count": 40,
    "minimum_component_preservation": 0.95,
    "minimum_foreground_recall": 0.95,
    "policy": "frozen-acceptance-pixel-gate-v1",
}
FOREGROUND_BENCHMARK_CONFIG_SHA256 = sha256(
    json.dumps(
        _BENCHMARK_POLICY,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
).hexdigest()


@dataclass(frozen=True)
class ValidatedBenchmarkSample:
    """Path-free local bytes and their already validated gold manifest."""

    manifest: GoldSampleManifest
    source_content: bytes
    gold_mask_content: bytes
    critical_component_masks: tuple[tuple[str, bytes], ...]


@dataclass(frozen=True)
class ValidatedBenchmarkDataset:
    """A sealed local dataset safe to pass to offline benchmark services."""

    samples: tuple[ValidatedBenchmarkSample, ...]
    seal_sha256: str
    acceptance_count: int
    calibration_count: int
    categories: tuple[str, ...]


def _private_file(root: Path, relative_value: object) -> tuple[Path, bytes]:
    if not isinstance(relative_value, str):
        raise DomainRuleError(_MANIFEST_INVALID)
    relative = Path(relative_value)
    if relative.is_absolute() or ".." in relative.parts:
        raise DomainRuleError(_FILE_INVALID)
    candidate = root / relative
    if candidate.is_symlink() or not candidate.is_file():
        raise DomainRuleError(_FILE_INVALID)
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError):
        raise DomainRuleError(_FILE_INVALID) from None
    if resolved.stat().st_mode & 0o077:
        raise DomainRuleError("foreground_benchmark_file_permissions")
    try:
        return resolved, resolved.read_bytes()
    except OSError:
        raise DomainRuleError(_FILE_INVALID) from None


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise DomainRuleError(_MANIFEST_INVALID)
    return value


def _positive_int(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise DomainRuleError(_MANIFEST_INVALID)
    return value


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise DomainRuleError(_MANIFEST_INVALID)
    return tuple(value)


def _verify_digest(content: bytes, expected: object) -> str:
    digest = _string(expected)
    if sha256(content).hexdigest() != digest:
        raise DomainRuleError("foreground_benchmark_digest_mismatch")
    return digest


def _verify_image(content: bytes, width: int, height: int, *, mask: bool) -> None:
    try:
        with Image.open(BytesIO(content)) as image:
            image.load()
            if image.size != (width, height):
                raise DomainRuleError(_FILE_INVALID)
            if mask and (image.format != "PNG" or image.mode != "L"):
                raise DomainRuleError(_FILE_INVALID)
    except DomainRuleError:
        raise
    except (Image.DecompressionBombError, UnidentifiedImageError, OSError, ValueError):
        raise DomainRuleError(_FILE_INVALID) from None


def _component(
    root: Path,
    payload: object,
    *,
    width: int,
    height: int,
) -> tuple[CriticalComponent, bytes]:
    if not isinstance(payload, dict):
        raise DomainRuleError(_MANIFEST_INVALID)
    _, content = _private_file(root, payload.get("mask_file"))
    _verify_image(content, width, height, mask=True)
    digest = _verify_digest(content, payload.get("mask_sha256"))
    return CriticalComponent(name=_string(payload.get("name")), mask_sha256=digest), content


def _sample(root: Path, payload: object) -> ValidatedBenchmarkSample:
    if not isinstance(payload, dict):
        raise DomainRuleError(_MANIFEST_INVALID)
    width = _positive_int(payload.get("width"))
    height = _positive_int(payload.get("height"))
    _, source = _private_file(root, payload.get("source_file"))
    _, gold = _private_file(root, payload.get("gold_mask_file"))
    _verify_image(source, width, height, mask=False)
    _verify_image(gold, width, height, mask=True)
    source_digest = _verify_digest(source, payload.get("source_sha256"))
    gold_digest = _verify_digest(gold, payload.get("gold_mask_sha256"))
    component_payloads = payload.get("critical_components")
    if not isinstance(component_payloads, list):
        raise DomainRuleError(_MANIFEST_INVALID)
    components = tuple(
        _component(root, item, width=width, height=height)
        for item in component_payloads
    )
    reviewed = payload.get("human_reviewed")
    if not isinstance(reviewed, bool):
        raise DomainRuleError(_MANIFEST_INVALID)
    manifest = GoldSampleManifest(
        sample_id=_string(payload.get("sample_id")),
        source_sha256=source_digest,
        width=width,
        height=height,
        mime_type=_string(payload.get("mime_type")),
        category=_string(payload.get("category")),
        difficulty=_string(payload.get("difficulty")),
        scene_tags=_string_tuple(payload.get("scene_tags")),
        material_tags=_string_tuple(payload.get("material_tags")),
        gold_mask_sha256=gold_digest,
        critical_components=tuple(component for component, _ in components),
        human_reviewed=reviewed,
        split=_string(payload.get("split")),  # type: ignore[arg-type]
    )
    return ValidatedBenchmarkSample(
        manifest=manifest,
        source_content=source,
        gold_mask_content=gold,
        critical_component_masks=tuple(
            (component.name, content) for component, content in components
        ),
    )


def _read_manifest(root: Path) -> list[object]:
    _, content = _private_file(root, "manifest.json")
    try:
        payload: Any = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise DomainRuleError(_MANIFEST_INVALID) from None
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise DomainRuleError(_MANIFEST_INVALID)
    samples = payload.get("samples")
    if not isinstance(samples, list):
        raise DomainRuleError(_MANIFEST_INVALID)
    return samples


def validate_dataset(root: Path) -> ValidatedBenchmarkDataset:
    """Load, verify, and seal a private benchmark without exposing its paths."""
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise DomainRuleError("foreground_benchmark_unavailable")
    try:
        resolved_root = root.resolve(strict=True)
    except OSError:
        raise DomainRuleError("foreground_benchmark_unavailable") from None
    samples = tuple(_sample(resolved_root, item) for item in _read_manifest(resolved_root))
    acceptance = tuple(sample for sample in samples if sample.manifest.split == "acceptance")
    calibration = tuple(sample for sample in samples if sample.manifest.split == "calibration")
    if len(acceptance) != 50:
        raise DomainRuleError("foreground_benchmark_acceptance_count_invalid")
    acceptance_sources = {sample.manifest.source_sha256 for sample in acceptance}
    calibration_sources = {sample.manifest.source_sha256 for sample in calibration}
    if acceptance_sources & calibration_sources:
        raise DomainRuleError("foreground_benchmark_split_overlap")

    difficulties = {sample.manifest.difficulty for sample in acceptance}
    scenes = {tag for sample in acceptance for tag in sample.manifest.scene_tags}
    materials = {tag for sample in acceptance for tag in sample.manifest.material_tags}
    components = {
        component.name
        for sample in acceptance
        for component in sample.manifest.critical_components
    }
    if (
        not _REQUIRED_DIFFICULTIES <= difficulties
        or not _REQUIRED_SCENES <= scenes
        or not _REQUIRED_MATERIALS <= materials
        or not _REQUIRED_COMPONENTS <= components
    ):
        raise DomainRuleError("foreground_benchmark_coverage_incomplete")

    manifests = tuple(sample.manifest for sample in samples)
    seal = benchmark_manifest_sha256(manifests)
    return ValidatedBenchmarkDataset(
        samples=samples,
        seal_sha256=seal,
        acceptance_count=len(acceptance),
        calibration_count=len(calibration),
        categories=tuple(sorted({sample.manifest.category for sample in samples})),
    )


def _atomic_private_write(path: Path, content: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    if path.is_symlink() or temporary.is_symlink():
        raise DomainRuleError(_FILE_INVALID)
    try:
        temporary.write_bytes(content)
        temporary.chmod(0o600)
        os.replace(temporary, path)
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise DomainRuleError(_FILE_INVALID) from None


def freeze_dataset(root: Path) -> ValidatedBenchmarkDataset:
    """Write a path-free canonical snapshot and seal after complete validation."""
    dataset = validate_dataset(root)
    payload = {
        "version": 1,
        "seal_sha256": dataset.seal_sha256,
        "samples": [
            sample.manifest.canonical_dict()
            for sample in sorted(dataset.samples, key=lambda item: item.manifest.sample_id)
        ],
    }
    serialized = json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode()
    _atomic_private_write(root / "frozen-manifest.json", serialized)
    _atomic_private_write(root / "seal.sha256", f"{dataset.seal_sha256}\n".encode("ascii"))
    return dataset


@dataclass(frozen=True)
class ForegroundBenchmarkReport:
    """Deterministic aggregate rebuilt only from persisted metric evidence."""

    run_id: UUID
    sample_count: int
    auto_eligible_count: int
    human_review_required_count: int
    blocked_count: int
    failure_count: int
    mean_iou: float
    mean_background_contamination: float
    minimum_critical_component_preservation: float
    maximum_auto_eligible_contamination: float
    bad_auto_eligible_count: int
    total_elapsed_ms: int
    hardest_sample_ids: tuple[str, ...]
    passed: bool


MaskProvider = Callable[[ValidatedBenchmarkSample], bytes]


class ForegroundBenchmarkService:
    """Run, resume, and replay a path-free local benchmark ledger."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def run_benchmark(
        self,
        *,
        customer_id: UUID,
        dataset: ValidatedBenchmarkDataset,
        model_manifest: LocalModelManifest | LocalForegroundPipelineManifest,
        config_sha256: str,
        idempotency_key: str | None = None,
        mask_provider: MaskProvider,
        resume_run_id: UUID | None = None,
    ) -> ForegroundBenchmarkRun:
        if not dataset.samples or len(config_sha256) != 64:
            raise DomainRuleError("foreground_benchmark_run_invalid")
        acceptance_samples = tuple(
            sample
            for sample in dataset.samples
            if sample.manifest.split == "acceptance"
        )
        if (
            not acceptance_samples
            or len(acceptance_samples) != dataset.acceptance_count
        ):
            raise DomainRuleError("foreground_benchmark_acceptance_count_invalid")
        sample_ids = [sample.manifest.sample_id for sample in acceptance_samples]
        if len(sample_ids) != len(set(sample_ids)):
            raise DomainRuleError("foreground_benchmark_duplicate_sample")
        model_digest = (
            pipeline_manifest_sha256(model_manifest)
            if isinstance(model_manifest, LocalForegroundPipelineManifest)
            else model_manifest_sha256(model_manifest)
        )
        run = self._resolve_run(
            customer_id=customer_id,
            dataset=dataset,
            model_digest=model_digest,
            config_sha256=config_sha256,
            idempotency_key=idempotency_key,
            resume_run_id=resume_run_id,
        )
        existing = {
            row.sample_id
            for row in self._session.scalars(
                select(ForegroundBenchmarkSampleResult).where(
                    ForegroundBenchmarkSampleResult.customer_id == customer_id,
                    ForegroundBenchmarkSampleResult.run_id == run.id,
                )
            )
        }
        for sample in sorted(
            acceptance_samples, key=lambda item: item.manifest.sample_id
        ):
            if sample.manifest.sample_id in existing:
                continue
            self._record_sample(run, sample, mask_provider)
            self._session.flush()
        rows = tuple(
            self._session.scalars(
                select(ForegroundBenchmarkSampleResult).where(
                    ForegroundBenchmarkSampleResult.customer_id == customer_id,
                    ForegroundBenchmarkSampleResult.run_id == run.id,
                )
            )
        )
        if len(rows) != run.total_samples:
            raise DomainRuleError("foreground_benchmark_resume_incomplete")
        run.completed_samples = sum(row.status == "succeeded" for row in rows)
        run.failed_samples = sum(row.status == "failed" for row in rows)
        run.status = "completed"
        run.completed_at = datetime.now(UTC)
        self._session.flush()
        return run

    def replay_report(
        self, *, customer_id: UUID, run_id: UUID
    ) -> ForegroundBenchmarkReport:
        run = self._session.scalar(
            select(ForegroundBenchmarkRun).where(
                ForegroundBenchmarkRun.id == run_id,
                ForegroundBenchmarkRun.customer_id == customer_id,
            )
        )
        if run is None or run.status == "running":
            raise DomainRuleError("foreground_benchmark_run_not_found")
        rows = tuple(
            self._session.scalars(
                select(ForegroundBenchmarkSampleResult).where(
                    ForegroundBenchmarkSampleResult.customer_id == customer_id,
                    ForegroundBenchmarkSampleResult.run_id == run_id,
                )
            )
        )
        count = len(rows)
        if count != run.total_samples:
            raise DomainRuleError("foreground_benchmark_evidence_incomplete")
        auto_rows = tuple(row for row in rows if row.verdict == "auto_eligible")
        minimum_component_preservation = min(
            (float(row.critical_component_preservation_rate) for row in rows),
            default=0.0,
        )
        maximum_auto_contamination = max(
            (float(row.background_contamination) for row in auto_rows),
            default=0.0,
        )
        bad_auto_eligible_count = sum(
            row.verdict == "auto_eligible"
            and (
                float(row.foreground_recall) < 0.95
                or float(row.background_contamination) >= 0.02
                or float(row.critical_component_preservation_rate) < 0.95
            )
            for row in rows
        )
        hardest = tuple(
            row.sample_id
            for row in sorted(
                rows,
                key=lambda row: (
                    row.status == "succeeded",
                    float(row.iou),
                    -float(row.background_contamination),
                    float(row.critical_component_preservation_rate),
                    row.sample_id,
                ),
            )[:10]
        )
        auto_count = len(auto_rows)
        failure_count = sum(row.status == "failed" for row in rows)
        passed = (
            count == 50
            and auto_count >= 40
            and minimum_component_preservation >= 0.95
            and maximum_auto_contamination < 0.02
            and bad_auto_eligible_count == 0
            and failure_count == 0
        )
        return ForegroundBenchmarkReport(
            run_id=run.id,
            sample_count=count,
            auto_eligible_count=auto_count,
            human_review_required_count=sum(
                row.verdict == "human_review_required" for row in rows
            ),
            blocked_count=sum(row.verdict == "blocked" for row in rows),
            failure_count=failure_count,
            mean_iou=(sum(float(row.iou) for row in rows) / count),
            mean_background_contamination=(
                sum(float(row.background_contamination) for row in rows) / count
            ),
            minimum_critical_component_preservation=minimum_component_preservation,
            maximum_auto_eligible_contamination=maximum_auto_contamination,
            bad_auto_eligible_count=bad_auto_eligible_count,
            total_elapsed_ms=sum(row.elapsed_ms for row in rows),
            hardest_sample_ids=hardest,
            passed=passed,
        )

    def _resolve_run(
        self,
        *,
        customer_id: UUID,
        dataset: ValidatedBenchmarkDataset,
        model_digest: str,
        config_sha256: str,
        idempotency_key: str | None,
        resume_run_id: UUID | None,
    ) -> ForegroundBenchmarkRun:
        if resume_run_id is None:
            run = ForegroundBenchmarkRun(
                customer_id=customer_id,
                dataset_seal_sha256=dataset.seal_sha256,
                model_manifest_sha256=model_digest,
                config_sha256=config_sha256,
                idempotency_key=idempotency_key or f"service-{uuid4()}",
                status="running",
            total_samples=dataset.acceptance_count,
                completed_samples=0,
                failed_samples=0,
            )
            self._session.add(run)
            self._session.flush()
            return run
        resumed_run = self._session.scalar(
            select(ForegroundBenchmarkRun).where(
                ForegroundBenchmarkRun.id == resume_run_id,
                ForegroundBenchmarkRun.customer_id == customer_id,
            )
        )
        if (
            resumed_run is None
            or resumed_run.status != "running"
            or resumed_run.dataset_seal_sha256 != dataset.seal_sha256
            or resumed_run.model_manifest_sha256 != model_digest
            or resumed_run.config_sha256 != config_sha256
            or (
                idempotency_key is not None
                and resumed_run.idempotency_key != idempotency_key
            )
            or resumed_run.total_samples != len(dataset.samples)
        ):
            raise DomainRuleError("foreground_benchmark_resume_mismatch")
        return resumed_run

    def _record_sample(
        self,
        run: ForegroundBenchmarkRun,
        sample: ValidatedBenchmarkSample,
        mask_provider: MaskProvider,
    ) -> None:
        started = perf_counter()
        try:
            predicted = mask_provider(sample)
            metric = score_foreground_mask(
                predicted,
                sample.gold_mask_content,
                sample.manifest.width,
                sample.manifest.height,
                critical_component_masks=dict(sample.critical_component_masks),
            )
            verdict = (
                "auto_eligible"
                if metric.pixel_safe_for_auto_eligible
                else "human_review_required"
            )
            reasons = self._metric_reasons(metric)
            values = {
                "status": "succeeded",
                "verdict": verdict,
                "true_positive_pixels": metric.true_positive_pixels,
                "false_positive_pixels": metric.false_positive_pixels,
                "false_negative_pixels": metric.false_negative_pixels,
                "iou": Decimal(str(metric.iou)),
                "foreground_recall": Decimal(str(metric.foreground_recall)),
                "background_contamination": Decimal(
                    str(metric.background_contamination)
                ),
                "critical_component_recalls": dict(
                    metric.critical_component_recalls
                ),
                "critical_component_preservation_rate": Decimal(
                    str(metric.critical_component_preservation_rate)
                ),
                "failure_code": None,
                "evidence_complete": True,
            }
        except Exception:  # noqa: BLE001 - persisted failures must never leak paths or bytes
            verdict = "blocked"
            reasons = ["foreground_candidate_failed"]
            values = {
                "status": "failed",
                "verdict": verdict,
                "true_positive_pixels": 0,
                "false_positive_pixels": 0,
                "false_negative_pixels": 0,
                "iou": Decimal(0),
                "foreground_recall": Decimal(0),
                "background_contamination": Decimal(0),
                "critical_component_recalls": {},
                "critical_component_preservation_rate": Decimal(0),
                "failure_code": "foreground_candidate_failed",
                "evidence_complete": False,
            }
        elapsed_ms = max(0, round((perf_counter() - started) * 1000))
        result = ForegroundBenchmarkSampleResult(
            customer_id=run.customer_id,
            run_id=run.id,
            sample_id=sample.manifest.sample_id,
            elapsed_ms=elapsed_ms,
            **{key: value for key, value in values.items() if key != "evidence_complete"},
        )
        self._session.add(result)
        self._session.flush()
        self._session.add(
            ForegroundShadowDecision(
                customer_id=run.customer_id,
                run_id=run.id,
                sample_result_id=result.id,
                foreground_version_id=None,
                model_manifest_sha256=run.model_manifest_sha256,
                config_sha256=run.config_sha256,
                verdict=verdict,
                reason_codes=reasons,
                evidence_complete=bool(values["evidence_complete"]),
            )
        )
        if values["status"] == "succeeded":
            run.completed_samples += 1
        else:
            run.failed_samples += 1

    @staticmethod
    def _metric_reasons(metric: Any) -> list[str]:
        reasons: list[str] = []
        if metric.foreground_recall < 0.95:
            reasons.append("foreground_recall_below_threshold")
        if metric.background_contamination >= 0.02:
            reasons.append("background_contamination_above_threshold")
        if metric.critical_component_preservation_rate < 0.95:
            reasons.append("critical_component_loss")
        return reasons
