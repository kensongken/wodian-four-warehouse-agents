"""Private, customer-scoped intake routes for the foreground gold workbench."""

from __future__ import annotations

from typing import Annotated, Literal, cast
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from opc_style.api.dependencies import (
    CustomerId,
    ForegroundBenchmarkWorkbenchStoreDependency,
    ProductForegroundExtractorDependency,
    get_session,
)
from opc_style.api.routes_real_photos import get_asset_content_store
from opc_style.domain.errors import DomainRuleError
from opc_style.domain.foreground_benchmark_workbench import (
    BENCHMARK_CONSENT_VERSION,
    WorkbenchDatasetDraft,
    evaluate_workbench_preflight,
)
from opc_style.domain.product_foreground import ForegroundPromptHints
from opc_style.models import StyleSample
from opc_style.schemas import (
    WorkbenchBlockerResponse,
    WorkbenchComponentAnnotationReopenRequest,
    WorkbenchDatasetResponse,
    WorkbenchDeleteResponse,
    WorkbenchFreezeRequest,
    WorkbenchFreezeResponse,
    WorkbenchHumanReviewRequest,
    WorkbenchLabelRequest,
    WorkbenchPreflightResponse,
    WorkbenchPromptSuggestionResponse,
    WorkbenchProposalDisagreementResponse,
    WorkbenchProposalRequest,
    WorkbenchReviewAuditResponse,
    WorkbenchReviewDispositionRequest,
    WorkbenchRevokeRequest,
    WorkbenchSampleResponse,
    WorkbenchStyleImportRequest,
    WorkbenchSuitabilityRequest,
)
from opc_style.services.foreground_benchmark_workbench import (
    MAX_SOURCE_BYTES,
    PrivateBenchmarkDraftStore,
)
from opc_style.services.foreground_prompt_suggestions import suggest_flat_lay_prompt
from opc_style.services.real_photos import AssetContentStore, AssetContentStoreError

router = APIRouter(
    prefix="/local/style-suite/api/foreground-benchmark-workbench",
    tags=["local-foreground-benchmark-workbench"],
)
SessionDependency = Annotated[Session, Depends(get_session)]
ContentStoreDependency = Annotated[AssetContentStore, Depends(get_asset_content_store)]


def _error(code: str, status_code: int) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": {"code": code}})


def _store_or_404(
    store: PrivateBenchmarkDraftStore | None,
) -> PrivateBenchmarkDraftStore:
    if store is None:
        raise DomainRuleError("foreground_benchmark_workbench_not_found")
    return store


def _status_for(code: str) -> int:
    if code in {
        "foreground_benchmark_workbench_not_found",
        "foreground_benchmark_workbench_unavailable",
    }:
        return status.HTTP_404_NOT_FOUND
    if code == "foreground_benchmark_image_limits_exceeded":
        return status.HTTP_413_CONTENT_TOO_LARGE
    if code in {
        "foreground_benchmark_image_invalid",
        "foreground_benchmark_proposal_failed",
        "foreground_benchmark_gold_mask_invalid",
        "foreground_benchmark_component_outside_gold",
        "foreground_benchmark_component_required",
        "foreground_benchmark_consent_required",
        "foreground_benchmark_consent_version_invalid",
        "foreground_benchmark_prompt_suggestion_unavailable",
        "foreground_benchmark_prompt_suggestion_category_unsupported",
        "foreground_benchmark_prompt_suggestion_scene_unsupported",
        "foreground_benchmark_workbench_suitability_invalid",
        "foreground_benchmark_workbench_review_disposition_invalid",
    }:
        return status.HTTP_422_UNPROCESSABLE_CONTENT
    return status.HTTP_409_CONFLICT


def _sample_response(sample: object) -> WorkbenchSampleResponse:
    from opc_style.domain.foreground_benchmark_workbench import WorkbenchSampleDraft

    assert isinstance(sample, WorkbenchSampleDraft)
    return WorkbenchSampleResponse(
        sample_id=sample.sample_id,
        source_sha256=sample.source_sha256,
        width=sample.width,
        height=sample.height,
        mime_type=cast(Literal["image/jpeg", "image/png"], sample.mime_type),
        split=sample.split,
        state=sample.state,
        category=sample.category,
        difficulty=cast(Literal["easy", "medium", "hard"] | None, sample.difficulty),
        scene_tags=list(sample.scene_tags),
        material_tags=list(sample.material_tags),
        critical_component_names=list(sample.critical_component_names),
        has_proposed_mask=sample.proposed_mask_sha256 is not None,
        has_coarse_mask=sample.coarse_mask_sha256 is not None,
        has_gold_mask=sample.gold_mask_sha256 is not None,
        proposal_extractor_version=sample.proposal_extractor_version,
        proposal_coverage_ratio=sample.proposal_coverage_ratio,
        proposal_border_contact_ratio=sample.proposal_border_contact_ratio,
        proposal_review_risk=sample.proposal_review_risk,
        gold_mask_coverage_ratio=sample.gold_mask_coverage_ratio,
        gold_mask_border_contact_ratio=sample.gold_mask_border_contact_ratio,
        gold_mask_soft_edge_ratio=sample.gold_mask_soft_edge_ratio,
        gold_mask_component_count=sample.gold_mask_component_count,
        gold_mask_review_risks=list(sample.gold_mask_review_risks),
        gold_mask_sha256=sample.gold_mask_sha256,
        component_mask_sha256s=dict(sample.component_mask_sha256s),
        human_reviewed=sample.human_review is not None,
        suitability_status=sample.suitability_status,
        suitability_reason=sample.suitability_reason,
        suitability_reviewer_scope=sample.suitability_reviewer_scope,
        suitability_reviewed_at=sample.suitability_reviewed_at,
        suitability_draft_revision=sample.suitability_draft_revision,
        review_disposition=sample.review_disposition,
        review_disposition_reviewer_scope=sample.review_disposition_reviewer_scope,
        review_disposition_recorded_at=sample.review_disposition_recorded_at,
        review_disposition_draft_revision=sample.review_disposition_draft_revision,
    )


def _dataset_response(draft: WorkbenchDatasetDraft) -> WorkbenchDatasetResponse:
    preflight = evaluate_workbench_preflight(draft)
    return WorkbenchDatasetResponse(
        dataset_id=draft.dataset_id,
        revision=draft.revision,
        state=draft.state,
        samples=[_sample_response(sample) for sample in draft.samples],
        preflight=WorkbenchPreflightResponse(
            pilot_ready=preflight.pilot_ready,
            pilot_blockers=[
                WorkbenchBlockerResponse(
                    code=blocker.code, missing_count=blocker.missing_count
                )
                for blocker in preflight.pilot_blockers
            ],
            ready_to_freeze=preflight.ready_to_freeze,
            calibration_count=preflight.calibration_count,
            acceptance_count=preflight.acceptance_count,
            unlabeled_count=preflight.unlabeled_count,
            unreviewed_count=preflight.unreviewed_count,
            unauthorized_count=preflight.unauthorized_count,
            duplicate_source_count=preflight.duplicate_source_count,
            suitability_pending_count=preflight.suitability_pending_count,
            excluded_count=preflight.excluded_count,
            blockers=[
                WorkbenchBlockerResponse(
                    code=blocker.code, missing_count=blocker.missing_count
                )
                for blocker in preflight.blockers
            ],
        ),
    )


def _require_consent(confirmed: str | None, version: str | None) -> None:
    if confirmed != "true":
        raise DomainRuleError("foreground_benchmark_consent_required")
    if version != BENCHMARK_CONSENT_VERSION:
        raise DomainRuleError("foreground_benchmark_consent_version_invalid")


async def _read_limited(request: Request) -> bytes:
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_SOURCE_BYTES:
            raise DomainRuleError("foreground_benchmark_image_limits_exceeded")
        chunks.append(chunk)
    return b"".join(chunks)


@router.post(
    "/datasets",
    response_model=WorkbenchDatasetResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_dataset(
    customer_id: CustomerId,
    store: ForegroundBenchmarkWorkbenchStoreDependency,
) -> WorkbenchDatasetResponse | JSONResponse:
    try:
        return _dataset_response(_store_or_404(store).create_dataset(customer_id))
    except DomainRuleError as error:
        return _error(error.code, _status_for(error.code))


@router.get(
    "/datasets/{dataset_id}/samples/{sample_id}/prompt-suggestion",
    response_model=WorkbenchPromptSuggestionResponse,
)
def prompt_suggestion(
    dataset_id: UUID,
    sample_id: str,
    customer_id: CustomerId,
    store: ForegroundBenchmarkWorkbenchStoreDependency,
) -> WorkbenchPromptSuggestionResponse | JSONResponse:
    try:
        private_store = _store_or_404(store)
        draft = private_store.load_dataset(customer_id, dataset_id)
        sample = next(
            (item for item in draft.samples if item.sample_id == sample_id), None
        )
        if sample is None:
            raise DomainRuleError("foreground_benchmark_workbench_not_found")
        if "flat_lay" not in sample.scene_tags:
            raise DomainRuleError(
                "foreground_benchmark_prompt_suggestion_scene_unsupported"
            )
        if sample.category != "earring":
            raise DomainRuleError(
                "foreground_benchmark_prompt_suggestion_category_unsupported"
            )
        suggestion = suggest_flat_lay_prompt(
            private_store.source_content(customer_id, dataset_id, sample)
        )
        if suggestion.hints.box is None:
            raise DomainRuleError("foreground_benchmark_prompt_suggestion_unavailable")
        return WorkbenchPromptSuggestionResponse(
            source_sha256=sample.source_sha256,
            box=suggestion.hints.box,
            positive_points=list(suggestion.hints.positive_points),
            negative_points=list(suggestion.hints.negative_points),
            method="flat-lay-color-components-v1",
            component_count=suggestion.component_count,
            review_required=True,
        )
    except DomainRuleError as error:
        return _error(error.code, _status_for(error.code))


@router.post(
    "/datasets/{dataset_id}/samples/{sample_id}/proposal",
    response_model=WorkbenchDatasetResponse,
)
def create_mask_proposal(
    dataset_id: UUID,
    sample_id: str,
    customer_id: CustomerId,
    store: ForegroundBenchmarkWorkbenchStoreDependency,
    extractor: ProductForegroundExtractorDependency,
    expected_revision: int = Query(ge=0),
    payload: WorkbenchProposalRequest | None = None,
) -> WorkbenchDatasetResponse | JSONResponse:
    if extractor is None:
        return _error(
            "foreground_benchmark_extractor_unavailable", status.HTTP_409_CONFLICT
        )
    try:
        draft = _store_or_404(store).propose_mask(
            customer_id,
            dataset_id,
            sample_id,
            expected_revision=expected_revision,
            extractor=extractor,
            prompt_hints=(
                ForegroundPromptHints(
                    box=payload.box,
                    positive_points=tuple(payload.positive_points),
                    negative_points=tuple(payload.negative_points),
                )
                if payload is not None
                else None
            ),
        )
        return _dataset_response(draft)
    except DomainRuleError as error:
        return _error(error.code, _status_for(error.code))


@router.post(
    "/datasets/{dataset_id}/samples/{sample_id}/gold-mask",
    response_model=WorkbenchDatasetResponse,
)
async def submit_gold_mask(
    dataset_id: UUID,
    sample_id: str,
    customer_id: CustomerId,
    store: ForegroundBenchmarkWorkbenchStoreDependency,
    expected_revision: Annotated[int, Form(ge=0)],
    gold_mask: Annotated[UploadFile, File()],
    component_names: Annotated[list[str] | None, Form()] = None,
    component_masks: Annotated[list[UploadFile] | None, File()] = None,
) -> WorkbenchDatasetResponse | JSONResponse:
    try:
        component_names = component_names or []
        component_masks = component_masks or []
        if len(component_names) != len(component_masks):
            raise DomainRuleError("foreground_benchmark_component_required")
        gold_content = await gold_mask.read(MAX_SOURCE_BYTES + 1)
        components = tuple(
            (name, await_file)
            for name, await_file in zip(
                component_names,
                [await item.read(MAX_SOURCE_BYTES + 1) for item in component_masks],
                strict=True,
            )
        )
        if len(gold_content) > MAX_SOURCE_BYTES or any(
            len(content) > MAX_SOURCE_BYTES for _, content in components
        ):
            raise DomainRuleError("foreground_benchmark_image_limits_exceeded")
        draft = _store_or_404(store).put_gold_revision(
            customer_id,
            dataset_id,
            sample_id,
            expected_revision=expected_revision,
            gold_mask=gold_content,
            component_masks=components,
        )
        return _dataset_response(draft)
    except DomainRuleError as error:
        return _error(error.code, _status_for(error.code))


@router.post(
    "/datasets/{dataset_id}/samples/{sample_id}/review",
    response_model=WorkbenchDatasetResponse,
)
def confirm_human_review(
    dataset_id: UUID,
    sample_id: str,
    payload: WorkbenchHumanReviewRequest,
    customer_id: CustomerId,
    store: ForegroundBenchmarkWorkbenchStoreDependency,
) -> WorkbenchDatasetResponse | JSONResponse:
    try:
        draft = _store_or_404(store).confirm_human_review(
            customer_id,
            dataset_id,
            sample_id,
            expected_revision=payload.expected_revision,
            confirmed=payload.confirmed,
            source_sha256=payload.source_sha256,
            gold_mask_sha256=payload.gold_mask_sha256,
            component_mask_sha256s=tuple(payload.component_mask_sha256s.items()),
            consent_text_version=payload.consent_text_version,
        )
        return _dataset_response(draft)
    except DomainRuleError as error:
        return _error(error.code, _status_for(error.code))


@router.get("/datasets/{dataset_id}", response_model=WorkbenchDatasetResponse)
def get_dataset(
    dataset_id: UUID,
    customer_id: CustomerId,
    store: ForegroundBenchmarkWorkbenchStoreDependency,
) -> WorkbenchDatasetResponse | JSONResponse:
    try:
        return _dataset_response(
            _store_or_404(store).load_dataset(customer_id, dataset_id)
        )
    except DomainRuleError as error:
        return _error(error.code, _status_for(error.code))


@router.get(
    "/datasets/{dataset_id}/review-audit",
    response_model=WorkbenchReviewAuditResponse,
)
def get_review_audit(
    dataset_id: UUID,
    customer_id: CustomerId,
    store: ForegroundBenchmarkWorkbenchStoreDependency,
) -> WorkbenchReviewAuditResponse | JSONResponse:
    """Return machine disagreement evidence without approving any sample."""
    try:
        private_store = _store_or_404(store)
        draft = private_store.load_dataset(customer_id, dataset_id)
        items: list[WorkbenchProposalDisagreementResponse] = []
        for sample in draft.samples:
            if sample.coarse_mask_sha256 is None or sample.proposed_mask_sha256 is None:
                continue
            metrics = private_store.proposal_disagreement(
                customer_id,
                dataset_id,
                sample.sample_id,
                loaded_draft=draft,
            )
            items.append(
                WorkbenchProposalDisagreementResponse(
                    sample_id=sample.sample_id,
                    intersection_over_union=metrics.intersection_over_union,
                    disagreement_ratio=metrics.disagreement_ratio,
                    coarse_only_ratio=metrics.coarse_only_ratio,
                    refined_only_ratio=metrics.refined_only_ratio,
                    coarse_polarity=metrics.coarse_polarity,
                    raw_coarse_coverage_ratio=metrics.raw_coarse_coverage_ratio,
                    raw_coarse_border_contact_ratio=(
                        metrics.raw_coarse_border_contact_ratio
                    ),
                )
            )
        return WorkbenchReviewAuditResponse(
            dataset_id=dataset_id,
            revision=draft.revision,
            items=items,
        )
    except DomainRuleError as error:
        return _error(error.code, _status_for(error.code))


@router.post(
    "/datasets/{dataset_id}/samples",
    response_model=WorkbenchDatasetResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_sample(
    dataset_id: UUID,
    request: Request,
    customer_id: CustomerId,
    store: ForegroundBenchmarkWorkbenchStoreDependency,
    split: Literal["calibration", "acceptance"] = Query(),
    expected_revision: int = Query(ge=0),
    consent_confirmed: str | None = Header(default=None, alias="X-Consent-Confirmed"),
    consent_version: str | None = Header(default=None, alias="X-Consent-Version"),
) -> WorkbenchDatasetResponse | JSONResponse:
    try:
        _require_consent(consent_confirmed, consent_version)
        content = await _read_limited(request)
        draft = _store_or_404(store).add_source_sample(
            customer_id,
            dataset_id,
            content,
            split=split,
            expected_revision=expected_revision,
            consent_text_version=BENCHMARK_CONSENT_VERSION,
        )
        return _dataset_response(draft)
    except DomainRuleError as error:
        return _error(error.code, _status_for(error.code))


@router.patch(
    "/datasets/{dataset_id}/samples/{sample_id}",
    response_model=WorkbenchDatasetResponse,
)
def label_sample(
    dataset_id: UUID,
    sample_id: str,
    payload: WorkbenchLabelRequest,
    customer_id: CustomerId,
    store: ForegroundBenchmarkWorkbenchStoreDependency,
) -> WorkbenchDatasetResponse | JSONResponse:
    try:
        draft = _store_or_404(store).label_sample(
            customer_id,
            dataset_id,
            sample_id,
            expected_revision=payload.expected_revision,
            category=payload.category,
            difficulty=payload.difficulty,
            scene_tags=tuple(payload.scene_tags),
            material_tags=tuple(payload.material_tags),
            critical_component_names=tuple(payload.critical_component_names),
        )
        return _dataset_response(draft)
    except DomainRuleError as error:
        return _error(error.code, _status_for(error.code))


@router.patch(
    "/datasets/{dataset_id}/samples/{sample_id}/component-annotation",
    response_model=WorkbenchDatasetResponse,
)
def reopen_component_annotation(
    dataset_id: UUID,
    sample_id: str,
    payload: WorkbenchComponentAnnotationReopenRequest,
    customer_id: CustomerId,
    store: ForegroundBenchmarkWorkbenchStoreDependency,
) -> WorkbenchDatasetResponse | JSONResponse:
    """Reopen one signed sample; the old human signature is never retained."""
    try:
        draft = _store_or_404(store).reopen_component_annotation(
            customer_id,
            dataset_id,
            sample_id,
            expected_revision=payload.expected_revision,
            critical_component_names=tuple(payload.critical_component_names),
            confirmed=payload.confirmed,
        )
        return _dataset_response(draft)
    except DomainRuleError as error:
        return _error(error.code, _status_for(error.code))


@router.patch(
    "/datasets/{dataset_id}/samples/{sample_id}/suitability",
    response_model=WorkbenchDatasetResponse,
)
def set_sample_suitability(
    dataset_id: UUID,
    sample_id: str,
    payload: WorkbenchSuitabilityRequest,
    customer_id: CustomerId,
    store: ForegroundBenchmarkWorkbenchStoreDependency,
) -> WorkbenchDatasetResponse | JSONResponse:
    try:
        draft = _store_or_404(store).set_sample_suitability(
            customer_id,
            dataset_id,
            sample_id,
            expected_revision=payload.expected_revision,
            decision=payload.decision,
            reason=payload.reason,
            reviewer_scope="local-workbench-human",
            confirmed=payload.confirmed,
        )
        return _dataset_response(draft)
    except DomainRuleError as error:
        return _error(error.code, _status_for(error.code))


@router.patch(
    "/datasets/{dataset_id}/samples/{sample_id}/review-disposition",
    response_model=WorkbenchDatasetResponse,
)
def set_sample_review_disposition(
    dataset_id: UUID,
    sample_id: str,
    payload: WorkbenchReviewDispositionRequest,
    customer_id: CustomerId,
    store: ForegroundBenchmarkWorkbenchStoreDependency,
) -> WorkbenchDatasetResponse | JSONResponse:
    try:
        draft = _store_or_404(store).set_sample_review_disposition(
            customer_id,
            dataset_id,
            sample_id,
            expected_revision=payload.expected_revision,
            disposition=payload.disposition,
            reviewer_scope="local-workbench-human",
            confirmed=payload.confirmed,
        )
        return _dataset_response(draft)
    except DomainRuleError as error:
        return _error(error.code, _status_for(error.code))


@router.delete(
    "/datasets/{dataset_id}/samples/{sample_id}",
    response_model=WorkbenchDeleteResponse,
)
def delete_sample(
    dataset_id: UUID,
    sample_id: str,
    customer_id: CustomerId,
    store: ForegroundBenchmarkWorkbenchStoreDependency,
    expected_revision: int = Query(ge=0),
) -> WorkbenchDeleteResponse | JSONResponse:
    try:
        draft = _store_or_404(store).delete_unfrozen_sample(
            customer_id,
            dataset_id,
            sample_id,
            expected_revision=expected_revision,
        )
        return WorkbenchDeleteResponse(deleted=True, revision=draft.revision)
    except DomainRuleError as error:
        return _error(error.code, _status_for(error.code))


@router.get("/datasets/{dataset_id}/samples/{sample_id}/thumbnail")
def thumbnail(
    dataset_id: UUID,
    sample_id: str,
    customer_id: CustomerId,
    store: ForegroundBenchmarkWorkbenchStoreDependency,
) -> Response:
    try:
        content = _store_or_404(store).thumbnail_jpeg(
            customer_id, dataset_id, sample_id
        )
        return Response(
            content=content,
            media_type="image/jpeg",
            headers={"Cache-Control": "private, no-store"},
        )
    except DomainRuleError as error:
        return _error(error.code, _status_for(error.code))


@router.get("/datasets/{dataset_id}/samples/{sample_id}/mask")
def mask_content(
    dataset_id: UUID,
    sample_id: str,
    customer_id: CustomerId,
    store: ForegroundBenchmarkWorkbenchStoreDependency,
    kind: Literal["proposal", "coarse", "gold", "component"] = Query(),
    component_name: str | None = Query(default=None, min_length=1, max_length=64),
) -> Response:
    try:
        content = _store_or_404(store).mask_content(
            customer_id,
            dataset_id,
            sample_id,
            kind=kind,
            component_name=component_name,
        )
        return Response(
            content=content,
            media_type="image/png",
            headers={"Cache-Control": "private, no-store"},
        )
    except DomainRuleError as error:
        return _error(error.code, _status_for(error.code))


@router.post(
    "/datasets/{dataset_id}/style-samples/import",
    response_model=WorkbenchDatasetResponse,
    status_code=status.HTTP_201_CREATED,
)
def import_style_sample(
    dataset_id: UUID,
    payload: WorkbenchStyleImportRequest,
    customer_id: CustomerId,
    store: ForegroundBenchmarkWorkbenchStoreDependency,
    session: SessionDependency,
    content_store: ContentStoreDependency,
    consent_confirmed: str | None = Header(default=None, alias="X-Consent-Confirmed"),
    consent_version: str | None = Header(default=None, alias="X-Consent-Version"),
) -> WorkbenchDatasetResponse | JSONResponse:
    try:
        _require_consent(consent_confirmed, consent_version)
        sample = session.scalar(
            select(StyleSample).where(
                StyleSample.id == payload.style_sample_id,
                StyleSample.customer_id == customer_id,
                StyleSample.withdrawn_at.is_(None),
            )
        )
        if sample is None or sample.content_sha256 is None:
            raise DomainRuleError("foreground_benchmark_workbench_not_found")
        content = content_store.read_style_sample_content(
            customer_id, sample.id, sample.content_sha256
        )
        draft = _store_or_404(store).add_source_sample(
            customer_id,
            dataset_id,
            content,
            split=payload.split,
            expected_revision=payload.expected_revision,
            consent_text_version=BENCHMARK_CONSENT_VERSION,
        )
        return _dataset_response(draft)
    except AssetContentStoreError:
        return _error(
            "foreground_benchmark_workbench_not_found", status.HTTP_404_NOT_FOUND
        )
    except DomainRuleError as error:
        return _error(error.code, _status_for(error.code))


@router.get(
    "/datasets/{dataset_id}/preflight", response_model=WorkbenchPreflightResponse
)
def dataset_preflight(
    dataset_id: UUID,
    customer_id: CustomerId,
    store: ForegroundBenchmarkWorkbenchStoreDependency,
) -> WorkbenchPreflightResponse | JSONResponse:
    try:
        return _dataset_response(
            _store_or_404(store).load_dataset(customer_id, dataset_id)
        ).preflight
    except DomainRuleError as error:
        return _error(error.code, _status_for(error.code))


@router.post("/datasets/{dataset_id}/freeze", response_model=WorkbenchFreezeResponse)
def freeze_workbench_dataset(
    dataset_id: UUID,
    payload: WorkbenchFreezeRequest,
    customer_id: CustomerId,
    store: ForegroundBenchmarkWorkbenchStoreDependency,
) -> WorkbenchFreezeResponse | JSONResponse:
    try:
        frozen = _store_or_404(store).materialize_and_freeze(
            customer_id,
            dataset_id,
            expected_revision=payload.expected_revision,
            idempotency_key=payload.idempotency_key,
        )
        return WorkbenchFreezeResponse(
            dataset_id=frozen.dataset_id,
            version_id=frozen.version_id,
            seal_sha256=frozen.dataset.seal_sha256,
            acceptance_count=frozen.dataset.acceptance_count,
            calibration_count=frozen.dataset.calibration_count,
            activated=True,
            benchmark_started=False,
        )
    except DomainRuleError as error:
        return _error(error.code, _status_for(error.code))


@router.post("/revocations", status_code=status.HTTP_204_NO_CONTENT)
def revoke_workbench_source(
    payload: WorkbenchRevokeRequest,
    customer_id: CustomerId,
    store: ForegroundBenchmarkWorkbenchStoreDependency,
) -> Response:
    try:
        if not payload.confirmed:
            raise DomainRuleError("foreground_benchmark_consent_required")
        _store_or_404(store).revoke_source(customer_id, payload.source_sha256)
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    except DomainRuleError as error:
        return _error(error.code, _status_for(error.code))
