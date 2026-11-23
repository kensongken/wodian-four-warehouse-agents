"""Immutable local-workbench policy for building foreground gold datasets."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, dataclass, replace
from string import hexdigits
from typing import Literal
from uuid import UUID

from opc_style.domain.errors import DomainRuleError
from opc_style.domain.foreground_benchmark import BenchmarkSplit

WorkbenchSampleState = Literal[
    "uploaded", "labeled", "mask_proposed", "human_reviewed", "frozen", "revoked"
]
GoldMaskReviewRisk = Literal[
    "border_contact", "soft_edges", "fragmented_fine_structure"
]
WorkbenchDatasetState = Literal["draft", "frozen", "revoked"]
SampleSuitabilityStatus = Literal["legacy_eligible", "pending", "eligible", "excluded"]
SampleReviewDisposition = Literal["pending", "candidate_usable", "needs_correction"]
SampleSuitabilityReason = Literal[
    "multi_product_scene",
    "no_unique_product_identity",
    "obstructed_by_tags",
    "other",
]

BENCHMARK_CONSENT_VERSION = "foreground_benchmark_local_v1"
_TRANSITION_INVALID = "foreground_benchmark_workbench_transition_invalid"


@dataclass(frozen=True)
class HumanReviewEvidence:
    """Exact bytes and authority facts approved by one human review."""

    source_sha256: str
    gold_mask_sha256: str
    component_mask_sha256s: tuple[tuple[str, str], ...]
    consent_text_version: str | None
    draft_revision: int
    reviewer_scope: str
    reviewed_at: str


@dataclass(frozen=True)
class WorkbenchSampleDraft:
    """Path-free metadata for one private workbench sample."""

    sample_id: str
    source_sha256: str
    width: int
    height: int
    mime_type: str
    split: BenchmarkSplit
    state: WorkbenchSampleState
    authorization_scope: str | None
    consent_text_version: str | None
    category: str | None
    difficulty: str | None
    scene_tags: tuple[str, ...]
    material_tags: tuple[str, ...]
    critical_component_names: tuple[str, ...]
    proposed_mask_sha256: str | None
    gold_mask_sha256: str | None
    component_mask_sha256s: tuple[tuple[str, str], ...]
    human_review: HumanReviewEvidence | None
    proposal_extractor_version: str | None = None
    coarse_mask_sha256: str | None = None
    proposal_coverage_ratio: float | None = None
    proposal_border_contact_ratio: float | None = None
    proposal_review_risk: Literal["carrier_included"] | None = None
    gold_mask_coverage_ratio: float | None = None
    gold_mask_border_contact_ratio: float | None = None
    gold_mask_soft_edge_ratio: float | None = None
    gold_mask_component_count: int | None = None
    gold_mask_review_risks: tuple[GoldMaskReviewRisk, ...] = ()
    suitability_status: SampleSuitabilityStatus = "legacy_eligible"
    suitability_reason: SampleSuitabilityReason | None = None
    suitability_reviewer_scope: str | None = None
    suitability_reviewed_at: str | None = None
    suitability_draft_revision: int | None = None
    review_disposition: SampleReviewDisposition = "pending"
    review_disposition_reviewer_scope: str | None = None
    review_disposition_recorded_at: str | None = None
    review_disposition_draft_revision: int | None = None


@dataclass(frozen=True)
class WorkbenchDatasetDraft:
    """Customer-owned draft whose revision protects concurrent edits."""

    dataset_id: UUID
    customer_id: UUID
    revision: int
    state: WorkbenchDatasetState
    samples: tuple[WorkbenchSampleDraft, ...]


@dataclass(frozen=True)
class WorkbenchSampleEvent:
    """One requested state transition and its optional immutable evidence."""

    target_state: WorkbenchSampleState
    proposed_mask_sha256: str | None = None
    gold_mask_sha256: str | None = None
    component_mask_sha256s: tuple[tuple[str, str], ...] = ()
    human_review: HumanReviewEvidence | None = None


@dataclass(frozen=True)
class WorkbenchBlocker:
    """Stable machine code plus a count safe to localize in the browser."""

    code: str
    missing_count: int


@dataclass(frozen=True)
class WorkbenchPreflight:
    """Complete deterministic readiness result for one draft revision."""

    pilot_ready: bool
    pilot_blockers: tuple[WorkbenchBlocker, ...]
    ready_to_freeze: bool
    calibration_count: int
    acceptance_count: int
    unlabeled_count: int
    unreviewed_count: int
    unauthorized_count: int
    duplicate_source_count: int
    suitability_pending_count: int
    excluded_count: int
    blockers: tuple[WorkbenchBlocker, ...]


_STATE_RANK: dict[WorkbenchSampleState, int] = {
    "uploaded": 0,
    "labeled": 1,
    "mask_proposed": 2,
    "human_reviewed": 3,
    "frozen": 4,
    "revoked": 5,
}


def _is_sha256(value: str | None) -> bool:
    return bool(
        value
        and len(value) == 64
        and all(character in hexdigits for character in value)
    )


def _canonical_components(
    components: tuple[tuple[str, str], ...],
) -> tuple[tuple[str, str], ...]:
    return tuple(sorted(components))


def _review_is_current(sample: WorkbenchSampleDraft) -> bool:
    review = sample.human_review
    return bool(
        sample.state in {"human_reviewed", "frozen"}
        and review is not None
        and _is_sha256(sample.gold_mask_sha256)
        and review.source_sha256 == sample.source_sha256
        and review.gold_mask_sha256 == sample.gold_mask_sha256
        and _canonical_components(review.component_mask_sha256s)
        == _canonical_components(sample.component_mask_sha256s)
        and review.consent_text_version == sample.consent_text_version
    )


def transition_sample(
    sample: WorkbenchSampleDraft, event: WorkbenchSampleEvent
) -> WorkbenchSampleDraft:
    """Apply one forward-only transition without accepting stale review evidence."""
    if sample.state == "revoked" or (
        event.target_state != "revoked"
        and _STATE_RANK[event.target_state] < _STATE_RANK[sample.state]
    ):
        raise DomainRuleError(_TRANSITION_INVALID)
    if sample.state == "frozen" and event.target_state != "revoked":
        raise DomainRuleError(_TRANSITION_INVALID)

    updated = replace(
        sample,
        state=event.target_state,
        proposed_mask_sha256=(
            event.proposed_mask_sha256
            if event.proposed_mask_sha256 is not None
            else sample.proposed_mask_sha256
        ),
        gold_mask_sha256=(
            event.gold_mask_sha256
            if event.gold_mask_sha256 is not None
            else sample.gold_mask_sha256
        ),
        component_mask_sha256s=(
            event.component_mask_sha256s
            if event.component_mask_sha256s
            else sample.component_mask_sha256s
        ),
        human_review=event.human_review,
    )
    if event.target_state == "mask_proposed" and not _is_sha256(
        updated.proposed_mask_sha256
    ):
        raise DomainRuleError(_TRANSITION_INVALID)
    if event.target_state == "human_reviewed" and (
        event.human_review is None
        or event.human_review.source_sha256 != updated.source_sha256
        or event.human_review.gold_mask_sha256 != updated.gold_mask_sha256
        or _canonical_components(event.human_review.component_mask_sha256s)
        != _canonical_components(updated.component_mask_sha256s)
        or event.human_review.consent_text_version != updated.consent_text_version
    ):
        raise DomainRuleError(_TRANSITION_INVALID)
    return updated


def _is_authorized(sample: WorkbenchSampleDraft) -> bool:
    return (
        sample.authorization_scope == BENCHMARK_CONSENT_VERSION
        and sample.consent_text_version == BENCHMARK_CONSENT_VERSION
        and sample.state != "revoked"
    )


def _is_labeled(sample: WorkbenchSampleDraft) -> bool:
    return bool(
        sample.category
        and sample.difficulty in {"easy", "medium", "hard"}
    )


def _append_shortfalls(
    blockers: list[WorkbenchBlocker],
    *,
    prefix: str,
    actual: Counter[str],
    quotas: tuple[tuple[str, int], ...],
) -> None:
    for key, required in quotas:
        missing = max(0, required - actual[key])
        if missing:
            blockers.append(
                WorkbenchBlocker(
                    code=f"foreground_benchmark_workbench_{prefix}_{key}_shortfall",
                    missing_count=missing,
                )
            )


def evaluate_workbench_preflight(draft: WorkbenchDatasetDraft) -> WorkbenchPreflight:
    """Return every policy blocker in stable business-facing order."""
    active = tuple(sample for sample in draft.samples if sample.state != "revoked")
    eligible = tuple(
        sample
        for sample in active
        if sample.suitability_status in {"legacy_eligible", "eligible"}
    )
    suitability_pending = sum(
        sample.suitability_status == "pending" for sample in active
    )
    excluded = sum(sample.suitability_status == "excluded" for sample in active)
    calibration = tuple(sample for sample in eligible if sample.split == "calibration")
    acceptance = tuple(sample for sample in eligible if sample.split == "acceptance")
    unauthorized = sum(not _is_authorized(sample) for sample in active)
    unlabeled = sum(not _is_labeled(sample) for sample in eligible)
    unreviewed = sum(not _review_is_current(sample) for sample in eligible)
    digest_counts = Counter(sample.source_sha256 for sample in active)
    duplicates = sum(count - 1 for count in digest_counts.values() if count > 1)

    pilot_blockers: list[WorkbenchBlocker] = []
    if suitability_pending:
        pilot_blockers.append(
            WorkbenchBlocker(
                "foreground_benchmark_workbench_suitability_pending",
                suitability_pending,
            )
        )
    if unauthorized:
        pilot_blockers.append(
            WorkbenchBlocker(
                "foreground_benchmark_workbench_unauthorized", unauthorized
            )
        )
    if duplicates:
        pilot_blockers.append(
            WorkbenchBlocker(
                "foreground_benchmark_workbench_duplicate_source", duplicates
            )
        )
    if not calibration:
        pilot_blockers.append(
            WorkbenchBlocker(
                "foreground_benchmark_workbench_pilot_calibration_required", 1
            )
        )
    if not acceptance:
        pilot_blockers.append(
            WorkbenchBlocker(
                "foreground_benchmark_workbench_pilot_acceptance_required", 1
            )
        )

    blockers: list[WorkbenchBlocker] = []
    if suitability_pending:
        blockers.append(
            WorkbenchBlocker(
                "foreground_benchmark_workbench_suitability_pending",
                suitability_pending,
            )
        )
    if unauthorized:
        blockers.append(
            WorkbenchBlocker(
                "foreground_benchmark_workbench_unauthorized", unauthorized
            )
        )
    if duplicates:
        blockers.append(
            WorkbenchBlocker(
                "foreground_benchmark_workbench_duplicate_source", duplicates
            )
        )
    if len(calibration) != 10:
        blockers.append(
            WorkbenchBlocker(
                "foreground_benchmark_workbench_calibration_count_invalid",
                abs(10 - len(calibration)),
            )
        )
    if len(acceptance) != 50:
        blockers.append(
            WorkbenchBlocker(
                "foreground_benchmark_workbench_acceptance_count_invalid",
                abs(50 - len(acceptance)),
            )
        )
    if unlabeled:
        blockers.append(
            WorkbenchBlocker("foreground_benchmark_workbench_unlabeled", unlabeled)
        )
    if unreviewed:
        blockers.append(
            WorkbenchBlocker(
                "foreground_benchmark_workbench_unreviewed", unreviewed
            )
        )

    category_counts = Counter(sample.category or "" for sample in acceptance)
    difficulty_counts = Counter(sample.difficulty or "" for sample in acceptance)
    scene_counts = Counter(tag for sample in acceptance for tag in sample.scene_tags)
    material_counts = Counter(
        tag for sample in acceptance for tag in sample.material_tags
    )
    component_counts = Counter(
        tag for sample in acceptance for tag in sample.critical_component_names
    )
    _append_shortfalls(
        blockers,
        prefix="category",
        actual=category_counts,
        quotas=(("bracelet", 5), ("necklace", 5), ("earring", 5), ("ring", 5)),
    )
    _append_shortfalls(
        blockers,
        prefix="difficulty",
        actual=difficulty_counts,
        quotas=(("easy", 10), ("medium", 15), ("hard", 15)),
    )
    _append_shortfalls(
        blockers,
        prefix="scene",
        actual=scene_counts,
        quotas=(
            ("complex_background", 5),
            ("glove", 5),
            ("tray", 5),
            ("bust", 5),
        ),
    )
    _append_shortfalls(
        blockers,
        prefix="material",
        actual=material_counts,
        quotas=(("fine_chain", 5), ("transparent", 5), ("reflective", 5)),
    )
    _append_shortfalls(
        blockers,
        prefix="component",
        actual=component_counts,
        quotas=(("clasp", 5), ("pendant", 5)),
    )
    return WorkbenchPreflight(
        pilot_ready=not pilot_blockers and draft.state == "draft",
        pilot_blockers=tuple(pilot_blockers),
        ready_to_freeze=not blockers and draft.state == "draft",
        calibration_count=len(calibration),
        acceptance_count=len(acceptance),
        unlabeled_count=unlabeled,
        unreviewed_count=unreviewed,
        unauthorized_count=unauthorized,
        duplicate_source_count=duplicates,
        suitability_pending_count=suitability_pending,
        excluded_count=excluded,
        blockers=tuple(blockers),
    )


def _sample_payload(sample: WorkbenchSampleDraft) -> dict[str, object]:
    value = asdict(sample)
    value["scene_tags"] = sorted(sample.scene_tags)
    value["material_tags"] = sorted(sample.material_tags)
    value["critical_component_names"] = sorted(sample.critical_component_names)
    value["component_mask_sha256s"] = [
        list(component) for component in sorted(sample.component_mask_sha256s)
    ]
    if sample.human_review is not None:
        review = asdict(sample.human_review)
        review["component_mask_sha256s"] = [
            list(component)
            for component in sorted(sample.human_review.component_mask_sha256s)
        ]
        value["human_review"] = review
    return value


def canonical_workbench_draft_json(draft: WorkbenchDatasetDraft) -> bytes:
    """Serialize path-free draft metadata independently of caller ordering."""
    payload = {
        "dataset_id": str(draft.dataset_id),
        "customer_id": str(draft.customer_id),
        "revision": draft.revision,
        "state": draft.state,
        "samples": [
            _sample_payload(sample)
            for sample in sorted(draft.samples, key=lambda item: item.sample_id)
        ],
    }
    return json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode()
