from __future__ import annotations

import json
from dataclasses import replace
from uuid import UUID

import pytest

from opc_style.domain.errors import DomainRuleError
from opc_style.domain.foreground_benchmark_workbench import (
    HumanReviewEvidence,
    WorkbenchBlocker,
    WorkbenchDatasetDraft,
    WorkbenchSampleDraft,
    WorkbenchSampleEvent,
    canonical_workbench_draft_json,
    evaluate_workbench_preflight,
    transition_sample,
)

CUSTOMER_ID = UUID("00000000-0000-4000-8000-000000000001")
DATASET_ID = UUID("00000000-0000-4000-8000-000000000002")
CONSENT_SCOPE = "foreground_benchmark_local_v1"


def _sha(index: int) -> str:
    return f"{index:064x}"


def _review(sample: WorkbenchSampleDraft, revision: int) -> HumanReviewEvidence:
    assert sample.gold_mask_sha256 is not None
    return HumanReviewEvidence(
        source_sha256=sample.source_sha256,
        gold_mask_sha256=sample.gold_mask_sha256,
        component_mask_sha256s=sample.component_mask_sha256s,
        consent_text_version=sample.consent_text_version,
        draft_revision=revision,
        reviewer_scope="test-reviewer",
        reviewed_at="2026-09-28T00:00:00+00:00",
    )


def _sample(
    index: int,
    *,
    split: str,
    category: str = "bracelet",
    difficulty: str = "easy",
    scene_tags: tuple[str, ...] = ("complex_background",),
    material_tags: tuple[str, ...] = ("fine_chain",),
    component_name: str = "clasp",
    state: str = "human_reviewed",
) -> WorkbenchSampleDraft:
    gold = _sha(10_000 + index)
    components = ((component_name, _sha(20_000 + index)),)
    sample = WorkbenchSampleDraft(
        sample_id=f"sample-{index:03d}",
        source_sha256=_sha(index + 1),
        width=1200,
        height=1200,
        mime_type="image/jpeg",
        split=split,  # type: ignore[arg-type]
        state=state,  # type: ignore[arg-type]
        authorization_scope=CONSENT_SCOPE,
        consent_text_version=CONSENT_SCOPE,
        category=category,
        difficulty=difficulty,
        scene_tags=scene_tags,
        material_tags=material_tags,
        critical_component_names=(component_name,),
        proposed_mask_sha256=None,
        gold_mask_sha256=gold,
        component_mask_sha256s=components,
        human_review=None,
    )
    return replace(sample, human_review=_review(sample, 7))


def _ready_draft() -> WorkbenchDatasetDraft:
    samples: list[WorkbenchSampleDraft] = []
    for index in range(10):
        samples.append(_sample(index, split="calibration"))
    categories = ("bracelet", "necklace", "earring", "ring")
    difficulties = ("easy",) * 10 + ("medium",) * 20 + ("hard",) * 20
    scenes = ("complex_background", "glove", "tray", "bust")
    materials = ("fine_chain", "transparent", "reflective")
    components = ("clasp", "pendant")
    for offset in range(50):
        samples.append(
            _sample(
                10 + offset,
                split="acceptance",
                category=categories[offset % len(categories)],
                difficulty=difficulties[offset],
                scene_tags=(scenes[offset % len(scenes)],),
                material_tags=(materials[offset % len(materials)],),
                component_name=components[offset % len(components)],
            )
        )
    return WorkbenchDatasetDraft(
        dataset_id=DATASET_ID,
        customer_id=CUSTOMER_ID,
        revision=7,
        state="draft",
        samples=tuple(samples),
    )


def test_preflight_rejects_duplicate_source_across_splits() -> None:
    draft = _ready_draft()
    duplicate = replace(draft.samples[10], source_sha256=draft.samples[0].source_sha256)
    result = evaluate_workbench_preflight(
        replace(draft, samples=(*draft.samples[:10], duplicate, *draft.samples[11:]))
    )

    assert not result.ready_to_freeze
    assert "foreground_benchmark_workbench_duplicate_source" in {
        blocker.code for blocker in result.blockers
    }
    assert result.duplicate_source_count == 1


@pytest.mark.parametrize(
    ("scope", "version"),
    [("style_learning", CONSENT_SCOPE), (CONSENT_SCOPE, "style-consent-v1")],
)
def test_preflight_rejects_non_benchmark_authorization(
    scope: str, version: str
) -> None:
    draft = _ready_draft()
    invalid = replace(
        draft.samples[0], authorization_scope=scope, consent_text_version=version
    )

    result = evaluate_workbench_preflight(replace(draft, samples=(invalid, *draft.samples[1:])))

    assert result.unauthorized_count == 1
    assert result.blockers[0].code == "foreground_benchmark_workbench_unauthorized"


def test_transition_rejects_backward_state_change() -> None:
    sample = _sample(1, split="calibration", state="human_reviewed")

    with pytest.raises(DomainRuleError) as error:
        transition_sample(sample, WorkbenchSampleEvent(target_state="labeled"))

    assert error.value.code == "foreground_benchmark_workbench_transition_invalid"


def test_machine_proposal_does_not_count_as_human_review() -> None:
    sample = replace(
        _sample(1, split="calibration"),
        state="mask_proposed",
        proposed_mask_sha256=_sha(99),
        gold_mask_sha256=None,
        component_mask_sha256s=(),
        human_review=None,
    )
    draft = replace(_ready_draft(), samples=(sample, *_ready_draft().samples[1:]))

    result = evaluate_workbench_preflight(draft)

    assert result.unreviewed_count == 1
    assert "foreground_benchmark_workbench_unreviewed" in {
        blocker.code for blocker in result.blockers
    }


def test_stale_human_review_digests_do_not_count() -> None:
    draft = _ready_draft()
    current = draft.samples[0]
    stale = replace(
        current,
        gold_mask_sha256=_sha(31_000),
        human_review=_review(current, draft.revision),
    )

    result = evaluate_workbench_preflight(replace(draft, samples=(stale, *draft.samples[1:])))

    assert result.unreviewed_count == 1
    assert not result.ready_to_freeze


def test_preflight_reports_all_coverage_quota_blockers() -> None:
    draft = _ready_draft()
    acceptance = tuple(
        replace(
            sample,
            category="other",
            difficulty="easy",
            scene_tags=(),
            material_tags=(),
            critical_component_names=("other",),
            component_mask_sha256s=(("other", sample.component_mask_sha256s[0][1]),),
            human_review=None,
            state="labeled",
        )
        for sample in draft.samples[10:]
    )
    result = evaluate_workbench_preflight(replace(draft, samples=(*draft.samples[:10], *acceptance)))

    codes = tuple(blocker.code for blocker in result.blockers)
    assert codes == (
        "foreground_benchmark_workbench_unreviewed",
        "foreground_benchmark_workbench_category_bracelet_shortfall",
        "foreground_benchmark_workbench_category_necklace_shortfall",
        "foreground_benchmark_workbench_category_earring_shortfall",
        "foreground_benchmark_workbench_category_ring_shortfall",
        "foreground_benchmark_workbench_difficulty_medium_shortfall",
        "foreground_benchmark_workbench_difficulty_hard_shortfall",
        "foreground_benchmark_workbench_scene_complex_background_shortfall",
        "foreground_benchmark_workbench_scene_glove_shortfall",
        "foreground_benchmark_workbench_scene_tray_shortfall",
        "foreground_benchmark_workbench_scene_bust_shortfall",
        "foreground_benchmark_workbench_material_fine_chain_shortfall",
        "foreground_benchmark_workbench_material_transparent_shortfall",
        "foreground_benchmark_workbench_material_reflective_shortfall",
        "foreground_benchmark_workbench_component_clasp_shortfall",
        "foreground_benchmark_workbench_component_pendant_shortfall",
    )


def test_exact_ten_calibration_and_fifty_acceptance_are_ready() -> None:
    result = evaluate_workbench_preflight(_ready_draft())

    assert result.ready_to_freeze
    assert result.pilot_ready
    assert result.pilot_blockers == ()
    assert result.calibration_count == 10
    assert result.acceptance_count == 50
    assert result.unlabeled_count == 0
    assert result.unreviewed_count == 0
    assert result.blockers == ()


def test_one_distinct_authorized_sample_per_split_starts_pilot_only() -> None:
    draft = replace(
        _ready_draft(),
        samples=(
            _sample(0, split="calibration", state="uploaded"),
            _sample(1, split="acceptance", state="uploaded"),
        ),
    )

    result = evaluate_workbench_preflight(draft)

    assert result.pilot_ready
    assert result.pilot_blockers == ()
    assert not result.ready_to_freeze
    assert result.calibration_count == 1
    assert result.acceptance_count == 1


def test_pilot_reports_missing_split_without_weakening_formal_gate() -> None:
    draft = replace(
        _ready_draft(),
        samples=(_sample(0, split="calibration", state="uploaded"),),
    )

    result = evaluate_workbench_preflight(draft)

    assert not result.pilot_ready
    assert result.pilot_blockers == (
        WorkbenchBlocker(
            "foreground_benchmark_workbench_pilot_acceptance_required", 1
        ),
    )
    assert not result.ready_to_freeze


def test_review_remains_current_when_another_sample_advances_dataset_revision() -> None:
    draft = _ready_draft()

    result = evaluate_workbench_preflight(replace(draft, revision=draft.revision + 1))

    assert result.ready_to_freeze
    assert result.unreviewed_count == 0


def test_more_than_ten_calibration_samples_cannot_pass_preflight() -> None:
    draft = _ready_draft()
    extra = replace(
        draft.samples[0],
        sample_id="sample-extra-calibration",
        source_sha256="f" * 64,
    )

    result = evaluate_workbench_preflight(
        replace(draft, samples=(*draft.samples, extra))
    )

    assert not result.ready_to_freeze
    assert result.calibration_count == 11
    assert WorkbenchBlocker(
        "foreground_benchmark_workbench_calibration_count_invalid", 1
    ) in result.blockers


def test_componentless_product_is_labeled_without_inventing_a_fitting() -> None:
    draft = _ready_draft()
    sample = draft.samples[0]
    assert sample.human_review is not None
    componentless = replace(
        sample,
        critical_component_names=(),
        component_mask_sha256s=(),
        human_review=replace(sample.human_review, component_mask_sha256s=()),
    )

    result = evaluate_workbench_preflight(
        replace(draft, samples=(componentless, *draft.samples[1:]))
    )

    assert result.ready_to_freeze
    assert result.unlabeled_count == 0


def test_suitability_gate_excludes_without_deleting_and_pending_blocks_pilot() -> None:
    draft = _ready_draft()
    pending = replace(draft.samples[0], suitability_status="pending")
    excluded = replace(
        draft.samples[10],
        suitability_status="excluded",
        suitability_reason="multi_product_scene",
    )

    result = evaluate_workbench_preflight(
        replace(draft, samples=(pending, *draft.samples[1:10], excluded, *draft.samples[11:]))
    )

    assert len(draft.samples) == 60
    assert excluded.sample_id == draft.samples[10].sample_id
    assert result.suitability_pending_count == 1
    assert result.excluded_count == 1
    assert result.calibration_count == 9
    assert result.acceptance_count == 49
    assert WorkbenchBlocker(
        "foreground_benchmark_workbench_suitability_pending", 1
    ) in result.pilot_blockers


def test_blocker_order_is_deterministic_independent_of_sample_order() -> None:
    draft = _ready_draft()
    reduced = replace(draft, samples=tuple(reversed(draft.samples[:5])))

    first = evaluate_workbench_preflight(reduced)
    second = evaluate_workbench_preflight(replace(reduced, samples=tuple(reversed(reduced.samples))))

    assert first.blockers == second.blockers


def test_canonical_json_is_independent_of_sample_and_tag_order() -> None:
    draft = _ready_draft()
    changed = replace(
        draft.samples[10],
        scene_tags=tuple(reversed(draft.samples[10].scene_tags)),
        material_tags=tuple(reversed(draft.samples[10].material_tags)),
    )
    reordered = replace(
        draft,
        samples=tuple(reversed((*draft.samples[:10], changed, *draft.samples[11:]))),
    )

    first = canonical_workbench_draft_json(draft)
    second = canonical_workbench_draft_json(reordered)

    assert first == second
    payload = json.loads(first)
    assert payload["dataset_id"] == str(DATASET_ID)
    assert payload["samples"][0]["sample_id"] == "sample-000"
