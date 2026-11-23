import time
from collections.abc import Callable
from functools import lru_cache
from typing import Any, Literal, Protocol, cast
from uuid import UUID

import boto3  # type: ignore[import-untyped]
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from product_engine.api.dependencies import get_runtime_database, get_runtime_settings
from product_engine.api.routes.album import error_response
from product_engine.domain.errors import (
    InvalidSignature,
    ObjectValidationFailed,
    TemplateVersionConflict,
)
from product_engine.repositories.templates import TemplateRepository
from product_engine.security.signatures import verify_request_signature
from product_engine.services.template_registry import TemplateRecord, TemplateRegistry
from product_engine.storage.protocol import PresignedUpload
from product_engine.storage.s3 import S3Client, S3ObjectStorage

Clock = Callable[[], int]
Secret = bytes | Callable[[], bytes]
MimeType = Literal["image/jpeg", "image/png", "image/heic"]
ImageType = Literal["main", "detail", "wrist", "scale", "packaging"]


class TemplateUploadRequest(BaseModel):
    template_code: str = Field(
        min_length=3,
        max_length=64,
        pattern=r"^[A-Z0-9][A-Z0-9-]+$",
    )
    version: int = Field(ge=1)
    category: str = Field(min_length=1, max_length=64)
    image_type: ImageType
    mime_type: MimeType
    byte_size: int = Field(gt=0, le=20 * 1024 * 1024)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class TemplateFinalizeRequest(TemplateUploadRequest):
    object_key: str = Field(min_length=1, max_length=1024)


class UploadResponse(BaseModel):
    object_key: str
    url: str
    headers: dict[str, str]
    expires_in_seconds: int


class TemplateResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    template_code: str
    version: int
    category: str
    image_type: str
    object_key: str
    sha256: str
    mime_type: str
    byte_size: int
    status: str


class TemplateListResponse(BaseModel):
    items: list[TemplateResponse]


class Registry(Protocol):
    async def request_upload(
        self,
        *,
        tenant_id: UUID,
        template_code: str,
        version: int,
        mime_type: str,
        sha256: str,
    ) -> PresignedUpload: ...

    async def finalize(
        self,
        *,
        tenant_id: UUID,
        template_code: str,
        version: int,
        category: str,
        image_type: str,
        object_key: str,
        mime_type: str,
        byte_size: int,
        sha256: str,
    ) -> TemplateRecord: ...

    async def list_active(
        self,
        tenant_id: UUID,
        category: str,
        status: str,
    ) -> list[TemplateRecord]: ...


async def authenticate_admin(
    request: Request,
    secret: bytes,
    now: Clock,
) -> UUID | JSONResponse:
    source_system = request.headers.get("X-Source-System")
    tenant_value = request.headers.get("X-Tenant-Id")
    timestamp_value = request.headers.get("X-Signature-Timestamp")
    signature = request.headers.get("X-Signature")
    if source_system != "admin" or not tenant_value or not timestamp_value or not signature:
        return error_response(
            401,
            "authentication_failed",
            "模板管理请求缺少有效的管理员签名。",
            "请由已授权的管理系统重新发送请求。",
        )
    try:
        tenant_id = UUID(tenant_value)
        timestamp = int(timestamp_value)
    except (TypeError, ValueError):
        return error_response(
            401,
            "authentication_failed",
            "模板管理请求的认证信息格式无效。",
            "请重新生成当前商家的签名后发送。",
        )

    body = await request.body()
    try:
        verify_request_signature(
            secret=secret,
            timestamp=timestamp,
            now=now(),
            tolerance_seconds=300,
            source_system=source_system,
            tenant_id=tenant_id,
            method=request.method,
            path=request.url.path,
            body=body,
            supplied_signature=signature,
        )
    except InvalidSignature:
        return error_response(
            401,
            "authentication_failed",
            "模板管理请求的签名无效或已经过期。",
            "请使用当前时间重新生成管理员签名。",
        )
    return tenant_id


def parse_payload(request_type: type[BaseModel], body: bytes) -> BaseModel | JSONResponse:
    try:
        return request_type.model_validate_json(body)
    except ValidationError:
        return error_response(
            422,
            "invalid_template_payload",
            "实拍模板资料不符合接口要求。",
            "请检查模板编码、版本、图片类型、大小和 SHA-256 后重试。",
        )


def build_templates_router(
    registry: Registry,
    secret: Secret,
    *,
    now: Clock = lambda: int(time.time()),
) -> APIRouter:
    router = APIRouter(tags=["实拍模板"])

    @router.post(
        "/v1/templates/uploads",
        status_code=201,
        response_model=UploadResponse,
        summary="领取实拍模板上传地址",
        description="为当前商家和指定模板版本生成私有上传地址。",
    )
    async def create_upload(request: Request) -> UploadResponse | JSONResponse:
        resolved_secret = secret() if callable(secret) else secret
        tenant = await authenticate_admin(request, resolved_secret, now)
        if isinstance(tenant, JSONResponse):
            return tenant
        parsed = parse_payload(TemplateUploadRequest, await request.body())
        if isinstance(parsed, JSONResponse):
            return parsed
        payload = cast(TemplateUploadRequest, parsed)
        upload = await registry.request_upload(
            tenant_id=tenant,
            template_code=payload.template_code,
            version=payload.version,
            mime_type=payload.mime_type,
            sha256=payload.sha256,
        )
        return UploadResponse.model_validate(upload, from_attributes=True)

    @router.post(
        "/v1/templates",
        status_code=201,
        response_model=TemplateResponse,
        summary="发布实拍模板版本",
        description="核对私有存储中的图片信息后发布不可覆盖的模板版本。",
    )
    async def finalize(request: Request) -> TemplateResponse | JSONResponse:
        resolved_secret = secret() if callable(secret) else secret
        tenant = await authenticate_admin(request, resolved_secret, now)
        if isinstance(tenant, JSONResponse):
            return tenant
        parsed = parse_payload(TemplateFinalizeRequest, await request.body())
        if isinstance(parsed, JSONResponse):
            return parsed
        payload = cast(TemplateFinalizeRequest, parsed)
        try:
            record = await registry.finalize(
                tenant_id=tenant,
                **payload.model_dump(),
            )
        except TemplateVersionConflict:
            return error_response(
                409,
                "template_version_conflict",
                "这个实拍模板版本已经发布，不能覆盖。",
                "请保留当前版本，并把修正版发布为更高版本。",
            )
        except ObjectValidationFailed as error:
            error_code = str(error)
            if error_code == "object_outside_tenant_prefix":
                return error_response(
                    422,
                    error_code,
                    "模板图片不属于当前商家的指定模板版本目录。",
                    "请使用本次上传接口返回的图片路径后重试。",
                )
            return error_response(
                422,
                "template_object_validation_failed",
                "模板图片缺失，或格式、大小、摘要与登记资料不一致。",
                "请重新上传原图，并使用一致的图片资料发布。",
            )
        return TemplateResponse.model_validate(record)

    @router.get(
        "/v1/templates",
        response_model=TemplateListResponse,
        summary="查询可用实拍模板",
        description="只返回当前商家、指定品类且已生效的模板版本。",
    )
    async def list_templates(
        request: Request,
        category: str,
        status: str = "active",
    ) -> TemplateListResponse | JSONResponse:
        resolved_secret = secret() if callable(secret) else secret
        tenant = await authenticate_admin(request, resolved_secret, now)
        if isinstance(tenant, JSONResponse):
            return tenant
        if status != "active" or not category.strip():
            return error_response(
                422,
                "invalid_template_filter",
                "模板查询条件无效。",
                "请提供品类，并把状态设为 active。",
            )
        records = await registry.list_active(tenant, category, status)
        return TemplateListResponse(
            items=[TemplateResponse.model_validate(record) for record in records]
        )

    return router


@lru_cache
def production_storage() -> S3ObjectStorage:
    settings = get_runtime_settings()
    client = cast(
        S3Client,
        boto3.client(
            "s3",
            endpoint_url=str(settings.s3_endpoint_url),
            region_name=settings.s3_region,
            aws_access_key_id=settings.s3_access_key_id.get_secret_value(),
            aws_secret_access_key=settings.s3_secret_access_key.get_secret_value(),
        ),
    )
    return S3ObjectStorage(client, settings.s3_bucket)


class ProductionTemplateRegistry:
    async def request_upload(self, **values: Any) -> PresignedUpload:
        async with get_runtime_database().session() as session:
            registry = TemplateRegistry(production_storage(), TemplateRepository(session))
            return await registry.request_upload(**values)

    async def finalize(self, **values: Any) -> TemplateRecord:
        async with get_runtime_database().session() as session:
            registry = TemplateRegistry(production_storage(), TemplateRepository(session))
            return await registry.finalize(**values)

    async def list_active(
        self,
        tenant_id: UUID,
        category: str,
        status: str,
    ) -> list[TemplateRecord]:
        async with get_runtime_database().session() as session:
            registry = TemplateRegistry(production_storage(), TemplateRepository(session))
            return await registry.list_active(tenant_id, category, status)


def admin_signing_secret() -> bytes:
    return get_runtime_settings().admin_signing_secret.get_secret_value().encode()


templates_router = build_templates_router(
    ProductionTemplateRegistry(),
    admin_signing_secret,
)
