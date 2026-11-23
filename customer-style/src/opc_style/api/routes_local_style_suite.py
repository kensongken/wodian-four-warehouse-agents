"""Local-only browser adapter for the corrected style-suite workflow."""

import json
import subprocess
import warnings
from collections.abc import Callable, Coroutine
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from typing import Annotated, Any, cast
from uuid import UUID

import httpx
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.routing import APIRoute
from PIL import Image, UnidentifiedImageError
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from opc_style.api.dependencies import (
    BackgroundGeneratorDependency,
    CustomerId,
    ProductForegroundExtractorDependency,
    get_session,
)
from opc_style.api.local_run_views import (
    project_local_batch_run,
    project_local_run,
    read_candidate_response,
    read_source_response,
)
from opc_style.api.routes_image_jobs import GeneratorDependency
from opc_style.api.routes_local_demo import bootstrap_demo_sandbox
from opc_style.api.routes_real_photos import get_asset_content_store
from opc_style.domain.analyzer import VisualStyleAnalyzer
from opc_style.domain.authenticity import FidelityBatchAssessor
from opc_style.domain.background_generator import BackgroundGenerationPreview
from opc_style.domain.errors import DomainRuleError
from opc_style.domain.image_generator import DeterministicLocalImageGenerator
from opc_style.domain.product_foreground import (
    ForegroundTransform,
    ProductForegroundExtractor,
)
from opc_style.domain.product_identity import (
    ProductIdentityAnalyzer,
    ProductIdentityDraft,
    ProductIdentityImage,
    identity_card_payload,
)
from opc_style.domain.style_reference_pack import resolve_style_reference_customer
from opc_style.models import (
    AuditEvent,
    Customer,
    ImageOutputJob,
    PixelBackgroundRun,
    ProductAsset,
    ProductAssetLink,
    ProductAssetPermissionEvent,
    ProductIdentityAnalysisRun,
    StyleSuiteRun,
    StyleSuiteRunItem,
)
from opc_style.providers.local_foreground_worker import LocalForegroundWorker
from opc_style.providers.macos_vision_foreground import MacOSVisionForegroundExtractor
from opc_style.providers.openai_fidelity import OpenAIFidelityAssessor
from opc_style.providers.openai_images import (
    REAL_MODEL_GENERATION_MODE,
    OpenAIImageGenerator,
)
from opc_style.providers.openai_product_identity import OpenAIProductIdentityAnalyzer
from opc_style.providers.openai_style_analysis import OpenAIVisualStyleAnalyzer
from opc_style.repositories import ProductImageRepository
from opc_style.schemas import (
    LocalRealFidelityRequest,
    LocalRealStyleAnalysisRequest,
    LocalRealStylePilotRequest,
    LocalStyleSuiteBatchRunResponse,
    LocalStyleSuiteHistoryResponse,
    LocalStyleSuiteRunResponse,
    PixelBackgroundComposeRequest,
    PixelBackgroundDecisionRequest,
    PixelBackgroundExecuteRequest,
    PixelBackgroundPreviewRequest,
    PixelBackgroundPreviewResponse,
    PixelBackgroundRunResponse,
    PixelCompositionRecommendationResponse,
    PixelCompositionResponse,
    ProductAssetResponse,
    ProductForegroundDecisionRequest,
    ProductForegroundEditForm,
    ProductForegroundVersionResponse,
    ProductIdentityAnalysisRequest,
    ProductIdentityAnalysisResponse,
    ProductIdentityCardConfirmationRequest,
    ProductIdentityCardVersionResponse,
    ProductIdentityManualDraftRequest,
    ProductIdentityObservationExecuteRequest,
    ProductIdentityObservationPreviewRequest,
    ProductIdentityObservationPreviewResponse,
    ProductIdentityObservationResponse,
    ProductIdentityObservationReviewRequest,
    ProductIdentityObservationReviewResponse,
    ProductIdentityPreviewResponse,
    StyleSuiteBatchCreateRequest,
    StyleSuiteBatchPreviewRequest,
    StyleSuiteBatchPreviewResponse,
    StyleSuiteCreateRequest,
    StyleSuiteExecutionResponse,
    StyleSuitePilotScope,
)
from opc_style.services.assets import ProductAssetIntake, ProductAssetService
from opc_style.services.foreground_risk_gate import LocalStageDisagreementEvaluator
from opc_style.services.pixel_preserving_style import PixelPreservingStyleService
from opc_style.services.product_foregrounds import (
    MaskEditOperationCounts,
    ProductForegroundOutcome,
    ProductForegroundService,
)
from opc_style.services.product_identity import (
    ProductIdentityCardOutcome,
    ProductIdentityService,
    validate_confirmed_product_identity_card,
)
from opc_style.services.product_identity_observations import (
    ProductIdentityObservationOutcome,
    ProductIdentityObservationService,
)
from opc_style.services.real_fidelity import RealFidelityService
from opc_style.services.real_photos import (
    AssetContentStore,
    AssetContentStoreError,
    LocalSyntheticAssetStore,
)
from opc_style.services.real_pilot_authorization import (
    issue_real_pilot_authorization,
)
from opc_style.services.real_style_analysis import (
    RealStyleAnalysisService,
    latest_successful_real_style_analysis,
)
from opc_style.services.style_analysis import StyleAnalysisService
from opc_style.services.style_reference_packs import StyleReferencePackService
from opc_style.services.style_suite_batch_scope import SignedBatchScope
from opc_style.services.style_suites import StyleSuiteService
from opc_style.settings import (
    LOCAL_PRODUCT_UPLOAD_HARD_MAX_BYTES,
    LOCAL_PRODUCT_UPLOAD_HARD_MAX_DIMENSION,
    LOCAL_PRODUCT_UPLOAD_HARD_MAX_PIXELS,
    LocalBridgeSettings,
    MissingPartCompletionSettings,
    RealBackgroundProviderSettings,
    RealFidelitySettings,
    RealImageProviderSettings,
    RealProductIdentitySettings,
    RealStyleAnalysisSettings,
    StyleSuitePlanningSettings,
)
from opc_style.web.identity_locked_page import IDENTITY_LOCKED_PAGE
from opc_style.web.multi_product_page import MULTI_PRODUCT_PAGE
from opc_style.web.output_center_page import OUTPUT_CENTER_PAGE
from opc_style.web.style_suite_page import STYLE_SUITE_PAGE
from opc_style.web.wear_page import WEAR_PAGE


class _SanitizedProductIdentityRoute(APIRoute):
    """Keep validation details and submitted values inside identity routes."""

    def get_route_handler(
        self,
    ) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original_handler = super().get_route_handler()

        async def sanitized_handler(request: Request) -> Response:
            try:
                return await original_handler(request)
            except RequestValidationError:
                code = (
                    "foreground_edit_invalid"
                    if request.url.path.endswith("/foreground-edits")
                    else "product_identity_request_invalid"
                )
                return JSONResponse(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    content={
                        "error": {
                            "code": code,
                            "retryable": False,
                        }
                    },
                )

        return sanitized_handler


router = APIRouter(tags=["local-style-suite"])
identity_router = APIRouter(route_class=_SanitizedProductIdentityRoute)
SessionDependency = Annotated[Session, Depends(get_session)]
StoreDependency = Annotated[AssetContentStore, Depends(get_asset_content_store)]


def build_real_style_analyzer(
    settings: RealStyleAnalysisSettings,
    *,
    transport: httpx.Client | None = None,
) -> VisualStyleAnalyzer | None:
    """Build the paid analyzer only from explicit startup authority."""
    if not settings.enabled:
        return None
    return OpenAIVisualStyleAnalyzer(settings, transport=transport or httpx.Client())


def get_real_style_analyzer(request: Request) -> VisualStyleAnalyzer | None:
    return getattr(request.app.state, "real_style_visual_analyzer", None)


RealStyleAnalyzerDependency = Annotated[
    VisualStyleAnalyzer | None, Depends(get_real_style_analyzer)
]


def build_real_product_identity_analyzer(
    settings: RealProductIdentitySettings,
    *,
    transport: httpx.Client | None = None,
) -> ProductIdentityAnalyzer | None:
    """Build the paid identity analyzer only from startup-owned authority."""
    if not settings.enabled:
        return None
    return OpenAIProductIdentityAnalyzer(
        settings,
        transport=transport or httpx.Client(),
    )


def get_real_product_identity_analyzer(
    request: Request,
) -> ProductIdentityAnalyzer | None:
    return getattr(request.app.state, "real_product_identity_analyzer", None)


RealProductIdentityAnalyzerDependency = Annotated[
    ProductIdentityAnalyzer | None, Depends(get_real_product_identity_analyzer)
]


def build_real_fidelity_assessor(
    settings: RealFidelitySettings,
    *,
    transport: httpx.Client | None = None,
) -> FidelityBatchAssessor | None:
    """Build the paid fidelity assessor only from explicit startup authority."""
    if not settings.enabled:
        return None
    return OpenAIFidelityAssessor(settings, transport=transport or httpx.Client())


def get_real_fidelity_assessor(request: Request) -> FidelityBatchAssessor | None:
    return getattr(request.app.state, "real_fidelity_assessor", None)


RealFidelityAssessorDependency = Annotated[
    FidelityBatchAssessor | None, Depends(get_real_fidelity_assessor)
]


@router.get("/local/style-suite", response_class=HTMLResponse)
def style_suite_page() -> str:
    settings = RealImageProviderSettings()
    runtime = json.dumps(
        {
            "real_enabled": settings.enabled,
            "model": settings.model if settings.enabled else "local-synthetic",
            "budget_usd": str(settings.max_run_usd if settings.enabled else 0),
        },
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return STYLE_SUITE_PAGE.replace("__IDENTITY_RUNTIME__", runtime)


@router.get("/local/style-suite/output-center", response_class=HTMLResponse)
def output_center_page() -> HTMLResponse:
    """Show all five PRD output entrances without claiming production readiness."""
    return HTMLResponse(OUTPUT_CENTER_PAGE, headers={"Cache-Control": "no-store"})


@router.get("/local/style-suite/multi-product", response_class=HTMLResponse)
def multi_product_page() -> HTMLResponse:
    return HTMLResponse(MULTI_PRODUCT_PAGE, headers={"Cache-Control": "no-store"})


@router.get("/local/style-suite/wear", response_class=HTMLResponse)
def wear_page() -> HTMLResponse:
    return HTMLResponse(WEAR_PAGE, headers={"Cache-Control": "no-store"})


@router.get("/local/style-suite/api/multi-product/options")
def multi_product_options(
    customer_id: CustomerId,
    session: SessionDependency,
) -> list[dict[str, object]]:
    """List one newest generation-ready real source per customer-owned product."""
    service = ProductAssetService(session)
    by_product: dict[str, ProductAsset] = {}
    for asset in ProductImageRepository(session).list_assets(customer_id):
        if service.readiness(customer_id, asset).generation_ready:
            by_product[asset.product_id] = asset
    return [
        {
            "product_id": asset.product_id,
            "asset_id": str(asset.id),
            "content_sha256": asset.content_sha256,
            "content_url": f"/local/style-suite/api/assets/{asset.id}/content",
        }
        for asset in by_product.values()
    ]


@router.get(
    "/local/style-suite/api/reviewable-image-jobs/{job_id}/candidates/{candidate_id}/content"
)
def reviewable_candidate_content(
    job_id: UUID,
    candidate_id: UUID,
    customer_id: CustomerId,
    session: SessionDependency,
    store: StoreDependency,
) -> Response:
    """Stream one local multi-product candidate inside the owning customer scope."""
    repository = ProductImageRepository(session)
    job = repository.get_output_job(customer_id, job_id)
    candidate = repository.get_candidate(customer_id, candidate_id)
    if (
        job is None
        or job.output_type not in {"multi_product", "wear", "scene"}
        or candidate is None
        or candidate.output_job_id != job.id
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    try:
        content = store.read_derivative(
            customer_id, job.id, candidate.sequence_number, candidate.content_sha256
        )
    except AssetContentStoreError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from error
    return Response(
        content,
        media_type=candidate.mime_type,
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Sha256": candidate.content_sha256,
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get(
    "/local/style-suite/api/multi-product/jobs/{job_id}/candidates/{candidate_id}/content"
)
def legacy_multi_product_candidate_content(
    job_id: UUID,
    candidate_id: UUID,
    customer_id: CustomerId,
    session: SessionDependency,
    store: StoreDependency,
) -> Response:
    """Retain the first local multi-product URL while the UI migrates."""
    return reviewable_candidate_content(
        job_id, candidate_id, customer_id, session, store
    )


@router.get("/local/style-suite/api/prd-readiness")
def prd_readiness() -> dict[str, object]:
    """Expose implementation scope separately from external production acceptance."""
    settings = RealImageProviderSettings()
    return {
        "prd_version": "2026-08-30",
        "scope": "local_mvp_implementation",
        "output_types": {
            "real_photo": "implemented_review_gated",
            "wear": "implemented_system_virtual_model_review_gated",
            "scene": "implemented_review_gated",
            "video": "implemented_real_upload_local_edit_review_gated",
            "multi_product": "implemented_2_to_5_review_gated",
        },
        "real_image_provider_enabled": settings.enabled,
        "production_deployed": False,
        "formal_quality_acceptance_passed": False,
        "publishable": False,
        "external_gates": [
            "production_infrastructure",
            "unseen_real_image_blind_test",
            "business_owner_release_signoff",
        ],
    }


@router.get("/local/style-suite/api/acceptance-runtime")
def acceptance_runtime(request: Request) -> dict[str, object]:
    """Expose the local candidate's actual revision without paths or secrets."""
    root = Path(__file__).resolve().parents[3]
    try:
        revision = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True, timeout=2
        ).strip()
        dirty = bool(
            subprocess.check_output(
                ["git", "status", "--porcelain", "--untracked-files=no"],
                cwd=root,
                text=True,
                timeout=2,
            ).strip()
        )
    except (OSError, subprocess.SubprocessError):
        revision, dirty = "unavailable", True
    extractor = getattr(request.app.state, "product_foreground_extractor", None)
    if isinstance(extractor, LocalForegroundWorker):
        engine = "attested-local-worker:" + extractor.startup_attestation_digest()[:12]
    elif isinstance(extractor, MacOSVisionForegroundExtractor):
        engine = extractor.extractor_version
    else:
        engine = "disabled" if extractor is None else "unrecognized"
    paid_provider_states = {
        "image_generation": RealImageProviderSettings().enabled,
        "background_generation": RealBackgroundProviderSettings().enabled,
        "style_analysis": RealStyleAnalysisSettings().enabled,
        "product_identity": RealProductIdentitySettings().enabled,
        "fidelity_assessment": RealFidelitySettings().enabled,
        "missing_part_completion": MissingPartCompletionSettings().enabled,
    }
    return {
        "revision": revision,
        "worktree_clean": not dirty,
        "foreground_engine": engine,
        "generation_backend": {
            "adapter_version": DeterministicLocalImageGenerator.adapter_version,
            "generation_mode": DeterministicLocalImageGenerator.generation_mode,
            "visual_quality_acceptance_allowed": False,
            "workflow_acceptance_allowed": True,
        },
        "paid_generation_enabled": (
            paid_provider_states["image_generation"]
            or paid_provider_states["background_generation"]
        ),
        "paid_provider_states": paid_provider_states,
        "zero_paid_mode": not any(paid_provider_states.values()),
        "review_required": True,
        "publishable": False,
    }


@router.get("/local/style-suite/identity-locked", response_class=HTMLResponse)
def identity_locked_page() -> str:
    settings = RealImageProviderSettings()
    runtime = json.dumps(
        {
            "real_enabled": settings.enabled,
            "model": settings.model if settings.enabled else "local-synthetic",
            "budget_usd": str(settings.max_run_usd if settings.enabled else 0),
        },
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return IDENTITY_LOCKED_PAGE.replace("__IDENTITY_RUNTIME__", runtime)


@router.get("/local/style-suite/api/style-library")
def local_style_library_status(
    customer_id: CustomerId,
    session: SessionDependency,
    store: StoreDependency,
) -> dict[str, object]:
    """Restore truthful saved-library readiness after a browser refresh."""
    return asdict(
        StyleAnalysisService(
            session,
            content_store=store,
            analyzer=None,
        ).library_status(customer_id)
    )


def _product_identity_service(
    request: Request,
    session: Session,
    store: AssetContentStore,
    analyzer: ProductIdentityAnalyzer | None,
) -> ProductIdentityService:
    settings = getattr(
        request.app.state,
        "real_product_identity_settings",
        RealProductIdentitySettings(),
    )
    if analyzer is None or not settings.enabled:
        raise DomainRuleError("product_identity_analysis_provider_disabled")
    return ProductIdentityService(
        session,
        content_store=store,
        analyzer=analyzer,
        model=settings.model,
        maximum_usd=settings.max_run_usd,
    )


class _PersistenceOnlyProductIdentityAnalyzer:
    """Fail closed if a durable-only service is ever used to dispatch."""

    analysis_mode = "real_product_identity_proposal"
    analyzer_version = "persistence-only-no-provider-v1"

    def analyze(self, image: ProductIdentityImage) -> ProductIdentityDraft:
        raise DomainRuleError("product_identity_analysis_provider_disabled")


_PERSISTENCE_ONLY_ANALYZER = _PersistenceOnlyProductIdentityAnalyzer()


def _durable_product_identity_service(
    session: Session,
    store: AssetContentStore,
) -> ProductIdentityService:
    """Build access to persisted runs/cards without paid-provider authority."""
    return ProductIdentityService(
        session,
        content_store=store,
        analyzer=_PERSISTENCE_ONLY_ANALYZER,
        model="persistence-only-no-provider-v1",
        # The service constructor validates paid execution dependencies. This
        # positive sentinel is never an authorization and is unreachable from
        # preview/execute routes; the analyzer above also fails closed.
        maximum_usd=Decimal("0.000001"),
    )


def _product_identity_error(error: DomainRuleError) -> JSONResponse:
    code = error.code
    if code == "product_identity_card_scope_mismatch":
        code = "product_identity_asset_unavailable"
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={"error": {"code": code, "retryable": False}},
    )


def _product_identity_observation_service(
    request: Request,
    session: Session,
    store: AssetContentStore,
    analyzer: ProductIdentityAnalyzer | None,
) -> ProductIdentityObservationService:
    settings = getattr(
        request.app.state,
        "real_product_identity_settings",
        RealProductIdentitySettings(),
    )
    provider_enabled = analyzer is not None and settings.enabled
    return ProductIdentityObservationService(
        session,
        content_store=store,
        analyzer=analyzer or _PERSISTENCE_ONLY_ANALYZER,
        model=(settings.model if provider_enabled else "local-exact-source-match"),
        maximum_usd=(settings.max_run_usd if provider_enabled else Decimal(0)),
        provider_enabled=provider_enabled,
    )


def _product_identity_observation_response(
    outcome: ProductIdentityObservationOutcome,
) -> ProductIdentityObservationResponse:
    return ProductIdentityObservationResponse.model_validate(
        {
            "run_id": outcome.run_id,
            "observation": identity_card_payload(outcome.observation),
            "decision": outcome.decision,
            "reason_codes": list(outcome.reason_codes),
            "result_sha256": outcome.result_sha256,
            "created": outcome.created,
        }
    )


@identity_router.get("/local/style-suite/api/assets/{asset_id}/content")
def read_product_asset_content(
    asset_id: UUID,
    customer_id: CustomerId,
    session: SessionDependency,
    store: StoreDependency,
) -> Response:
    """Return authorized original bytes without revealing their storage path."""
    asset = session.scalar(
        select(ProductAsset).where(
            ProductAsset.id == asset_id,
            ProductAsset.customer_id == customer_id,
        )
    )
    if asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    try:
        content = store.read_original(customer_id, asset.id, asset.content_sha256)
    except AssetContentStoreError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from error
    return Response(
        content=content,
        media_type=asset.mime_type,
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@identity_router.get(
    "/local/style-suite/api/assets/{asset_id}",
    response_model=ProductAssetResponse,
)
def read_local_product_asset(
    asset_id: UUID,
    customer_id: CustomerId,
    session: SessionDependency,
) -> ProductAssetResponse:
    """Restore one authorized local product into a browser review session."""
    asset = session.scalar(
        select(ProductAsset).where(
            ProductAsset.id == asset_id,
            ProductAsset.customer_id == customer_id,
        )
    )
    if asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    intake = ProductAssetIntake(
        asset=asset,
        readiness=ProductAssetService(session).readiness(customer_id, asset),
        created=False,
    )
    from opc_style.api.routes_assets import _asset_response

    return _asset_response(intake)


@identity_router.get(
    "/local/style-suite/api/assets/{asset_id}/identity-analysis-preview",
    response_model=ProductIdentityPreviewResponse,
)
def preview_product_identity_analysis(
    asset_id: UUID,
    request: Request,
    customer_id: CustomerId,
    session: SessionDependency,
    store: StoreDependency,
    analyzer: RealProductIdentityAnalyzerDependency,
) -> ProductIdentityPreviewResponse | JSONResponse:
    """Quote one verified product image without calling the provider."""
    try:
        preview = _product_identity_service(request, session, store, analyzer).preview(
            customer_id, asset_id
        )
    except DomainRuleError as error:
        return _product_identity_error(error)
    response = asdict(preview)
    response["reconciliation_required"] = (
        session.scalar(
            select(ProductIdentityAnalysisRun.id).where(
                ProductIdentityAnalysisRun.customer_id == customer_id,
                ProductIdentityAnalysisRun.product_asset_id == asset_id,
                ProductIdentityAnalysisRun.product_id == preview.product_id,
                ProductIdentityAnalysisRun.source_content_sha256
                == preview.source_content_sha256,
                ProductIdentityAnalysisRun.dispatch_state == "dispatch_uncertain",
                ProductIdentityAnalysisRun.outbound_attempts == 1,
            )
        )
        is not None
    )
    return ProductIdentityPreviewResponse.model_validate(response)


@identity_router.post(
    "/local/style-suite/api/assets/{asset_id}/identity-analysis-runs",
    response_model=ProductIdentityAnalysisResponse,
    status_code=status.HTTP_201_CREATED,
)
def execute_product_identity_analysis(
    asset_id: UUID,
    payload: ProductIdentityAnalysisRequest,
    request: Request,
    customer_id: CustomerId,
    session: SessionDependency,
    store: StoreDependency,
    analyzer: RealProductIdentityAnalyzerDependency,
    response: Response,
) -> ProductIdentityAnalysisResponse | JSONResponse:
    """Dispatch at most once after exact quote confirmation."""
    try:
        outcome = _product_identity_service(request, session, store, analyzer).execute(
            customer_id,
            asset_id,
            idempotency_key=payload.idempotency_key,
            confirmed_maximum_usd=payload.confirmed_maximum_usd,
            confirmed_manifest_sha256=payload.confirmed_manifest_sha256,
        )
    except DomainRuleError as error:
        return _product_identity_error(error)
    if not outcome.created:
        response.status_code = status.HTTP_200_OK
    return ProductIdentityAnalysisResponse(
        run_id=outcome.run_id,
        proposal=identity_card_payload(outcome.proposal),
        created=outcome.created,
    )


@identity_router.post(
    "/local/style-suite/api/assets/{asset_id}/identity-manual-drafts",
    response_model=ProductIdentityAnalysisResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_manual_product_identity_draft(
    asset_id: UUID,
    payload: ProductIdentityManualDraftRequest,
    customer_id: CustomerId,
    session: SessionDependency,
    store: StoreDependency,
    response: Response,
) -> ProductIdentityAnalysisResponse | JSONResponse:
    """Create one local blank card without provider access or cost."""
    try:
        outcome = _durable_product_identity_service(session, store).create_manual_draft(
            customer_id,
            asset_id,
            idempotency_key=payload.idempotency_key,
        )
    except DomainRuleError as error:
        return _product_identity_error(error)
    if not outcome.created:
        response.status_code = status.HTTP_200_OK
    return ProductIdentityAnalysisResponse(
        run_id=outcome.run_id,
        proposal=identity_card_payload(outcome.proposal),
        created=outcome.created,
    )


def _product_identity_card_response(
    outcome: ProductIdentityCardOutcome,
) -> ProductIdentityCardVersionResponse:
    return ProductIdentityCardVersionResponse(
        id=outcome.card_version_id,
        version=outcome.version,
        card_sha256=outcome.card_sha256,
        card=identity_card_payload(outcome.card),
        created=outcome.created,
    )


@identity_router.post(
    "/local/style-suite/api/assets/{asset_id}/identity-card-versions",
    response_model=ProductIdentityCardVersionResponse,
    status_code=status.HTTP_201_CREATED,
)
def confirm_product_identity_card(
    asset_id: UUID,
    payload: ProductIdentityCardConfirmationRequest,
    customer_id: CustomerId,
    session: SessionDependency,
    store: StoreDependency,
    response: Response,
) -> ProductIdentityCardVersionResponse | JSONResponse:
    """Append one immutable customer-confirmed card version."""
    try:
        outcome = _durable_product_identity_service(session, store).confirm(
            customer_id,
            asset_id,
            analysis_run_id=payload.analysis_run_id,
            card=payload.card,
            confirmed=payload.confirmed,
        )
    except DomainRuleError as error:
        return _product_identity_error(error)
    if not outcome.created:
        response.status_code = status.HTTP_200_OK
    return _product_identity_card_response(outcome)


@identity_router.get(
    "/local/style-suite/api/assets/{asset_id}/identity-card-versions",
    response_model=list[ProductIdentityCardVersionResponse],
)
def list_product_identity_cards(
    asset_id: UUID,
    customer_id: CustomerId,
    session: SessionDependency,
    store: StoreDependency,
) -> list[ProductIdentityCardVersionResponse] | JSONResponse:
    """List only current-source cards in descending version order."""
    try:
        outcomes = _durable_product_identity_service(session, store).versions(
            customer_id, asset_id
        )
    except DomainRuleError as error:
        return _product_identity_error(error)
    return [_product_identity_card_response(outcome) for outcome in outcomes]


@identity_router.post(
    "/local/style-suite/api/assets/{asset_id}/identity-observations/preview",
    response_model=ProductIdentityObservationPreviewResponse,
)
def preview_product_identity_observation(
    asset_id: UUID,
    payload: ProductIdentityObservationPreviewRequest,
    request: Request,
    customer_id: CustomerId,
    session: SessionDependency,
    store: StoreDependency,
    analyzer: RealProductIdentityAnalyzerDependency,
) -> ProductIdentityObservationPreviewResponse | JSONResponse:
    """Quote a sealed same-product comparison without calling the provider."""
    try:
        preview = _product_identity_observation_service(
            request, session, store, analyzer
        ).preview(customer_id, asset_id, payload.card_version_id)
    except DomainRuleError as error:
        return _product_identity_error(error)
    return ProductIdentityObservationPreviewResponse.model_validate(asdict(preview))


@identity_router.post(
    "/local/style-suite/api/assets/{asset_id}/identity-observation-runs",
    response_model=ProductIdentityObservationResponse,
    status_code=status.HTTP_201_CREATED,
)
def execute_product_identity_observation(
    asset_id: UUID,
    payload: ProductIdentityObservationExecuteRequest,
    request: Request,
    customer_id: CustomerId,
    session: SessionDependency,
    store: StoreDependency,
    analyzer: RealProductIdentityAnalyzerDependency,
    response: Response,
) -> ProductIdentityObservationResponse | JSONResponse:
    """Dispatch one paid observation only after exact quote confirmation."""
    try:
        outcome = _product_identity_observation_service(
            request, session, store, analyzer
        ).execute(
            customer_id,
            asset_id,
            payload.card_version_id,
            idempotency_key=payload.idempotency_key,
            confirmed_maximum_usd=payload.confirmed_maximum_usd,
            confirmed_manifest_sha256=payload.confirmed_manifest_sha256,
        )
    except DomainRuleError as error:
        return _product_identity_error(error)
    if not outcome.created:
        response.status_code = status.HTTP_200_OK
    return _product_identity_observation_response(outcome)


@identity_router.get(
    "/local/style-suite/api/assets/{asset_id}/identity-observations/current",
    response_model=ProductIdentityObservationResponse,
)
def current_product_identity_observation(
    asset_id: UUID,
    card_version_id: UUID,
    request: Request,
    customer_id: CustomerId,
    session: SessionDependency,
    store: StoreDependency,
    analyzer: RealProductIdentityAnalyzerDependency,
) -> ProductIdentityObservationResponse | JSONResponse:
    """Restore only still-current observation evidence after refresh."""
    try:
        outcome = _product_identity_observation_service(
            request, session, store, analyzer
        ).current(customer_id, asset_id, card_version_id)
    except DomainRuleError as error:
        return _product_identity_error(error)
    if outcome is None:
        return _product_identity_error(
            DomainRuleError("product_identity_observation_unavailable")
        )
    return _product_identity_observation_response(outcome)


@identity_router.post(
    "/local/style-suite/api/identity-observation-runs/{run_id}/reviews",
    response_model=ProductIdentityObservationReviewResponse,
    status_code=status.HTTP_201_CREATED,
)
def review_product_identity_observation(
    run_id: UUID,
    payload: ProductIdentityObservationReviewRequest,
    request: Request,
    customer_id: CustomerId,
    session: SessionDependency,
    store: StoreDependency,
    analyzer: RealProductIdentityAnalyzerDependency,
    response: Response,
) -> ProductIdentityObservationReviewResponse | JSONResponse:
    """Append one human ruling without changing the observation or card."""
    try:
        outcome = _product_identity_observation_service(
            request, session, store, analyzer
        ).review(
            customer_id,
            run_id,
            decision=payload.decision,
            reason=payload.reason,
            confirmed=payload.confirmed,
        )
    except DomainRuleError as error:
        return _product_identity_error(error)
    if not outcome.created:
        response.status_code = status.HTTP_200_OK
    return ProductIdentityObservationReviewResponse.model_validate(
        {
            "review_id": outcome.review_id,
            "observation_run_id": outcome.observation_run_id,
            "decision": outcome.decision,
            "reason": outcome.reason,
            "created": outcome.created,
        }
    )


@identity_router.get(
    "/local/style-suite/api/identity-observation-runs/{run_id}/review",
    response_model=ProductIdentityObservationReviewResponse,
)
def current_product_identity_observation_review(
    run_id: UUID,
    request: Request,
    customer_id: CustomerId,
    session: SessionDependency,
    store: StoreDependency,
    analyzer: RealProductIdentityAnalyzerDependency,
) -> ProductIdentityObservationReviewResponse:
    """Restore an existing append-only human ruling without creating one."""
    outcome = _product_identity_observation_service(
        request, session, store, analyzer
    ).current_review(customer_id, run_id)
    if outcome is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return ProductIdentityObservationReviewResponse.model_validate(
        {
            "review_id": outcome.review_id,
            "observation_run_id": outcome.observation_run_id,
            "decision": outcome.decision,
            "reason": outcome.reason,
            "created": outcome.created,
        }
    )


def _foreground_service(
    session: Session,
    store: AssetContentStore,
    extractor: ProductForegroundExtractor | None = None,
) -> ProductForegroundService:
    return ProductForegroundService(
        session,
        content_store=store,
        extractor=extractor,
        shadow_evaluator=(
            LocalStageDisagreementEvaluator()
            if isinstance(extractor, LocalForegroundWorker)
            else None
        ),
    )


def _foreground_response(
    outcome: ProductForegroundOutcome,
) -> ProductForegroundVersionResponse:
    base = f"/local/style-suite/api/foreground-versions/{outcome.id}"
    return ProductForegroundVersionResponse(
        id=outcome.id,
        asset_id=outcome.asset_id,
        product_id=outcome.product_id,
        version=outcome.version,
        source_content_sha256=outcome.source_content_sha256,
        foreground_sha256=outcome.foreground_sha256,
        mask_sha256=outcome.mask_sha256,
        width=outcome.width,
        height=outcome.height,
        extractor_version=outcome.extractor_version,
        manifest_sha256=outcome.manifest_sha256,
        state=outcome.state,
        quality_flags=list(outcome.quality_flags),
        shadow_verdict=outcome.shadow_verdict,
        shadow_reason_codes=list(outcome.shadow_reason_codes),
        shadow_model_manifest_sha256=outcome.shadow_model_manifest_sha256,
        pipeline_manifest_sha256=outcome.pipeline_manifest_sha256,
        stage_receipts_sha256=outcome.stage_receipts_sha256,
        foreground_content_url=f"{base}/foreground/content",
        mask_content_url=f"{base}/mask/content",
        disagreement_content_url=(
            f"{base}/disagreement/content" if outcome.disagreement_available else None
        ),
        disagreement_classification=outcome.disagreement_classification,
        disagreement_ratio=outcome.disagreement_ratio,
        coarse_only_ratio=outcome.coarse_only_ratio,
        refined_only_ratio=outcome.refined_only_ratio,
        coarse_only_detached_ratio=outcome.coarse_only_detached_ratio,
        publishable=False,
        created=outcome.created,
    )


def _foreground_error(error: DomainRuleError) -> JSONResponse:
    retryable = error.code == "foreground_candidate_busy"
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={"error": {"code": error.code, "retryable": retryable}},
    )


def _foreground_edit_error(error: DomainRuleError) -> JSONResponse:
    status_code = (
        status.HTTP_422_UNPROCESSABLE_CONTENT
        if error.code in {"foreground_edit_invalid", "foreground_edit_touches_edge"}
        else status.HTTP_409_CONFLICT
    )
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": error.code, "retryable": False}},
    )


@identity_router.post(
    "/local/style-suite/api/products/{asset_id}/foreground-preview",
    response_model=ProductForegroundVersionResponse,
    status_code=status.HTTP_201_CREATED,
)
def preview_product_foreground(
    asset_id: UUID,
    customer_id: CustomerId,
    session: SessionDependency,
    store: StoreDependency,
    extractor: ProductForegroundExtractorDependency,
) -> ProductForegroundVersionResponse | JSONResponse:
    """Create a free local foreground proposal without any paid model call."""
    try:
        outcome = _foreground_service(session, store, extractor).preview(
            customer_id, asset_id
        )
    except DomainRuleError as error:
        return _foreground_error(error)
    return _foreground_response(outcome)


@identity_router.post(
    "/local/style-suite/api/products/{asset_id}/foreground-edits",
    response_model=ProductForegroundVersionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def edit_product_foreground(
    asset_id: UUID,
    request: Request,
    customer_id: CustomerId,
    session: SessionDependency,
    store: StoreDependency,
    response: Response,
    mask: Annotated[UploadFile, File()],
    base_version_id: Annotated[str, Form()],
    base_manifest_sha256: Annotated[str, Form()],
    edit_manifest_sha256: Annotated[str, Form()],
    editor_version: Annotated[str, Form()],
    idempotency_key: Annotated[str, Form()],
    magic_wand_count: Annotated[str, Form()],
    erase_stroke_count: Annotated[str, Form()],
    restore_stroke_count: Annotated[str, Form()],
) -> ProductForegroundVersionResponse | JSONResponse:
    """Append one local mask correction without invoking an external provider."""
    expected_fields = {
        "mask",
        "base_version_id",
        "base_manifest_sha256",
        "edit_manifest_sha256",
        "editor_version",
        "idempotency_key",
        "magic_wand_count",
        "erase_stroke_count",
        "restore_stroke_count",
    }
    form = await request.form()
    if set(form.keys()) != expected_fields or len(form.multi_items()) != len(
        expected_fields
    ):
        return _error_response(
            "foreground_edit_invalid", status.HTTP_422_UNPROCESSABLE_CONTENT
        )
    try:
        payload = ProductForegroundEditForm.model_validate(
            {
                "base_version_id": base_version_id,
                "base_manifest_sha256": base_manifest_sha256,
                "edit_manifest_sha256": edit_manifest_sha256,
                "editor_version": editor_version,
                "idempotency_key": idempotency_key,
                "magic_wand_count": magic_wand_count,
                "erase_stroke_count": erase_stroke_count,
                "restore_stroke_count": restore_stroke_count,
            }
        )
    except ValidationError:
        return _error_response(
            "foreground_edit_invalid", status.HTTP_422_UNPROCESSABLE_CONTENT
        )
    content = await mask.read(LOCAL_PRODUCT_UPLOAD_HARD_MAX_BYTES + 1)
    await mask.close()
    if len(content) > LOCAL_PRODUCT_UPLOAD_HARD_MAX_BYTES:
        return _error_response(
            "foreground_edit_invalid", status.HTTP_413_CONTENT_TOO_LARGE
        )
    try:
        outcome = _foreground_service(session, store).edit(
            customer_id,
            asset_id,
            base_version_id=payload.base_version_id,
            base_manifest_sha256=payload.base_manifest_sha256,
            mask_png=content,
            editor_version=payload.editor_version,
            edit_manifest_sha256=payload.edit_manifest_sha256,
            idempotency_key=payload.idempotency_key,
            operation_counts=MaskEditOperationCounts(
                magic_wand_count=payload.magic_wand_count,
                erase_stroke_count=payload.erase_stroke_count,
                restore_stroke_count=payload.restore_stroke_count,
            ),
        )
    except DomainRuleError as error:
        return _foreground_edit_error(error)
    if not outcome.created:
        response.status_code = status.HTTP_200_OK
    return _foreground_response(outcome)


@identity_router.post(
    "/local/style-suite/api/products/{asset_id}/foreground-versions",
    response_model=ProductForegroundVersionResponse,
    status_code=status.HTTP_201_CREATED,
)
def decide_product_foreground(
    asset_id: UUID,
    payload: ProductForegroundDecisionRequest,
    customer_id: CustomerId,
    session: SessionDependency,
    store: StoreDependency,
    response: Response,
) -> ProductForegroundVersionResponse | JSONResponse:
    """Apply the only allowed proposed-to-final human decision."""
    try:
        outcome = _foreground_service(session, store).decide(
            customer_id,
            asset_id,
            version_id=payload.version_id,
            manifest_sha256=payload.manifest_sha256,
            confirmation_key=payload.confirmation_key,
            decision=payload.decision,
            confirmed=payload.confirmed,
        )
    except DomainRuleError as error:
        return _foreground_error(error)
    if not outcome.created:
        response.status_code = status.HTTP_200_OK
    return _foreground_response(outcome)


@identity_router.get(
    "/local/style-suite/api/products/{asset_id}/foreground-versions",
    response_model=list[ProductForegroundVersionResponse],
)
def list_product_foregrounds(
    asset_id: UUID,
    customer_id: CustomerId,
    session: SessionDependency,
    store: StoreDependency,
) -> list[ProductForegroundVersionResponse] | JSONResponse:
    try:
        outcomes = _foreground_service(session, store).versions(customer_id, asset_id)
    except DomainRuleError as error:
        return _foreground_error(error)
    return [_foreground_response(outcome) for outcome in outcomes]


@identity_router.get(
    "/local/style-suite/api/foreground-versions/{version_id}/foreground/content",
    response_model=None,
)
def read_product_foreground_content(
    version_id: UUID,
    customer_id: CustomerId,
    session: SessionDependency,
    store: StoreDependency,
) -> Response | JSONResponse:
    try:
        content = _foreground_service(session, store).read_foreground(
            customer_id, version_id
        )
    except DomainRuleError as error:
        return _foreground_error(error)
    return Response(
        content=content,
        media_type="image/png",
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@identity_router.get(
    "/local/style-suite/api/foreground-versions/{version_id}/mask/content",
    response_model=None,
)
def read_product_mask_content(
    version_id: UUID,
    customer_id: CustomerId,
    session: SessionDependency,
    store: StoreDependency,
) -> Response | JSONResponse:
    try:
        content = _foreground_service(session, store).read_mask(customer_id, version_id)
    except DomainRuleError as error:
        return _foreground_error(error)
    return Response(
        content=content,
        media_type="image/png",
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@identity_router.post(
    "/local/style-suite/api/foreground-versions/{version_id}/disagreement/prepare",
    response_model=ProductForegroundVersionResponse,
)
def prepare_product_foreground_disagreement(
    version_id: UUID,
    customer_id: CustomerId,
    session: SessionDependency,
    store: StoreDependency,
    extractor: ProductForegroundExtractorDependency,
) -> ProductForegroundVersionResponse | JSONResponse:
    """Reproduce sealed local stages before appending a private risk-map receipt."""
    try:
        outcome = _foreground_service(
            session, store, extractor
        ).ensure_disagreement_evidence(customer_id, version_id)
    except DomainRuleError as error:
        return _foreground_error(error)
    return _foreground_response(outcome)


@identity_router.get(
    "/local/style-suite/api/foreground-versions/{version_id}/disagreement/content",
    response_model=None,
)
def read_product_foreground_disagreement_content(
    version_id: UUID,
    customer_id: CustomerId,
    session: SessionDependency,
    store: StoreDependency,
) -> Response | JSONResponse:
    try:
        content = _foreground_service(session, store).read_disagreement_map(
            customer_id, version_id
        )
    except DomainRuleError as error:
        return _foreground_error(error)
    return Response(
        content=content,
        media_type="image/png",
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


router.include_router(identity_router)


def _real_style_service(
    request: Request,
    session: Session,
    store: AssetContentStore,
    analyzer: VisualStyleAnalyzer | None,
) -> RealStyleAnalysisService:
    settings = getattr(
        request.app.state, "real_style_analysis_settings", RealStyleAnalysisSettings()
    )
    if analyzer is None or not settings.enabled:
        raise DomainRuleError("real_style_analysis_provider_disabled")
    return RealStyleAnalysisService(
        session,
        content_store=store,
        analyzer=analyzer,
        model=settings.model,
        maximum_usd=settings.max_run_usd,
    )


@router.get("/local/style-suite/api/real-style-analysis/preview")
def preview_real_style_analysis(
    request: Request,
    customer_id: CustomerId,
    session: SessionDependency,
    store: StoreDependency,
    analyzer: RealStyleAnalyzerDependency,
) -> JSONResponse:
    """Quote and seal the exact photo scope without any provider call."""
    try:
        preview = _real_style_service(request, session, store, analyzer).preview(
            customer_id
        )
    except DomainRuleError as error:
        return _error_response(error.code, status.HTTP_409_CONFLICT)
    return JSONResponse(content=jsonable_encoder(asdict(preview)))


@router.post("/local/style-suite/api/real-style-analysis/runs")
def execute_real_style_analysis(
    payload: LocalRealStyleAnalysisRequest,
    request: Request,
    customer_id: CustomerId,
    session: SessionDependency,
    store: StoreDependency,
    analyzer: RealStyleAnalyzerDependency,
    response: Response,
) -> JSONResponse:
    """Spend at most once after exact cost and manifest confirmation."""
    try:
        outcome = _real_style_service(request, session, store, analyzer).execute(
            customer_id,
            idempotency_key=payload.idempotency_key,
            confirmed_maximum_usd=payload.confirmed_maximum_usd,
            confirmed_reference_manifest_sha256=(
                payload.confirmed_reference_manifest_sha256
            ),
        )
    except DomainRuleError as error:
        retryable = error.code == "real_style_analysis_outcome_uncertain"
        return JSONResponse(
            status_code=(503 if retryable else 409),
            content={"error": {"code": error.code, "retryable": retryable}},
        )
    response.status_code = 201 if outcome.created else 200
    return JSONResponse(
        status_code=response.status_code,
        content=jsonable_encoder(
            {
                "run_id": outcome.run_id,
                "created": outcome.created,
                "proposal": asdict(outcome.proposal),
            }
        ),
    )


@router.get("/local/style-suite/api/real-style-analysis/latest")
def latest_real_style_analysis(
    customer_id: CustomerId,
    session: SessionDependency,
) -> JSONResponse:
    """Restore the newest saved proposal without contacting the provider."""
    outcome = latest_successful_real_style_analysis(session, customer_id)
    if outcome is None:
        return _error_response(
            "real_style_analysis_not_found", status.HTTP_404_NOT_FOUND
        )
    return JSONResponse(
        content=jsonable_encoder(
            {
                "run_id": outcome.run_id,
                "created": False,
                "proposal": asdict(outcome.proposal),
            }
        )
    )


def _real_fidelity_service(
    request: Request,
    session: Session,
    store: AssetContentStore,
    assessor: FidelityBatchAssessor | None,
) -> RealFidelityService:
    settings = getattr(
        request.app.state, "real_fidelity_settings", RealFidelitySettings()
    )
    if assessor is None or not settings.enabled:
        raise DomainRuleError("real_fidelity_provider_disabled")
    return RealFidelityService(
        session,
        content_store=store,
        assessor=assessor,
        model=settings.model,
        maximum_usd=settings.max_run_usd,
    )


@router.get("/local/style-suite/api/runs/{run_id}/fidelity-preview")
def preview_real_fidelity(
    run_id: UUID,
    request: Request,
    customer_id: CustomerId,
    session: SessionDependency,
    store: StoreDependency,
    assessor: RealFidelityAssessorDependency,
) -> JSONResponse:
    """Quote and seal one product plus three candidates without provider use."""
    try:
        preview = _real_fidelity_service(request, session, store, assessor).preview(
            customer_id, run_id
        )
    except DomainRuleError as error:
        return _error_response(error.code, status.HTTP_409_CONFLICT)
    return JSONResponse(content=jsonable_encoder(asdict(preview)))


@router.post("/local/style-suite/api/runs/{run_id}/fidelity-checks")
def execute_real_fidelity(
    run_id: UUID,
    payload: LocalRealFidelityRequest,
    request: Request,
    customer_id: CustomerId,
    session: SessionDependency,
    store: StoreDependency,
    assessor: RealFidelityAssessorDependency,
    response: Response,
) -> JSONResponse:
    """Spend at most once after exact visual scope and cost confirmation."""
    try:
        outcome = _real_fidelity_service(request, session, store, assessor).execute(
            customer_id,
            run_id,
            idempotency_key=payload.idempotency_key,
            confirmed_maximum_usd=payload.confirmed_maximum_usd,
            confirmed_manifest_sha256=payload.confirmed_manifest_sha256,
        )
    except DomainRuleError as error:
        retryable = error.code == "real_fidelity_outcome_uncertain"
        return JSONResponse(
            status_code=(503 if retryable else 409),
            content={"error": {"code": error.code, "retryable": retryable}},
        )
    response.status_code = (
        status.HTTP_201_CREATED if outcome.created else status.HTTP_200_OK
    )
    return JSONResponse(
        status_code=response.status_code,
        content={"run_id": str(outcome.run_id), "created": outcome.created},
    )


@router.post(
    "/local/style-suite/api/synthetic-history",
    response_model=LocalStyleSuiteHistoryResponse,
    status_code=status.HTTP_201_CREATED,
)
def prepare_synthetic_history(
    request: Request, customer_id: CustomerId
) -> LocalStyleSuiteHistoryResponse | JSONResponse:
    """Compatibility proxy for the physically isolated local demo sandbox."""
    return bootstrap_demo_sandbox(request, customer_id)


@router.post(
    "/local/style-suite/api/product",
    response_model=ProductAssetResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_local_product(
    request: Request,
    customer_id: CustomerId,
    session: SessionDependency,
    store: StoreDependency,
    x_product_id: Annotated[str, Header(alias="X-Product-Id")],
    x_photo_id: Annotated[str, Header(alias="X-Photo-Id")],
) -> ProductAssetResponse | JSONResponse:
    try:
        settings: LocalBridgeSettings = request.app.state.local_bridge_settings
        content = await _collect_product_image(request, settings)
        mime, width, height = _decode_local_product_image(content, settings)
    except DomainRuleError as error:
        status_code = 413 if error.code == "product_image_limits_exceeded" else 422
        return _error_response(error.code, status_code)
    if session.get(Customer, customer_id) is None:
        session.add(Customer(id=customer_id, display_name="Local synthetic customer"))
        session.flush()
    asset = ProductAsset(
        customer_id=customer_id,
        product_id=x_product_id,
        photo_id=x_photo_id,
        identity="original_real_photo",
        confirmed=True,
        source_type="synthetic_metadata",
        quality_flags=["local_synthetic_acceptance"],
        occurred_at=datetime.now(UTC),
        content_sha256=sha256(content).hexdigest(),
        mime_type=mime,
        width=width,
        height=height,
        storage_path=None,
    )
    if not isinstance(store, LocalSyntheticAssetStore):
        raise HTTPException(status_code=409, detail="local_synthetic_store_required")
    session.add(asset)
    try:
        session.flush()
        store.persist_original(customer_id, asset.id, content, asset.content_sha256)
        session.add(
            ProductAssetPermissionEvent(
                customer_id=customer_id,
                product_asset_id=asset.id,
                transform_permission=True,
                occurred_at=datetime.now(UTC),
                provenance="local_synthetic_acceptance",
            )
        )
        session.commit()
    except BaseException:
        session.rollback()
        if asset.id is not None:
            store.discard_uncommitted_asset(
                customer_id,
                "originals",
                asset.id,
                asset.content_sha256,
            )
        raise
    session.refresh(asset)
    intake = ProductAssetIntake(
        asset=asset,
        readiness=ProductAssetService(session).readiness(customer_id, asset),
        created=True,
    )
    from opc_style.api.routes_assets import _asset_response

    return _asset_response(intake)


async def _collect_product_image(
    request: Request, settings: LocalBridgeSettings
) -> bytes:
    chunks: list[bytes] = []
    total = 0
    max_bytes = min(
        settings.local_classification_bridge_max_bytes,
        LOCAL_PRODUCT_UPLOAD_HARD_MAX_BYTES,
    )
    try:
        async for chunk in request.stream():
            total += len(chunk)
            if total > max_bytes:
                raise DomainRuleError("product_image_limits_exceeded")
            chunks.append(chunk)
    except DomainRuleError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        raise DomainRuleError("product_image_invalid") from error
    return b"".join(chunks)


def _decode_local_product_image(
    content: bytes, settings: LocalBridgeSettings
) -> tuple[str, int, int]:
    mime_by_format = {"JPEG": "image/jpeg", "PNG": "image/png"}
    max_width = min(
        settings.local_classification_bridge_max_width,
        LOCAL_PRODUCT_UPLOAD_HARD_MAX_DIMENSION,
    )
    max_height = min(
        settings.local_classification_bridge_max_height,
        LOCAL_PRODUCT_UPLOAD_HARD_MAX_DIMENSION,
    )
    max_pixels = min(
        settings.local_classification_bridge_max_pixels,
        LOCAL_PRODUCT_UPLOAD_HARD_MAX_PIXELS,
    )
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(content)) as image:
                image_format = image.format
                width, height = image.size
                frames = getattr(image, "n_frames", 1)
                if image_format not in mime_by_format:
                    raise DomainRuleError("product_image_format_unsupported")
                if (
                    width > max_width
                    or height > max_height
                    or width * height > max_pixels
                    or frames != 1
                ):
                    raise DomainRuleError("product_image_limits_exceeded")
                image.verify()
            with Image.open(BytesIO(content)) as decoded:
                if decoded.format != image_format:
                    raise DomainRuleError("product_image_invalid")
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
    ) as error:
        raise DomainRuleError("product_image_invalid") from error
    return mime_by_format[image_format], width, height


@router.delete("/local/style-suite/api/assets/{asset_id}")
def delete_local_product_asset(
    asset_id: UUID,
    customer_id: CustomerId,
    session: SessionDependency,
    store: StoreDependency,
) -> JSONResponse:
    """Delete only an unreferenced customer-owned local product upload."""
    asset = session.scalar(
        select(ProductAsset).where(
            ProductAsset.id == asset_id,
            ProductAsset.customer_id == customer_id,
        )
    )
    if asset is None:
        raise HTTPException(status_code=404)
    already_deleted = any(
        event.payload.get("asset_id") == str(asset_id)
        for event in session.scalars(
            select(AuditEvent).where(
                AuditEvent.customer_id == customer_id,
                AuditEvent.event_type == "local_asset.deleted",
            )
        )
    )
    if already_deleted:
        return JSONResponse(
            status_code=200,
            content={"id": str(asset_id), "state": "deleted"},
        )
    referenced = session.scalar(
        select(StyleSuiteRun.id).where(
            StyleSuiteRun.customer_id == customer_id,
            StyleSuiteRun.source_asset_id == asset_id,
        )
    )
    if referenced is not None:
        return _error_response("local_asset_lineage_retained", 409)
    if not isinstance(store, LocalSyntheticAssetStore):
        return _error_response("local_asset_store_unavailable", 409)
    content = store.read_original(customer_id, asset.id, asset.content_sha256)
    store.discard_uncommitted_asset(
        customer_id, "originals", asset.id, asset.content_sha256
    )
    try:
        session.add(
            AuditEvent(
                customer_id=customer_id,
                event_type="local_asset.deleted",
                payload={"asset_id": str(asset_id)},
                occurred_at=datetime.now(UTC),
            )
        )
        session.commit()
    except BaseException:
        session.rollback()
        store.persist_original(customer_id, asset_id, content, asset.content_sha256)
        raise
    return JSONResponse(
        status_code=200, content={"id": str(asset_id), "state": "deleted"}
    )


@router.post("/local/style-suite/api/workspace/delete")
def delete_local_workspace_assets(
    request: dict[str, bool],
    customer_id: CustomerId,
    session: SessionDependency,
    store: StoreDependency,
) -> JSONResponse:
    """Report partial local cleanup honestly, one immutable asset at a time."""
    if request.get("confirmed") is not True:
        return _error_response("local_workspace_delete_confirmation_required", 409)
    asset_ids = tuple(
        session.scalars(
            select(ProductAsset.id).where(ProductAsset.customer_id == customer_id)
        )
    )
    results: list[dict[str, str]] = []
    for asset_id in asset_ids:
        response = delete_local_product_asset(asset_id, customer_id, session, store)
        payload = json.loads(bytes(response.body))
        state = "deleted" if response.status_code == 200 else "retained"
        results.append({"id": str(asset_id), "state": state})
        if state == "retained":
            results[-1]["reason"] = payload["error"]["code"]
    status_code = 207 if any(row["state"] == "retained" for row in results) else 200
    return JSONResponse(status_code=status_code, content={"results": results})


@router.post(
    "/local/style-suite/api/batch-runs/preview",
    response_model=StyleSuiteBatchPreviewResponse,
)
def preview_local_real_style_batch(
    batch_request: StyleSuiteBatchPreviewRequest,
    customer_id: CustomerId,
    session: SessionDependency,
    store: StoreDependency,
    request: Request,
) -> StyleSuiteBatchPreviewResponse | JSONResponse:
    """Return a signed, read-only quote for at most six real candidates."""
    try:
        planning_settings = _real_batch_planning_settings(request)
        preview = StyleSuiteService(
            session,
            settings=planning_settings,
            content_store=store,
            enforce_real_pilot_boundary=True,
            required_style_origin="customer_upload",
        ).preview_batch(
            customer_id,
            batch_request,
            secret=request.app.state.local_style_suite_preview_secret,
        )
        real_image_settings = getattr(
            request.app.state,
            "real_image_provider_settings",
            RealImageProviderSettings(),
        )
        if (
            real_image_settings.enabled
            and preview.maximum_estimated_cost_usd > real_image_settings.max_run_usd
        ):
            raise DomainRuleError("style_suite_batch_cost_exceeded")
        return preview
    except DomainRuleError as error:
        return _error_response(error.code, status.HTTP_409_CONFLICT)


def _pixel_style_service(
    request: Request,
    session: Session,
    store: AssetContentStore,
    generator: Any,
) -> PixelPreservingStyleService:
    settings = getattr(
        request.app.state, "real_background_settings", RealBackgroundProviderSettings()
    )
    return PixelPreservingStyleService(
        session,
        content_store=store,
        settings=settings,
        generator=generator,
    )


def _pixel_background_output_size(slot_rules: dict[str, object]) -> tuple[int, int]:
    """Map a slot ratio to one of the image provider's supported canvas sizes."""
    raw_ratio = slot_rules.get("aspect_ratio")
    if not isinstance(raw_ratio, str):
        return (1024, 1024)
    try:
        width_text, height_text = raw_ratio.split(":", maxsplit=1)
        width, height = int(width_text), int(height_text)
    except (TypeError, ValueError):
        return (1024, 1024)
    if width <= 0 or height <= 0 or width == height:
        return (1024, 1024)
    return (1024, 1536) if height > width else (1536, 1024)


@router.post(
    "/local/style-suite/api/pixel-backgrounds/preview",
    response_model=PixelBackgroundPreviewResponse,
)
def preview_pixel_background(
    payload: PixelBackgroundPreviewRequest,
    customer_id: CustomerId,
    session: SessionDependency,
    store: StoreDependency,
    generator: BackgroundGeneratorDependency,
    request: Request,
) -> PixelBackgroundPreviewResponse | JSONResponse:
    item = session.scalar(
        select(StyleSuiteRunItem).where(
            StyleSuiteRunItem.customer_id == customer_id,
            StyleSuiteRunItem.run_id == payload.style_suite_run_id,
            StyleSuiteRunItem.id == payload.style_suite_run_item_id,
        )
    )
    if (
        item is None
        or item.style_reference_pack_id is None
        or item.state not in {"pixel_background_planned", "succeeded"}
    ):
        return _error_response("pixel_preserving_ineligible", status.HTTP_409_CONFLICT)
    controlled_style = {
        key: item.style_snapshot[key]
        for key in (
            "name",
            "tone",
            "palette",
            "lighting",
            "composition",
            "backgrounds",
            "people_and_props",
        )
        if key in item.style_snapshot
    }
    slot_rules = dict(item.slot_snapshot.get("rules", {}))
    try:
        preview = _pixel_style_service(request, session, store, generator).preview(
            customer_id,
            style_suite_run_id=payload.style_suite_run_id,
            style_suite_run_item_id=item.id,
            foreground_version_id=payload.foreground_version_id,
            style_reference_pack_id=item.style_reference_pack_id,
            slot_rules=slot_rules,
            style_rules=cast(dict[str, object], controlled_style),
            output_size=_pixel_background_output_size(slot_rules),
        )
    except DomainRuleError as error:
        return _error_response(error.code, status.HTTP_409_CONFLICT)
    signed = SignedBatchScope.issue(
        request.app.state.local_style_suite_preview_secret,
        {
            "kind": "pixel-background-preview-v1",
            "style_suite_run_item_id": str(item.id),
            "preview": jsonable_encoder(asdict(preview)),
        },
    )
    return PixelBackgroundPreviewResponse(
        preview_token=signed.token,
        style_suite_run_id=preview.style_suite_run_id,
        style_suite_run_item_id=item.id,
        foreground_version_id=preview.foreground_version_id,
        style_reference_pack_id=preview.style_reference_pack_id,
        authorized_manifest_sha256=preview.authorized_manifest_sha256,
        model=preview.model,
        maximum_usd=preview.maximum_usd,
        provider_image_count=preview.provider_image_count,
    )


def _preview_from_token(
    request: Request, token: str
) -> tuple[UUID, BackgroundGenerationPreview]:
    signed = SignedBatchScope.verify(
        request.app.state.local_style_suite_preview_secret, token
    )
    try:
        payload = signed.payload
        if payload.get("kind") != "pixel-background-preview-v1":
            raise TypeError
        raw = payload["preview"]
        if not isinstance(raw, dict):
            raise TypeError
        item_id = UUID(str(payload["style_suite_run_item_id"]))
        preview = BackgroundGenerationPreview(
            customer_id=UUID(str(raw["customer_id"])),
            style_suite_run_id=UUID(str(raw["style_suite_run_id"])),
            foreground_version_id=UUID(str(raw["foreground_version_id"])),
            style_reference_pack_id=UUID(str(raw["style_reference_pack_id"])),
            authorized_manifest_sha256=str(raw["authorized_manifest_sha256"]),
            authorized_manifest=dict(raw["authorized_manifest"]),
            model=str(raw["model"]),
            maximum_usd=Decimal(str(raw["maximum_usd"])),
            provider_image_count=int(raw["provider_image_count"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise DomainRuleError("style_suite_preview_invalid") from error
    return item_id, preview


@router.post(
    "/local/style-suite/api/pixel-background-runs",
    response_model=PixelBackgroundRunResponse,
    status_code=status.HTTP_201_CREATED,
)
def execute_pixel_background(
    payload: PixelBackgroundExecuteRequest,
    customer_id: CustomerId,
    session: SessionDependency,
    store: StoreDependency,
    generator: BackgroundGeneratorDependency,
    request: Request,
    response: Response,
) -> PixelBackgroundRunResponse | JSONResponse:
    try:
        _, preview = _preview_from_token(request, payload.preview_token)
        outcome = _pixel_style_service(
            request, session, store, generator
        ).execute_background(
            customer_id,
            preview,
            idempotency_key=payload.idempotency_key,
            confirmed_maximum_usd=payload.confirmed_maximum_usd,
        )
    except DomainRuleError as error:
        return _error_response(error.code, status.HTTP_409_CONFLICT)
    if not outcome.created:
        response.status_code = status.HTTP_200_OK
    return PixelBackgroundRunResponse(**asdict(outcome))


@router.post(
    "/local/style-suite/api/pixel-background-runs/{run_id}/decision",
    response_model=PixelBackgroundRunResponse,
)
def decide_pixel_background(
    run_id: UUID,
    payload: PixelBackgroundDecisionRequest,
    customer_id: CustomerId,
    session: SessionDependency,
    store: StoreDependency,
    generator: BackgroundGeneratorDependency,
    request: Request,
) -> PixelBackgroundRunResponse | JSONResponse:
    try:
        outcome = _pixel_style_service(
            request, session, store, generator
        ).confirm_background(customer_id, run_id, decision=payload.decision)
    except DomainRuleError as error:
        return _error_response(error.code, status.HTTP_409_CONFLICT)
    return PixelBackgroundRunResponse(**asdict(outcome))


@router.get(
    "/local/style-suite/api/pixel-background-runs/{run_id}/content",
    response_model=None,
)
def read_pixel_background_content(
    run_id: UUID,
    customer_id: CustomerId,
    session: SessionDependency,
    store: StoreDependency,
) -> Response | JSONResponse:
    """Return only the customer-owned generated empty background for review."""
    row = session.scalar(
        select(PixelBackgroundRun).where(
            PixelBackgroundRun.customer_id == customer_id,
            PixelBackgroundRun.id == run_id,
        )
    )
    if row is None or row.result_sha256 is None:
        return _error_response(
            "pixel_background_unavailable", status.HTTP_404_NOT_FOUND
        )
    try:
        content = store.read_pixel_background(customer_id, run_id, row.result_sha256)
    except DomainRuleError as error:
        return _error_response(error.code, status.HTTP_409_CONFLICT)
    return Response(
        content=content,
        media_type="image/png",
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _resolve_pixel_background_item(
    items: list[StyleSuiteRunItem], background: PixelBackgroundRun
) -> StyleSuiteRunItem | None:
    """Resolve one exact suite cell; never guess between equal-looking slots."""
    eligible_items = [
        row for row in items if row.state in {"pixel_background_planned", "succeeded"}
    ]
    raw_item_id = background.authorized_manifest.get("style_suite_run_item_id")
    item = None
    if isinstance(raw_item_id, str):
        try:
            manifest_item_id = UUID(raw_item_id)
        except ValueError as error:
            raise DomainRuleError("pixel_composition_scope_mismatch") from error
        item = next((row for row in eligible_items if row.id == manifest_item_id), None)
    else:
        # Legacy manifests predate the explicit item binding. Prefer the slot
        # encoded by the old idempotency key and fail closed if it is ambiguous.
        slot_matches = [
            row
            for row in eligible_items
            if background.idempotency_key.startswith(
                f"pixel-background-{row.slot_key}-"
            )
        ]
        if len(slot_matches) == 1:
            item = slot_matches[0]
        else:
            manifest_pack_id = str(
                background.authorized_manifest.get("style_reference_pack_id")
            )
            manifest_rules = background.authorized_manifest.get("slot_rules")
            legacy_matches = [
                row
                for row in eligible_items
                if str(row.style_reference_pack_id) == manifest_pack_id
                and row.slot_snapshot.get("rules") == manifest_rules
            ]
            if len(legacy_matches) == 1:
                item = legacy_matches[0]
    return item


def _pixel_output_job(
    session: Session, customer_id: UUID, run_id: UUID
) -> ImageOutputJob:
    background = session.scalar(
        select(PixelBackgroundRun).where(
            PixelBackgroundRun.customer_id == customer_id,
            PixelBackgroundRun.id == run_id,
        )
    )
    if background is None:
        raise DomainRuleError("pixel_background_unavailable")
    items = session.scalars(
        select(StyleSuiteRunItem).where(
            StyleSuiteRunItem.customer_id == customer_id,
            StyleSuiteRunItem.run_id == background.style_suite_run_id,
        )
    ).all()
    item = _resolve_pixel_background_item(list(items), background)
    suite = session.get(StyleSuiteRun, background.style_suite_run_id)
    if item is None or suite is None or suite.customer_id != customer_id:
        raise DomainRuleError("pixel_composition_scope_mismatch")
    if item.output_job_id is not None:
        job = session.get(ImageOutputJob, item.output_job_id)
        if job is None:
            raise DomainRuleError("pixel_composition_scope_mismatch")
        return job
    key = (
        f"suite:{suite.id}:{item.scene_style_version_id}:"
        f"{item.slot_key}:{suite.source_content_sha256}"
    )
    if item.style_reference_manifest_sha256 is not None:
        key = f"{key}:{item.style_reference_manifest_sha256}"
    job = ImageOutputJob(
        customer_id=customer_id,
        output_type="product_style",
        style_card_version_id=item.scene_style_version_id,
        scene_style_id=item.scene_style_id,
        output_ratio=str(item.slot_snapshot["rules"].get("aspect_ratio", "1:1")),
        adapter_version="pillow-pixel-compositor-v1",
        status="awaiting_customer_review",
        request_metadata={
            "slot_key": item.slot_key,
            "sources": [
                {"product_id": suite.product_id, "asset_id": str(suite.source_asset_id)}
            ],
            "generation_mode": "pixel_preserving_isolated",
            "publishable": False,
        },
        idempotency_key=key,
    )
    session.add(job)
    session.flush()
    session.add(
        ProductAssetLink(
            customer_id=customer_id,
            product_asset_id=suite.source_asset_id,
            output_job_id=job.id,
            position=1,
        )
    )
    session.flush()
    item.output_job_id = job.id
    session.commit()
    return job


@router.post(
    "/local/style-suite/api/pixel-background-runs/{run_id}/compose-preview",
    response_model=None,
)
def preview_pixel_composition(
    run_id: UUID,
    payload: PixelBackgroundComposeRequest,
    customer_id: CustomerId,
    session: SessionDependency,
    store: StoreDependency,
    generator: BackgroundGeneratorDependency,
    request: Request,
) -> Response | JSONResponse:
    """Return a verified local preview without creating a candidate or receipt."""
    try:
        composition = _pixel_style_service(
            request, session, store, generator
        ).preview_composition(
            customer_id,
            run_id,
            transform=ForegroundTransform(
                payload.translate_x,
                payload.translate_y,
                payload.uniform_scale,
                payload.rotation_degrees,
            ),
            output_size=(payload.output_width, payload.output_height),
        )
    except DomainRuleError as error:
        return _error_response(error.code, status.HTTP_409_CONFLICT)
    return Response(
        content=composition.png_bytes,
        media_type="image/png",
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "X-Preview-Only": "true",
            "X-Result-Sha256": composition.output_sha256,
        },
    )


@router.get(
    "/local/style-suite/api/pixel-background-runs/{run_id}/compose-recommendation",
    response_model=PixelCompositionRecommendationResponse,
)
def recommend_pixel_composition(
    run_id: UUID,
    customer_id: CustomerId,
    session: SessionDependency,
    store: StoreDependency,
    generator: BackgroundGeneratorDependency,
    request: Request,
) -> PixelCompositionRecommendationResponse | JSONResponse:
    try:
        recommendation = _pixel_style_service(
            request, session, store, generator
        ).recommend_composition(customer_id, run_id)
    except DomainRuleError as error:
        return _error_response(error.code, status.HTTP_409_CONFLICT)
    return PixelCompositionRecommendationResponse(**asdict(recommendation))


@router.post(
    "/local/style-suite/api/pixel-background-runs/{run_id}/compose",
    response_model=PixelCompositionResponse,
)
def compose_pixel_background(
    run_id: UUID,
    payload: PixelBackgroundComposeRequest,
    customer_id: CustomerId,
    session: SessionDependency,
    store: StoreDependency,
    generator: BackgroundGeneratorDependency,
    request: Request,
) -> PixelCompositionResponse | JSONResponse:
    try:
        job = _pixel_output_job(session, customer_id, run_id)
        outcome = _pixel_style_service(request, session, store, generator).compose(
            customer_id,
            run_id,
            output_job_id=job.id,
            transform=ForegroundTransform(
                payload.translate_x,
                payload.translate_y,
                payload.uniform_scale,
                payload.rotation_degrees,
            ),
            output_size=(payload.output_width, payload.output_height),
        )
    except DomainRuleError as error:
        return _error_response(error.code, status.HTTP_409_CONFLICT)
    item = session.scalar(
        select(StyleSuiteRunItem).where(
            StyleSuiteRunItem.customer_id == customer_id,
            StyleSuiteRunItem.output_job_id == job.id,
        )
    )
    if item is not None:
        item.state = "succeeded"
        item.failure_reason = None
        session.commit()
    return PixelCompositionResponse(**asdict(outcome))


@router.get(
    "/local/style-suite/api/pixel-background-runs/{run_id}",
    response_model=PixelBackgroundRunResponse,
)
def pixel_background_state(
    run_id: UUID,
    customer_id: CustomerId,
    session: SessionDependency,
) -> PixelBackgroundRunResponse | JSONResponse:
    row = session.scalar(
        select(PixelBackgroundRun).where(
            PixelBackgroundRun.customer_id == customer_id,
            PixelBackgroundRun.id == run_id,
        )
    )
    if row is None:
        raise HTTPException(status_code=404)
    try:
        output_width, output_height = (
            PixelPreservingStyleService._authorized_output_size(row)
        )
    except DomainRuleError as error:
        return _error_response(error.code, status.HTTP_409_CONFLICT)
    return PixelBackgroundRunResponse(
        id=row.id,
        dispatch_state=cast(Any, row.dispatch_state),
        outbound_attempts=row.outbound_attempts,
        result_sha256=row.result_sha256,
        review_state=cast(Any, row.review_state),
        failure_code=row.failure_code,
        output_width=output_width,
        output_height=output_height,
        created=False,
    )


@router.post(
    "/local/style-suite/api/batch-runs",
    response_model=StyleSuiteExecutionResponse,
    status_code=status.HTTP_201_CREATED,
)
def execute_local_real_style_batch(
    batch_request: StyleSuiteBatchCreateRequest,
    customer_id: CustomerId,
    session: SessionDependency,
    store: StoreDependency,
    generator: GeneratorDependency,
    request: Request,
    response: Response,
) -> StyleSuiteExecutionResponse | JSONResponse:
    """Execute one signed first-round batch through the paid provider ledger."""
    if (
        not isinstance(generator, OpenAIImageGenerator)
        or generator.generation_mode != REAL_MODEL_GENERATION_MODE
    ):
        return _error_response("real_image_provider_disabled", status.HTTP_409_CONFLICT)
    replay = session.scalar(
        select(StyleSuiteRun.id).where(
            StyleSuiteRun.customer_id == customer_id,
            StyleSuiteRun.idempotency_key == batch_request.idempotency_key,
        )
    )
    try:
        result = StyleSuiteService(
            session,
            settings=_real_batch_planning_settings(request),
            content_store=store,
            generator=generator,
            enforce_real_pilot_boundary=True,
            required_style_origin="customer_upload",
        ).create_batch(
            customer_id,
            batch_request,
            secret=request.app.state.local_style_suite_preview_secret,
        )
    except DomainRuleError as error:
        return _error_response(error.code, status.HTTP_409_CONFLICT)
    if replay is not None:
        response.status_code = status.HTTP_200_OK
    return result


def _real_batch_planning_settings(request: Request) -> StyleSuitePlanningSettings:
    """Use the provider's actual reserve estimate for preview and execution."""
    planning = cast(
        StyleSuitePlanningSettings,
        request.app.state.style_suite_planning_settings,
    )
    provider = getattr(
        request.app.state,
        "real_image_provider_settings",
        RealImageProviderSettings(),
    )
    if not provider.enabled:
        return planning
    return StyleSuitePlanningSettings(
        real_generation_enabled=True,
        candidate_estimate_usd=provider.estimated_request_usd,
    )


@router.post(
    "/local/style-suite/api/runs",
    response_model=StyleSuiteExecutionResponse,
    status_code=status.HTTP_201_CREATED,
)
def execute_local_suite(
    request: StyleSuiteCreateRequest,
    customer_id: CustomerId,
    session: SessionDependency,
    store: StoreDependency,
    response: Response,
) -> StyleSuiteExecutionResponse:
    """Run the real orchestrator with a fixed zero-network acceptance adapter."""
    replay = session.scalar(
        select(StyleSuiteRun.id).where(
            StyleSuiteRun.customer_id == customer_id,
            StyleSuiteRun.idempotency_key == request.idempotency_key,
        )
    )
    result = StyleSuiteService(
        session,
        settings=StyleSuitePlanningSettings(
            real_generation_enabled=True, candidate_estimate_usd=Decimal("0.12")
        ),
        content_store=store,
        generator=DeterministicLocalImageGenerator(),
        allow_synthetic_product_style=True,
    ).create(
        customer_id,
        request.product_id,
        request.source_asset_id,
        idempotency_key=request.idempotency_key,
    )
    if replay is not None:
        response.status_code = status.HTTP_200_OK
    return result


@router.post(
    "/local/style-suite/api/real-pilot/runs",
    response_model=StyleSuiteExecutionResponse,
    status_code=status.HTTP_201_CREATED,
)
def execute_local_real_pilot(
    request: LocalRealStylePilotRequest,
    customer_id: CustomerId,
    session: SessionDependency,
    store: StoreDependency,
    generator: GeneratorDependency,
    response: Response,
) -> StyleSuiteExecutionResponse | JSONResponse:
    """Execute one sealed, locally reviewed paid-model pilot when authority matches."""
    if (
        not isinstance(generator, OpenAIImageGenerator)
        or generator.generation_mode != REAL_MODEL_GENERATION_MODE
    ):
        return _error_response("real_image_provider_disabled", status.HTTP_409_CONFLICT)
    if request.confirmed_max_usd != generator.max_run_usd:
        return _error_response(
            "real_pilot_cost_confirmation_required", status.HTTP_409_CONFLICT
        )
    replay = session.scalar(
        select(StyleSuiteRun.id).where(
            StyleSuiteRun.customer_id == customer_id,
            StyleSuiteRun.idempotency_key == request.idempotency_key,
        )
    )
    try:
        validate_confirmed_product_identity_card(
            session,
            store,
            customer_id,
            request.product_id,
            request.source_asset_id,
            request.product_identity_card_version_id,
        )
        authorization = issue_real_pilot_authorization(
            customer_id=customer_id,
            product_id=request.product_id,
            source_asset_id=request.source_asset_id,
            scene_style_version_id=request.scene_style_version_id,
            slot_key=request.slot_key,
            idempotency_key=request.idempotency_key,
            authorized_usd=generator.max_run_usd,
        )
        result = StyleSuiteService(
            session,
            settings=StyleSuitePlanningSettings(real_generation_enabled=True),
            content_store=store,
            generator=generator,
            allow_synthetic_product_style=True,
            real_pilot_authorization=authorization,
            enforce_real_pilot_boundary=True,
            required_style_origin="customer_upload",
        ).create(
            customer_id,
            request.product_id,
            request.source_asset_id,
            idempotency_key=request.idempotency_key,
            pilot_scope=StyleSuitePilotScope(
                scene_style_version_id=request.scene_style_version_id,
                slot_key=request.slot_key,
            ),
            product_identity_card_version_id=request.product_identity_card_version_id,
            product_identity_observation_run_id=(
                request.product_identity_observation_run_id
            ),
            product_identity_observation_review_id=(
                request.product_identity_observation_review_id
            ),
        )
    except DomainRuleError as error:
        code = (
            404
            if error.code
            in {
                "style_suite_pilot_scope_unavailable",
                "style_suite_source_unavailable",
            }
            else 409
        )
        return _error_response(error.code, code)
    if replay is not None:
        response.status_code = status.HTTP_200_OK
    return result


@router.get(
    "/local/style-suite/api/assets/{asset_id}/latest-run",
    response_model=LocalStyleSuiteRunResponse,
)
def latest_local_real_style_run(
    asset_id: UUID,
    customer_id: CustomerId,
    session: SessionDependency,
    request: Request,
) -> LocalStyleSuiteRunResponse:
    """Return the newest usable real-pilot run for one customer-owned product."""
    run_id = session.scalar(
        select(StyleSuiteRun.id)
        .where(
            StyleSuiteRun.customer_id == customer_id,
            StyleSuiteRun.source_asset_id == asset_id,
            StyleSuiteRun.real_generation_enabled.is_(True),
            StyleSuiteRun.pilot_scope.is_not(None),
            StyleSuiteRun.state.in_(("awaiting_review", "completed")),
        )
        .order_by(StyleSuiteRun.created_at.desc(), StyleSuiteRun.id.desc())
        .limit(1)
    )
    if run_id is None:
        raise HTTPException(status_code=404)
    return project_local_run(
        session,
        customer_id=customer_id,
        run_id=run_id,
        preview_secret=request.app.state.local_style_suite_preview_secret,
        candidate_url_prefix="/local/style-suite/api/runs",
    )


@router.get(
    "/local/style-suite/api/latest-run",
    response_model=LocalStyleSuiteRunResponse,
)
def latest_local_real_style_run_for_customer(
    customer_id: CustomerId,
    session: SessionDependency,
    request: Request,
) -> LocalStyleSuiteRunResponse:
    """Recover the newest usable real run after browser-local scope was lost."""
    run_id = session.scalar(
        select(StyleSuiteRun.id)
        .where(
            StyleSuiteRun.customer_id == customer_id,
            StyleSuiteRun.real_generation_enabled.is_(True),
            StyleSuiteRun.pilot_scope.is_not(None),
            StyleSuiteRun.state.in_(("awaiting_review", "completed")),
        )
        .order_by(StyleSuiteRun.created_at.desc(), StyleSuiteRun.id.desc())
        .limit(1)
    )
    if run_id is None:
        raise HTTPException(status_code=404)
    return project_local_run(
        session,
        customer_id=customer_id,
        run_id=run_id,
        preview_secret=request.app.state.local_style_suite_preview_secret,
        candidate_url_prefix="/local/style-suite/api/runs",
    )


@router.get(
    "/local/style-suite/api/runs/{run_id}",
    response_model=LocalStyleSuiteRunResponse,
)
def local_run_state(
    run_id: UUID,
    customer_id: CustomerId,
    session: SessionDependency,
    request: Request,
) -> LocalStyleSuiteRunResponse:
    try:
        return project_local_run(
            session,
            customer_id=customer_id,
            run_id=run_id,
            preview_secret=request.app.state.local_style_suite_preview_secret,
            candidate_url_prefix="/local/style-suite/api/runs",
        )
    except DomainRuleError as error:
        raise HTTPException(status_code=404) from error


@router.get(
    "/local/style-suite/api/batch-runs/{run_id}",
    response_model=LocalStyleSuiteBatchRunResponse,
)
def local_batch_run_state(
    run_id: UUID,
    customer_id: CustomerId,
    session: SessionDependency,
    request: Request,
) -> LocalStyleSuiteBatchRunResponse:
    """Restore a real first-round batch from durable server state."""
    try:
        return project_local_batch_run(
            session,
            customer_id=customer_id,
            run_id=run_id,
            preview_secret=request.app.state.local_style_suite_preview_secret,
            candidate_url_prefix="/local/style-suite/api/runs",
        )
    except DomainRuleError as error:
        raise HTTPException(status_code=404) from error


@router.get(
    "/local/style-suite/api/reference-packs/{pack_id}/samples/{sample_id}/content",
    response_class=Response,
)
def local_style_reference_content(
    pack_id: UUID,
    sample_id: UUID,
    request: Request,
    session: SessionDependency,
    store: StoreDependency,
    access: str = "",
) -> Response:
    """Return one signed and still-authorized historical reference image."""
    customer_id = resolve_style_reference_customer(
        request.app.state.local_style_suite_preview_secret,
        pack_id,
        sample_id,
        access,
    )
    if customer_id is None:
        raise HTTPException(status_code=404)
    try:
        pack = StyleReferencePackService(session).get(customer_id, pack_id)
        reference = next(
            item for item in pack.references if item.sample_id == sample_id
        )
        content = store.read_style_sample_content(
            customer_id, sample_id, reference.content_sha256
        )
    except (DomainRuleError, AssetContentStoreError, StopIteration) as error:
        raise HTTPException(status_code=404) from error
    return Response(
        content=content,
        media_type=reference.mime_type,
        headers={"Cache-Control": "private, no-store"},
    )


def _error_response(code: str, status_code: int) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "retryable": False}},
    )


@router.get(
    "/local/style-suite/api/runs/{run_id}/candidates/{candidate_id}/content",
    response_class=Response,
)
def local_candidate_content(
    run_id: UUID,
    candidate_id: UUID,
    request: Request,
    session: SessionDependency,
    store: StoreDependency,
    access: str = "",
    x_customer_id: Annotated[str | None, Header(alias="X-Customer-Id")] = None,
) -> Response:
    """Return only an attested candidate inside the caller-owned suite lineage."""
    header_customer_id: UUID | None = None
    if x_customer_id is not None:
        try:
            header_customer_id = UUID(x_customer_id)
        except ValueError as error:
            raise HTTPException(status_code=404) from error
    return read_candidate_response(
        session,
        store,
        secret=request.app.state.local_style_suite_preview_secret,
        run_id=run_id,
        candidate_id=candidate_id,
        access=access,
        header_customer_id=header_customer_id,
    )


@router.get(
    "/local/style-suite/api/runs/{run_id}/source/content",
    response_class=Response,
)
def local_source_content(
    run_id: UUID,
    request: Request,
    session: SessionDependency,
    store: StoreDependency,
    access: str = "",
    x_customer_id: Annotated[str | None, Header(alias="X-Customer-Id")] = None,
) -> Response:
    """Return the signed original product image for side-by-side review."""
    header_customer_id: UUID | None = None
    if x_customer_id is not None:
        try:
            header_customer_id = UUID(x_customer_id)
        except ValueError as error:
            raise HTTPException(status_code=404) from error
    return read_source_response(
        session,
        store,
        secret=request.app.state.local_style_suite_preview_secret,
        run_id=run_id,
        access=access,
        header_customer_id=header_customer_id,
    )
