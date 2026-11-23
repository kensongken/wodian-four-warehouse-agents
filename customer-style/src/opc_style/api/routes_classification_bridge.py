"""Feature-gated loopback API for the local classification test bridge."""

from __future__ import annotations

import hmac
import ipaddress
from collections.abc import Iterator
from contextlib import contextmanager
from io import BytesIO
from typing import Annotated, Literal, cast
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Request,
    Response,
    status,
)
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session, sessionmaker

from opc_style.api.dependencies import get_session
from opc_style.api.routes_image_jobs import GeneratorDependency
from opc_style.api.routes_image_reviews import (
    image_review_response,
    product_image_view_response,
)
from opc_style.contracts.classification_handoff import parse_classification_handoff_v1
from opc_style.db import create_database_engine
from opc_style.domain.errors import DomainRuleError
from opc_style.domain.image_generator import DeterministicLocalImageGenerator
from opc_style.models import ImageOutputJob
from opc_style.schemas import (
    ImageReviewRequest,
    LocalApprovedAssociationResponse,
    LocalReviewCandidateResponse,
    LocalReviewDecisionResponse,
    LocalReviewJobResponse,
)
from opc_style.services.classification_bridge import (
    BridgeResult,
    ClassificationBridgeInProgressError,
    ClassificationBridgeNotFoundError,
    ClassificationBridgeOutcomeUncertainError,
    ClassificationBridgeService,
)
from opc_style.services.image_jobs import (
    ImageGenerationInProgressError,
    _forget_local_store,
    _remember_local_store,
)
from opc_style.services.image_reviews import (
    ImageReviewConflictError,
    ImageReviewNotFoundError,
    ImageReviewOutcomeUncertainError,
)
from opc_style.services.local_media_stage import LocalMediaStage
from opc_style.services.real_photos import (
    AssetContentStoreError,
    LocalSyntheticAssetStore,
)
from opc_style.services.review_sessions import ReviewSessionService
from opc_style.settings import LocalBridgeSettings

router = APIRouter(
    prefix="/v1/local-test/classification-bridge", tags=["local-test-bridge"]
)


def configure_classification_bridge(
    app: FastAPI, settings: LocalBridgeSettings, *, database_url: str
) -> None:
    """Attach local-only resources once after the enabled router is registered."""
    state = app.state
    asset_root = settings.local_classification_bridge_asset_root
    if asset_root is None:
        raise RuntimeError("local_classification_bridge_asset_root_required")
    store = LocalSyntheticAssetStore(asset_root, reopen=asset_root.exists())
    engine = create_database_engine(database_url)
    state.classification_bridge_settings = settings
    state.classification_bridge_stage = None
    state.classification_bridge_store = store
    state.classification_bridge_database_engine = engine
    state.session_factory = sessionmaker(
        bind=engine, expire_on_commit=False, class_=Session
    )
    for job_id in store.recovered_job_ids:
        _remember_local_store(job_id, store)


def close_classification_bridge(app: FastAPI) -> None:
    """Release local descriptors on application shutdown without side effects."""
    state = app.state
    stage = getattr(state, "classification_bridge_stage", None)
    store = getattr(state, "classification_bridge_store", None)
    engine = getattr(state, "classification_bridge_database_engine", None)
    if stage is not None:
        stage.close()
    if store is not None:
        for job_id in store.recovered_job_ids:
            _forget_local_store(job_id, store)
        store.close()
    if engine is not None:
        engine.dispose()


@router.post("/media", status_code=status.HTTP_201_CREATED, response_model=None)
async def stage_media(
    request: Request,
    x_photo_id: Annotated[UUID, Header(alias="X-Photo-Id")],
    x_content_sha256: Annotated[str, Header(alias="X-Content-Sha256")],
) -> dict[str, object] | JSONResponse:
    """Accept only decoded loopback bytes and return an opaque receipt."""
    try:
        _require_loopback(request)
        nonce_error = _require_ephemeral_nonce(request)
        if nonce_error is not None:
            return nonce_error
        receipt = _stage(request).stage(
            photo_id=x_photo_id,
            claimed_sha256=x_content_sha256,
            body=BytesIO(await request.body()),
        )
    except DomainRuleError as error:
        return _domain_error(error)
    return {
        "receipt_id": str(receipt.receipt_id),
        "photo_id": str(receipt.photo_id),
        "content_sha256": receipt.content_sha256,
        "mime_type": receipt.mime_type,
        "width": receipt.width,
        "height": receipt.height,
        "expires_at": receipt.expires_at.isoformat(),
    }


@router.post("/handoffs", status_code=status.HTTP_201_CREATED, response_model=None)
async def accept_handoff(
    request: Request,
    response: Response,
    session: Annotated[Session, Depends(get_session)],
    generator: GeneratorDependency,
) -> dict[str, object] | JSONResponse:
    """Parse canonical bytes, then consume receipt-bound media as one local unit."""
    try:
        _require_loopback(request)
        nonce_error = _require_ephemeral_nonce(request)
        if nonce_error is not None:
            return nonce_error
        handoff = parse_classification_handoff_v1(await request.body())
    except (TypeError, ValueError):
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "error": {"code": "classification_handoff_invalid", "retryable": False}
            },
        )
    except DomainRuleError as error:
        return _domain_error(error)
    try:
        result = ClassificationBridgeService(
            session,
            _stage(request),
            lambda: _store(request),
            image_generator=generator,
            authenticity_failure_product_id=(
                request.app.state.classification_bridge_settings.local_classification_bridge_authenticity_failure_product_id
            ),
        ).accept(handoff)
    except DomainRuleError as error:
        return _domain_error(error)
    except ClassificationBridgeOutcomeUncertainError:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "error": {
                    "code": "classification_handoff_cleanup_pending",
                    "retryable": True,
                }
            },
        )
    except ClassificationBridgeInProgressError:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "error": {
                    "code": "classification_handoff_in_progress",
                    "retryable": True,
                }
            },
        )
    except AssetContentStoreError:
        return _storage_error()
    if not result.created:
        response.status_code = status.HTTP_200_OK
    return _result_payload(result, session)


@router.get("/handoffs/{handoff_id}", response_model=None)
def get_handoff(
    handoff_id: UUID,
    request: Request,
    session: Annotated[Session, Depends(get_session)],
) -> dict[str, object] | JSONResponse:
    """Read only a completed local handoff receipt; no product media is returned."""
    try:
        _require_loopback(request)
        nonce_error = _require_ephemeral_nonce(request)
        if nonce_error is not None:
            return nonce_error
        result = ClassificationBridgeService(
            session,
            _stage(request),
            lambda: _store(request),
            authenticity_failure_product_id=(
                request.app.state.classification_bridge_settings.local_classification_bridge_authenticity_failure_product_id
            ),
        ).get(handoff_id)
    except DomainRuleError as error:
        return _domain_error(error)
    except ClassificationBridgeNotFoundError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from error
    return _result_payload(result, session)


def _review_nonce(request: Request) -> None:
    try:
        _require_loopback(request)
    except DomainRuleError as error:
        raise HTTPException(status_code=403, detail=error.code) from error
    nonce_error = _require_ephemeral_nonce(request)
    if nonce_error is not None:
        raise HTTPException(
            status_code=nonce_error.status_code,
            detail="local_bridge_nonce_required_or_invalid",
        )


ReviewNonce = Annotated[None, Depends(_review_nonce)]
SessionDependency = Annotated[Session, Depends(get_session)]


def _review_service(request: Request, session: Session) -> ReviewSessionService:
    return ReviewSessionService(
        session, _store(request), DeterministicLocalImageGenerator()
    )


@contextmanager
def _review_errors() -> Iterator[None]:
    try:
        yield
    except ImageReviewNotFoundError as error:
        raise HTTPException(status_code=404) from error
    except (ImageReviewConflictError, DomainRuleError) as error:
        raise HTTPException(status_code=409, detail=error.code) from error
    except ImageGenerationInProgressError as error:
        raise HTTPException(
            status_code=409, detail="review_generation_in_progress"
        ) from error
    except (ImageReviewOutcomeUncertainError, AssetContentStoreError) as error:
        raise HTTPException(
            status_code=503, detail="local_review_content_unavailable"
        ) from error


@router.get("/image-outputs/{job_id}", response_model=LocalReviewJobResponse)
def get_local_review_job(
    job_id: UUID, request: Request, _: ReviewNonce, session: SessionDependency
) -> LocalReviewJobResponse:
    with _review_errors():
        output = _review_service(request, session).get_job(job_id)
        row = output.session
        return LocalReviewJobResponse(
            job_id=output.job.id,
            handoff_id=output.handoff_id,
            output_type=output.job.output_type,
            job_status=output.job.status,
            status=row.status if row else output.job.status,
            session_id=row.id if row else None,
            current_job_id=row.current_job_id if row else output.job.id,
            regeneration_count=row.regeneration_count if row else 0,
            candidates=[
                LocalReviewCandidateResponse(
                    candidate_id=item.id,
                    sequence_number=item.sequence_number,
                    content_sha256=item.content_sha256,
                    mime_type=item.mime_type,
                    width=item.width,
                    height=item.height,
                    identity=item.identity,
                    content_url=_candidate_url(job_id, item.id),
                )
                for item in output.candidates
            ],
            source_product_ids=[asset.product_id for asset in output.source_assets],
            source_asset_ids=[asset.id for asset in output.source_assets],
            approved_candidate_id=output.approved_candidate_id,
            approved_associations=[
                LocalApprovedAssociationResponse(
                    product_id=item.product_id,
                    candidate_id=item.image_candidate_id,
                    asset_id=item.source_asset_id,
                )
                for item in output.approved_associations
            ],
        )


@router.get("/image-outputs/{job_id}/candidates/{candidate_id}/content")
def get_local_candidate_content(
    job_id: UUID,
    candidate_id: UUID,
    request: Request,
    _: ReviewNonce,
    session: SessionDependency,
) -> Response:
    with _review_errors():
        content, mime_type = _review_service(request, session).read_candidate(
            job_id, candidate_id
        )
        return Response(
            content=content,
            media_type=mime_type,
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )


@router.get("/assets/{asset_id}/content")
def get_local_asset_content(
    asset_id: UUID,
    job_id: UUID,
    request: Request,
    _: ReviewNonce,
    session: SessionDependency,
) -> Response:
    with _review_errors():
        content, mime_type = _review_service(request, session).read_real_asset(
            job_id, asset_id
        )
        return Response(
            content=content,
            media_type=mime_type,
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )


@router.post(
    "/image-outputs/{job_id}/candidates/{candidate_id}/review",
    response_model=LocalReviewDecisionResponse,
    response_model_exclude={"suite_state"},
    status_code=201,
)
def review_local_candidate(
    job_id: UUID,
    candidate_id: UUID,
    body: ImageReviewRequest,
    request: Request,
    response: Response,
    _: ReviewNonce,
    session: SessionDependency,
) -> LocalReviewDecisionResponse:
    with _review_errors():
        service = _review_service(request, session)
        output = service.get_job(job_id, open_review=False)
        if candidate_id not in {item.id for item in output.candidates}:
            raise ImageReviewNotFoundError
        output = service.get_job(job_id)
        if output.session is None:
            raise ImageReviewConflictError("review_job_not_awaiting_customer")
        result = service.decide(
            output.job.customer_id,
            output.session.id,
            candidate_id,
            decision=body.decision,
            reason=body.reason,
            idempotency_key=body.idempotency_key,
        )
        if not result.created:
            response.status_code = 200
        # The acknowledged intent stays historical even when a later tab has
        # advanced this session. Expose the live cursor separately for reload.
        outcome: Literal[
            "approved",
            "regenerated",
            "manual_cap_reached",
            "manual_generation_unavailable",
        ]
        if result.review.decision == "approved":
            outcome = "approved"
            outcome_status = "approved"
            outcome_count = result.round.round_number - 1
        elif result.next_job is None:
            outcome = "manual_cap_reached"
            outcome_status = "manual_attention_required"
            outcome_count = 3
        elif result.next_job.status in {
            "generation_failed",
            "authenticity_failed",
            "cleanup_pending",
        }:
            outcome = "manual_generation_unavailable"
            outcome_status = "manual_attention_required"
            outcome_count = result.round.round_number
        else:
            outcome = "regenerated"
            outcome_status = "awaiting_review"
            outcome_count = result.round.round_number
        return LocalReviewDecisionResponse(
            **image_review_response(result.image_review).model_dump(),
            session_id=result.session.id,
            next_job_id=result.next_job.id if result.next_job else None,
            regeneration_count=outcome_count,
            status=outcome_status,
            outcome=outcome,
            current_session_status=result.session.status,
            current_job_id=result.session.current_job_id,
            current_regeneration_count=result.session.regeneration_count,
        )


@router.get("/products/{product_id}/images")
def get_local_product_images(
    product_id: str,
    handoff_id: UUID,
    request: Request,
    _: ReviewNonce,
    session: SessionDependency,
    view: Literal["default", "real"] = "default",
) -> dict[str, object]:
    with _review_errors():
        result, asset_jobs = _review_service(request, session).get_product_images(
            handoff_id, product_id, view=view
        )
        payload = product_image_view_response(result).model_dump(mode="json")
        for item in payload["images"]:
            if (
                item["image_kind"] == "style_image"
                and result.style_candidate is not None
            ):
                item["content_url"] = _candidate_url(
                    result.style_candidate.output_job_id, result.style_candidate.id
                )
            else:
                item["content_url"] = (
                    f"{router.prefix}/assets/{item['asset_id']}/content?job_id={asset_jobs[UUID(item['asset_id'])]}"
                )
        return {**payload, "publishable": False}


def _candidate_url(job_id: UUID, candidate_id: UUID) -> str:
    return f"{router.prefix}/image-outputs/{job_id}/candidates/{candidate_id}/content"


def _require_loopback(request: Request) -> None:
    host = request.client.host if request.client is not None else ""
    try:
        is_loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        is_loopback = host == "localhost"
    if not is_loopback:
        raise DomainRuleError("local_classification_bridge_loopback_required")


def _require_ephemeral_nonce(request: Request) -> JSONResponse | None:
    """Check the startup-only bridge nonce without exposing it in an error."""

    settings: LocalBridgeSettings = request.app.state.classification_bridge_settings
    expected = settings.local_classification_bridge_nonce
    supplied = request.headers.get("X-Local-Classification-Bridge-Nonce")
    if expected is None or not supplied:
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={
                "error": {
                    "code": "local_classification_bridge_nonce_required",
                    "retryable": False,
                }
            },
        )
    if not hmac.compare_digest(supplied, expected.get_secret_value()):
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={
                "error": {
                    "code": "local_classification_bridge_nonce_invalid",
                    "retryable": False,
                }
            },
        )
    return None


def _stage(request: Request) -> LocalMediaStage:
    state = request.app.state
    stage = cast(LocalMediaStage | None, state.classification_bridge_stage)
    if stage is None:
        stage = LocalMediaStage(
            settings=state.classification_bridge_settings, caller_host="127.0.0.1"
        )
        state.classification_bridge_stage = stage
    return stage


def _store(request: Request) -> LocalSyntheticAssetStore:
    state = request.app.state
    store = cast(LocalSyntheticAssetStore | None, state.classification_bridge_store)
    if store is None:
        raise AssetContentStoreError("asset_content_store_unconfigured")
    return store


def _domain_error(error: DomainRuleError) -> JSONResponse:
    status_code = (
        status.HTTP_503_SERVICE_UNAVAILABLE
        if error.code
        in {
            "local_style_scene_pin_required",
            "local_multi_product_sources_required",
            "local_media_receipt_unavailable",
            "local_media_receipt_expired",
            "local_media_receipt_mismatch",
            "local_media_receipt_decode_mismatch",
        }
        else status.HTTP_403_FORBIDDEN
        if error.code == "local_classification_bridge_loopback_required"
        else status.HTTP_409_CONFLICT
    )
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": error.code, "retryable": status_code == 503}},
    )


def _storage_error() -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={
            "error": {
                "code": "local_classification_bridge_storage_unavailable",
                "retryable": True,
            }
        },
    )


def _result_payload(result: BridgeResult, session: Session) -> dict[str, object]:
    return {
        "handoff_id": str(result.handoff_id),
        "status": result.status,
        "output_job_ids": [str(value) for value in result.output_job_ids],
        "output_jobs": [
            {
                "job_id": str(job.id),
                "output_type": job.output_type,
                "status": job.status,
            }
            for job_id in result.output_job_ids
            if (job := session.get(ImageOutputJob, job_id)) is not None
        ],
        "created": result.created,
        "suite_preview_url": result.suite_preview_url,
        "publishable": False,
    }
