"""Deterministic contracts and pixel metrics for local foreground benchmarks."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from hashlib import sha256
from io import BytesIO
from string import hexdigits
from typing import Literal

from PIL import Image, UnidentifiedImageError

from opc_style.domain.errors import DomainRuleError

RiskVerdict = Literal["auto_eligible", "human_review_required", "blocked"]
BenchmarkSplit = Literal["calibration", "acceptance"]
ModelArtifactRole = Literal["coarse_segmenter", "alpha_refiner"]

_INVALID_MANIFEST = "foreground_benchmark_manifest_invalid"
_INVALID_MASK = "foreground_benchmark_mask_invalid"
_COMPONENT_PRESERVATION_THRESHOLD = 0.95
_BACKGROUND_CONTAMINATION_THRESHOLD = 0.02


@dataclass(frozen=True)
class CriticalComponent:
    """One visible product component whose own pixels must survive extraction."""

    name: str
    mask_sha256: str


@dataclass(frozen=True)
class GoldSampleManifest:
    """Path-free evidence for one locally stored, human-reviewed gold sample."""

    sample_id: str
    source_sha256: str
    width: int
    height: int
    mime_type: str
    category: str
    difficulty: str
    scene_tags: tuple[str, ...]
    material_tags: tuple[str, ...]
    gold_mask_sha256: str
    critical_components: tuple[CriticalComponent, ...]
    human_reviewed: bool
    split: BenchmarkSplit

    def canonical_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["scene_tags"] = sorted(self.scene_tags)
        value["material_tags"] = sorted(self.material_tags)
        value["critical_components"] = sorted(
            (asdict(component) for component in self.critical_components),
            key=lambda component: str(component["name"]),
        )
        return value


@dataclass(frozen=True)
class LocalModelManifest:
    """Auditable identity for an explicitly installed local model runtime."""

    model_name: str
    model_version: str
    source_url: str
    license_id: str
    weights_sha256: str
    model_tree_sha256: str
    executable_sha256: str
    runtime_versions: tuple[tuple[str, str], ...]
    total_bytes: int

    def canonical_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=True, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class LocalModelArtifactManifest:
    """One pinned, commercially permitted model artifact in a local pipeline."""

    role: ModelArtifactRole
    model_name: str
    model_version: str
    source_url: str
    source_revision: str
    license_id: str
    license_sha256: str
    weights_sha256: str
    code_tree_sha256: str
    weights_source_url: str = ""
    weights_source_revision: str = ""
    code_source_url: str = ""
    code_source_revision: str = ""
    license_source_url: str = ""
    license_source_revision: str = ""
    files_sha256: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class LocalForegroundPipelineManifest:
    """Complete identity for the sealed coarse-plus-refinement runtime."""

    pipeline_version: str
    artifacts: tuple[LocalModelArtifactManifest, ...]
    executable_sha256: str
    worker_sha256: str
    runtime_versions: tuple[tuple[str, str], ...]
    total_bytes: int

    def canonical_json(self) -> str:
        return json.dumps(
            asdict(self), ensure_ascii=True, sort_keys=True, separators=(",", ":")
        )


@dataclass(frozen=True)
class ForegroundMetricResult:
    """Pixel facts derived from a predicted mask and frozen gold evidence."""

    true_positive_pixels: int
    false_positive_pixels: int
    false_negative_pixels: int
    iou: float
    foreground_recall: float
    background_contamination: float
    critical_component_recalls: tuple[tuple[str, float], ...]
    critical_component_preservation_rate: float

    @property
    def pixel_safe_for_auto_eligible(self) -> bool:
        return (
            self.foreground_recall >= _COMPONENT_PRESERVATION_THRESHOLD
            and self.background_contamination < _BACKGROUND_CONTAMINATION_THRESHOLD
            and self.critical_component_preservation_rate
            >= _COMPONENT_PRESERVATION_THRESHOLD
        )


@dataclass(frozen=True)
class RiskSignals:
    """Provider-neutral facts consumed by the later calibrated shadow gate."""

    coarse_refined_iou: float | None
    area_delta_ratio: float | None
    touches_edge: bool
    component_count: int
    fine_structure_ratio: float
    high_risk_components: tuple[str, ...]
    evidence_complete: bool


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in hexdigits for character in value)


def _validate_component(component: CriticalComponent) -> None:
    if not component.name.strip() or not _is_sha256(component.mask_sha256):
        raise DomainRuleError(_INVALID_MANIFEST)


def _validate_sample(sample: GoldSampleManifest) -> None:
    if (
        not sample.sample_id.strip()
        or not _is_sha256(sample.source_sha256)
        or not _is_sha256(sample.gold_mask_sha256)
        or sample.width <= 0
        or sample.height <= 0
        or not sample.mime_type.startswith("image/")
        or not sample.category.strip()
        or not sample.difficulty.strip()
        or sample.split not in {"calibration", "acceptance"}
    ):
        raise DomainRuleError(_INVALID_MANIFEST)
    if not sample.human_reviewed:
        raise DomainRuleError("foreground_benchmark_gold_unreviewed")
    component_names: set[str] = set()
    for component in sample.critical_components:
        _validate_component(component)
        if component.name in component_names:
            raise DomainRuleError(_INVALID_MANIFEST)
        component_names.add(component.name)


def benchmark_manifest_sha256(samples: tuple[GoldSampleManifest, ...]) -> str:
    """Hash a validated sample set independently of caller ordering."""
    if not samples:
        raise DomainRuleError(_INVALID_MANIFEST)
    sample_ids: set[str] = set()
    source_digests: set[str] = set()
    canonical: list[dict[str, object]] = []
    for sample in samples:
        _validate_sample(sample)
        if sample.sample_id in sample_ids:
            raise DomainRuleError("foreground_benchmark_duplicate_sample")
        if sample.source_sha256 in source_digests:
            raise DomainRuleError("foreground_benchmark_duplicate_source")
        sample_ids.add(sample.sample_id)
        source_digests.add(sample.source_sha256)
        canonical.append(sample.canonical_dict())
    payload = json.dumps(
        sorted(canonical, key=lambda sample: str(sample["sample_id"])),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return sha256(payload).hexdigest()


def model_manifest_sha256(manifest: LocalModelManifest) -> str:
    """Hash a complete local-model identity after rejecting incomplete evidence."""
    required_runtime_names = {
        "pillow",
        "python",
        "safetensors",
        "torch",
        "torchvision",
        "transformers",
    }
    runtime_versions = tuple(manifest.runtime_versions)
    if (
        not manifest.model_name.strip()
        or not manifest.model_version.strip()
        or not manifest.source_url.startswith("https://")
        or not manifest.license_id.strip()
        or not _is_sha256(manifest.weights_sha256)
        or not _is_sha256(manifest.model_tree_sha256)
        or not _is_sha256(manifest.executable_sha256)
        or runtime_versions != tuple(sorted(runtime_versions))
        or {name for name, _ in runtime_versions} != required_runtime_names
        or any(not name.strip() or not version.strip() for name, version in runtime_versions)
        or manifest.total_bytes <= 0
    ):
        raise DomainRuleError("foreground_model_manifest_invalid")
    return sha256(manifest.canonical_json().encode()).hexdigest()


def pipeline_manifest_sha256(manifest: LocalForegroundPipelineManifest) -> str:
    """Hash a two-stage pipeline after rejecting incomplete or unsafe evidence."""
    required_runtime_names = {
        "pillow",
        "python",
        "safetensors",
        "torch",
        "torchvision",
        "transformers",
    }
    if manifest.pipeline_version == "sam2-birefnet-official-v1":
        required_runtime_names.update({"einops", "kornia", "timm"})
    runtime_versions = tuple(manifest.runtime_versions)
    roles = tuple(artifact.role for artifact in manifest.artifacts)
    if (
        not manifest.pipeline_version.strip()
        or roles != ("coarse_segmenter", "alpha_refiner")
        or not _is_sha256(manifest.executable_sha256)
        or not _is_sha256(manifest.worker_sha256)
        or runtime_versions != tuple(sorted(runtime_versions))
        or {name for name, _ in runtime_versions} != required_runtime_names
        or any(
            not name.strip() or not version.strip() for name, version in runtime_versions
        )
        or manifest.total_bytes <= 0
    ):
        raise DomainRuleError("foreground_pipeline_manifest_invalid")
    for artifact in manifest.artifacts:
        expected_license = {
            "coarse_segmenter": "Apache-2.0",
            "alpha_refiner": "MIT",
        }[artifact.role]
        if (
            not artifact.model_name.strip()
            or not artifact.model_version.strip()
            or not artifact.source_url.startswith("https://")
            or len(artifact.source_revision) not in {40, 64}
            or any(character not in hexdigits for character in artifact.source_revision)
            or artifact.license_id != expected_license
            or not _is_sha256(artifact.license_sha256)
            or not _is_sha256(artifact.weights_sha256)
            or not _is_sha256(artifact.code_tree_sha256)
        ):
            raise DomainRuleError("foreground_pipeline_manifest_invalid")
        if manifest.pipeline_version == "sam2-birefnet-official-v1":
            expected_files = {
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
            }[artifact.role]
            source_values = (
                artifact.weights_source_url,
                artifact.weights_source_revision,
                artifact.code_source_url,
                artifact.code_source_revision,
                artifact.license_source_url,
                artifact.license_source_revision,
            )
            if (
                tuple(name for name, _ in artifact.files_sha256) != expected_files
                or any(not _is_sha256(digest) for _, digest in artifact.files_sha256)
                or any(not value.strip() for value in source_values)
                or any(
                    not url.startswith("https://")
                    for url in (
                        artifact.weights_source_url,
                        artifact.code_source_url,
                        artifact.license_source_url,
                    )
                )
                or any(
                    len(revision) not in {40, 64}
                    or any(character not in hexdigits for character in revision)
                    for revision in (
                        artifact.weights_source_revision,
                        artifact.code_source_revision,
                        artifact.license_source_revision,
                    )
                )
                or dict(artifact.files_sha256).get("model.safetensors")
                != artifact.weights_sha256
                or dict(artifact.files_sha256).get("LICENSE")
                != artifact.license_sha256
            ):
                raise DomainRuleError("foreground_pipeline_manifest_invalid")
    return sha256(manifest.canonical_json().encode()).hexdigest()


def _load_binary_mask(content: bytes, width: int, height: int) -> bytes:
    if width <= 0 or height <= 0:
        raise DomainRuleError(_INVALID_MASK)
    try:
        with Image.open(BytesIO(content)) as image:
            image.load()
            if image.format != "PNG" or image.mode != "L" or image.size != (width, height):
                raise DomainRuleError(_INVALID_MASK)
            return image.point(lambda value: 255 if value >= 128 else 0, mode="L").tobytes()
    except DomainRuleError:
        raise
    except (Image.DecompressionBombError, UnidentifiedImageError, OSError, ValueError):
        raise DomainRuleError(_INVALID_MASK) from None


def score_foreground_mask(
    predicted_png: bytes,
    gold_png: bytes,
    width: int,
    height: int,
    *,
    critical_component_masks: Mapping[str, bytes] | None = None,
) -> ForegroundMetricResult:
    """Score one predicted mask against gold and optional component masks."""
    predicted = _load_binary_mask(predicted_png, width, height)
    gold = _load_binary_mask(gold_png, width, height)
    if not any(gold):
        raise DomainRuleError(_INVALID_MASK)

    true_positive = sum(1 for actual, expected in zip(predicted, gold, strict=True) if actual and expected)
    false_positive = sum(1 for actual, expected in zip(predicted, gold, strict=True) if actual and not expected)
    false_negative = sum(1 for actual, expected in zip(predicted, gold, strict=True) if not actual and expected)
    predicted_count = true_positive + false_positive
    gold_count = true_positive + false_negative
    union_count = true_positive + false_positive + false_negative

    component_recalls: list[tuple[str, float]] = []
    for name, mask_png in sorted((critical_component_masks or {}).items()):
        if not name.strip():
            raise DomainRuleError(_INVALID_MASK)
        component = _load_binary_mask(mask_png, width, height)
        component_count = sum(1 for value in component if value)
        if component_count == 0 or any(value and not expected for value, expected in zip(component, gold, strict=True)):
            raise DomainRuleError(_INVALID_MASK)
        preserved = sum(1 for value, actual in zip(component, predicted, strict=True) if value and actual)
        component_recalls.append((name, preserved / component_count))

    preservation_rate = (
        sum(1 for _, recall in component_recalls if recall >= _COMPONENT_PRESERVATION_THRESHOLD)
        / len(component_recalls)
        if component_recalls
        else 1.0
    )
    return ForegroundMetricResult(
        true_positive_pixels=true_positive,
        false_positive_pixels=false_positive,
        false_negative_pixels=false_negative,
        iou=true_positive / union_count if union_count else 0.0,
        foreground_recall=true_positive / gold_count,
        background_contamination=false_positive / predicted_count if predicted_count else 0.0,
        critical_component_recalls=tuple(component_recalls),
        critical_component_preservation_rate=preservation_rate,
    )
