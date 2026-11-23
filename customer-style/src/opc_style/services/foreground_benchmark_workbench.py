"""Atomic repository-external storage for private benchmark drafts."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import stat
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast
from uuid import UUID, uuid4

from PIL import Image, ImageChops, UnidentifiedImageError

from opc_style.domain.errors import DomainRuleError
from opc_style.domain.foreground_benchmark import BenchmarkSplit
from opc_style.domain.foreground_benchmark_workbench import (
    GoldMaskReviewRisk,
    HumanReviewEvidence,
    SampleReviewDisposition,
    SampleSuitabilityReason,
    SampleSuitabilityStatus,
    WorkbenchDatasetDraft,
    WorkbenchDatasetState,
    WorkbenchSampleDraft,
    WorkbenchSampleState,
    canonical_workbench_draft_json,
    evaluate_workbench_preflight,
)
from opc_style.domain.product_foreground import (
    ForegroundExtractionRequest,
    ForegroundPromptHints,
    ProductForegroundExtractor,
    validate_extracted_foreground,
)
from opc_style.services.foreground_benchmarks import (
    ValidatedBenchmarkDataset,
    freeze_dataset,
    validate_dataset,
)

_UNAVAILABLE = "foreground_benchmark_workbench_unavailable"
_NOT_FOUND = "foreground_benchmark_workbench_not_found"
_FILE_INVALID = "foreground_benchmark_workbench_file_invalid"
_DIGEST_MISMATCH = "foreground_benchmark_workbench_digest_mismatch"
_REVISION_CONFLICT = "foreground_benchmark_workbench_revision_conflict"
_WRITE_FAILED = "foreground_benchmark_workbench_write_failed"
_IMMUTABLE = "foreground_benchmark_workbench_dataset_immutable"
_IMAGE_INVALID = "foreground_benchmark_image_invalid"
_IMAGE_LIMITS_EXCEEDED = "foreground_benchmark_image_limits_exceeded"
_SOURCE_DUPLICATE = "foreground_benchmark_source_duplicate"
_SPLIT_LIMIT_EXCEEDED = "foreground_benchmark_split_limit_exceeded"
_GOLD_MASK_INVALID = "foreground_benchmark_gold_mask_invalid"
_COMPONENT_OUTSIDE_GOLD = "foreground_benchmark_component_outside_gold"
_COMPONENT_REQUIRED = "foreground_benchmark_component_required"
_REVIEW_STALE = "foreground_benchmark_review_stale"
_PROPOSAL_FAILED = "foreground_benchmark_proposal_failed"
_ACTIVE_NOT_FOUND = "foreground_benchmark_active_not_found"
_AUTHORIZATION_REVOKED = "foreground_benchmark_authorization_revoked"
_SUITABILITY_INVALID = "foreground_benchmark_workbench_suitability_invalid"
_REVIEW_DISPOSITION_INVALID = (
    "foreground_benchmark_workbench_review_disposition_invalid"
)

MAX_SOURCE_BYTES = 12 * 1024 * 1024
# The frozen acceptance set contains exact-hash 4284px originals. Keep a
# bounded margin above those files so the review workbench can preserve their
# evidence hashes instead of silently resizing them.
MAX_SOURCE_DIMENSION = 4608
MAX_SOURCE_PIXELS = 20_000_000


def assess_proposal_mask_risk(
    mask_png: bytes,
    *,
    scene_tags: tuple[str, ...] = (),
) -> tuple[float, float, Literal["carrier_included"] | None]:
    """Triage masks that likely retained a carrier, hand, or display support."""
    try:
        with Image.open(BytesIO(mask_png)) as opened:
            opened.load()
            if opened.format != "PNG" or opened.mode != "L":
                raise ValueError
            mask = opened.point(lambda value: 255 if value >= 128 else 0, mode="L")
    except (OSError, UnidentifiedImageError, ValueError):
        raise DomainRuleError(_PROPOSAL_FAILED) from None

    width, height = mask.size
    histogram = mask.histogram()
    active = width * height - histogram[0]
    coverage_ratio = active / (width * height)
    perimeter = max(1, 2 * width + 2 * height - 4)

    def active_in(box: tuple[int, int, int, int]) -> int:
        return mask.crop(box).histogram()[255]

    edge_active = (
        active_in((0, 0, width, 1))
        + active_in((0, height - 1, width, height))
        + active_in((0, 1, 1, height - 1))
        + active_in((width - 1, 1, width, height - 1))
    )
    border_contact_ratio = edge_active / perimeter
    large_edge_carrier = coverage_ratio >= 0.5 and border_contact_ratio >= 0.1
    hand_scene_carrier = (
        bool({"hand_held", "glove"}.intersection(scene_tags)) and coverage_ratio >= 0.3
    )
    # Earrings photographed on a bust normally occupy only a small part of the
    # frame. A substantially larger mask usually means the mannequin head was
    # retained with the product. This is a review gate, not an automatic edit.
    bust_scene_carrier = "bust" in scene_tags and coverage_ratio >= 0.08
    risk: Literal["carrier_included"] | None = (
        "carrier_included"
        if large_edge_carrier or hand_scene_carrier or bust_scene_carrier
        else None
    )
    return round(coverage_ratio, 6), round(border_contact_ratio, 6), risk


def assess_proposal_disagreement(
    coarse_mask_png: bytes,
    refined_mask_png: bytes,
) -> tuple[float, float, float, float]:
    """Compare two machine candidates without treating either as ground truth."""
    assessment = assess_proposal_disagreement_evidence(
        coarse_mask_png,
        refined_mask_png,
    )
    return (
        assessment.intersection_over_union,
        assessment.disagreement_ratio,
        assessment.coarse_only_ratio,
        assessment.refined_only_ratio,
    )


@dataclass(frozen=True)
class ProposalDisagreementAssessment:
    """Auditable comparison evidence, including any coarse-mask polarity repair."""

    intersection_over_union: float
    disagreement_ratio: float
    coarse_only_ratio: float
    refined_only_ratio: float
    coarse_polarity: Literal["foreground", "background_inverted_for_comparison"]
    raw_coarse_coverage_ratio: float
    raw_coarse_border_contact_ratio: float


def _binary_mask_metrics(
    coarse: Image.Image,
    refined: Image.Image,
) -> tuple[float, float, float, float]:
    intersection = ImageChops.multiply(coarse, refined).histogram()[255]
    union = ImageChops.lighter(coarse, refined).histogram()[255]
    if union == 0:
        return 1.0, 0.0, 0.0, 0.0
    coarse_only = ImageChops.subtract(coarse, refined).histogram()[255]
    refined_only = ImageChops.subtract(refined, coarse).histogram()[255]
    return (
        round(intersection / union, 6),
        round((coarse_only + refined_only) / union, 6),
        round(coarse_only / union, 6),
        round(refined_only / union, 6),
    )


def _mask_coverage_and_border(mask: Image.Image) -> tuple[float, float]:
    width, height = mask.size
    active = mask.histogram()[255]
    perimeter = max(1, 2 * width + 2 * height - 4)
    edge_active = (
        mask.crop((0, 0, width, 1)).histogram()[255]
        + mask.crop((0, height - 1, width, height)).histogram()[255]
        + mask.crop((0, 1, 1, height - 1)).histogram()[255]
        + mask.crop((width - 1, 1, width, height - 1)).histogram()[255]
    )
    return active / (width * height), edge_active / perimeter


def assess_proposal_disagreement_evidence(
    coarse_mask_png: bytes,
    refined_mask_png: bytes,
) -> ProposalDisagreementAssessment:
    """Compare candidates and expose a narrowly gated polarity normalization.

    A prompt-free coarse model can occasionally select the surrounding
    background. We only invert that candidate for comparison when it covers
    most of the frame, touches an implausible amount of border, and inversion materially
    improves agreement. The original evidence bytes are never changed.
    """
    masks: list[Image.Image] = []
    for content in (coarse_mask_png, refined_mask_png):
        try:
            with Image.open(BytesIO(content)) as opened:
                opened.load()
                if opened.format != "PNG" or opened.mode != "L":
                    raise ValueError
                masks.append(
                    opened.point(lambda value: 255 if value >= 128 else 0, mode="L")
                )
        except (OSError, UnidentifiedImageError, ValueError):
            raise DomainRuleError(_PROPOSAL_FAILED) from None
    coarse, refined = masks
    if coarse.size != refined.size:
        raise DomainRuleError(_PROPOSAL_FAILED)
    raw_metrics = _binary_mask_metrics(coarse, refined)
    coverage_ratio, border_contact_ratio = _mask_coverage_and_border(coarse)
    inverted_metrics = _binary_mask_metrics(ImageChops.invert(coarse), refined)
    should_invert = (
        coverage_ratio >= 0.65
        and border_contact_ratio >= 0.1
        and inverted_metrics[0] >= raw_metrics[0] + 0.1
    )
    metrics = inverted_metrics if should_invert else raw_metrics
    return ProposalDisagreementAssessment(
        intersection_over_union=metrics[0],
        disagreement_ratio=metrics[1],
        coarse_only_ratio=metrics[2],
        refined_only_ratio=metrics[3],
        coarse_polarity=(
            "background_inverted_for_comparison" if should_invert else "foreground"
        ),
        raw_coarse_coverage_ratio=round(coverage_ratio, 6),
        raw_coarse_border_contact_ratio=round(border_contact_ratio, 6),
    )


def assess_gold_mask_quality(
    mask_png: bytes,
    *,
    width: int,
    height: int,
) -> tuple[float, float, float, int, tuple[GoldMaskReviewRisk, ...]]:
    """Measure a machine draft without treating the result as human review."""
    try:
        with Image.open(BytesIO(mask_png)) as opened:
            opened.load()
            if (
                opened.format != "PNG"
                or opened.mode != "L"
                or opened.size != (width, height)
            ):
                raise ValueError
            mask = opened.copy()
    except (OSError, UnidentifiedImageError, ValueError):
        raise DomainRuleError(_GOLD_MASK_INVALID) from None

    histogram = mask.histogram()
    positive = width * height - histogram[0]
    hard = sum(histogram[128:])
    soft = sum(histogram[1:255])
    coverage_ratio = hard / (width * height)
    perimeter = max(1, 2 * width + 2 * height - 4)

    def positive_in(box: tuple[int, int, int, int]) -> int:
        edge = mask.crop(box)
        return edge.width * edge.height - edge.histogram()[0]

    border_positive = (
        positive_in((0, 0, width, 1))
        + positive_in((0, height - 1, width, height))
        + positive_in((0, 1, 1, height - 1))
        + positive_in((width - 1, 1, width, height - 1))
    )
    border_contact_ratio = border_positive / perimeter
    soft_edge_ratio = soft / max(1, positive)

    scale = min(1.0, 512 / max(width, height))
    review_width = max(1, round(width * scale))
    review_height = max(1, round(height * scale))
    review = mask.resize((review_width, review_height), Image.Resampling.NEAREST).point(
        lambda value: 255 if value >= 128 else 0, mode="L"
    )
    pixels = bytearray(review.tobytes())
    seen = bytearray(len(pixels))
    component_count = 0
    for start, value in enumerate(pixels):
        if not value or seen[start]:
            continue
        stack = [start]
        seen[start] = 1
        size = 0
        while stack:
            index = stack.pop()
            size += 1
            y, x = divmod(index, review_width)
            neighbors = (
                index - 1 if x else -1,
                index + 1 if x + 1 < review_width else -1,
                index - review_width if y else -1,
                index + review_width if y + 1 < review_height else -1,
            )
            for neighbor in neighbors:
                if neighbor >= 0 and pixels[neighbor] and not seen[neighbor]:
                    seen[neighbor] = 1
                    stack.append(neighbor)
        if size >= 4:
            component_count += 1

    risks: list[GoldMaskReviewRisk] = []
    if border_positive:
        risks.append("border_contact")
    if soft_edge_ratio >= 0.35:
        risks.append("soft_edges")
    if component_count > 1:
        risks.append("fragmented_fine_structure")
    return (
        round(coverage_ratio, 6),
        round(border_contact_ratio, 6),
        round(soft_edge_ratio, 6),
        component_count,
        tuple(risks),
    )


@dataclass(frozen=True)
class StoredWorkbenchSource:
    sample_id: str
    source_sha256: str
    relative_path: str


@dataclass(frozen=True)
class StoredWorkbenchMask:
    sample_id: str
    revision_id: UUID
    mask_sha256: str
    relative_path: str


@dataclass(frozen=True)
class FrozenWorkbenchDataset:
    dataset_id: UUID
    version_id: UUID
    root: Path
    dataset: ValidatedBenchmarkDataset


def decode_workbench_image(content: bytes) -> tuple[str, int, int]:
    """Fully verify one JPEG/PNG without trusting its filename or headers."""
    if len(content) > MAX_SOURCE_BYTES:
        raise DomainRuleError(_IMAGE_LIMITS_EXCEEDED)
    mime_by_format = {"JPEG": "image/jpeg", "PNG": "image/png"}
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(content)) as image:
                image_format = image.format
                width, height = image.size
                frames = getattr(image, "n_frames", 1)
                if image_format not in mime_by_format:
                    raise DomainRuleError(_IMAGE_INVALID)
                if (
                    width > MAX_SOURCE_DIMENSION
                    or height > MAX_SOURCE_DIMENSION
                    or width * height > MAX_SOURCE_PIXELS
                    or frames != 1
                ):
                    raise DomainRuleError(_IMAGE_LIMITS_EXCEEDED)
                image.verify()
            with Image.open(BytesIO(content)) as decoded:
                if decoded.format != image_format:
                    raise DomainRuleError(_IMAGE_INVALID)
                decoded.load()
    except DomainRuleError:
        raise
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        OSError,
        SyntaxError,
        UnidentifiedImageError,
        ValueError,
    ):
        raise DomainRuleError(_IMAGE_INVALID) from None
    return mime_by_format[image_format], width, height


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat(follow_symlinks=False).st_mode)


def _safe_id(value: str) -> str:
    if (
        not value
        or len(value) > 100
        or any(not (character.isalnum() or character == "-") for character in value)
    ):
        raise DomainRuleError(_FILE_INVALID)
    return value


def _private_directory(path: Path) -> None:
    if path.exists():
        if path.is_symlink() or not path.is_dir() or _mode(path) & 0o077:
            raise DomainRuleError(_FILE_INVALID)
        return
    try:
        path.mkdir(mode=0o700)
        path.chmod(0o700)
    except OSError:
        raise DomainRuleError(_WRITE_FAILED) from None


def _private_file(path: Path) -> bytes:
    try:
        if path.is_symlink() or not path.is_file() or _mode(path) & 0o077:
            raise DomainRuleError(_FILE_INVALID)
        return path.read_bytes()
    except DomainRuleError:
        raise
    except OSError:
        raise DomainRuleError(_FILE_INVALID) from None


def _atomic_private_write(path: Path, content: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_bytes(content)
        temporary.chmod(0o600)
        os.replace(temporary, path)
        path.chmod(0o600)
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise DomainRuleError(_WRITE_FAILED) from None


@contextmanager
def _exclusive_private_lock(path: Path) -> Iterator[None]:
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
        os.chmod(path, 0o600)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise DomainRuleError(_FILE_INVALID)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    except DomainRuleError:
        raise
    except OSError:
        raise DomainRuleError(_WRITE_FAILED) from None
    finally:
        if "descriptor" in locals():
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


def _review(payload: object) -> HumanReviewEvidence | None:
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise DomainRuleError(_FILE_INVALID)
    try:
        return HumanReviewEvidence(
            source_sha256=str(payload["source_sha256"]),
            gold_mask_sha256=str(payload["gold_mask_sha256"]),
            component_mask_sha256s=tuple(
                (str(item[0]), str(item[1]))
                for item in payload["component_mask_sha256s"]
            ),
            consent_text_version=(
                str(payload["consent_text_version"])
                if payload.get("consent_text_version") is not None
                else None
            ),
            draft_revision=int(payload["draft_revision"]),
            reviewer_scope=str(payload.get("reviewer_scope") or "legacy_unknown"),
            reviewed_at=str(payload.get("reviewed_at") or "legacy_unknown"),
        )
    except (KeyError, TypeError, ValueError):
        raise DomainRuleError(_FILE_INVALID) from None


def _sample(payload: object) -> WorkbenchSampleDraft:
    if not isinstance(payload, dict):
        raise DomainRuleError(_FILE_INVALID)
    try:
        return WorkbenchSampleDraft(
            sample_id=str(payload["sample_id"]),
            source_sha256=str(payload["source_sha256"]),
            width=int(payload["width"]),
            height=int(payload["height"]),
            mime_type=str(payload["mime_type"]),
            split=cast(BenchmarkSplit, str(payload["split"])),
            state=cast(WorkbenchSampleState, str(payload["state"])),
            authorization_scope=(
                str(payload["authorization_scope"])
                if payload.get("authorization_scope") is not None
                else None
            ),
            consent_text_version=(
                str(payload["consent_text_version"])
                if payload.get("consent_text_version") is not None
                else None
            ),
            category=(
                str(payload["category"])
                if payload.get("category") is not None
                else None
            ),
            difficulty=(
                str(payload["difficulty"])
                if payload.get("difficulty") is not None
                else None
            ),
            scene_tags=tuple(str(item) for item in payload["scene_tags"]),
            material_tags=tuple(str(item) for item in payload["material_tags"]),
            critical_component_names=tuple(
                str(item) for item in payload["critical_component_names"]
            ),
            proposed_mask_sha256=(
                str(payload["proposed_mask_sha256"])
                if payload.get("proposed_mask_sha256") is not None
                else None
            ),
            coarse_mask_sha256=(
                str(payload["coarse_mask_sha256"])
                if payload.get("coarse_mask_sha256") is not None
                else None
            ),
            gold_mask_sha256=(
                str(payload["gold_mask_sha256"])
                if payload.get("gold_mask_sha256") is not None
                else None
            ),
            component_mask_sha256s=tuple(
                (str(item[0]), str(item[1]))
                for item in payload["component_mask_sha256s"]
            ),
            human_review=_review(payload.get("human_review")),
            proposal_extractor_version=(
                str(payload["proposal_extractor_version"])
                if payload.get("proposal_extractor_version") is not None
                else None
            ),
            proposal_coverage_ratio=(
                float(payload["proposal_coverage_ratio"])
                if payload.get("proposal_coverage_ratio") is not None
                else None
            ),
            proposal_border_contact_ratio=(
                float(payload["proposal_border_contact_ratio"])
                if payload.get("proposal_border_contact_ratio") is not None
                else None
            ),
            proposal_review_risk=(
                cast(Literal["carrier_included"], str(payload["proposal_review_risk"]))
                if payload.get("proposal_review_risk") is not None
                else None
            ),
            gold_mask_coverage_ratio=(
                float(payload["gold_mask_coverage_ratio"])
                if payload.get("gold_mask_coverage_ratio") is not None
                else None
            ),
            gold_mask_border_contact_ratio=(
                float(payload["gold_mask_border_contact_ratio"])
                if payload.get("gold_mask_border_contact_ratio") is not None
                else None
            ),
            gold_mask_soft_edge_ratio=(
                float(payload["gold_mask_soft_edge_ratio"])
                if payload.get("gold_mask_soft_edge_ratio") is not None
                else None
            ),
            gold_mask_component_count=(
                int(payload["gold_mask_component_count"])
                if payload.get("gold_mask_component_count") is not None
                else None
            ),
            gold_mask_review_risks=tuple(
                cast(GoldMaskReviewRisk, str(item))
                for item in payload.get("gold_mask_review_risks", [])
            ),
            suitability_status=cast(
                SampleSuitabilityStatus,
                str(payload.get("suitability_status") or "legacy_eligible"),
            ),
            suitability_reason=(
                cast(SampleSuitabilityReason, str(payload["suitability_reason"]))
                if payload.get("suitability_reason") is not None
                else None
            ),
            suitability_reviewer_scope=(
                str(payload["suitability_reviewer_scope"])
                if payload.get("suitability_reviewer_scope") is not None
                else None
            ),
            suitability_reviewed_at=(
                str(payload["suitability_reviewed_at"])
                if payload.get("suitability_reviewed_at") is not None
                else None
            ),
            suitability_draft_revision=(
                int(payload["suitability_draft_revision"])
                if payload.get("suitability_draft_revision") is not None
                else None
            ),
            review_disposition=cast(
                SampleReviewDisposition,
                str(payload.get("review_disposition") or "pending"),
            ),
            review_disposition_reviewer_scope=(
                str(payload["review_disposition_reviewer_scope"])
                if payload.get("review_disposition_reviewer_scope") is not None
                else None
            ),
            review_disposition_recorded_at=(
                str(payload["review_disposition_recorded_at"])
                if payload.get("review_disposition_recorded_at") is not None
                else None
            ),
            review_disposition_draft_revision=(
                int(payload["review_disposition_draft_revision"])
                if payload.get("review_disposition_draft_revision") is not None
                else None
            ),
        )
    except (KeyError, TypeError, ValueError):
        raise DomainRuleError(_FILE_INVALID) from None


def _decode_draft(content: bytes) -> WorkbenchDatasetDraft:
    try:
        payload: Any = json.loads(content)
        if not isinstance(payload, dict):
            raise TypeError
        return WorkbenchDatasetDraft(
            dataset_id=UUID(str(payload["dataset_id"])),
            customer_id=UUID(str(payload["customer_id"])),
            revision=int(payload["revision"]),
            state=cast(WorkbenchDatasetState, str(payload["state"])),
            samples=tuple(_sample(item) for item in payload["samples"]),
        )
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        raise DomainRuleError(_FILE_INVALID) from None


class PrivateBenchmarkDraftStore:
    """Own private bytes and expose only path-free domain values."""

    def __init__(self, root: Path) -> None:
        if not root.is_absolute() or root.is_symlink():
            raise DomainRuleError(_UNAVAILABLE)
        try:
            if not root.exists():
                root.mkdir(parents=True, mode=0o700)
                root.chmod(0o700)
            if not root.is_dir() or _mode(root) & 0o077:
                raise DomainRuleError(_UNAVAILABLE)
            self._root = root.resolve(strict=True)
            drafts = self._root / "drafts"
            _private_directory(drafts)
            self._drafts = drafts
            self._frozen = self._root / "frozen"
            self._active = self._root / "active"
            self._revocations = self._root / "revocations"
            self._proposal_disagreement_cache: dict[
                tuple[str, str], ProposalDisagreementAssessment
            ] = {}
            _private_directory(self._frozen)
            _private_directory(self._active)
            _private_directory(self._revocations)
        except DomainRuleError:
            raise
        except OSError:
            raise DomainRuleError(_UNAVAILABLE) from None

    def _customer_dir(self, customer_id: UUID) -> Path:
        path = self._drafts / str(customer_id)
        _private_directory(path)
        return path

    def _dataset_dir(
        self, customer_id: UUID, dataset_id: UUID, *, create: bool = False
    ) -> Path:
        customer = self._customer_dir(customer_id)
        path = customer / str(dataset_id)
        if create:
            _private_directory(path)
        elif not path.exists() or path.is_symlink() or not path.is_dir():
            raise DomainRuleError(_NOT_FOUND)
        elif _mode(path) & 0o077:
            raise DomainRuleError(_FILE_INVALID)
        return path

    def _write_snapshot(self, dataset: Path, draft: WorkbenchDatasetDraft) -> None:
        revisions = dataset / "revisions"
        _private_directory(revisions)
        content = canonical_workbench_draft_json(draft)
        digest = sha256(content).hexdigest()
        snapshot_name = f"{draft.revision:08d}-{digest}.json"
        snapshot = revisions / snapshot_name
        if not snapshot.exists():
            _atomic_private_write(snapshot, content)
        pointer = json.dumps(
            {
                "revision": draft.revision,
                "snapshot": snapshot_name,
                "snapshot_sha256": digest,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        _atomic_private_write(dataset / "draft.json", pointer)

    def create_dataset(self, customer_id: UUID) -> WorkbenchDatasetDraft:
        draft = WorkbenchDatasetDraft(
            dataset_id=uuid4(),
            customer_id=customer_id,
            revision=0,
            state="draft",
            samples=(),
        )
        dataset = self._dataset_dir(customer_id, draft.dataset_id, create=True)
        self._write_snapshot(dataset, draft)
        return draft

    def load_dataset(
        self, customer_id: UUID, dataset_id: UUID
    ) -> WorkbenchDatasetDraft:
        dataset = self._dataset_dir(customer_id, dataset_id)
        try:
            pointer: Any = json.loads(_private_file(dataset / "draft.json"))
            if not isinstance(pointer, dict):
                raise TypeError
            revision = int(pointer["revision"])
            digest = str(pointer["snapshot_sha256"])
            snapshot_name = str(pointer["snapshot"])
            if snapshot_name != f"{revision:08d}-{digest}.json":
                raise ValueError
            content = _private_file(dataset / "revisions" / snapshot_name)
            if sha256(content).hexdigest() != digest:
                raise DomainRuleError(_DIGEST_MISMATCH)
            draft = _decode_draft(content)
            if (
                draft.customer_id != customer_id
                or draft.dataset_id != dataset_id
                or draft.revision != revision
            ):
                raise DomainRuleError(_FILE_INVALID)
            return draft
        except DomainRuleError:
            raise
        except (
            KeyError,
            TypeError,
            ValueError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ):
            raise DomainRuleError(_FILE_INVALID) from None

    def save_dataset(
        self,
        customer_id: UUID,
        draft: WorkbenchDatasetDraft,
        *,
        expected_revision: int,
    ) -> WorkbenchDatasetDraft:
        self.load_dataset(customer_id, draft.dataset_id)
        dataset = self._dataset_dir(customer_id, draft.dataset_id)
        with _exclusive_private_lock(dataset / ".write.lock"):
            current = self.load_dataset(customer_id, draft.dataset_id)
            if current.revision != expected_revision:
                raise DomainRuleError(_REVISION_CONFLICT)
            if current.state in {"frozen", "revoked"}:
                raise DomainRuleError(_IMMUTABLE)
            if (
                draft.customer_id != customer_id
                or draft.dataset_id != current.dataset_id
            ):
                raise DomainRuleError(_NOT_FOUND)
            updated = replace(draft, revision=expected_revision + 1)
            self._write_snapshot(dataset, updated)
            return updated

    def put_source(
        self,
        customer_id: UUID,
        dataset_id: UUID,
        sample_id: str,
        content: bytes,
        content_sha256: str,
    ) -> StoredWorkbenchSource:
        if sha256(content).hexdigest() != content_sha256:
            raise DomainRuleError(_DIGEST_MISMATCH)
        dataset = self._dataset_dir(customer_id, dataset_id)
        samples = dataset / "samples"
        _private_directory(samples)
        sample = samples / _safe_id(sample_id)
        _private_directory(sample)
        path = sample / "source.bin"
        relative = path.relative_to(dataset).as_posix()
        if path.exists():
            existing = _private_file(path)
            if sha256(existing).hexdigest() != content_sha256:
                raise DomainRuleError(_DIGEST_MISMATCH)
        else:
            _atomic_private_write(path, content)
        return StoredWorkbenchSource(sample_id, content_sha256, relative)

    def put_mask_revision(
        self,
        customer_id: UUID,
        dataset_id: UUID,
        sample_id: str,
        content: bytes,
        content_sha256: str,
    ) -> StoredWorkbenchMask:
        if sha256(content).hexdigest() != content_sha256:
            raise DomainRuleError(_DIGEST_MISMATCH)
        dataset = self._dataset_dir(customer_id, dataset_id)
        masks = dataset / "samples" / _safe_id(sample_id) / "masks"
        parent = masks.parent
        _private_directory(dataset / "samples")
        _private_directory(parent)
        _private_directory(masks)
        revision_id = uuid4()
        path = masks / f"{revision_id}.png"
        _atomic_private_write(path, content)
        return StoredWorkbenchMask(
            sample_id,
            revision_id,
            content_sha256,
            path.relative_to(dataset).as_posix(),
        )

    def read_private_content(
        self,
        customer_id: UUID,
        dataset_id: UUID,
        relative_path: str,
        *,
        expected_sha256: str | None = None,
    ) -> bytes:
        dataset = self._dataset_dir(customer_id, dataset_id)
        relative = PurePosixPath(relative_path)
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            raise DomainRuleError(_FILE_INVALID)
        candidate = dataset.joinpath(*relative.parts)
        current = dataset
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise DomainRuleError(_FILE_INVALID)
        content = _private_file(candidate)
        if (
            expected_sha256 is not None
            and sha256(content).hexdigest() != expected_sha256
        ):
            raise DomainRuleError(_DIGEST_MISMATCH)
        return content

    def delete_unfrozen_sample(
        self,
        customer_id: UUID,
        dataset_id: UUID,
        sample_id: str,
        *,
        expected_revision: int,
    ) -> WorkbenchDatasetDraft:
        draft = self.load_dataset(customer_id, dataset_id)
        if draft.state != "draft":
            raise DomainRuleError(_IMMUTABLE)
        updated = self.save_dataset(
            customer_id,
            replace(
                draft,
                samples=tuple(
                    sample for sample in draft.samples if sample.sample_id != sample_id
                ),
            ),
            expected_revision=expected_revision,
        )
        sample_path = (
            self._dataset_dir(customer_id, dataset_id) / "samples" / _safe_id(sample_id)
        )
        if sample_path.exists() and not sample_path.is_symlink():
            shutil.rmtree(sample_path)
        return updated

    def add_source_sample(
        self,
        customer_id: UUID,
        dataset_id: UUID,
        content: bytes,
        *,
        split: BenchmarkSplit,
        expected_revision: int,
        consent_text_version: str,
    ) -> WorkbenchDatasetDraft:
        """Validate and append one authorized source without recording origin metadata."""
        draft = self.load_dataset(customer_id, dataset_id)
        if draft.revision != expected_revision:
            raise DomainRuleError(_REVISION_CONFLICT)
        split_limit = 10 if split == "calibration" else 50
        split_count = sum(
            sample.split == split
            and sample.state != "revoked"
            and sample.suitability_status != "excluded"
            for sample in draft.samples
        )
        if split_count >= split_limit:
            raise DomainRuleError(_SPLIT_LIMIT_EXCEEDED)
        mime_type, width, height = decode_workbench_image(content)
        digest = sha256(content).hexdigest()
        if any(sample.source_sha256 == digest for sample in draft.samples):
            raise DomainRuleError(_SOURCE_DUPLICATE)
        sample_id = f"sample-{uuid4()}"
        sample = WorkbenchSampleDraft(
            sample_id=sample_id,
            source_sha256=digest,
            width=width,
            height=height,
            mime_type=mime_type,
            split=split,
            state="uploaded",
            authorization_scope=consent_text_version,
            consent_text_version=consent_text_version,
            category=None,
            difficulty=None,
            scene_tags=(),
            material_tags=(),
            critical_component_names=(),
            proposed_mask_sha256=None,
            gold_mask_sha256=None,
            component_mask_sha256s=(),
            human_review=None,
            suitability_status="pending",
        )
        self.put_source(customer_id, dataset_id, sample_id, content, digest)
        return self.save_dataset(
            customer_id,
            replace(draft, samples=(*draft.samples, sample)),
            expected_revision=expected_revision,
        )

    def set_sample_suitability(
        self,
        customer_id: UUID,
        dataset_id: UUID,
        sample_id: str,
        *,
        expected_revision: int,
        decision: Literal["pending", "eligible", "excluded"],
        reason: SampleSuitabilityReason | None,
        reviewer_scope: str,
        confirmed: bool,
    ) -> WorkbenchDatasetDraft:
        """Record an explicit human suitability decision while preserving the sample."""
        draft = self.load_dataset(customer_id, dataset_id)
        if draft.revision != expected_revision:
            raise DomainRuleError(_REVISION_CONFLICT)
        if draft.state != "draft" or not confirmed:
            raise DomainRuleError(_SUITABILITY_INVALID)
        if (decision == "excluded") != (reason is not None):
            raise DomainRuleError(_SUITABILITY_INVALID)
        matched = False
        samples: list[WorkbenchSampleDraft] = []
        reviewed_at = datetime.now(UTC).isoformat()
        for sample in draft.samples:
            if sample.sample_id != sample_id:
                samples.append(sample)
                continue
            matched = True
            if sample.state in {"frozen", "revoked"}:
                raise DomainRuleError(_IMMUTABLE)
            samples.append(
                replace(
                    sample,
                    suitability_status=decision,
                    suitability_reason=reason,
                    suitability_reviewer_scope=(
                        None if decision == "pending" else reviewer_scope
                    ),
                    suitability_reviewed_at=(
                        None if decision == "pending" else reviewed_at
                    ),
                    suitability_draft_revision=(
                        None if decision == "pending" else draft.revision
                    ),
                )
            )
        if not matched:
            raise DomainRuleError(_NOT_FOUND)
        return self.save_dataset(
            customer_id,
            replace(draft, samples=tuple(samples)),
            expected_revision=expected_revision,
        )

    def set_sample_review_disposition(
        self,
        customer_id: UUID,
        dataset_id: UUID,
        sample_id: str,
        *,
        expected_revision: int,
        disposition: Literal["pending", "candidate_usable", "needs_correction"],
        reviewer_scope: str,
        confirmed: bool,
    ) -> WorkbenchDatasetDraft:
        """Record review-queue triage without promoting a sample to human gold."""
        draft = self.load_dataset(customer_id, dataset_id)
        if draft.revision != expected_revision:
            raise DomainRuleError(_REVISION_CONFLICT)
        if draft.state != "draft" or not confirmed:
            raise DomainRuleError(_REVIEW_DISPOSITION_INVALID)
        matched = False
        samples: list[WorkbenchSampleDraft] = []
        recorded_at = datetime.now(UTC).isoformat()
        for sample in draft.samples:
            if sample.sample_id != sample_id:
                samples.append(sample)
                continue
            matched = True
            if sample.state in {"frozen", "revoked"}:
                raise DomainRuleError(_IMMUTABLE)
            if (
                sample.suitability_status == "excluded"
                or not sample.proposed_mask_sha256
            ):
                raise DomainRuleError(_REVIEW_DISPOSITION_INVALID)
            samples.append(
                replace(
                    sample,
                    review_disposition=disposition,
                    review_disposition_reviewer_scope=(
                        None if disposition == "pending" else reviewer_scope
                    ),
                    review_disposition_recorded_at=(
                        None if disposition == "pending" else recorded_at
                    ),
                    review_disposition_draft_revision=(
                        None if disposition == "pending" else draft.revision
                    ),
                )
            )
        if not matched:
            raise DomainRuleError(_NOT_FOUND)
        return self.save_dataset(
            customer_id,
            replace(draft, samples=tuple(samples)),
            expected_revision=expected_revision,
        )

    def label_sample(
        self,
        customer_id: UUID,
        dataset_id: UUID,
        sample_id: str,
        *,
        expected_revision: int,
        category: str,
        difficulty: str,
        scene_tags: tuple[str, ...],
        material_tags: tuple[str, ...],
        critical_component_names: tuple[str, ...],
    ) -> WorkbenchDatasetDraft:
        draft = self.load_dataset(customer_id, dataset_id)
        matched = False
        samples: list[WorkbenchSampleDraft] = []
        for sample in draft.samples:
            if sample.sample_id != sample_id:
                samples.append(sample)
                continue
            matched = True
            if sample.state not in {"uploaded", "labeled"}:
                raise DomainRuleError(_IMMUTABLE)
            samples.append(
                replace(
                    sample,
                    state="labeled",
                    category=category,
                    difficulty=difficulty,
                    scene_tags=tuple(sorted(set(scene_tags))),
                    material_tags=tuple(sorted(set(material_tags))),
                    critical_component_names=tuple(
                        sorted(set(critical_component_names))
                    ),
                )
            )
        if not matched:
            raise DomainRuleError(_NOT_FOUND)
        return self.save_dataset(
            customer_id,
            replace(draft, samples=tuple(samples)),
            expected_revision=expected_revision,
        )

    def source_content(
        self, customer_id: UUID, dataset_id: UUID, sample: WorkbenchSampleDraft
    ) -> bytes:
        return self.read_private_content(
            customer_id,
            dataset_id,
            f"samples/{_safe_id(sample.sample_id)}/source.bin",
            expected_sha256=sample.source_sha256,
        )

    def reopen_component_annotation(
        self,
        customer_id: UUID,
        dataset_id: UUID,
        sample_id: str,
        *,
        expected_revision: int,
        critical_component_names: tuple[str, ...],
        confirmed: bool,
    ) -> WorkbenchDatasetDraft:
        """Invalidate a prior review before changing its component-mask contract."""
        draft = self.load_dataset(customer_id, dataset_id)
        if draft.revision != expected_revision:
            raise DomainRuleError(_REVISION_CONFLICT)
        names = tuple(sorted(set(critical_component_names)))
        if draft.state != "draft" or not confirmed:
            raise DomainRuleError(_COMPONENT_REQUIRED)
        matched = False
        samples: list[WorkbenchSampleDraft] = []
        for sample in draft.samples:
            if sample.sample_id != sample_id:
                samples.append(sample)
                continue
            matched = True
            if sample.state in {"uploaded", "labeled", "frozen", "revoked"}:
                raise DomainRuleError(_IMMUTABLE)
            samples.append(
                replace(
                    sample,
                    state="mask_proposed",
                    critical_component_names=names,
                    component_mask_sha256s=(),
                    human_review=None,
                )
            )
        if not matched:
            raise DomainRuleError(_NOT_FOUND)
        return self.save_dataset(
            customer_id,
            replace(draft, samples=tuple(samples)),
            expected_revision=expected_revision,
        )

    def thumbnail_jpeg(
        self,
        customer_id: UUID,
        dataset_id: UUID,
        sample_id: str,
        *,
        max_dimension: int = 384,
    ) -> bytes:
        draft = self.load_dataset(customer_id, dataset_id)
        sample = next(
            (item for item in draft.samples if item.sample_id == sample_id), None
        )
        if sample is None:
            raise DomainRuleError(_NOT_FOUND)
        content = self.source_content(customer_id, dataset_id, sample)
        try:
            with Image.open(BytesIO(content)) as image:
                normalized = image.convert("RGB")
                normalized.thumbnail((max_dimension, max_dimension))
                output = BytesIO()
                normalized.save(output, format="JPEG", quality=82, optimize=True)
                return output.getvalue()
        except (OSError, UnidentifiedImageError, ValueError):
            raise DomainRuleError(_IMAGE_INVALID) from None

    def propose_mask(
        self,
        customer_id: UUID,
        dataset_id: UUID,
        sample_id: str,
        *,
        expected_revision: int,
        extractor: ProductForegroundExtractor,
        prompt_hints: ForegroundPromptHints | None = None,
    ) -> WorkbenchDatasetDraft:
        draft = self.load_dataset(customer_id, dataset_id)
        if draft.revision != expected_revision:
            raise DomainRuleError(_REVISION_CONFLICT)
        sample = next(
            (item for item in draft.samples if item.sample_id == sample_id), None
        )
        if sample is None:
            raise DomainRuleError(_NOT_FOUND)
        source = self.source_content(customer_id, dataset_id, sample)
        request = ForegroundExtractionRequest(
            customer_id=customer_id,
            asset_id=dataset_id,
            product_id=sample.sample_id,
            source_content=source,
            source_content_sha256=sample.source_sha256,
            mime_type=sample.mime_type,
            width=sample.width,
            height=sample.height,
            prompt_hints=prompt_hints,
        )
        try:
            result = extractor.extract(request)
            validate_extracted_foreground(result)
            if (result.width, result.height) != (sample.width, sample.height):
                raise DomainRuleError(_PROPOSAL_FAILED)
        # Extractors are provider plug-ins; sanitize every provider failure here.
        except Exception:  # noqa: BLE001
            raise DomainRuleError(_PROPOSAL_FAILED) from None
        self.put_mask_revision(
            customer_id, dataset_id, sample_id, result.mask_png, result.mask_sha256
        )
        if result.coarse_mask_png is not None and result.coarse_mask_sha256 is not None:
            self.put_mask_revision(
                customer_id,
                dataset_id,
                sample_id,
                result.coarse_mask_png,
                result.coarse_mask_sha256,
            )
        coverage_ratio, border_contact_ratio, review_risk = assess_proposal_mask_risk(
            result.mask_png,
            scene_tags=sample.scene_tags,
        )
        updated_sample = replace(
            sample,
            state="mask_proposed",
            proposed_mask_sha256=result.mask_sha256,
            coarse_mask_sha256=result.coarse_mask_sha256,
            proposal_extractor_version=result.extractor_version,
            proposal_coverage_ratio=coverage_ratio,
            proposal_border_contact_ratio=border_contact_ratio,
            proposal_review_risk=review_risk,
            human_review=None,
        )
        return self.save_dataset(
            customer_id,
            replace(
                draft,
                samples=tuple(
                    updated_sample if item.sample_id == sample_id else item
                    for item in draft.samples
                ),
            ),
            expected_revision=expected_revision,
        )

    @staticmethod
    def _canonical_mask(
        content: bytes, width: int, height: int
    ) -> tuple[bytes, Image.Image]:
        try:
            with Image.open(BytesIO(content)) as opened:
                opened.load()
                if opened.format != "PNG" or opened.mode != "L":
                    raise DomainRuleError(_GOLD_MASK_INVALID)
                image = opened.copy()
        except DomainRuleError:
            raise
        except (
            Image.DecompressionBombError,
            UnidentifiedImageError,
            OSError,
            ValueError,
        ):
            raise DomainRuleError(_GOLD_MASK_INVALID) from None
        if image.size != (width, height):
            raise DomainRuleError(_GOLD_MASK_INVALID)
        binary = image.point(lambda value: 255 if value >= 128 else 0, mode="L")
        active = width * height - binary.histogram()[0]
        if active in {0, width * height}:
            raise DomainRuleError(_GOLD_MASK_INVALID)
        output = BytesIO()
        binary.save(output, format="PNG", optimize=False)
        return output.getvalue(), binary

    def put_gold_revision(
        self,
        customer_id: UUID,
        dataset_id: UUID,
        sample_id: str,
        *,
        expected_revision: int,
        gold_mask: bytes,
        component_masks: tuple[tuple[str, bytes], ...],
    ) -> WorkbenchDatasetDraft:
        draft = self.load_dataset(customer_id, dataset_id)
        if draft.revision != expected_revision:
            raise DomainRuleError(_REVISION_CONFLICT)
        sample = next(
            (item for item in draft.samples if item.sample_id == sample_id), None
        )
        if sample is None:
            raise DomainRuleError(_NOT_FOUND)
        supplied_names = {name for name, _ in component_masks}
        if supplied_names != set(sample.critical_component_names) or len(
            supplied_names
        ) != len(component_masks):
            raise DomainRuleError(_COMPONENT_REQUIRED)
        canonical_gold, gold_image = self._canonical_mask(
            gold_mask, sample.width, sample.height
        )
        quality = assess_gold_mask_quality(
            gold_mask, width=sample.width, height=sample.height
        )
        canonical_components: list[tuple[str, bytes, str]] = []
        gold_pixels = gold_image.tobytes()
        for name, content in component_masks:
            canonical, component = self._canonical_mask(
                content, sample.width, sample.height
            )
            if any(
                component_pixel and not gold_pixel
                for component_pixel, gold_pixel in zip(
                    component.tobytes(), gold_pixels, strict=True
                )
            ):
                raise DomainRuleError(_COMPONENT_OUTSIDE_GOLD)
            canonical_components.append(
                (name, canonical, sha256(canonical).hexdigest())
            )
        gold_digest = sha256(canonical_gold).hexdigest()
        if (
            gold_digest == sample.gold_mask_sha256
            and sample.gold_mask_coverage_ratio is not None
        ):
            quality = (
                sample.gold_mask_coverage_ratio,
                sample.gold_mask_border_contact_ratio or 0.0,
                sample.gold_mask_soft_edge_ratio or 0.0,
                sample.gold_mask_component_count or 0,
                sample.gold_mask_review_risks,
            )
        self.put_mask_revision(
            customer_id, dataset_id, sample_id, canonical_gold, gold_digest
        )
        for _, content, digest in canonical_components:
            self.put_mask_revision(customer_id, dataset_id, sample_id, content, digest)
        updated_sample = replace(
            sample,
            state="mask_proposed",
            gold_mask_sha256=gold_digest,
            component_mask_sha256s=tuple(
                sorted((name, digest) for name, _, digest in canonical_components)
            ),
            gold_mask_coverage_ratio=quality[0],
            gold_mask_border_contact_ratio=quality[1],
            gold_mask_soft_edge_ratio=quality[2],
            gold_mask_component_count=quality[3],
            gold_mask_review_risks=quality[4],
            human_review=None,
        )
        return self.save_dataset(
            customer_id,
            replace(
                draft,
                samples=tuple(
                    updated_sample if item.sample_id == sample_id else item
                    for item in draft.samples
                ),
            ),
            expected_revision=expected_revision,
        )

    def confirm_human_review(
        self,
        customer_id: UUID,
        dataset_id: UUID,
        sample_id: str,
        *,
        expected_revision: int,
        confirmed: bool,
        source_sha256: str,
        gold_mask_sha256: str,
        component_mask_sha256s: tuple[tuple[str, str], ...],
        consent_text_version: str,
    ) -> WorkbenchDatasetDraft:
        draft = self.load_dataset(customer_id, dataset_id)
        if draft.revision != expected_revision:
            raise DomainRuleError(_REVISION_CONFLICT)
        sample = next(
            (item for item in draft.samples if item.sample_id == sample_id), None
        )
        if sample is None:
            raise DomainRuleError(_NOT_FOUND)
        if (
            not confirmed
            or sample.gold_mask_sha256 is None
            or source_sha256 != sample.source_sha256
            or gold_mask_sha256 != sample.gold_mask_sha256
            or tuple(sorted(component_mask_sha256s))
            != tuple(sorted(sample.component_mask_sha256s))
            or consent_text_version != sample.consent_text_version
        ):
            raise DomainRuleError(_REVIEW_STALE)
        review = HumanReviewEvidence(
            source_sha256=source_sha256,
            gold_mask_sha256=gold_mask_sha256,
            component_mask_sha256s=tuple(sorted(component_mask_sha256s)),
            consent_text_version=consent_text_version,
            draft_revision=expected_revision + 1,
            reviewer_scope=f"local_customer_operator:{customer_id}",
            reviewed_at=datetime.now(UTC).isoformat(),
        )
        updated_sample = replace(sample, state="human_reviewed", human_review=review)
        return self.save_dataset(
            customer_id,
            replace(
                draft,
                samples=tuple(
                    updated_sample if item.sample_id == sample_id else item
                    for item in draft.samples
                ),
            ),
            expected_revision=expected_revision,
        )

    def _mask_content_by_digest(
        self,
        customer_id: UUID,
        dataset_id: UUID,
        sample_id: str,
        digest: str,
    ) -> bytes:
        dataset = self._dataset_dir(customer_id, dataset_id)
        masks = dataset / "samples" / _safe_id(sample_id) / "masks"
        if not masks.is_dir() or masks.is_symlink():
            raise DomainRuleError(_FILE_INVALID)
        for path in masks.iterdir():
            if path.is_file() and not path.is_symlink():
                content = _private_file(path)
                if sha256(content).hexdigest() == digest:
                    return content
        raise DomainRuleError(_DIGEST_MISMATCH)

    def mask_content(
        self,
        customer_id: UUID,
        dataset_id: UUID,
        sample_id: str,
        *,
        kind: Literal["proposal", "coarse", "gold", "component"],
        component_name: str | None = None,
        loaded_draft: WorkbenchDatasetDraft | None = None,
    ) -> bytes:
        draft = loaded_draft or self.load_dataset(customer_id, dataset_id)
        if draft.customer_id != customer_id or draft.dataset_id != dataset_id:
            raise DomainRuleError(_NOT_FOUND)
        sample = next(
            (item for item in draft.samples if item.sample_id == sample_id), None
        )
        if sample is None:
            raise DomainRuleError(_NOT_FOUND)
        if kind == "proposal":
            digest = sample.proposed_mask_sha256
        elif kind == "coarse":
            digest = sample.coarse_mask_sha256
        elif kind == "gold":
            digest = sample.gold_mask_sha256
        else:
            digest = dict(sample.component_mask_sha256s).get(component_name or "")
        if digest is None:
            raise DomainRuleError(_NOT_FOUND)
        return self._mask_content_by_digest(customer_id, dataset_id, sample_id, digest)

    def proposal_disagreement(
        self,
        customer_id: UUID,
        dataset_id: UUID,
        sample_id: str,
        *,
        loaded_draft: WorkbenchDatasetDraft,
    ) -> ProposalDisagreementAssessment:
        """Cache deterministic machine comparison without changing draft state."""
        if (
            loaded_draft.customer_id != customer_id
            or loaded_draft.dataset_id != dataset_id
        ):
            raise DomainRuleError(_NOT_FOUND)
        sample = next(
            (item for item in loaded_draft.samples if item.sample_id == sample_id), None
        )
        if (
            sample is None
            or sample.coarse_mask_sha256 is None
            or sample.proposed_mask_sha256 is None
        ):
            raise DomainRuleError(_NOT_FOUND)
        cache_key = (sample.coarse_mask_sha256, sample.proposed_mask_sha256)
        cached = self._proposal_disagreement_cache.get(cache_key)
        if cached is not None:
            return cached
        metrics = assess_proposal_disagreement_evidence(
            self._mask_content_by_digest(
                customer_id,
                dataset_id,
                sample_id,
                sample.coarse_mask_sha256,
            ),
            self._mask_content_by_digest(
                customer_id,
                dataset_id,
                sample_id,
                sample.proposed_mask_sha256,
            ),
        )
        if len(self._proposal_disagreement_cache) >= 512:
            self._proposal_disagreement_cache.pop(
                next(iter(self._proposal_disagreement_cache))
            )
        self._proposal_disagreement_cache[cache_key] = metrics
        return metrics

    def _active_pointer(self, customer_id: UUID) -> dict[str, object] | None:
        path = self._active / f"{customer_id}.json"
        if not path.exists():
            return None
        try:
            payload: Any = json.loads(_private_file(path))
            if not isinstance(payload, dict):
                raise TypeError
            return payload
        except (TypeError, ValueError, json.JSONDecodeError, UnicodeDecodeError):
            raise DomainRuleError(_FILE_INVALID) from None

    def _frozen_from_pointer(
        self, customer_id: UUID, pointer: dict[str, object]
    ) -> FrozenWorkbenchDataset:
        try:
            if UUID(str(pointer["customer_id"])) != customer_id:
                raise ValueError
            dataset_id = UUID(str(pointer["dataset_id"]))
            version_id = UUID(str(pointer["version_id"]))
            expected_seal = str(pointer["seal_sha256"])
        except (KeyError, TypeError, ValueError):
            raise DomainRuleError(_FILE_INVALID) from None
        root = self._frozen / str(customer_id) / str(version_id)
        dataset = validate_dataset(root)
        if dataset.seal_sha256 != expected_seal:
            raise DomainRuleError(_DIGEST_MISMATCH)
        return FrozenWorkbenchDataset(dataset_id, version_id, root, dataset)

    def materialize_and_freeze(
        self,
        customer_id: UUID,
        dataset_id: UUID,
        *,
        expected_revision: int,
        idempotency_key: str,
    ) -> FrozenWorkbenchDataset:
        if not idempotency_key or len(idempotency_key) > 100:
            raise DomainRuleError(_FILE_INVALID)
        pointer = self._active_pointer(customer_id)
        if pointer is not None and (
            str(pointer.get("dataset_id")) == str(dataset_id)
            and pointer.get("idempotency_key") == idempotency_key
        ):
            return self._frozen_from_pointer(customer_id, pointer)

        draft = self.load_dataset(customer_id, dataset_id)
        if draft.revision != expected_revision:
            raise DomainRuleError(_REVISION_CONFLICT)
        if draft.state != "draft":
            raise DomainRuleError(_IMMUTABLE)
        preflight = evaluate_workbench_preflight(draft)
        if not preflight.ready_to_freeze:
            raise DomainRuleError("foreground_benchmark_workbench_not_ready")
        revoked = self._revoked_digests(customer_id)
        if any(sample.source_sha256 in revoked for sample in draft.samples):
            raise DomainRuleError(_AUTHORIZATION_REVOKED)

        customer_frozen = self._frozen / str(customer_id)
        _private_directory(customer_frozen)
        version_id = uuid4()
        temporary = customer_frozen / f".tmp-{version_id}"
        final = customer_frozen / str(version_id)
        pointer_path = self._active / f"{customer_id}.json"
        previous_pointer = (
            _private_file(pointer_path) if pointer_path.exists() else None
        )
        pointer_written = False
        final_created = False
        _private_directory(temporary)
        manifest_samples: list[dict[str, object]] = []
        try:
            for sample in sorted(draft.samples, key=lambda item: item.sample_id):
                sample_dir = temporary / "samples" / _safe_id(sample.sample_id)
                _private_directory(temporary / "samples")
                _private_directory(sample_dir)
                source = self.source_content(customer_id, dataset_id, sample)
                source_rel = f"samples/{sample.sample_id}/source.bin"
                gold_rel = f"samples/{sample.sample_id}/gold.png"
                assert sample.gold_mask_sha256 is not None
                gold = self._mask_content_by_digest(
                    customer_id,
                    dataset_id,
                    sample.sample_id,
                    sample.gold_mask_sha256,
                )
                _atomic_private_write(temporary / source_rel, source)
                _atomic_private_write(temporary / gold_rel, gold)
                components: list[dict[str, str]] = []
                for name, digest in sample.component_mask_sha256s:
                    relative = f"samples/{sample.sample_id}/component-{name}.png"
                    content = self._mask_content_by_digest(
                        customer_id, dataset_id, sample.sample_id, digest
                    )
                    _atomic_private_write(temporary / relative, content)
                    components.append(
                        {"name": name, "mask_file": relative, "mask_sha256": digest}
                    )
                manifest_samples.append(
                    {
                        "sample_id": sample.sample_id,
                        "source_file": source_rel,
                        "source_sha256": sample.source_sha256,
                        "width": sample.width,
                        "height": sample.height,
                        "mime_type": sample.mime_type,
                        "category": sample.category,
                        "difficulty": sample.difficulty,
                        "scene_tags": list(sample.scene_tags),
                        "material_tags": list(sample.material_tags),
                        "gold_mask_file": gold_rel,
                        "gold_mask_sha256": sample.gold_mask_sha256,
                        "critical_components": components,
                        "human_reviewed": True,
                        "split": sample.split,
                    }
                )
            _atomic_private_write(
                temporary / "manifest.json",
                json.dumps(
                    {"version": 1, "samples": manifest_samples},
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode(),
            )
            frozen_dataset = freeze_dataset(temporary)
            os.replace(temporary, final)
            final_created = True
            pointer_payload = {
                "customer_id": str(customer_id),
                "dataset_id": str(dataset_id),
                "version_id": str(version_id),
                "seal_sha256": frozen_dataset.seal_sha256,
                "idempotency_key": idempotency_key,
            }
            _atomic_private_write(
                pointer_path,
                json.dumps(
                    pointer_payload,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode(),
            )
            pointer_written = True
            self.save_dataset(
                customer_id,
                replace(draft, state="frozen"),
                expected_revision=expected_revision,
            )
            return FrozenWorkbenchDataset(dataset_id, version_id, final, frozen_dataset)
        except BaseException:
            if pointer_written:
                if previous_pointer is None:
                    pointer_path.unlink(missing_ok=True)
                else:
                    _atomic_private_write(pointer_path, previous_pointer)
            if final_created and final.exists() and not final.is_symlink():
                shutil.rmtree(final)
            if temporary.exists() and not temporary.is_symlink():
                shutil.rmtree(temporary)
            raise

    def load_active_frozen_dataset(
        self, customer_id: UUID, *, enforce_revocations: bool = True
    ) -> ValidatedBenchmarkDataset:
        pointer = self._active_pointer(customer_id)
        if pointer is None:
            raise DomainRuleError(_ACTIVE_NOT_FOUND)
        frozen = self._frozen_from_pointer(customer_id, pointer)
        revoked = self._revoked_digests(customer_id)
        if enforce_revocations and any(
            sample.manifest.source_sha256 in revoked
            for sample in frozen.dataset.samples
        ):
            raise DomainRuleError(_AUTHORIZATION_REVOKED)
        return frozen.dataset

    def _revoked_digests(self, customer_id: UUID) -> set[str]:
        path = self._revocations / f"{customer_id}.json"
        if not path.exists():
            return set()
        try:
            payload: Any = json.loads(_private_file(path))
            if not isinstance(payload, dict) or not isinstance(
                payload.get("digests"), list
            ):
                raise TypeError
            return {str(item) for item in payload["digests"]}
        except (TypeError, ValueError, json.JSONDecodeError, UnicodeDecodeError):
            raise DomainRuleError(_FILE_INVALID) from None

    def revoke_source(self, customer_id: UUID, source_sha256: str) -> None:
        if len(source_sha256) != 64:
            raise DomainRuleError(_FILE_INVALID)
        revoked = self._revoked_digests(customer_id)
        revoked.add(source_sha256)
        _atomic_private_write(
            self._revocations / f"{customer_id}.json",
            json.dumps(
                {"digests": sorted(revoked)},
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode(),
        )
