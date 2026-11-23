"""Customer-isolated API routes for main and scene style-card versions."""

from dataclasses import asdict
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from opc_style.api.dependencies import CustomerId, get_session
from opc_style.api.routes_real_photos import get_asset_content_store
from opc_style.domain.analyzer import VisualStyleAnalyzer
from opc_style.domain.errors import DomainRuleError
from opc_style.schemas import (
    HistoricalStyleGroupsConfirmRequest,
    MainStyleCardEditRequest,
    MainStyleCardResponse,
    SceneStyleCardCreateRequest,
    SceneStyleCardEditRequest,
    SceneStyleCardResponse,
)
from opc_style.services.style_reference_packs import StyleReferencePackService
from opc_style.services.styles import (
    HistoricalStyleConfirmationService,
    InsufficientStyleSamplesError,
    MainStyleCardNotFoundError,
    MainStyleCardService,
    MainStyleCardSnapshot,
    SceneStyleCardNotFoundError,
    SceneStyleCardService,
    SceneStyleCardSnapshot,
)

router = APIRouter(prefix="/v1/style-cards", tags=["style-cards"])
SessionDependency = Annotated[Session, Depends(get_session)]


@router.post("/main/propose")
def propose_main_style_groups(
    request: Request, customer_id: CustomerId, session: SessionDependency
) -> JSONResponse:
    """Suggest from verified pixels using an explicitly app-configured analyzer."""
    analyzer = cast(
        VisualStyleAnalyzer | None,
        getattr(request.app.state, "style_visual_analyzer", None),
    )
    try:
        proposal = MainStyleCardService(
            session,
            content_store=get_asset_content_store(request),
            visual_analyzer=analyzer,
        ).propose(customer_id)
    except DomainRuleError as error:
        return _style_domain_error_response(error)
    return JSONResponse(content=jsonable_encoder(asdict(proposal)))


@router.post(
    "/main/generate",
    response_model=MainStyleCardResponse,
    status_code=status.HTTP_201_CREATED,
)
def generate_main_style_card(
    customer_id: CustomerId, session: SessionDependency
) -> MainStyleCardResponse | JSONResponse:
    """Append a local-test analysis version from active authorized samples."""
    try:
        snapshot = MainStyleCardService(session).generate(customer_id)
    except InsufficientStyleSamplesError as error:
        return _error_response(
            "insufficient_style_samples",
            status.HTTP_409_CONFLICT,
            active_sample_count=error.readiness.active_sample_count,
            samples_needed=error.readiness.samples_needed,
        )
    except DomainRuleError as error:
        return _style_domain_error_response(error)
    return _style_card_response(snapshot)


@router.patch("/main", response_model=MainStyleCardResponse)
def edit_main_style_card(
    request: MainStyleCardEditRequest,
    customer_id: CustomerId,
    session: SessionDependency,
) -> MainStyleCardResponse | JSONResponse:
    """Create a customer-edit revision without updating historical rows."""
    try:
        snapshot = MainStyleCardService(session).edit(
            customer_id,
            request.model_dump(exclude_none=True),
        )
    except MainStyleCardNotFoundError:
        return _error_response("main_style_card_not_found", status.HTTP_404_NOT_FOUND)
    except DomainRuleError as error:
        return _style_domain_error_response(error)
    return _style_card_response(snapshot)


@router.get("/main", response_model=MainStyleCardResponse)
def get_main_style_card(
    customer_id: CustomerId, session: SessionDependency
) -> MainStyleCardResponse | JSONResponse:
    """Return the requesting customer's current active main-card revision."""
    try:
        snapshot = MainStyleCardService(session).get(customer_id)
    except MainStyleCardNotFoundError:
        return _error_response("main_style_card_not_found", status.HTTP_404_NOT_FOUND)
    return _style_card_response(snapshot)


@router.post(
    "/scenes",
    response_model=SceneStyleCardResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_scene_style_card(
    request: SceneStyleCardCreateRequest,
    customer_id: CustomerId,
    session: SessionDependency,
) -> SceneStyleCardResponse | JSONResponse:
    """Create a scene pinned to the customer's current main-card version."""
    payload = request.model_dump(exclude_none=True)
    name = payload.pop("name")
    try:
        snapshot = SceneStyleCardService(session).create(customer_id, name, payload)
    except MainStyleCardNotFoundError:
        return _error_response("main_style_card_not_found", status.HTTP_404_NOT_FOUND)
    except DomainRuleError as error:
        return _style_domain_error_response(error)
    return _scene_style_card_response(snapshot)


@router.post(
    "/scenes/confirm",
    response_model=list[SceneStyleCardResponse],
    status_code=status.HTTP_201_CREATED,
)
def confirm_historical_styles(
    request: HistoricalStyleGroupsConfirmRequest,
    customer_id: CustomerId,
    session: SessionDependency,
) -> list[SceneStyleCardResponse] | JSONResponse:
    """Seal only the customer's explicitly confirmed historical groups and suites."""
    try:
        snapshots = HistoricalStyleConfirmationService(session).confirm(
            customer_id, request
        )
        for snapshot in snapshots:
            if snapshot.scene.style_origin == "customer_upload":
                try:
                    StyleReferencePackService(session).get_or_create(
                        customer_id, snapshot.version.id
                    )
                except DomainRuleError as error:
                    if error.code != "style_reference_pack_unavailable":
                        raise
    except DomainRuleError as error:
        return _style_domain_error_response(error)
    return [_scene_style_card_response(snapshot) for snapshot in snapshots]


@router.get(
    "/scenes/{scene_id}/versions/{version_id}", response_model=SceneStyleCardResponse
)
def get_scene_style_version(
    scene_id: UUID,
    version_id: UUID,
    customer_id: CustomerId,
    session: SessionDependency,
) -> SceneStyleCardResponse | JSONResponse:
    try:
        snapshot = SceneStyleCardService(session).get_version(
            customer_id, scene_id, version_id
        )
    except SceneStyleCardNotFoundError:
        return _error_response("scene_style_card_not_found", status.HTTP_404_NOT_FOUND)
    except MainStyleCardNotFoundError:
        return _error_response("main_style_card_not_found", status.HTTP_404_NOT_FOUND)
    return _scene_style_card_response(snapshot)


@router.patch("/scenes/{scene_id}", response_model=SceneStyleCardResponse)
def edit_scene_style_card(
    scene_id: UUID,
    request: SceneStyleCardEditRequest,
    customer_id: CustomerId,
    session: SessionDependency,
) -> SceneStyleCardResponse | JSONResponse:
    """Append a customer-owned scene revision without changing historical rows."""
    try:
        snapshot = SceneStyleCardService(session).edit_scene(
            customer_id,
            scene_id,
            request.model_dump(exclude_unset=True),
        )
    except SceneStyleCardNotFoundError:
        return _error_response("scene_style_card_not_found", status.HTTP_404_NOT_FOUND)
    except MainStyleCardNotFoundError:
        return _error_response("main_style_card_not_found", status.HTTP_404_NOT_FOUND)
    except DomainRuleError as error:
        return _style_domain_error_response(error)
    return _scene_style_card_response(snapshot)


@router.post(
    "/scenes/{scene_id}/deactivate",
    response_model=SceneStyleCardResponse,
)
def deactivate_scene_style_card(
    scene_id: UUID,
    customer_id: CustomerId,
    session: SessionDependency,
) -> SceneStyleCardResponse | JSONResponse:
    """Free an active scene slot while preserving every immutable revision."""
    try:
        snapshot = SceneStyleCardService(session).deactivate(customer_id, scene_id)
    except SceneStyleCardNotFoundError:
        return _error_response("scene_style_card_not_found", status.HTTP_404_NOT_FOUND)
    except MainStyleCardNotFoundError:
        return _error_response("main_style_card_not_found", status.HTTP_404_NOT_FOUND)
    except DomainRuleError as error:
        return _style_domain_error_response(error)
    return _scene_style_card_response(snapshot)


@router.get("/scenes", response_model=list[SceneStyleCardResponse])
def list_scene_style_cards(
    customer_id: CustomerId,
    session: SessionDependency,
) -> list[SceneStyleCardResponse] | JSONResponse:
    """List only the requesting customer's active and inactive scene history."""
    try:
        snapshots = SceneStyleCardService(session).list(customer_id)
    except MainStyleCardNotFoundError:
        return _error_response("main_style_card_not_found", status.HTTP_404_NOT_FOUND)
    return [_scene_style_card_response(snapshot) for snapshot in snapshots]


def _style_card_response(snapshot: MainStyleCardSnapshot) -> MainStyleCardResponse:
    payload: dict[str, Any] = {
        "id": snapshot.card.id,
        "version_id": snapshot.version.id,
        "version": snapshot.version.version,
        "kind": snapshot.card.kind,
        "status": "active" if snapshot.card.active else "inactive",
        "created_at": snapshot.version.created_at,
    }
    payload.update(snapshot.version.content)
    return MainStyleCardResponse.model_validate(payload)


def _scene_style_card_response(
    snapshot: SceneStyleCardSnapshot,
) -> SceneStyleCardResponse:
    payload: dict[str, Any] = {
        "id": snapshot.card.id,
        "version_id": snapshot.version.id,
        "version": snapshot.version.version,
        "kind": snapshot.card.kind,
        "status": "active" if snapshot.scene.active else "inactive",
        "name": snapshot.version.content["name"],
        "main_style_card_version_id": snapshot.version.content[
            "main_style_card_version_id"
        ],
        "overrides": snapshot.version.content["overrides"],
        "created_at": snapshot.version.created_at,
    }
    payload.update(snapshot.resolved_content)
    return SceneStyleCardResponse.model_validate(payload)


def _error_response(code: str, status_code: int, **details: Any) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=jsonable_encoder({"error": {"code": code, **details}}),
    )


def _style_domain_error_response(error: DomainRuleError) -> JSONResponse:
    if error.code in {
        "historical_style_evidence_unavailable",
        "historical_style_origin_mismatch",
        "style_confirmation_conflict",
        "invalid_scene_field",
    }:
        return _error_response(error.code, status.HTTP_409_CONFLICT)
    if error.code.startswith("style_visual_analysis_"):
        return _error_response(error.code, status.HTTP_409_CONFLICT)
    if error.code in {
        "invalid_analyzer_evidence",
        "invalid_analyzer_style_content",
    }:
        return _error_response(error.code, status.HTTP_409_CONFLICT)
    if error.code in {"style_card_write_conflict", "style_samples_changed"}:
        return _error_response(
            error.code,
            status.HTTP_409_CONFLICT,
            retryable=True,
        )
    if error.code == "style_card_no_changes":
        return _error_response(error.code, status.HTTP_409_CONFLICT)
    if error.code in {"scene_limit_reached", "scene_name_conflict"}:
        return _error_response(error.code, status.HTTP_409_CONFLICT)
    raise error
