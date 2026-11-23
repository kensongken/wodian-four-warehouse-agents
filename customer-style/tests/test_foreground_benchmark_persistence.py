from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from io import BytesIO
from uuid import UUID, uuid4

import pytest
from PIL import Image
from sqlalchemy.orm import Session

from opc_style.domain.errors import DomainRuleError
from opc_style.domain.foreground_benchmark import (
    CriticalComponent,
    GoldSampleManifest,
    LocalModelManifest,
)
from opc_style.models import (
    Customer,
    ForegroundBenchmarkRun,
    ForegroundBenchmarkSampleResult,
    ForegroundShadowDecision,
)
from opc_style.services.foreground_benchmarks import (
    ForegroundBenchmarkService,
    ValidatedBenchmarkDataset,
    ValidatedBenchmarkSample,
)


def _mask(values: list[int]) -> bytes:
    image = Image.new("L", (4, 4))
    image.putdata(values)
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _dataset(*sample_ids: str) -> ValidatedBenchmarkDataset:
    requested_ids = sample_ids or ("sample-1",)
    gold = _mask([0, 0, 0, 0, 0, 255, 255, 0, 0, 255, 255, 0, 0, 0, 0, 0])
    source = Image.new("RGB", (4, 4), (10, 20, 30))
    source_output = BytesIO()
    source.save(source_output, format="PNG")
    source_bytes = source_output.getvalue()
    component = _mask([0, 0, 0, 0, 0, 255, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0])
    manifest = GoldSampleManifest(
        sample_id="sample-1",
        source_sha256=sha256(source_bytes).hexdigest(),
        width=4,
        height=4,
        mime_type="image/png",
        category="bracelet",
        difficulty="easy",
        scene_tags=("complex_background",),
        material_tags=("reflective",),
        gold_mask_sha256=sha256(gold).hexdigest(),
        critical_components=(
            CriticalComponent(name="pendant", mask_sha256=sha256(component).hexdigest()),
        ),
        human_reviewed=True,
        split="acceptance",
    )
    samples = tuple(
        ValidatedBenchmarkSample(
            manifest=replace(manifest, sample_id=sample_id),
            source_content=source_bytes,
            gold_mask_content=gold,
            critical_component_masks=(("pendant", component),),
        )
        for sample_id in requested_ids
    )
    return ValidatedBenchmarkDataset(
        samples=samples,
        seal_sha256="a" * 64,
        acceptance_count=len(samples),
        calibration_count=0,
        categories=("bracelet",),
    )


def _model() -> LocalModelManifest:
    return LocalModelManifest(
        model_name="sam2",
        model_version="sealed-test",
        source_url="https://example.invalid/sam2",
        license_id="Apache-2.0",
        weights_sha256="b" * 64,
        model_tree_sha256="c" * 64,
        executable_sha256="d" * 64,
        runtime_versions=(
            ("pillow", "11.3.0"),
            ("python", "3.9.6"),
            ("safetensors", "0.7.0"),
            ("torch", "2.8.0"),
            ("torchvision", "0.23.0"),
            ("transformers", "4.57.6"),
        ),
        total_bytes=184_000_000,
    )


def _customer(session: Session, customer_id: UUID) -> None:
    session.add(Customer(id=customer_id, display_name="Benchmark owner"))
    session.flush()


def test_run_persists_replayable_metrics_without_private_bytes(session: Session) -> None:
    customer_id = uuid4()
    _customer(session, customer_id)
    dataset = _dataset()
    service = ForegroundBenchmarkService(session)

    run = service.run_benchmark(
        customer_id=customer_id,
        dataset=dataset,
        model_manifest=_model(),
        config_sha256="e" * 64,
        mask_provider=lambda sample: sample.gold_mask_content,
    )
    report = service.replay_report(customer_id=customer_id, run_id=run.id)

    assert run.status == "completed"
    assert report.sample_count == 1
    assert report.auto_eligible_count == 1
    assert report.failure_count == 0
    result = session.query(ForegroundBenchmarkSampleResult).one()
    decision = session.query(ForegroundShadowDecision).one()
    assert result.sample_id == "sample-1"
    assert result.iou == 1
    assert decision.verdict == "auto_eligible"
    assert decision.pipeline_manifest_sha256 is None
    assert decision.stage_receipts_sha256 is None
    assert decision.risk_parameters_sha256 is None
    table_columns = {column.name for column in ForegroundBenchmarkSampleResult.__table__.columns}
    assert not {"source", "image", "mask", "path"} & table_columns


def test_formal_run_scores_acceptance_only_and_keeps_calibration_out_of_grade(
    session: Session,
) -> None:
    customer_id = uuid4()
    _customer(session, customer_id)
    base = _dataset("calibration-1", "acceptance-1")
    calibration, acceptance = base.samples
    dataset = replace(
        base,
        samples=(
            replace(
                calibration,
                manifest=replace(calibration.manifest, split="calibration"),
            ),
            acceptance,
        ),
        acceptance_count=1,
        calibration_count=1,
    )

    run = ForegroundBenchmarkService(session).run_benchmark(
        customer_id=customer_id,
        dataset=dataset,
        model_manifest=_model(),
        config_sha256="e" * 64,
        mask_provider=lambda sample: sample.gold_mask_content,
    )

    assert run.total_samples == 1
    assert session.query(ForegroundBenchmarkSampleResult).one().sample_id == (
        "acceptance-1"
    )


def test_run_failure_is_sanitized_and_cross_customer_replay_is_hidden(session: Session) -> None:
    owner_id, stranger_id = uuid4(), uuid4()
    _customer(session, owner_id)
    _customer(session, stranger_id)
    service = ForegroundBenchmarkService(session)

    def fail(_: ValidatedBenchmarkSample) -> bytes:
        raise RuntimeError("/private/customer/photo.jpg SECRET")

    run = service.run_benchmark(
        customer_id=owner_id,
        dataset=_dataset(),
        model_manifest=_model(),
        config_sha256="e" * 64,
        mask_provider=fail,
    )

    result = session.query(ForegroundBenchmarkSampleResult).one()
    assert result.failure_code == "foreground_candidate_failed"
    assert "private" not in result.failure_code
    with pytest.raises(DomainRuleError) as hidden:
        service.replay_report(customer_id=stranger_id, run_id=run.id)
    assert hidden.value.code == "foreground_benchmark_run_not_found"


def test_finished_evidence_cannot_be_updated_or_deleted(session: Session) -> None:
    customer_id = uuid4()
    _customer(session, customer_id)
    service = ForegroundBenchmarkService(session)
    run = service.run_benchmark(
        customer_id=customer_id,
        dataset=_dataset(),
        model_manifest=_model(),
        config_sha256="e" * 64,
        mask_provider=lambda sample: sample.gold_mask_content,
    )
    session.commit()
    result = session.query(ForegroundBenchmarkSampleResult).one()

    result.elapsed_ms = 999
    with pytest.raises(DomainRuleError, match="foreground_benchmark_evidence_immutable"):
        session.flush()
    session.rollback()

    persisted = session.get(ForegroundBenchmarkRun, run.id)
    assert persisted is not None
    session.delete(persisted)
    with pytest.raises(DomainRuleError, match="foreground_benchmark_evidence_immutable"):
        session.flush()


def test_interrupted_run_resumes_only_missing_samples(session: Session) -> None:
    customer_id = uuid4()
    _customer(session, customer_id)
    dataset = _dataset("sample-1", "sample-2")
    service = ForegroundBenchmarkService(session)
    first_calls: list[str] = []

    def interrupt_after_first(sample: ValidatedBenchmarkSample) -> bytes:
        first_calls.append(sample.manifest.sample_id)
        if sample.manifest.sample_id == "sample-2":
            raise KeyboardInterrupt
        return sample.gold_mask_content

    with pytest.raises(KeyboardInterrupt):
        service.run_benchmark(
            customer_id=customer_id,
            dataset=dataset,
            model_manifest=_model(),
            config_sha256="e" * 64,
            mask_provider=interrupt_after_first,
        )
    running = session.query(ForegroundBenchmarkRun).one()
    assert running.status == "running"
    resumed_calls: list[str] = []

    def finish(sample: ValidatedBenchmarkSample) -> bytes:
        resumed_calls.append(sample.manifest.sample_id)
        return sample.gold_mask_content

    completed = service.run_benchmark(
        customer_id=customer_id,
        dataset=dataset,
        model_manifest=_model(),
        config_sha256="e" * 64,
        mask_provider=finish,
        resume_run_id=running.id,
    )

    assert first_calls == ["sample-1", "sample-2"]
    assert resumed_calls == ["sample-2"]
    assert completed.status == "completed"
    assert session.query(ForegroundBenchmarkSampleResult).count() == 2
