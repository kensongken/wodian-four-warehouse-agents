"""Private, local-only foreground benchmark control and evidence routes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
from typing import Annotated, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from opc_style.api.dependencies import CustomerId, get_session
from opc_style.domain.errors import DomainRuleError
from opc_style.domain.foreground_benchmark import (
    LocalForegroundPipelineManifest,
    pipeline_manifest_sha256,
)
from opc_style.models import (
    ForegroundBenchmarkRun,
    ForegroundBenchmarkSampleResult,
)
from opc_style.schemas import (
    ForegroundBenchmarkDatasetResponse,
    ForegroundBenchmarkReportResponse,
    ForegroundBenchmarkRunRequest,
    ForegroundBenchmarkRunResponse,
)
from opc_style.services.foreground_benchmark_workbench import PrivateBenchmarkDraftStore
from opc_style.services.foreground_benchmarks import (
    FOREGROUND_BENCHMARK_CONFIG_SHA256,
    ForegroundBenchmarkService,
    ValidatedBenchmarkDataset,
    validate_dataset,
)
from opc_style.settings import ForegroundBenchmarkSettings

router = APIRouter(
    prefix="/local/style-suite/api/foreground-benchmarks",
    tags=["local-foreground-benchmark"],
)
SessionDependency = Annotated[Session, Depends(get_session)]
DatasetLoader = Callable[..., ValidatedBenchmarkDataset]


def _error(code: str, status_code: int = status.HTTP_409_CONFLICT) -> JSONResponse:
    return JSONResponse(
        status_code=status_code, content={"error": {"code": code, "retryable": False}}
    )


def _dataset(
    request: Request,
    customer_id: UUID,
    *,
    allow_revoked: bool = False,
) -> ValidatedBenchmarkDataset:
    workbench_store = getattr(
        request.app.state, "foreground_benchmark_workbench_store", None
    )
    if isinstance(workbench_store, PrivateBenchmarkDraftStore):
        try:
            return workbench_store.load_active_frozen_dataset(
                customer_id, enforce_revocations=not allow_revoked
            )
        except DomainRuleError as error:
            if error.code != "foreground_benchmark_active_not_found":
                raise
    settings = getattr(
        request.app.state,
        "foreground_benchmark_settings",
        ForegroundBenchmarkSettings(),
    )
    if not settings.enabled or settings.root is None:
        raise DomainRuleError("foreground_benchmark_unavailable")
    loader: DatasetLoader = getattr(
        request.app.state, "foreground_benchmark_dataset_loader", validate_dataset
    )
    return loader(settings.root)


def _run_response(run: ForegroundBenchmarkRun) -> ForegroundBenchmarkRunResponse:
    return ForegroundBenchmarkRunResponse(
        id=run.id,
        status=cast(Literal["running", "completed", "failed"], run.status),
        total_samples=run.total_samples,
        completed_samples=run.completed_samples,
        failed_samples=run.failed_samples,
        dataset_seal_sha256=run.dataset_seal_sha256,
        model_manifest_sha256=run.model_manifest_sha256,
        config_sha256=run.config_sha256,
        failure_code=run.failure_code,
        publishable=False,
    )


def _owned_run(
    session: Session, customer_id: UUID, run_id: UUID
) -> ForegroundBenchmarkRun:
    run = session.scalar(
        select(ForegroundBenchmarkRun).where(
            ForegroundBenchmarkRun.id == run_id,
            ForegroundBenchmarkRun.customer_id == customer_id,
        )
    )
    if run is None:
        raise DomainRuleError("foreground_benchmark_run_not_found")
    return run


@router.get("/validate", response_model=ForegroundBenchmarkDatasetResponse)
def validate_private_foreground_dataset(
    request: Request, customer_id: CustomerId
) -> ForegroundBenchmarkDatasetResponse | JSONResponse:
    try:
        dataset = _dataset(request, customer_id)
    except DomainRuleError as error:
        return _error(
            error.code,
            status.HTTP_404_NOT_FOUND
            if error.code == "foreground_benchmark_unavailable"
            else status.HTTP_409_CONFLICT,
        )
    return ForegroundBenchmarkDatasetResponse(
        enabled=True,
        dataset_seal_sha256=dataset.seal_sha256,
        acceptance_count=dataset.acceptance_count,
        calibration_count=dataset.calibration_count,
        categories=list(dataset.categories),
    )


@router.post(
    "/runs",
    response_model=ForegroundBenchmarkRunResponse,
    status_code=status.HTTP_201_CREATED,
)
def start_private_foreground_benchmark(
    payload: ForegroundBenchmarkRunRequest,
    request: Request,
    customer_id: CustomerId,
    session: SessionDependency,
    response: Response,
) -> ForegroundBenchmarkRunResponse | JSONResponse:
    try:
        dataset = _dataset(request, customer_id)
        if dataset.seal_sha256 != payload.dataset_seal_sha256:
            raise DomainRuleError("foreground_benchmark_digest_mismatch")
        if payload.config_sha256 != FOREGROUND_BENCHMARK_CONFIG_SHA256:
            raise DomainRuleError("foreground_benchmark_config_mismatch")
        existing = session.scalar(
            select(ForegroundBenchmarkRun).where(
                ForegroundBenchmarkRun.customer_id == customer_id,
                ForegroundBenchmarkRun.idempotency_key == payload.idempotency_key,
            )
        )
        if existing is not None:
            if (
                existing.dataset_seal_sha256 != payload.dataset_seal_sha256
                or existing.model_manifest_sha256 != payload.model_manifest_sha256
                or existing.config_sha256 != payload.config_sha256
            ):
                raise DomainRuleError("foreground_benchmark_idempotency_conflict")
            response.status_code = status.HTTP_200_OK
            return _run_response(existing)
        model_manifest = getattr(
            request.app.state, "foreground_benchmark_model_manifest", None
        )
        mask_provider = getattr(
            request.app.state, "foreground_benchmark_mask_provider", None
        )
        if not isinstance(model_manifest, LocalForegroundPipelineManifest) or not callable(
            mask_provider
        ):
            raise DomainRuleError("foreground_benchmark_candidate_unavailable")
        if pipeline_manifest_sha256(model_manifest) != payload.model_manifest_sha256:
            raise DomainRuleError("foreground_candidate_manifest_mismatch")
        run = ForegroundBenchmarkService(session).run_benchmark(
            customer_id=customer_id,
            dataset=dataset,
            model_manifest=model_manifest,
            config_sha256=payload.config_sha256,
            idempotency_key=payload.idempotency_key,
            mask_provider=mask_provider,
        )
        session.commit()
        return _run_response(run)
    except DomainRuleError as error:
        session.rollback()
        code = (
            status.HTTP_404_NOT_FOUND
            if error.code
            in {
                "foreground_benchmark_unavailable",
                "foreground_benchmark_run_not_found",
            }
            else status.HTTP_409_CONFLICT
        )
        return _error(error.code, code)


@router.get("/runs/{run_id}", response_model=ForegroundBenchmarkRunResponse)
def private_foreground_benchmark_status(
    run_id: UUID, customer_id: CustomerId, session: SessionDependency
) -> ForegroundBenchmarkRunResponse:
    try:
        return _run_response(_owned_run(session, customer_id, run_id))
    except DomainRuleError as error:
        raise HTTPException(status_code=404, detail=error.code) from error


def _category_breakdown(
    dataset: ValidatedBenchmarkDataset,
    rows: tuple[ForegroundBenchmarkSampleResult, ...],
) -> dict[str, dict[str, int]]:
    categories = {
        sample.manifest.sample_id: sample.manifest.category
        for sample in dataset.samples
    }
    result: dict[str, dict[str, int]] = {}
    for row in rows:
        bucket = result.setdefault(
            categories.get(row.sample_id, "unknown"),
            {
                "total": 0,
                "auto_eligible": 0,
                "human_review_required": 0,
                "blocked": 0,
                "failed": 0,
            },
        )
        bucket["total"] += 1
        bucket[row.verdict] += 1
        if row.status == "failed":
            bucket["failed"] += 1
    return dict(sorted(result.items()))


def _report_response(
    session: Session,
    customer_id: UUID,
    run_id: UUID,
    dataset: ValidatedBenchmarkDataset,
) -> ForegroundBenchmarkReportResponse:
    run = _owned_run(session, customer_id, run_id)
    if run.dataset_seal_sha256 != dataset.seal_sha256:
        raise DomainRuleError("foreground_benchmark_digest_mismatch")
    report = ForegroundBenchmarkService(session).replay_report(
        customer_id=customer_id, run_id=run_id
    )
    rows = tuple(
        session.scalars(
            select(ForegroundBenchmarkSampleResult).where(
                ForegroundBenchmarkSampleResult.customer_id == customer_id,
                ForegroundBenchmarkSampleResult.run_id == run_id,
            )
        )
    )
    payload = asdict(report)
    payload.update(
        status="PASS" if report.passed else "FAIL",
        category_breakdown=_category_breakdown(dataset, rows),
        dataset_seal_sha256=run.dataset_seal_sha256,
        model_manifest_sha256=run.model_manifest_sha256,
        config_sha256=run.config_sha256,
        publishable=False,
    )
    payload.pop("passed")
    return ForegroundBenchmarkReportResponse(**payload)


@router.get("/runs/{run_id}/report", response_model=ForegroundBenchmarkReportResponse)
def private_foreground_benchmark_report(
    run_id: UUID, request: Request, customer_id: CustomerId, session: SessionDependency
) -> ForegroundBenchmarkReportResponse | JSONResponse:
    try:
        return _report_response(
            session,
            customer_id,
            run_id,
            _dataset(request, customer_id, allow_revoked=True),
        )
    except DomainRuleError as error:
        return _error(
            error.code,
            status.HTTP_404_NOT_FOUND
            if error.code
            in {
                "foreground_benchmark_unavailable",
                "foreground_benchmark_run_not_found",
            }
            else status.HTTP_409_CONFLICT,
        )


@router.get(
    "/runs/{run_id}/report.md",
    response_class=PlainTextResponse,
    response_model=None,
)
def private_foreground_benchmark_markdown(
    run_id: UUID, request: Request, customer_id: CustomerId, session: SessionDependency
) -> PlainTextResponse | JSONResponse:
    try:
        report = _report_response(
            session,
            customer_id,
            run_id,
            _dataset(request, customer_id, allow_revoked=True),
        )
    except DomainRuleError as error:
        return _error(
            error.code,
            status.HTTP_404_NOT_FOUND
            if error.code
            in {
                "foreground_benchmark_unavailable",
                "foreground_benchmark_run_not_found",
            }
            else status.HTTP_409_CONFLICT,
        )
    lines = [
        "# 本地珠宝前景基准报告",
        "",
        f"- 结论：{report.status}",
        f"- 样本：{report.sample_count}",
        f"- 技术可通过：{report.auto_eligible_count}",
        f"- 人工检查：{report.human_review_required_count}",
        f"- 阻断：{report.blocked_count}",
        f"- 失败：{report.failure_count}",
        f"- 最低关键部件保留率：{report.minimum_critical_component_preservation:.6f}",
        f"- 技术可通过最大背景污染率：{report.maximum_auto_eligible_contamination:.6f}",
        f"- 坏图误放：{report.bad_auto_eligible_count}",
        f"- 数据集摘要：{report.dataset_seal_sha256}",
        f"- 模型摘要：{report.model_manifest_sha256}",
        f"- 配置摘要：{report.config_sha256}",
        "- 说明：本报告不可发布，且不会取消页面人工确认。",
    ]
    return PlainTextResponse(
        "\n".join(lines) + "\n", headers={"Cache-Control": "no-store"}
    )


@router.get("/runs/{run_id}/samples/{sample_id}/thumbnail")
def private_foreground_benchmark_thumbnail(
    run_id: UUID,
    sample_id: str,
    request: Request,
    customer_id: CustomerId,
    session: SessionDependency,
    kind: Annotated[Literal["source", "gold"], Query()] = "source",
) -> Response:
    try:
        run = _owned_run(session, customer_id, run_id)
        dataset = _dataset(request, customer_id)
        if run.dataset_seal_sha256 != dataset.seal_sha256:
            raise DomainRuleError("foreground_benchmark_digest_mismatch")
        sample = next(
            (item for item in dataset.samples if item.manifest.sample_id == sample_id),
            None,
        )
        if sample is None:
            raise DomainRuleError("foreground_benchmark_sample_not_found")
        content = (
            sample.source_content if kind == "source" else sample.gold_mask_content
        )
        media_type = sample.manifest.mime_type if kind == "source" else "image/png"
        return Response(
            content=content,
            media_type=media_type,
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )
    except DomainRuleError as error:
        raise HTTPException(status_code=404, detail=error.code) from error
