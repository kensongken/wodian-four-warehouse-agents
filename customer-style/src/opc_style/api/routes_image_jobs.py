"""Customer-isolated scene and multi-product image-output routes."""

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from functools import lru_cache
from hashlib import sha256
from typing import Annotated, Any, Literal, cast
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from opc_style.api.dependencies import CustomerId, get_session
from opc_style.api.local_run_views import (
    candidate_access_token,
    resolve_candidate_customer,
)
from opc_style.api.routes_real_photos import (
    get_asset_content_store,
    real_photo_output_response,
)
from opc_style.domain.authenticity import derive_candidate_batch_state
from opc_style.domain.errors import DomainRuleError
from opc_style.domain.identity_background_generator import (
    BackgroundGenerator,
    BackgroundSafetyChecker,
    DeterministicBackgroundSafetyChecker,
    DeterministicLocalBackgroundGenerator,
)
from opc_style.domain.image_generator import (
    DeterministicLocalImageGenerator,
    ImageGenerator,
)
from opc_style.domain.subject_seal import (
    SubjectSegmenter,
)
from opc_style.models import SceneStyle, StyleCardVersion
from opc_style.providers.identity_openai_backgrounds import OpenAIBackgroundGenerator
from opc_style.providers.macos_vision_subjects import MacOSVisionSubjectSegmenter
from opc_style.providers.openai_images import OpenAIImageGenerator, RealImageBudget
from opc_style.repositories import ProductImageRepository
from opc_style.schemas import (
    ImageGenerationCandidateResponse,
    ImageGenerationOutputRequest,
    ImageGenerationOutputResponse,
    ImageOutputStatusResponse,
)
from opc_style.services.identity_locked_jobs import (
    IdentityLockedJobService,
    require_identity_locked_paid_style_ready,
)
from opc_style.services.image_jobs import (
    ImageGenerationInProgressError,
    ImageGenerationOutcomeUncertainError,
    ImageGenerationOutput,
    ImageJobService,
    ImageOutputNotFoundError,
    ImageSourceSelection,
)
from opc_style.services.image_reviews import derive_review_candidate_states
from opc_style.services.real_photos import (
    AssetContentStore,
    AssetContentStoreError,
    RealPhotoOutputNotFoundError,
    RealPhotoOutputOutcomeUncertainError,
    RealPhotoOutputService,
)
from opc_style.services.real_pilot_authorization import (
    RealPilotAuthorizationCapability,
)
from opc_style.services.subject_seals import (
    ProductSubjectSealNotFoundError,
    ProductSubjectSealService,
    SubjectSealContentStore,
)
from opc_style.settings import RealImageProviderSettings

router = APIRouter(prefix="/v1/image-outputs", tags=["image-outputs"])
SessionDependency = Annotated[Session, Depends(get_session)]
ContentStoreDependency = Annotated[AssetContentStore, Depends(get_asset_content_store)]


def get_image_generator() -> ImageGenerator:
    """Return the startup-pinned generator without per-request provider selection."""
    return _runtime_image_generator()


def build_image_generator(
    settings: RealImageProviderSettings,
    *,
    transport: httpx.Client | None = None,
) -> ImageGenerator:
    """Build exactly one fail-closed adapter from trusted startup settings."""
    if not settings.enabled:
        return DeterministicLocalImageGenerator()
    return OpenAIImageGenerator(
        settings,
        transport=transport or httpx.Client(),
        budget=RealImageBudget(Decimal(settings.max_run_usd)),
    )


@lru_cache(maxsize=1)
def _runtime_image_generator() -> ImageGenerator:
    return build_image_generator(RealImageProviderSettings())


GeneratorDependency = Annotated[ImageGenerator, Depends(get_image_generator)]


def get_identity_subject_segmenter() -> SubjectSegmenter:
    """Return the local fail-closed foreground extractor used for real uploads."""
    return MacOSVisionSubjectSegmenter()


def get_background_generator() -> BackgroundGenerator:
    """Return the startup-pinned empty-background generator."""
    return _runtime_background_generator()


def build_background_generator(
    settings: RealImageProviderSettings,
    *,
    transport: httpx.Client | None = None,
) -> BackgroundGenerator:
    """Use the paid adapter only when startup has explicit key and budget authority."""
    if not settings.enabled:
        return DeterministicLocalBackgroundGenerator()
    return OpenAIBackgroundGenerator(
        settings,
        transport=transport or httpx.Client(),
        budget=RealImageBudget(Decimal(settings.max_run_usd)),
    )


@lru_cache(maxsize=1)
def _runtime_background_generator() -> BackgroundGenerator:
    return build_background_generator(RealImageProviderSettings())


def get_background_safety_checker() -> BackgroundSafetyChecker:
    """Return the local fail-closed background safety boundary."""
    return DeterministicBackgroundSafetyChecker()


SubjectSegmenterDependency = Annotated[
    SubjectSegmenter, Depends(get_identity_subject_segmenter)
]
BackgroundGeneratorDependency = Annotated[
    BackgroundGenerator, Depends(get_background_generator)
]
BackgroundSafetyDependency = Annotated[
    BackgroundSafetyChecker, Depends(get_background_safety_checker)
]


def _identity_locked_manifest(
    customer_id: UUID,
    request: ImageGenerationOutputRequest,
    generator: BackgroundGenerator,
) -> str:
    settings = RealImageProviderSettings()
    payload = {
        "customer_id": str(customer_id),
        "source": request.sources[0].model_dump(mode="json") if request.sources else None,
        "scene_style_id": str(request.scene_style_id),
        "style_card_version_id": str(request.style_card_version_id),
        "slot_key": request.slot_key,
        "subject_seal_id": str(request.subject_seal_id) if request.subject_seal_id else None,
        "output_ratio": request.output_ratio,
        "idempotency_key": request.idempotency_key,
        "adapter_version": getattr(generator, "adapter_version", "unknown"),
        "model": settings.model if settings.enabled else "local-synthetic",
        "candidate_count": 3,
        "maximum_usd": str(settings.max_run_usd if settings.enabled else Decimal(0)),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return sha256(canonical).hexdigest()


@router.post("/product-style/identity-locked/preview", response_model=None)
def preview_identity_locked_product_style_output(
    request: ImageGenerationOutputRequest,
    customer_id: CustomerId,
    session: SessionDependency,
    content_store: ContentStoreDependency,
    background_generator: BackgroundGeneratorDependency,
) -> dict[str, object] | JSONResponse:
    """Quote one exact identity-locked request without creating rows or calling a model."""
    if len(request.sources) != 1:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT)
    settings = RealImageProviderSettings()
    real_enabled = isinstance(background_generator, OpenAIBackgroundGenerator)
    if real_enabled and request.subject_seal_id is None:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "error": {
                    "code": "subject_mask_confirmation_required",
                    "retryable": False,
                }
            },
        )
    if real_enabled:
        subject_seal_id = request.subject_seal_id
        if subject_seal_id is None:  # narrowed above; kept fail-closed for typing.
            raise HTTPException(status_code=status.HTTP_409_CONFLICT)
        try:
            ProductSubjectSealService(
                session, cast(SubjectSealContentStore, content_store)
            ).require_reviewed(
                customer_id,
                subject_seal_id,
                asset_id=request.sources[0].asset_id,
            )
        except (DomainRuleError, ProductSubjectSealNotFoundError) as error:
            code = getattr(error, "code", "subject_seal_not_found")
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content={"error": {"code": code, "retryable": False}},
            )
        scene = session.scalar(
            select(SceneStyle).where(
                SceneStyle.customer_id == customer_id,
                SceneStyle.id == request.scene_style_id,
            )
        )
        version = session.scalar(
            select(StyleCardVersion).where(
                StyleCardVersion.customer_id == customer_id,
                StyleCardVersion.id == request.style_card_version_id,
            )
        )
        if scene is None or version is None or scene.style_card_id != version.style_card_id:
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content={
                    "error": {
                        "code": "identity_locked_input_not_found",
                        "retryable": False,
                    }
                },
            )
        try:
            require_identity_locked_paid_style_ready(
                session,
                customer_id=customer_id,
                scene=scene,
                version=version,
                slot_key=request.slot_key,
            )
        except DomainRuleError as error:
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content={"error": {"code": error.code, "retryable": False}},
            )
    return {
        "generation_mode": "real_background_local_composite" if real_enabled else "synthetic_test_only",
        "model": settings.model if real_enabled else "local-synthetic",
        "candidate_count": 3,
        "maximum_usd": str(settings.max_run_usd if real_enabled else Decimal(0)),
        "quote_kind": (
            "local_authorization_cap_not_provider_invoice_guarantee"
            if real_enabled
            else "no_provider_charge"
        ),
        "automatic_retry": False,
        "manifest_sha256": _identity_locked_manifest(
            customer_id, request, background_generator
        ),
        "sends_product_image_to_provider": False,
        "publishable": False,
    }


@router.post(
    "/product-style",
    response_model=ImageGenerationOutputResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_product_style_output(
    request: ImageGenerationOutputRequest,
    customer_id: CustomerId,
    session: SessionDependency,
    content_store: ContentStoreDependency,
    generator: GeneratorDependency,
    response: Response,
) -> ImageGenerationOutputResponse | JSONResponse:
    """Create one non-publishable style job for one confirmed product."""
    return _create_output(
        request,
        customer_id,
        session,
        content_store,
        generator,
        response,
        output_type="product_style",
    )


@router.post(
    "/product-style/identity-locked",
    response_model=ImageGenerationOutputResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_identity_locked_product_style_output(
    request: ImageGenerationOutputRequest,
    http_request: Request,
    customer_id: CustomerId,
    session: SessionDependency,
    content_store: ContentStoreDependency,
    segmenter: SubjectSegmenterDependency,
    background_generator: BackgroundGeneratorDependency,
    background_safety_checker: BackgroundSafetyDependency,
    response: Response,
    x_confirmed_maximum_usd: Annotated[str | None, Header()] = None,
    x_confirmed_manifest_sha256: Annotated[str | None, Header()] = None,
) -> ImageGenerationOutputResponse | JSONResponse:
    """Compose one confirmed product into three empty local backgrounds."""
    if len(request.sources) != 1:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT)
    if isinstance(background_generator, OpenAIBackgroundGenerator):
        if request.subject_seal_id is None:
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content={
                    "error": {
                        "code": "subject_mask_confirmation_required",
                        "retryable": False,
                    }
                },
            )
        settings = RealImageProviderSettings()
        expected_manifest = _identity_locked_manifest(
            customer_id, request, background_generator
        )
        if (
            x_confirmed_maximum_usd != str(settings.max_run_usd)
            or x_confirmed_manifest_sha256 != expected_manifest
        ):
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content={
                    "error": {
                        "code": "identity_locked_cost_confirmation_required",
                        "retryable": False,
                    }
                },
            )
    try:
        result = IdentityLockedJobService(
            session,
            cast(Any, content_store),
            segmenter=segmenter,
            background_generator=background_generator,
            background_safety_checker=background_safety_checker,
        ).create(
            customer_id,
            asset_id=request.sources[0].asset_id,
            scene_style_id=request.scene_style_id,
            style_card_version_id=request.style_card_version_id,
            output_ratio=request.output_ratio,
            idempotency_key=request.idempotency_key,
            subject_seal_id=request.subject_seal_id,
            slot_key=request.slot_key,
        )
    except ProductSubjectSealNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from error
    except DomainRuleError as error:
        if error.code == "identity_locked_input_not_found":
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from error
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"error": {"code": error.code, "retryable": False}},
        )
    if not result.created:
        response.status_code = status.HTTP_200_OK
    return _generation_response(
        result, preview_secret=http_request.app.state.identity_content_secret
    )


@router.post(
    "/scene",
    response_model=ImageGenerationOutputResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_scene_output(
    request: ImageGenerationOutputRequest,
    customer_id: CustomerId,
    session: SessionDependency,
    content_store: ContentStoreDependency,
    generator: GeneratorDependency,
    response: Response,
) -> ImageGenerationOutputResponse | JSONResponse:
    """Create one synthetic scene-image job for exactly one selected product."""
    return _create_output(
        request,
        customer_id,
        session,
        content_store,
        generator,
        response,
        output_type="scene",
    )


@router.post(
    "/wear",
    response_model=ImageGenerationOutputResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_wear_output(
    request: ImageGenerationOutputRequest,
    customer_id: CustomerId,
    session: SessionDependency,
    content_store: ContentStoreDependency,
    generator: GeneratorDependency,
    response: Response,
) -> ImageGenerationOutputResponse | JSONResponse:
    """Create three review-gated on-hand or wearing candidates for one product."""
    return _create_output(
        request,
        customer_id,
        session,
        content_store,
        generator,
        response,
        output_type="wear",
    )


@router.post(
    "/multi-product",
    response_model=ImageGenerationOutputResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_multi_product_output(
    request: ImageGenerationOutputRequest,
    customer_id: CustomerId,
    session: SessionDependency,
    content_store: ContentStoreDependency,
    generator: GeneratorDependency,
    response: Response,
) -> ImageGenerationOutputResponse | JSONResponse:
    """Create one synthetic composition job for two to five selected products."""
    return _create_output(
        request,
        customer_id,
        session,
        content_store,
        generator,
        response,
        output_type="multi_product",
    )


@router.get("/{job_id}/candidates/{candidate_id}/content")
def get_identity_locked_candidate_content(
    job_id: UUID,
    candidate_id: UUID,
    request: Request,
    session: SessionDependency,
    content_store: ContentStoreDependency,
    access: str = "",
    x_customer_id: Annotated[str | None, Header(alias="X-Customer-Id")] = None,
) -> Response:
    """Stream an attested customer-owned candidate without exposing its path."""
    customer_id = _signed_or_header_customer(
        request,
        lineage_id=job_id,
        candidate_id=candidate_id,
        access=access,
        x_customer_id=x_customer_id,
    )
    repository = ProductImageRepository(session)
    job = repository.get_output_job(customer_id, job_id)
    candidate = repository.get_candidate(customer_id, candidate_id)
    if (
        job is None
        or job.subject_seal_id is None
        or candidate is None
        or candidate.output_job_id != job.id
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    try:
        content = content_store.read_derivative(
            customer_id, job.id, candidate.sequence_number, candidate.content_sha256
        )
    except AssetContentStoreError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from error
    return Response(
        content=content,
        media_type=candidate.mime_type,
        headers={
            "Cache-Control": "private, no-store",
            "X-Synthetic-Artifact": "true",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/{job_id}/original-content")
def get_identity_locked_original_content(
    job_id: UUID,
    request: Request,
    session: SessionDependency,
    content_store: ContentStoreDependency,
    source: UUID | None = None,
    access: str = "",
    x_customer_id: Annotated[str | None, Header(alias="X-Customer-Id")] = None,
) -> Response:
    """Stream the verified source photo for an identity-locked job."""
    customer_id = _signed_or_header_customer(
        request,
        lineage_id=job_id,
        candidate_id=source,
        access=access,
        x_customer_id=x_customer_id,
    )
    repository = ProductImageRepository(session)
    job = repository.get_output_job(customer_id, job_id)
    if job is None or job.subject_seal_id is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    source_ids = job.request_metadata.get("source_asset_ids")
    if not isinstance(source_ids, list) or len(source_ids) != 1:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    try:
        asset_id = UUID(str(source_ids[0]))
    except (TypeError, ValueError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None
    asset = repository.get_asset(customer_id, asset_id)
    if asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    try:
        content = content_store.read_original(
            customer_id, asset.id, asset.content_sha256
        )
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


@router.get(
    "/{job_id}",
    response_model=ImageOutputStatusResponse,
)
def get_image_output(
    job_id: UUID,
    request: Request,
    customer_id: CustomerId,
    session: SessionDependency,
    content_store: ContentStoreDependency,
    generator: GeneratorDependency,
) -> ImageOutputStatusResponse | JSONResponse:
    """Return any supported customer-owned image job without leaking foreign jobs."""
    job = ProductImageRepository(session).get_output_job(customer_id, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if job.subject_seal_id is not None:
        candidates = tuple(
            ProductImageRepository(session).list_candidates(customer_id, job.id)
        )
        if any(
            not content_store.verify_derivative(
                customer_id,
                job.id,
                candidate.sequence_number,
                candidate.content_sha256,
            )
            for candidate in candidates
        ):
            return _retryable_error("image_generation_outcome_uncertain")
        return _generation_response(
            ImageGenerationOutput(job, candidates, created=False),
            preview_secret=request.app.state.identity_content_secret,
        )
    if job.output_type in {"scene", "multi_product", "product_style", "wear"}:
        try:
            generation_result = ImageJobService(session, content_store, generator).get(
                customer_id, job_id
            )
            repository = ProductImageRepository(session)
            check_sets = tuple(
                repository.list_authenticity_checks(customer_id, candidate.id)
                for candidate in generation_result.candidates
            )
            batch_state = derive_candidate_batch_state(
                generation_result.job.status, check_sets
            )
            states = derive_review_candidate_states(
                repository,
                customer_id,
                generation_result.job,
                generation_result.candidates,
            ) or {
                candidate.id: batch_state for candidate in generation_result.candidates
            }
            return _generation_response(generation_result, candidate_states=states)
        except ImageOutputNotFoundError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from error
        except ImageGenerationOutcomeUncertainError:
            return _retryable_error("image_generation_outcome_uncertain")
    if job.output_type == "real_photo":
        try:
            real_photo_result = RealPhotoOutputService(session, content_store).get(
                customer_id, job_id
            )
            repository = ProductImageRepository(session)
            check_sets = tuple(
                repository.list_authenticity_checks(customer_id, candidate.id)
                for candidate in real_photo_result.candidates
            )
            batch_state = derive_candidate_batch_state(
                real_photo_result.job.status, check_sets
            )
            states = derive_review_candidate_states(
                repository,
                customer_id,
                real_photo_result.job,
                real_photo_result.candidates,
            ) or {
                candidate.id: batch_state for candidate in real_photo_result.candidates
            }
            return real_photo_output_response(
                real_photo_result, candidate_states=states
            )
        except RealPhotoOutputNotFoundError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from error
        except RealPhotoOutputOutcomeUncertainError:
            return _retryable_error("image_generation_outcome_uncertain")
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)


def _create_output(
    request: ImageGenerationOutputRequest,
    customer_id: UUID,
    session: Session,
    content_store: AssetContentStore,
    generator: ImageGenerator,
    response: Response,
    *,
    output_type: str,
    real_pilot_authorization: RealPilotAuthorizationCapability | None = None,
) -> ImageGenerationOutputResponse | JSONResponse:
    try:
        result = ImageJobService(
            session,
            content_store,
            generator,
            real_pilot_authorization=real_pilot_authorization,
            enforce_real_pilot_boundary=True,
        ).create(
            customer_id,
            output_type=output_type,
            sources=tuple(
                ImageSourceSelection(
                    product_id=source.product_id, asset_id=source.asset_id
                )
                for source in request.sources
            ),
            scene_style_id=request.scene_style_id,
            style_card_version_id=request.style_card_version_id,
            output_ratio=request.output_ratio,
            idempotency_key=request.idempotency_key,
            slot_key=request.slot_key,
            product_identity_card_version_id=(
                request.product_identity_card_version_id
            ),
            product_identity_card_sha256=request.product_identity_card_sha256,
            product_identity_card=request.product_identity_card,
            wear_presentation=(
                request.wear_presentation.model_dump(mode="json")
                if output_type == "wear" and request.wear_presentation is not None
                else None
            ),
        )
    except ImageOutputNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from error
    except ImageGenerationOutcomeUncertainError:
        return _retryable_error("image_generation_outcome_uncertain")
    except ImageGenerationInProgressError:
        return _retryable_error("image_generation_in_progress")
    except DomainRuleError as error:
        if error.code in {
            "style_slot_seal_mismatch",
            "style_reference_seal_mismatch",
            "style_reference_store_mismatch",
            "style_reference_content_unavailable",
            "style_reference_content_mismatch",
        }:
            # These require service/storage recovery, not a different user choice.
            # Keep the underlying integrity and namespace details private.
            return _retryable_error("image_generation_preflight_unavailable")
        if error.code not in {
            "scene_requires_one_product",
            "wear_requires_one_product",
            "product_style_requires_one_product",
            "multi_product_requires_2_to_5_distinct_products",
            "organizer_confirmation_missing",
            "transform_permission_unknown",
            "transform_permission_denied",
            "style_or_scene_inactive",
            "image_output_idempotency_conflict",
            "unsupported_image_generator",
            "real_image_provider_disabled",
            "real_image_pilot_authorization_required",
            "real_pilot_customer_upload_style_required",
            "confirmed_product_identity_card_required",
            "generation_source_identity_count",
            "style_slot_unavailable",
            "style_slot_ratio_mismatch",
            "style_reference_unconfirmed",
            "style_reference_permission_unavailable",
            "style_reference_limit_exceeded",
        }:
            raise
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content=jsonable_encoder(
                {"error": {"code": error.code, "retryable": False}}
            ),
        )
    if not result.created:
        response.status_code = status.HTTP_200_OK
    return _generation_response(result)


def _retryable_error(code: str) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"error": {"code": code, "retryable": True}},
    )


def _generation_response(
    result: ImageGenerationOutput,
    *,
    candidate_states: Mapping[UUID, str] | None = None,
    preview_secret: bytes | None = None,
) -> ImageGenerationOutputResponse:
    job = result.job
    metadata = job.request_metadata
    product_ids = cast(list[str], metadata["source_product_ids"])
    asset_ids = [
        UUID(str(value)) for value in cast(list[str], metadata["source_asset_ids"])
    ]
    style_card_version_id = UUID(str(metadata["style_card_version_id"]))
    main_style_value = metadata.get("main_style_card_version_id")
    main_style_card_version_id = (
        UUID(str(main_style_value)) if main_style_value else None
    )
    scene_style_id = UUID(str(metadata["scene_style_id"]))
    signed_candidates = (
        {
            candidate.id: candidate_access_token(
                preview_secret, job.customer_id, job.id, candidate.id
            )
            for candidate in result.candidates
        }
        if preview_secret is not None and job.subject_seal_id is not None
        else {}
    )
    first_candidate = result.candidates[0] if result.candidates else None
    original_content_url = None
    if first_candidate is not None and first_candidate.id in signed_candidates:
        original_content_url = (
            f"/v1/image-outputs/{job.id}/original-content?source={first_candidate.id}"
            f"&access={signed_candidates[first_candidate.id]}"
        )
    return ImageGenerationOutputResponse(
        id=job.id,
        output_type=cast(
            Literal["scene", "multi_product", "product_style", "wear"],
            job.output_type,
        ),
        output_ratio=job.output_ratio,
        adapter_version=job.adapter_version,
        generation_mode=str(metadata["generation_mode"]),
        synthetic_test_only=metadata["synthetic_test_only"] is True,
        publishable=metadata["publishable"] is True,
        status=job.status,
        failure_reason=job.failure_reason,
        source_product_ids=product_ids,
        source_asset_ids=asset_ids,
        style_card_version_id=style_card_version_id,
        main_style_card_version_id=main_style_card_version_id,
        scene_style_id=scene_style_id,
        subject_lock_state=(
            "locked" if job.identity_lock_phase == "candidates_attested" else None
        ),
        subject_seal_id=job.subject_seal_id,
        original_content_url=original_content_url,
        wear_presentation=cast(
            dict[str, object] | None, metadata.get("wear_presentation")
        ),
        presentation_origin=(
            "system_virtual_model" if job.output_type == "wear" else None
        ),
        real_wear_photo=False if job.output_type == "wear" else None,
        candidates=[
            ImageGenerationCandidateResponse(
                id=candidate.id,
                output_job_id=job.id,
                sequence_number=candidate.sequence_number,
                identity=candidate.identity,
                candidate_state=(
                    candidate_states.get(candidate.id, "pending_authenticity_check")
                    if candidate_states is not None
                    else "pending_authenticity_check"
                ),
                content_sha256=candidate.content_sha256,
                mime_type=candidate.mime_type,
                width=candidate.width,
                height=candidate.height,
                adapter_version=candidate.adapter_version,
                generation_mode=str(candidate.artifact_metadata["generation_mode"]),
                synthetic_test_only=(
                    candidate.artifact_metadata["synthetic_test_only"] is True
                ),
                publishable=candidate.artifact_metadata["publishable"] is True,
                source_product_ids=[
                    str(value)
                    for value in cast(
                        list[str], candidate.artifact_metadata["source_product_ids"]
                    )
                ],
                source_asset_ids=[
                    UUID(str(value))
                    for value in cast(
                        list[str], candidate.artifact_metadata["source_asset_ids"]
                    )
                ],
                style_card_version_id=UUID(
                    str(candidate.artifact_metadata["style_card_version_id"])
                ),
                main_style_card_version_id=(
                    UUID(str(candidate.artifact_metadata["main_style_card_version_id"]))
                    if candidate.artifact_metadata["main_style_card_version_id"]
                    else None
                ),
                scene_style_id=UUID(str(candidate.artifact_metadata["scene_style_id"])),
                protected_pixels_match=(
                    candidate.artifact_metadata.get("identity_lock_code")
                    == "identity_lock_passed"
                    if "identity_lock_code" in candidate.artifact_metadata
                    else None
                ),
                content_url=(
                    f"/v1/image-outputs/{job.id}/candidates/{candidate.id}/content"
                    f"?access={signed_candidates[candidate.id]}"
                    if candidate.id in signed_candidates
                    else None
                ),
            )
            for candidate in result.candidates
        ],
        created_at=_as_utc(job.created_at),
        updated_at=_as_utc(job.updated_at),
    )


def _signed_or_header_customer(
    request: Request,
    *,
    lineage_id: UUID,
    candidate_id: UUID | None,
    access: str,
    x_customer_id: str | None,
) -> UUID:
    header_customer: UUID | None = None
    if x_customer_id is not None:
        try:
            header_customer = UUID(x_customer_id)
        except ValueError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from error
    signed_customer = (
        resolve_candidate_customer(
            request.app.state.identity_content_secret,
            lineage_id,
            candidate_id,
            access,
        )
        if access and candidate_id is not None
        else None
    )
    customer_id = signed_customer or header_customer
    if customer_id is None or (
        signed_customer is not None
        and header_customer is not None
        and signed_customer != header_customer
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return customer_id


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
