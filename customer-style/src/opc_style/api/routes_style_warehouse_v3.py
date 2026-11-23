"""HTTP boundary owned by the independent customer-style warehouse."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session, sessionmaker

from opc_style.contracts.style_warehouse_v3 import StyleWarehouseDeliveryV3
from opc_style.repositories_style_warehouse import (
    StyleWarehouseV3Conflict,
    StyleWarehouseV3Repository,
)
from opc_style.services.classification_media import (
    ClassificationMediaUnavailable,
    source_image_read_authorized,
    verify_source_images,
)
from opc_style.services.style_warehouse_v3 import (
    InvalidStyleWarehouseSignature,
    StyleWarehouseV3Service,
    verify_style_request_signature,
)

router = APIRouter(prefix="/internal/style-warehouse/v3")
AttemptResult = Literal["accepted", "rejected", "duplicate", "conflict", "error"]


def _error(code: str, status_code: int) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error_code": code})


def _sessions(request: Request) -> sessionmaker[Session]:
    factory = request.app.state.style_warehouse_v3_session_factory
    assert isinstance(factory, sessionmaker)
    return factory


def _tenant_uuid(value: str) -> UUID | None:
    try:
        return UUID(value)
    except ValueError:
        return None


def _record_attempt(
    request: Request,
    *,
    tenant_id: UUID | None,
    handoff_id: UUID | None,
    payload_sha256: str | None,
    result: AttemptResult,
    error_code: str | None,
) -> None:
    with _sessions(request)() as session:
        StyleWarehouseV3Repository(session).record_attempt(
            tenant_id=tenant_id,
            handoff_id=handoff_id,
            payload_sha256=payload_sha256,
            result=result,
            error_code=error_code,
        )


@router.post("/handoffs")
async def receive_handoff(request: Request) -> JSONResponse:
    body = await request.body()
    body_sha256 = hashlib.sha256(body).hexdigest()
    settings = request.app.state.style_warehouse_v3_settings
    source_system = request.headers.get("X-Source-System", "")
    tenant_header = request.headers.get("X-Tenant-Id", "")
    try:
        verify_style_request_signature(
            secret=settings.signing_secret.get_secret_value(),
            signature=request.headers.get("X-Signature", ""),
            timestamp=request.headers.get("X-Signature-Timestamp", ""),
            source_system=source_system,
            tenant_id=tenant_header,
            method=request.method,
            path=request.url.path,
            body=body,
            now=datetime.now(UTC),
            tolerance_seconds=settings.signature_tolerance_seconds,
        )
    except InvalidStyleWarehouseSignature:
        _record_attempt(
            request,
            tenant_id=None,
            handoff_id=None,
            payload_sha256=body_sha256,
            result="error",
            error_code="invalid_style_warehouse_signature",
        )
        return _error("invalid_style_warehouse_signature", 401)

    try:
        delivery = StyleWarehouseDeliveryV3.model_validate_json(body)
    except ValidationError:
        _record_attempt(
            request,
            tenant_id=_tenant_uuid(tenant_header),
            handoff_id=None,
            payload_sha256=body_sha256,
            result="error",
            error_code="style_payload_invalid",
        )
        return _error("style_payload_invalid", 422)
    if str(delivery.payload.tenant_id) != tenant_header:
        _record_attempt(
            request,
            tenant_id=_tenant_uuid(tenant_header),
            handoff_id=delivery.handoff_id,
            payload_sha256=delivery.payload_sha256,
            result="conflict",
            error_code="style_tenant_mismatch",
        )
        return _error("style_tenant_mismatch", 409)

    if settings.verify_source_images and source_image_read_authorized(delivery.payload):
        try:
            verified_images = await verify_source_images(
                delivery.payload,
                base_url=settings.classification_media_base_url or "",
                secret=settings.classification_media_secret.get_secret_value(),
                max_bytes=settings.media_max_bytes,
                transport=getattr(
                    request.app.state, "classification_media_transport", None
                ),
            )
            if set(verified_images) != {
                str(image.asset_id) for image in delivery.payload.images
            }:
                raise ClassificationMediaUnavailable(
                    "classification_media_integrity_failed"
                )
        except ClassificationMediaUnavailable:
            _record_attempt(
                request,
                tenant_id=delivery.payload.tenant_id,
                handoff_id=delivery.handoff_id,
                payload_sha256=delivery.payload_sha256,
                result="error",
                error_code="classification_media_unavailable_or_invalid",
            )
            return _error("classification_media_unavailable_or_invalid", 502)

    with _sessions(request)() as session:
        repository = StyleWarehouseV3Repository(session)
        try:
            receipt = StyleWarehouseV3Service(repository).receive(delivery)
        except StyleWarehouseV3Conflict:
            repository.record_attempt(
                tenant_id=delivery.payload.tenant_id,
                handoff_id=delivery.handoff_id,
                payload_sha256=delivery.payload_sha256,
                result="conflict",
                error_code="style_handoff_conflict",
            )
            return _error("style_handoff_conflict", 409)
        repository.record_attempt(
            tenant_id=delivery.payload.tenant_id,
            handoff_id=delivery.handoff_id,
            payload_sha256=delivery.payload_sha256,
            result=(
                "duplicate" if receipt.disposition == "duplicate" else receipt.decision
            ),
            error_code=receipt.rejection_code,
        )
    status_code = 202 if receipt.disposition == "accepted" else 200
    return JSONResponse(status_code=status_code, content=jsonable_encoder(receipt))


@router.get("/receipts/{receipt_id}")
def get_receipt(receipt_id: UUID, request: Request) -> JSONResponse:
    settings = request.app.state.style_warehouse_v3_settings
    tenant_header = request.headers.get("X-Tenant-Id", "")
    try:
        verify_style_request_signature(
            secret=settings.signing_secret.get_secret_value(),
            signature=request.headers.get("X-Signature", ""),
            timestamp=request.headers.get("X-Signature-Timestamp", ""),
            source_system=request.headers.get("X-Source-System", ""),
            tenant_id=tenant_header,
            method=request.method,
            path=request.url.path,
            body=b"",
            now=datetime.now(UTC),
            tolerance_seconds=settings.signature_tolerance_seconds,
        )
    except InvalidStyleWarehouseSignature:
        return _error("invalid_style_warehouse_signature", 401)
    tenant_id = _tenant_uuid(tenant_header)
    if tenant_id is None:
        return _error("invalid_style_warehouse_signature", 401)
    with _sessions(request)() as session:
        receipt = StyleWarehouseV3Repository(session).get_receipt(tenant_id, receipt_id)
    if receipt is None:
        return _error("style_receipt_not_found", 404)
    return JSONResponse(status_code=200, content=jsonable_encoder(receipt))
