from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from threading import Barrier, Lock, get_ident
from typing import Literal
from uuid import UUID

import pytest
from PIL import Image
from pydantic import ValidationError

from opc_style.domain.errors import DomainRuleError
from opc_style.domain.foreground_benchmark_workbench import (
    BENCHMARK_CONSENT_VERSION,
    HumanReviewEvidence,
    WorkbenchDatasetDraft,
    WorkbenchSampleDraft,
)
from opc_style.services.foreground_benchmark_workbench import (
    PrivateBenchmarkDraftStore,
    assess_gold_mask_quality,
    assess_proposal_mask_risk,
)
from opc_style.settings import ForegroundBenchmarkWorkbenchSettings

CUSTOMER_A = UUID("00000000-0000-4000-8000-0000000000a1")
CUSTOMER_B = UUID("00000000-0000-4000-8000-0000000000b2")


def _sample(index: int = 1) -> WorkbenchSampleDraft:
    return WorkbenchSampleDraft(
        sample_id=f"sample-{index}",
        source_sha256=f"{index:064x}",
        width=32,
        height=24,
        mime_type="image/png",
        split="calibration",
        state="uploaded",
        authorization_scope="foreground_benchmark_local_v1",
        consent_text_version="foreground_benchmark_local_v1",
        category=None,
        difficulty=None,
        scene_tags=(),
        material_tags=(),
        critical_component_names=(),
        proposed_mask_sha256=None,
        gold_mask_sha256=None,
        component_mask_sha256s=(),
        human_review=None,
    )


def _png(index: int, *, mask_box: tuple[int, int, int, int] | None = None) -> bytes:
    image = Image.new("L" if mask_box else "RGB", (8, 8), 0 if mask_box else (index, 20, 30))
    if mask_box:
        for x in range(mask_box[0], mask_box[2]):
            for y in range(mask_box[1], mask_box[3]):
                image.putpixel((x, y), 255)
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _large_mask(box: tuple[int, int, int, int]) -> bytes:
    image = Image.new("L", (100, 100), 0)
    for x in range(box[0], box[2]):
        for y in range(box[1], box[3]):
            image.putpixel((x, y), 255)
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def test_proposal_risk_triage_flags_large_edge_touching_carriers_only() -> None:
    safe_coverage, safe_border, safe_risk = assess_proposal_mask_risk(
        _large_mask((30, 30, 70, 70))
    )
    risky_coverage, risky_border, risky_risk = assess_proposal_mask_risk(
        _large_mask((0, 0, 90, 100))
    )

    assert (safe_coverage, safe_border, safe_risk) == (0.16, 0.0, None)
    assert risky_coverage == 0.9
    assert risky_border > 0.1
    assert risky_risk == "carrier_included"


def test_gold_mask_quality_flags_soft_and_fragmented_fine_structure() -> None:
    image = Image.new("L", (100, 100), 0)
    for x in range(20, 40):
        for y in range(20, 40):
            image.putpixel((x, y), 255)
    for x in range(55, 75):
        for y in range(20, 40):
            image.putpixel((x, y), 64)
    for x in range(80, 85):
        for y in range(80, 85):
            image.putpixel((x, y), 255)
    output = BytesIO()
    image.save(output, format="PNG")

    coverage, border, soft, components, risks = assess_gold_mask_quality(
        output.getvalue(), width=100, height=100
    )

    assert coverage == 0.0425
    assert border == 0
    assert soft > 0.48
    assert components == 2
    assert risks == ("soft_edges", "fragmented_fine_structure")


def test_proposal_risk_triage_flags_large_masks_in_hand_scenes() -> None:
    coverage, border, risk = assess_proposal_mask_risk(
        _large_mask((20, 10, 80, 80)),
        scene_tags=("hand_held", "reflective"),
    )
    same_mask_without_hand_scene = assess_proposal_mask_risk(
        _large_mask((20, 10, 80, 80))
    )

    assert coverage == 0.42
    assert border == 0
    assert risk == "carrier_included"
    assert same_mask_without_hand_scene == (0.42, 0.0, None)


def test_proposal_risk_triage_flags_masks_that_include_a_bust() -> None:
    included = assess_proposal_mask_risk(
        _large_mask((20, 20, 50, 50)),
        scene_tags=("bust",),
    )
    clean_earring = assess_proposal_mask_risk(
        _large_mask((45, 45, 55, 55)),
        scene_tags=("bust",),
    )
    same_mask_without_bust = assess_proposal_mask_risk(
        _large_mask((20, 20, 50, 50))
    )

    assert included == (0.09, 0.0, "carrier_included")
    assert clean_earring == (0.01, 0.0, None)
    assert same_mask_without_bust == (0.09, 0.0, None)


def _ready_dataset(store: PrivateBenchmarkDraftStore) -> WorkbenchDatasetDraft:
    draft = store.create_dataset(CUSTOMER_A)
    samples: list[WorkbenchSampleDraft] = []
    categories = ("bracelet", "necklace", "earring", "ring")
    difficulties = ("easy", "medium", "hard")
    scenes = ("complex_background", "glove", "tray", "bust")
    materials = ("fine_chain", "transparent", "reflective")
    for index in range(60):
        sample_id = f"sample-{index:03d}"
        source = _png(index + 1)
        gold = _png(0, mask_box=(1, 1, 7, 7))
        component = _png(0, mask_box=(2, 2, 4, 4))
        source_digest = sha256(source).hexdigest()
        gold_digest = sha256(gold).hexdigest()
        component_digest = sha256(component).hexdigest()
        component_name = "clasp" if index % 2 == 0 else "pendant"
        store.put_source(CUSTOMER_A, draft.dataset_id, sample_id, source, source_digest)
        store.put_mask_revision(CUSTOMER_A, draft.dataset_id, sample_id, gold, gold_digest)
        store.put_mask_revision(
            CUSTOMER_A, draft.dataset_id, sample_id, component, component_digest
        )
        samples.append(
            WorkbenchSampleDraft(
                sample_id=sample_id,
                source_sha256=source_digest,
                width=8,
                height=8,
                mime_type="image/png",
                split="calibration" if index < 10 else "acceptance",
                state="human_reviewed",
                authorization_scope=BENCHMARK_CONSENT_VERSION,
                consent_text_version=BENCHMARK_CONSENT_VERSION,
                category=categories[index % len(categories)],
                difficulty=difficulties[index % len(difficulties)],
                scene_tags=(scenes[index % len(scenes)],),
                material_tags=(materials[index % len(materials)],),
                critical_component_names=(component_name,),
                proposed_mask_sha256=None,
                gold_mask_sha256=gold_digest,
                component_mask_sha256s=((component_name, component_digest),),
                human_review=HumanReviewEvidence(
                    source_sha256=source_digest,
                    gold_mask_sha256=gold_digest,
                    component_mask_sha256s=((component_name, component_digest),),
                    consent_text_version=BENCHMARK_CONSENT_VERSION,
                    draft_revision=1,
                    reviewer_scope="test-reviewer",
                    reviewed_at="2026-09-28T00:00:00+00:00",
                ),
            )
        )
    return store.save_dataset(
        CUSTOMER_A, replace(draft, samples=tuple(samples)), expected_revision=0
    )


def test_workbench_settings_are_disabled_by_default_and_require_absolute_root() -> None:
    assert not ForegroundBenchmarkWorkbenchSettings().enabled
    with pytest.raises(ValidationError):
        ForegroundBenchmarkWorkbenchSettings(enabled=True, root=Path("relative"))


def test_store_rejects_symlink_root(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)

    with pytest.raises(DomainRuleError) as error:
        PrivateBenchmarkDraftStore(link)

    assert error.value.code == "foreground_benchmark_workbench_unavailable"


def test_create_save_and_restart_recovery_use_private_permissions(tmp_path: Path) -> None:
    root = tmp_path / "private"
    store = PrivateBenchmarkDraftStore(root)

    created = store.create_dataset(CUSTOMER_A)
    saved = store.save_dataset(
        CUSTOMER_A,
        replace(created, samples=(_sample(),)),
        expected_revision=0,
    )
    recovered = PrivateBenchmarkDraftStore(root).load_dataset(
        CUSTOMER_A, created.dataset_id
    )

    assert saved.revision == 1
    assert recovered == saved
    assert root.stat().st_mode & 0o777 == 0o700
    for path in root.rglob("*"):
        assert path.stat().st_mode & 0o777 == (0o700 if path.is_dir() else 0o600)


def test_stale_revision_is_rejected_without_lost_update(tmp_path: Path) -> None:
    store = PrivateBenchmarkDraftStore(tmp_path / "private")
    draft = store.create_dataset(CUSTOMER_A)
    first = store.save_dataset(
        CUSTOMER_A, replace(draft, samples=(_sample(),)), expected_revision=0
    )

    with pytest.raises(DomainRuleError) as error:
        store.save_dataset(CUSTOMER_A, replace(draft, samples=()), expected_revision=0)

    assert error.value.code == "foreground_benchmark_workbench_revision_conflict"
    assert store.load_dataset(CUSTOMER_A, draft.dataset_id) == first


@pytest.mark.parametrize(("split", "limit"), [("calibration", 10), ("acceptance", 50)])
def test_source_intake_rejects_samples_beyond_split_limit(
    tmp_path: Path, split: Literal["calibration", "acceptance"], limit: int
) -> None:
    store = PrivateBenchmarkDraftStore(tmp_path / "private")
    draft = store.create_dataset(CUSTOMER_A)
    for index in range(limit):
        draft = store.add_source_sample(
            CUSTOMER_A,
            draft.dataset_id,
            _png(index + 1),
            split=split,
            expected_revision=draft.revision,
            consent_text_version=BENCHMARK_CONSENT_VERSION,
        )

    with pytest.raises(DomainRuleError) as error:
        store.add_source_sample(
            CUSTOMER_A,
            draft.dataset_id,
            _png(limit + 1),
            split=split,
            expected_revision=draft.revision,
            consent_text_version=BENCHMARK_CONSENT_VERSION,
        )

    assert error.value.code == "foreground_benchmark_split_limit_exceeded"
    recovered = store.load_dataset(CUSTOMER_A, draft.dataset_id)
    assert recovered.revision == limit
    assert len(recovered.samples) == limit


def test_excluded_sample_does_not_consume_an_intake_slot(tmp_path: Path) -> None:
    store = PrivateBenchmarkDraftStore(tmp_path / "private")
    draft = store.create_dataset(CUSTOMER_A)
    for index in range(10):
        draft = store.add_source_sample(
            CUSTOMER_A,
            draft.dataset_id,
            _png(index + 1),
            split="calibration",
            expected_revision=draft.revision,
            consent_text_version=BENCHMARK_CONSENT_VERSION,
        )
    first = draft.samples[0]
    draft = store.set_sample_suitability(
        CUSTOMER_A,
        draft.dataset_id,
        first.sample_id,
        expected_revision=draft.revision,
        decision="excluded",
        reason="no_unique_product_identity",
        reviewer_scope="test-reviewer",
        confirmed=True,
    )

    replaced = store.add_source_sample(
        CUSTOMER_A,
        draft.dataset_id,
        _png(11),
        split="calibration",
        expected_revision=draft.revision,
        consent_text_version=BENCHMARK_CONSENT_VERSION,
    )

    assert len(replaced.samples) == 11
    assert sum(
        sample.split == "calibration" and sample.suitability_status != "excluded"
        for sample in replaced.samples
    ) == 10


def test_simultaneous_saves_allow_only_one_revision_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = PrivateBenchmarkDraftStore(tmp_path / "private")
    draft = store.create_dataset(CUSTOMER_A)
    original_load = store.load_dataset
    barrier = Barrier(2)
    seen_threads: set[int] = set()
    seen_lock = Lock()

    def synchronized_first_load(
        customer_id: UUID, dataset_id: UUID
    ) -> WorkbenchDatasetDraft:
        loaded = original_load(customer_id, dataset_id)
        thread_id = get_ident()
        with seen_lock:
            first = thread_id not in seen_threads
            seen_threads.add(thread_id)
        if first:
            barrier.wait(timeout=5)
        return loaded

    monkeypatch.setattr(store, "load_dataset", synchronized_first_load)

    def save(index: int) -> str:
        try:
            store.save_dataset(
                CUSTOMER_A,
                replace(draft, samples=(_sample(index),)),
                expected_revision=0,
            )
            return "saved"
        except DomainRuleError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(pool.map(save, (2, 3)))

    assert outcomes == ["foreground_benchmark_workbench_revision_conflict", "saved"]


def test_crash_before_pointer_replace_recovers_previous_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = PrivateBenchmarkDraftStore(tmp_path / "private")
    draft = store.create_dataset(CUSTOMER_A)
    real_replace = os.replace

    def fail_pointer_replace(source: str | Path, destination: str | Path) -> None:
        if Path(destination).name == "draft.json":
            raise OSError("simulated crash")
        real_replace(source, destination)

    monkeypatch.setattr(
        "opc_style.services.foreground_benchmark_workbench.os.replace",
        fail_pointer_replace,
    )
    with pytest.raises(DomainRuleError) as error:
        store.save_dataset(
            CUSTOMER_A, replace(draft, samples=(_sample(),)), expected_revision=0
        )

    assert error.value.code == "foreground_benchmark_workbench_write_failed"
    assert PrivateBenchmarkDraftStore(tmp_path / "private").load_dataset(
        CUSTOMER_A, draft.dataset_id
    ) == draft


def test_foreign_customer_cannot_discover_dataset(tmp_path: Path) -> None:
    store = PrivateBenchmarkDraftStore(tmp_path / "private")
    draft = store.create_dataset(CUSTOMER_A)

    with pytest.raises(DomainRuleError) as error:
        store.load_dataset(CUSTOMER_B, draft.dataset_id)

    assert error.value.code == "foreground_benchmark_workbench_not_found"


def test_source_write_is_idempotent_and_detects_tampering(tmp_path: Path) -> None:
    store = PrivateBenchmarkDraftStore(tmp_path / "private")
    draft = store.create_dataset(CUSTOMER_A)
    content = b"private-image-bytes"
    digest = sha256(content).hexdigest()

    first = store.put_source(
        CUSTOMER_A, draft.dataset_id, "opaque-sample", content, digest
    )
    second = store.put_source(
        CUSTOMER_A, draft.dataset_id, "opaque-sample", content, digest
    )
    assert first == second
    assert "private" not in first.relative_path
    assert store.read_private_content(
        CUSTOMER_A, draft.dataset_id, first.relative_path
    ) == content

    source_path = root_source = (
        tmp_path
        / "private"
        / "drafts"
        / str(CUSTOMER_A)
        / str(draft.dataset_id)
        / first.relative_path
    )
    root_source.write_bytes(b"changed")
    root_source.chmod(0o600)
    with pytest.raises(DomainRuleError) as error:
        store.read_private_content(
            CUSTOMER_A,
            draft.dataset_id,
            first.relative_path,
            expected_sha256=digest,
        )
    assert source_path.exists()
    assert error.value.code == "foreground_benchmark_workbench_digest_mismatch"


def test_store_rejects_path_escape_symlink_and_open_permissions(tmp_path: Path) -> None:
    store = PrivateBenchmarkDraftStore(tmp_path / "private")
    draft = store.create_dataset(CUSTOMER_A)
    dataset_root = (
        tmp_path
        / "private"
        / "drafts"
        / str(CUSTOMER_A)
        / str(draft.dataset_id)
    )
    unsafe = dataset_root / "unsafe.bin"
    unsafe.write_bytes(b"unsafe")
    unsafe.chmod(0o644)
    link = dataset_root / "link.bin"
    link.symlink_to(unsafe)

    for relative in ("../unsafe.bin", "link.bin", "unsafe.bin"):
        with pytest.raises(DomainRuleError) as error:
            store.read_private_content(CUSTOMER_A, draft.dataset_id, relative)
        assert error.value.code == "foreground_benchmark_workbench_file_invalid"


def test_delete_is_limited_to_unfrozen_samples(tmp_path: Path) -> None:
    store = PrivateBenchmarkDraftStore(tmp_path / "private")
    draft = store.create_dataset(CUSTOMER_A)
    saved = store.save_dataset(
        CUSTOMER_A, replace(draft, samples=(_sample(),)), expected_revision=0
    )

    deleted = store.delete_unfrozen_sample(
        CUSTOMER_A, saved.dataset_id, "sample-1", expected_revision=1
    )
    assert deleted.samples == ()
    frozen = store.save_dataset(
        CUSTOMER_A, replace(deleted, state="frozen"), expected_revision=2
    )
    with pytest.raises(DomainRuleError) as error:
        store.delete_unfrozen_sample(
            CUSTOMER_A, frozen.dataset_id, "sample-1", expected_revision=3
        )
    assert error.value.code == "foreground_benchmark_workbench_dataset_immutable"


def test_mask_revisions_are_immutable_private_files(tmp_path: Path) -> None:
    store = PrivateBenchmarkDraftStore(tmp_path / "private")
    draft = store.create_dataset(CUSTOMER_A)
    first_content = b"first-canonical-mask"
    second_content = b"second-canonical-mask"
    first = store.put_mask_revision(
        CUSTOMER_A,
        draft.dataset_id,
        "sample-1",
        first_content,
        sha256(first_content).hexdigest(),
    )
    second = store.put_mask_revision(
        CUSTOMER_A,
        draft.dataset_id,
        "sample-1",
        second_content,
        sha256(second_content).hexdigest(),
    )

    assert first.revision_id != second.revision_id
    assert first.relative_path != second.relative_path
    assert store.read_private_content(
        CUSTOMER_A,
        draft.dataset_id,
        first.relative_path,
        expected_sha256=first.mask_sha256,
    ) == first_content
    assert store.read_private_content(
        CUSTOMER_A,
        draft.dataset_id,
        second.relative_path,
        expected_sha256=second.mask_sha256,
    ) == second_content


def test_freeze_is_idempotent_customer_scoped_and_survives_restart(
    tmp_path: Path,
) -> None:
    root = tmp_path / "private"
    store = PrivateBenchmarkDraftStore(root)
    draft = _ready_dataset(store)

    first = store.materialize_and_freeze(
        CUSTOMER_A,
        draft.dataset_id,
        expected_revision=1,
        idempotency_key="freeze-ready-v1",
    )
    replay = store.materialize_and_freeze(
        CUSTOMER_A,
        draft.dataset_id,
        expected_revision=1,
        idempotency_key="freeze-ready-v1",
    )
    recovered = PrivateBenchmarkDraftStore(root).load_active_frozen_dataset(CUSTOMER_A)

    assert replay.version_id == first.version_id
    assert recovered.seal_sha256 == first.dataset.seal_sha256
    assert recovered.acceptance_count == 50
    assert recovered.calibration_count == 10
    assert store.load_dataset(CUSTOMER_A, draft.dataset_id).state == "frozen"
    with pytest.raises(DomainRuleError) as foreign:
        store.load_active_frozen_dataset(CUSTOMER_B)
    assert foreign.value.code == "foreground_benchmark_active_not_found"


def test_post_freeze_revocation_blocks_future_loading_without_erasing_version(
    tmp_path: Path,
) -> None:
    store = PrivateBenchmarkDraftStore(tmp_path / "private")
    draft = _ready_dataset(store)
    frozen = store.materialize_and_freeze(
        CUSTOMER_A,
        draft.dataset_id,
        expected_revision=1,
        idempotency_key="freeze-before-revoke",
    )
    revoked_digest = draft.samples[0].source_sha256

    store.revoke_source(CUSTOMER_A, revoked_digest)

    with pytest.raises(DomainRuleError) as revoked:
        store.load_active_frozen_dataset(CUSTOMER_A)
    assert revoked.value.code == "foreground_benchmark_authorization_revoked"
    assert frozen.root.is_dir()
    assert (frozen.root / "frozen-manifest.json").is_file()


def test_pre_freeze_revocation_blocks_materialization_and_activation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "private"
    store = PrivateBenchmarkDraftStore(root)
    draft = _ready_dataset(store)
    store.revoke_source(CUSTOMER_A, draft.samples[0].source_sha256)

    with pytest.raises(DomainRuleError) as revoked:
        store.materialize_and_freeze(
            CUSTOMER_A,
            draft.dataset_id,
            expected_revision=1,
            idempotency_key="freeze-after-revoke",
        )

    assert revoked.value.code == "foreground_benchmark_authorization_revoked"
    assert not (root / "active" / f"{CUSTOMER_A}.json").exists()
    frozen_customer = root / "frozen" / str(CUSTOMER_A)
    assert not frozen_customer.exists() or not any(frozen_customer.iterdir())


def test_freeze_failure_restores_pointer_and_removes_partial_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "private"
    store = PrivateBenchmarkDraftStore(root)
    draft = _ready_dataset(store)

    def fail_final_save(*args: object, **kwargs: object) -> WorkbenchDatasetDraft:
        del args, kwargs
        raise DomainRuleError("simulated_freeze_commit_failure")

    monkeypatch.setattr(store, "save_dataset", fail_final_save)
    with pytest.raises(DomainRuleError, match="simulated_freeze_commit_failure"):
        store.materialize_and_freeze(
            CUSTOMER_A,
            draft.dataset_id,
            expected_revision=1,
            idempotency_key="freeze-fails",
        )

    assert not (root / "active" / f"{CUSTOMER_A}.json").exists()
    frozen_customer = root / "frozen" / str(CUSTOMER_A)
    assert not frozen_customer.exists() or not any(frozen_customer.iterdir())
