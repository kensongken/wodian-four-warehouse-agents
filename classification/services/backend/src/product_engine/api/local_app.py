# ruff: noqa: E501

import hashlib
import json
import mimetypes
import os
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Annotated, BinaryIO, Literal, Protocol, cast
from urllib.parse import quote
from uuid import UUID
from zipfile import ZIP_DEFLATED, ZipFile

import boto3  # type: ignore[import-untyped]
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from PIL import Image, UnidentifiedImageError
from pydantic import (
    BaseModel,
    ConfigDict,
    StrictBool,
    StrictInt,
    StringConstraints,
    ValidationError,
)

from product_engine.config import Settings
from product_engine.contracts.employee_formal_handoff import (
    EmployeeFormalHandoffReceipt,
    EmployeeFormalHandoffRequest,
)
from product_engine.contracts.material_evidence import (
    MaterialEvidenceInsufficientReceipt,
    MaterialEvidenceReceipt,
    MaterialEvidenceRevocationReceipt,
)
from product_engine.contracts.style_test_bridge import (
    CustomerTestId,
    ProductCategory,
    ProductForm,
    ProductId,
)
from product_engine.contracts.style_warehouse_v2 import (
    StyleTransformAuthorizationCommand,
)
from product_engine.db import Database
from product_engine.integrations.style_test_bridge_client import (
    ConfirmedLocalPhoto,
    ProductImage,
    ReviewDecision,
    StyleBridgeClient,
    StyleBridgeError,
)
from product_engine.repositories.local_templates import LocalTemplateRepository
from product_engine.repositories.material_evidence import (
    MaterialEvidenceActorContext,
    MaterialEvidenceFailure,
)
from product_engine.repositories.sales_handoffs import (
    PreparedSalesPreview,
    StoredSalesHandoff,
    WarehouseHandoffView,
)
from product_engine.repositories.style_handoffs import (
    PreparedStyleEligibility,
    StoredStyleHandoff,
    StyleTransformAuthorizationView,
)
from product_engine.services.album_review_inbox import (
    AlbumReviewImageContent,
    AlbumReviewInbox,
    AlbumReviewInboxFailure,
    AlbumReviewProduct,
    SqlAlbumReviewStore,
)
from product_engine.services.classification_confirmations import (
    ClassificationConfirmationFailure,
)
from product_engine.services.classification_import_provenance import (
    ClassificationImportProvenanceStore,
)
from product_engine.services.confirmed_product_content_package import (
    build_confirmed_product_content_package,
)
from product_engine.services.conservative_pricing import (
    ConservativePricingFailure,
    PriceAnchor,
    build_conservative_price_candidate,
)
from product_engine.services.employee_batch_promotions import (
    EmployeeBatchPromotionFailure,
    EmployeeBatchPromotionService,
)
from product_engine.services.employee_handoff_receipts import (
    EmployeeHandoffReceiptFailure,
    EmployeeHandoffReceiptStore,
)
from product_engine.services.employee_package_validator import (
    IMAGE_SUFFIXES,
    EmployeePackageValidationFailure,
    is_employee_package_image,
    validate_employee_package,
)
from product_engine.services.image_recognition import (
    HistoricalRecognitionReference,
    ImageRecognitionCandidate,
    RecognitionService,
    RecognitionStore,
    VisionProvider,
    build_default_vision_provider,
)
from product_engine.services.local_pilot_acceptances import (
    AcceptanceDecision,
    LocalPilotAcceptanceFailure,
    LocalPilotAcceptanceStore,
)
from product_engine.services.local_pilot_review_package import (
    LocalPilotReviewPackage,
    LocalPilotReviewPackageFailure,
    build_local_pilot_review_package,
    inspect_local_pilot_review_package,
)
from product_engine.services.local_upload import (
    MAX_UPLOAD_BYTES,
    LocalUploadRejected,
    LocalUploadService,
)
from product_engine.services.local_upload_batches import (
    InitialRecognitionResult,
    LocalProductFormCode,
    LocalUploadBatchFailure,
    LocalUploadBatchStore,
    UploadBatch,
    UploadBatchItem,
)
from product_engine.services.mall_candidate_imports import (
    MallCandidateImportFailure,
    build_mall_candidate_import,
)
from product_engine.services.mall_classification_handoff import (
    MallClassificationHandoffFailure,
    build_mall_classification_handoff,
)
from product_engine.services.material_evidence_uploads import (
    MAX_EVIDENCE_BYTES,
    MaterialEvidenceUploadFailure,
)
from product_engine.services.photo_usage_classifier import (
    LocalPhotoUsageClassifier,
    prepare_classification_image,
)
from product_engine.services.pilot_release_gate import (
    PilotReleaseDecision,
    evaluate_pilot_release,
)
from product_engine.services.pilot_release_status import build_pilot_release_status
from product_engine.services.price_candidate_confirmations import (
    PriceCandidateConfirmationFailure,
    confirm_conservative_price_candidate,
)
from product_engine.services.product_passport_drafts import (
    ProductPassportDraftFailure,
    build_product_passport_draft,
)
from product_engine.services.runtime_classification_confirmations import (
    ClassificationConfirmationWriter,
    RuntimeClassificationConfirmationWriter,
)
from product_engine.services.runtime_employee_batch_promotions import (
    RuntimeEmployeePromotionWriter,
)
from product_engine.services.runtime_material_evidence import (
    MaterialEvidenceInsufficientInput,
    MaterialEvidenceProductView,
    MaterialEvidenceRegistrationInput,
    MaterialEvidenceRevocationInput,
    MaterialEvidenceUploadReceipt,
    RuntimeMaterialEvidenceService,
)
from product_engine.services.runtime_sales_handoffs import (
    RuntimeSalesHandoffFailure,
    RuntimeSalesHandoffService,
)
from product_engine.services.runtime_style_handoffs import (
    RuntimeStyleHandoffFailure,
    RuntimeStyleHandoffService,
)
from product_engine.services.runtime_warehouse_v3 import (
    RuntimeWarehouseV3Failure,
    RuntimeWarehouseV3Service,
)
from product_engine.services.style_test_handoff import (
    ConfirmedProductIdentity,
    ConfirmedUploadItem,
    StyleHandoffFailure,
    StyleHandoffIdempotencyRegistry,
    assess_style_handoff_readiness,
)
from product_engine.services.template_registry import TemplateRecord
from product_engine.services.training_label_versions import (
    TrainingLabelFailure,
    TrainingLabelInput,
    TrainingLabelVersion,
    TrainingLabelVersionStore,
)
from product_engine.services.voice_commands import (
    VoiceCommandFailure,
    parse_voice_command,
)
from product_engine.storage.local import LocalObjectStorage
from product_engine.storage.s3 import S3Client, S3ObjectStorage

PROJECT_ROOT = Path(__file__).resolve().parents[5]
DEFAULT_DATA_ROOT = PROJECT_ROOT / ".local-data"
PAGE_PATH = Path(__file__).with_name("local_upload_page.html")
IMAGE_FILE_SELECTION_PATH = Path(__file__).with_name("image_file_selection.mjs")
VOICE_PERMISSIONS_PATH = Path(__file__).with_name("voice_permissions.mjs")
VOICE_TRANSCRIPT_PATH = Path(__file__).with_name("voice_transcript.mjs")
RECOGNITION_PREVIEW_PATH = Path(__file__).with_name("recognition_preview.mjs")
RECOGNITION_STATUS_VIEW_PATH = Path(__file__).with_name("recognition_status_view.mjs")
ALBUM_REVIEW_OPEN_PATH = Path(__file__).with_name("album_review_open.mjs")
CLASSIFICATION_WRITEBACK_PATH = Path(__file__).with_name("classification_writeback.mjs")
EMPLOYEE_FORMAL_HANDOFF_PATH = Path(__file__).with_name("employee_formal_handoff.mjs")
SALES_DELIVERY_STATUS_PATH = Path(__file__).with_name("sales_delivery_status.mjs")
SALES_HANDOFF_PATH = Path(__file__).with_name("sales_handoff.mjs")
STYLE_ELIGIBILITY_HANDOFF_PATH = Path(__file__).with_name("style_eligibility_handoff.mjs")
PHOTO_USAGE_PREVIEW_PATH = Path(__file__).with_name("photo_usage_preview.mjs")
STYLE_CANDIDATE_REVIEW_PATH = Path(__file__).with_name("style_candidate_review.mjs")
PRODUCT_PASSPORT_DRAFT_VIEW_PATH = Path(__file__).with_name("product_passport_draft_view.mjs")
SERVICE_RULE_FORM_PATH = Path(__file__).with_name("service_rule_form.mjs")
PILOT_REVIEW_REQUEST_GUARD_PATH = Path(__file__).with_name("pilot_review_request_guard.mjs")
PILOT_ACCEPTANCE_FORM_PATH = Path(__file__).with_name("pilot_acceptance_form.mjs")
MATERIAL_EVIDENCE_PATH = Path(__file__).with_name("material_evidence.mjs")

ERRORS = {
    "missing_file": (
        422,
        "还没有选择图片。",
        "请选择一张 JPEG、PNG 或 HEIC 图片后再上传。",
    ),
    "empty_file": (
        422,
        "这张图片没有内容。",
        "请重新选择一张可以正常打开的图片。",
    ),
    "file_too_large": (
        413,
        "这张图片超过了 20 MiB。",
        "请压缩图片到 20 MiB 以内后重新上传。",
    ),
    "unsupported_image": (
        422,
        "无法识别这张图片的格式。",
        "请选择一张 JPEG、PNG 或 HEIC 图片后再上传。",
    ),
    "mime_type_mismatch": (
        422,
        "图片内容与文件格式不一致。",
        "请在相册中重新导出这张图片后再上传。",
    ),
}


class AlbumReviewInboxReader(Protocol):
    async def list_latest(self, tenant_id: UUID) -> Sequence[AlbumReviewProduct]: ...

    async def read_image(
        self,
        tenant_id: UUID,
        product_code: str,
        package_version: int,
        asset_id: UUID,
    ) -> AlbumReviewImageContent: ...


class SalesHandoffService(Protocol):
    async def prepare(self, classification_id: UUID) -> PreparedSalesPreview: ...

    async def deliver(self, handoff_id: UUID) -> WarehouseHandoffView: ...

    async def read(self, handoff_id: UUID) -> StoredSalesHandoff: ...


class StyleHandoffService(Protocol):
    async def authorize(
        self,
        classification_id: UUID,
        command: StyleTransformAuthorizationCommand,
    ) -> StyleTransformAuthorizationView: ...

    async def revoke(self, authorization_id: UUID) -> StyleTransformAuthorizationView: ...

    async def prepare(self, classification_id: UUID) -> PreparedStyleEligibility: ...

    async def evaluate(self, handoff_id: UUID) -> StoredStyleHandoff: ...

    async def read(self, handoff_id: UUID) -> StoredStyleHandoff: ...


class MaterialEvidenceRuntime(Protocol):
    async def upload(
        self,
        product_code: str,
        filename: str,
        mime_type: str,
        content: bytes,
    ) -> MaterialEvidenceUploadReceipt: ...

    async def register(
        self,
        product_code: str,
        request: MaterialEvidenceRegistrationInput,
    ) -> MaterialEvidenceReceipt: ...

    async def keep_insufficient(
        self,
        product_code: str,
        request: MaterialEvidenceInsufficientInput,
    ) -> MaterialEvidenceInsufficientReceipt: ...

    async def revoke(
        self,
        evidence_id: UUID,
        request: MaterialEvidenceRevocationInput,
    ) -> MaterialEvidenceRevocationReceipt: ...

    async def read(self, product_code: str) -> MaterialEvidenceProductView: ...


class WarehouseV3Runtime(Protocol):
    async def prepare_sales(self, classification_id: UUID) -> object: ...
    async def deliver_sales(self, handoff_id: UUID) -> object: ...
    async def prepare_style(self, classification_id: UUID) -> object: ...
    async def evaluate_style(self, handoff_id: UUID) -> object: ...
    async def read_receipt(self, receipt_id: UUID) -> object: ...


class EmployeeBatchPromotionRuntime(Protocol):
    async def promote(
        self,
        batch_id: str,
        request_id: UUID,
    ) -> EmployeeFormalHandoffReceipt: ...

    def latest(self, batch_id: str) -> EmployeeFormalHandoffReceipt | None: ...


class RuntimeAlbumReviewInbox:
    def __init__(self, database: Database, objects: S3ObjectStorage) -> None:
        self.database = database
        self.objects = objects

    async def list_latest(self, tenant_id: UUID) -> Sequence[AlbumReviewProduct]:
        async with self.database.session() as session:
            return await AlbumReviewInbox(SqlAlbumReviewStore(session), self.objects).list_latest(
                tenant_id
            )

    async def read_image(
        self,
        tenant_id: UUID,
        product_code: str,
        package_version: int,
        asset_id: UUID,
    ) -> AlbumReviewImageContent:
        async with self.database.session() as session:
            return await AlbumReviewInbox(SqlAlbumReviewStore(session), self.objects).read_image(
                tenant_id, product_code, package_version, asset_id
            )


def build_runtime_album_review_inbox() -> tuple[AlbumReviewInboxReader | None, UUID | None]:
    raw_tenant_id = os.environ.get("LOCAL_ALBUM_SYNC_TENANT_ID")
    if raw_tenant_id is None:
        return None, None
    try:
        tenant_id = UUID(raw_tenant_id)
        settings = Settings()  # type: ignore[call-arg]
    except (ValueError, ValidationError):
        return None, None
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
    return (
        RuntimeAlbumReviewInbox(
            Database(settings.database_url),
            S3ObjectStorage(client, settings.s3_bucket),
        ),
        tenant_id,
    )


def build_runtime_employee_batch_promotion_service(
    data_root: Path,
    source_storage: LocalObjectStorage,
    batch_store: LocalUploadBatchStore,
    training_version_store: TrainingLabelVersionStore,
) -> EmployeeBatchPromotionService | None:
    raw_tenant_id = os.environ.get("LOCAL_ALBUM_SYNC_TENANT_ID")
    if raw_tenant_id is None:
        return None
    try:
        tenant_id = UUID(raw_tenant_id)
        settings = Settings()  # type: ignore[call-arg]
    except (ValueError, ValidationError):
        return None
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
    return EmployeeBatchPromotionService(
        tenant_id=tenant_id,
        batch_store=batch_store,
        training_version_store=training_version_store,
        source_storage=source_storage,
        destination_storage=S3ObjectStorage(client, settings.s3_bucket),
        database_writer=RuntimeEmployeePromotionWriter(
            Database(settings.database_url),
            tenant_id,
        ),
        receipts=EmployeeHandoffReceiptStore(data_root / "employee-formal-handoffs.json"),
        provenance=ClassificationImportProvenanceStore(data_root / "album-review-imports.json"),
    )


TRAINING_ERRORS = {
    "version_conflict": (
        409,
        "这份页面中的结果已经不是最新版。",
        "请重新加载最新版本后再修改。",
    ),
    "no_previous_version": (
        409,
        "当前结果还没有可撤销的上一版。",
        "请继续修改当前结果，保存后即可使用撤销功能。",
    ),
    "required_field_missing": (
        422,
        "材质、形态、可见特征和销售卖点都至少需要填写一项。",
        "请补全空白内容后重新保存。",
    ),
    "invalid_price_range": (
        422,
        "建议价格区间不完整或上下限不正确。",
        "请同时填写价格上下限和依据，并确保下限小于上限。",
    ),
    "invalid_service_rule": (
        422,
        "每条服务规则都需要填写服务编码、适用条件和客户说明。",
        "请补全这一条服务规则，或清空整行后再保存。",
    ),
    "forbidden_unsupported_claim": (
        422,
        "内容包含图片无法核实的真伪、产地、保值或固定价格承诺。",
        "请改为可见事实或待验证备注后重新保存。",
    ),
}

BATCH_ERRORS = {
    "target_batch_not_found": (
        404,
        "找不到目标批次。",
        "请先选择一个已建立的目标商品批次。",
    ),
    "batch_not_found": (
        404,
        "找不到这个上传批次。",
        "请重新选择图片并开始一个新批次。",
    ),
    "position_conflict": (
        409,
        "这个位置已经上传了另一张图片。",
        "请刷新批次后只重试未完成或失败的图片。",
    ),
    "target_product_binding_conflict": (
        409,
        "目标批次已经绑定了另一个测试商品。",
        "请重新选择与目标批次一致的商品，或新建一个空批次。",
    ),
    "style_test_batch_sealed": (
        409,
        "这个批次已经交给本机测试服务，不能再调整图片。",
        "请新建一个批次继续整理，避免改写已交接事实。",
    ),
    "invalid_expected_count": (
        422,
        "批次图片数量必须大于零。",
        "请至少选择一张图片。",
    ),
    "image_not_found": (
        404,
        "找不到这张图片的本地文件。",
        "请在批次中重新上传这张图片。",
    ),
    "item_not_reviewable": (
        409,
        "这张图片不能进入审核状态。",
        "请刷新批次并选择一张待审核商品图。",
    ),
    "item_not_confirmable": (409, "这张图片不能更正用途。", "请重新上传可正常打开的图片。"),
    "high_confidence_photo_not_editable": (
        409,
        "高置信度图片不需要预先选择分类。",
        "仅在黄色提示卡中更正低置信度建议。",
    ),
    "confirmation_version_conflict": (
        409,
        "这张图片已有不同的确认版本。",
        "请刷新批次后继续审核。",
    ),
    "style_test_permission_conflict": (
        409,
        "本批次已有不同的测试授权事实。",
        "请新建批次，避免覆盖已留痕的授权。",
    ),
}

EMPLOYEE_PACKAGE_ERRORS = {
    "invalid_zip": ("资料包不是有效的压缩文件。", "请将一份 Excel 和商品图片放进 ZIP 后重新上传。"),
    "unsafe_archive_path": ("资料包包含不安全的文件路径。", "请重新创建资料包后再上传。"),
    "unsafe_archive_link": (
        "资料包不能包含链接文件。",
        "请重新压缩实际的图片文件后再上传。",
    ),
    "template_file_missing": (
        "资料包中需要且只能有一份表格。",
        "请保留一份商品资料表格后重新压缩。",
    ),
    "missing_product_images": (
        "资料包中没有找到商品图片。",
        "请将 JPEG、PNG 或 HEIC 图片放进 ZIP 后重新上传。",
    ),
    "main_image_column_missing": (
        "表格中没有找到“主图文件名”列。",
        "请使用系统提供的员工资料表格后重新压缩。",
    ),
    "image_file_missing": (
        "表格中的图片文件名与 ZIP 里的图片不一致。",
        "请检查图片文件名是否完全一致后重新压缩。",
    ),
    "unmatched_product_images": (
        "部分图片无法归入表格中的商品编码。",
        "请删除文件名中的多余符号，并使用“编码.jpg、编码-2.jpg”的命名方式后重试。",
    ),
    "ambiguous_product_images": (
        "同一商品的附图序号出现了两个候选文件。",
        "请保留其中一个文件，确保每个“编码-序号”只对应一张图片后重试。",
    ),
    "duplicate_product_code": (
        "表格中存在重复的商品编码。",
        "请保留每个商品编码的一行资料后重试。",
    ),
    "invalid_template_file": (
        "资料包中的表格无法打开。",
        "请重新导出商品资料表格后再压缩上传。",
    ),
}

PRODUCT_PASSPORT_DRAFT_ERRORS = {
    "training_result_not_found": (
        404,
        "找不到这张商品图的人工资料。",
        "请先上传商品图片并进入逐张审核。",
    ),
    "human_confirmation_required": (
        409,
        "这张商品图还没有训练员人工确认版本。",
        "请先审核并保存材质、形态、可见特征和卖点，再生成商品资料草稿。",
    ),
    "classification_not_human_confirmed": (
        409,
        "当前材质类目还不能交给商城使用。",
        "请由训练员在首批六品类中确认，或保留信息不足。",
    ),
}


class TrainingLabelSaveRequest(TrainingLabelInput):
    base_version: int


class RollbackRequest(BaseModel):
    base_version: int


class PriceCandidateConfirmRequest(BaseModel):
    base_version: int
    standard_code: str
    anchor_version: int
    minimum: str
    maximum: str
    basis: str
    anchor_confirmed_by: str
    confirmed_by: str


class CreateBatchRequest(BaseModel):
    expected_count: int


class PhotoCategoryCorrectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    corrected_category: str
    reviewer_note: str


class ConfirmAndGenerateRequest(BaseModel):
    """Explicit local-test permission, never an inferred customer consent."""

    model_config = ConfigDict(extra="forbid")

    customer_test_id: str
    product_id: str
    main_photo_id: UUID
    transform_permission: StrictBool


class LocalReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: ReviewDecision
    reason: (
        Annotated[
            str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=255)
        ]
        | None
    ) = None
    idempotency_key: Annotated[
        str,
        StringConstraints(
            strict=True, min_length=1, max_length=255, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"
        ),
    ]


class ClassificationConfirmationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    local_batch_id: Annotated[
        str,
        StringConstraints(
            strict=True,
            strip_whitespace=True,
            min_length=1,
            max_length=128,
        ),
    ]


class StyleAuthorizationRequest(StyleTransformAuthorizationCommand):
    pass


def _local_review_error(error: StyleBridgeError) -> JSONResponse:
    messages = {
        "local_review_content_unavailable": "图片暂时无法读取，请稍后重试。",
        "review_job_not_awaiting_customer": "图片仍在检查，请稍后刷新查看。",
        "review_generation_in_progress": "正在生成下一轮图片，请稍后查看。",
        "review_idempotency_conflict": "本次选择与已保存的审核记录冲突，请刷新后查看结果。",
        "candidate_review_already_decided": "这张图片已有审核结果，请刷新后查看。",
        "review_candidate_not_current": "这张图片不属于当前轮次，请刷新后重新选择。",
        "review_session_already_closed": "本次审核已结束，请刷新后查看结果。",
    }
    return JSONResponse(
        status_code=503,
        content={
            "ok": False,
            "error_code": error.code,
            "retryable": bool(getattr(error, "retryable", True)),
            "message_zh": messages.get(error.code, "本地测试服务暂时不可用，请稍后重试。"),
        },
    )


def _local_product_image(
    image: ProductImage, *, fallback_job_id: UUID | None = None
) -> dict[str, object]:
    payload = cast(dict[str, object], jsonable_encoder(image))
    job_id = image.content_job_id or fallback_job_id
    if job_id is None:
        raise StyleBridgeError("style_bridge_product_image_invalid")
    if image.image_kind == "style_image":
        payload["content_url"] = (
            f"/local/api/style-jobs/{job_id}/candidates/{image.candidate_id}/image"
        )
    else:
        payload["content_url"] = f"/local/api/style-jobs/{job_id}/assets/{image.asset_id}/image"
    payload["synthetic_test_only"] = True
    return payload


class StyleTestCatalogEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    customer_test_id: CustomerTestId
    product_id: ProductId
    product_category: ProductCategory
    product_form: ProductForm
    display_label: str | None = None


class MoveBatchItemRequest(BaseModel):
    to_position: StrictInt


class MoveToBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_batch_id: str
    target_product_id: ProductId
    to_position: StrictInt | None = None


class VoiceCommandRequest(BaseModel):
    transcript: str


def error_response(error_code: str) -> JSONResponse:
    status_code, message, next_step = ERRORS.get(
        error_code,
        (
            500,
            "图片暂时无法上传。",
            "请稍后重新选择图片再试一次。",
        ),
    )
    return JSONResponse(
        status_code=status_code,
        content={
            "ok": False,
            "error_code": error_code,
            "message_zh": message,
            "next_step_zh": next_step,
        },
    )


def serialize_template(record: TemplateRecord) -> dict[str, str | int | UUID]:
    return {
        "id": record.id,
        "tenant_id": record.tenant_id,
        "template_code": record.template_code,
        "version": record.version,
        "category": record.category,
        "image_type": record.image_type,
        "object_key": record.object_key,
        "sha256": record.sha256,
        "mime_type": record.mime_type,
        "byte_size": record.byte_size,
        "status": record.status,
    }


def serialize_training_result(record: TrainingLabelVersion) -> dict[str, object]:
    return record.model_dump(mode="json")


def serialize_recognition_result(
    record: ImageRecognitionCandidate,
) -> dict[str, object]:
    return record.model_dump(mode="json")


def training_error_response(error_code: str) -> JSONResponse:
    status_code, message, next_step = TRAINING_ERRORS.get(
        error_code,
        (
            500,
            "人工标准暂时无法保存。",
            "请稍后重试，当前生效版本不会被覆盖。",
        ),
    )
    return JSONResponse(
        status_code=status_code,
        content={
            "ok": False,
            "error_code": error_code,
            "message_zh": message,
            "next_step_zh": next_step,
        },
    )


def price_confirmation_error_response(error_code: str) -> JSONResponse:
    if error_code == "version_conflict":
        status_code = 409
        message = "这份页面中的商品资料已经不是最新版。"
        next_step = "请重新加载最新版本后，再确认候选价格。"
    elif error_code == "formal_price_already_present":
        status_code = 409
        message = "当前版本已经有人工确认价格。"
        next_step = "如需调整，请在人工编辑区新增版本，不要重复确认候选。"
    else:
        status_code = 422
        message = "候选价格未通过人工确认前检查。"
        next_step = "请核对价格标准、区间、依据和确认人后重新提交。"
    return JSONResponse(
        status_code=status_code,
        content={
            "ok": False,
            "error_code": error_code,
            "message_zh": message,
            "next_step_zh": next_step,
        },
    )


def batch_error_response(error_code: str) -> JSONResponse:
    status_code, message, next_step = BATCH_ERRORS.get(
        error_code,
        (
            500,
            "批量上传暂时无法继续。",
            "请刷新页面后继续未完成的图片。",
        ),
    )
    return JSONResponse(
        status_code=status_code,
        content={
            "ok": False,
            "error_code": error_code,
            "message_zh": message,
            "next_step_zh": next_step,
        },
    )


def product_passport_draft_error_response(error_code: str) -> JSONResponse:
    status_code, message, next_step = PRODUCT_PASSPORT_DRAFT_ERRORS.get(
        error_code,
        (
            500,
            "商品资料草稿暂时无法生成。",
            "请稍后重新打开当前商品图。",
        ),
    )
    return JSONResponse(
        status_code=status_code,
        content={
            "ok": False,
            "error_code": error_code,
            "message_zh": message,
            "next_step_zh": next_step,
        },
    )


def employee_package_error_response(
    error_code: str,
    details: dict[str, object] | None = None,
) -> JSONResponse:
    message, next_step = EMPLOYEE_PACKAGE_ERRORS.get(
        error_code,
        ("资料包暂时无法校验。", "请稍后重新上传资料包。"),
    )
    return JSONResponse(
        status_code=422,
        content={
            "ok": False,
            "error_code": error_code,
            "message_zh": message,
            "next_step_zh": next_step,
            **(details or {}),
        },
    )


def serialize_batch_item(item: UploadBatchItem) -> dict[str, object]:
    serialized = item.model_dump(mode="json")
    serialized["商品序号"] = item.product_sequence or item.position + 1
    serialized["商品编码"] = item.product_code or (
        f"商品-{str(item.item_id).split('-')[0].upper()}-{item.position + 1:03d}"
    )
    serialized["图片角色"] = {"main": "主图", "detail": "附图"}.get(
        item.image_role or "",
        "待确认",
    )
    serialized["商品名称"] = item.product_name or ""
    serialized["Excel建议价格"] = item.merchant_price_reference or ""
    return serialized


def serialize_batch(batch: UploadBatch) -> dict[str, object]:
    serialized = batch.model_dump(mode="json")
    serialized_items = [serialize_batch_item(item) for item in batch.items]
    serialized["items"] = serialized_items
    serialized["counts"] = batch.counts
    imported_codes = {item.product_code for item in batch.items if item.product_code}
    ungrouped = sum(
        item.product_code is None and item.status in {"pending_review", "reviewed"}
        for item in batch.items
    )
    serialized["product_count"] = len(imported_codes) + ungrouped
    product_groups: dict[tuple[int, str], dict[str, object]] = {}
    for item in serialized_items:
        if item["status"] not in {"pending_review", "reviewed"}:
            continue
        product_sequence = item["商品序号"]
        assert isinstance(product_sequence, int)
        key = (product_sequence, str(item["商品编码"]))
        group = product_groups.setdefault(
            key,
            {
                "商品编码": item["商品编码"],
                "商品序号": item["商品序号"],
                "图片数量": 0,
                "商品名称": item["商品名称"],
                "Excel建议价格": item["Excel建议价格"],
                "主图项目编号": None,
                "已审核图片数量": 0,
                "审核状态": "待审核",
                "销售策略状态": "blocked",
                "销售策略输入预览": None,
                "图片": [],
            },
        )
        image = {
            "项目编号": item["item_id"],
            "文件名": item["filename"],
            "图片角色": item["图片角色"],
            "批次位置": item["position"],
        }
        images = group["图片"]
        assert isinstance(images, list)
        images.append(image)
        group["图片数量"] = len(images)
        if item["status"] == "reviewed":
            reviewed_image_count = group["已审核图片数量"]
            assert isinstance(reviewed_image_count, int)
            group["已审核图片数量"] = reviewed_image_count + 1
        if item["图片角色"] == "主图":
            group["主图项目编号"] = item["item_id"]
    for group in product_groups.values():
        if group["已审核图片数量"] == group["图片数量"]:
            group["审核状态"] = "已完成"
            group["销售策略状态"] = "preview_ready"
            group_items = [
                item
                for item in serialized_items
                if item["商品序号"] == group["商品序号"]
                and item["商品编码"] == group["商品编码"]
                and item["status"] == "reviewed"
            ]
            first_result = group_items[0]["initial_result"]
            assert isinstance(first_result, dict)
            typed_group_items: list[dict[str, object]] = []
            for item in group_items:
                initial_result = item["initial_result"]
                assert isinstance(initial_result, dict)
                typed_group_items.append(initial_result)
            observations = list(
                dict.fromkeys(
                    observation
                    for initial_result in typed_group_items
                    for observation in cast(list[str], initial_result["visual_observations"])
                )
            )
            selling_points = list(
                dict.fromkeys(
                    selling_point
                    for initial_result in typed_group_items
                    for selling_point in cast(list[str], initial_result["selling_points"])
                )
            )
            group_images = group["图片"]
            assert isinstance(group_images, list)
            preview_images: list[dict[str, object]] = []
            for image in group_images:
                assert isinstance(image, dict)
                preview_images.append(
                    {
                        "项目编号": image["项目编号"],
                        "图片角色": image["图片角色"],
                        "文件名": image["文件名"],
                    }
                )
            group["销售策略输入预览"] = {
                "商品编码": group["商品编码"],
                "商品名称": group["商品名称"],
                "状态": "preview_only",
                "图片": preview_images,
                "材质": first_result["material_label"],
                "形态": first_result["product_form"],
                "可见特征": observations,
                "卖点候选": selling_points,
                "价格状态": first_result["price_status"],
                "价格参考": group["Excel建议价格"],
                "未验证事项": ["真伪未验证", "产地未验证", "品质未验证", "仓库验收待完成"],
                "禁止自动动作": ["联系客户", "公开发布", "发送微信", "自动报价"],
            }
    serialized["product_groups"] = list(product_groups.values())
    return serialized


def _style_bridge_panel() -> str:
    """Return the optional, narrow-screen local-test enhancement only when gated on."""

    return """
<style>
  #style-bridge-panel { margin: 24px auto 0; width: min(calc(100% - 32px), 980px); }
  #style-bridge-panel .bridge-card { padding: 20px; border: 1px solid #d8d8ea; border-radius: 20px; background: #f8f7ff; }
  #style-bridge-panel .bridge-items { display: grid; gap: 12px; margin-top: 14px; }
  #style-bridge-panel .bridge-item { display: grid; grid-template-columns: 72px 1fr; gap: 12px; padding: 12px; border-radius: 14px; background: #fff; }
  #style-bridge-panel .bridge-item img { width: 72px; height: 72px; border-radius: 10px; object-fit: cover; background: #eee; }
  #style-bridge-panel .bridge-meta { margin: 4px 0; color: #756764; font-size: 13px; overflow-wrap: anywhere; }
  #style-bridge-panel .bridge-action { margin-top: 8px; color: #8d2f2a; border-color: #d99a96; }
  #style-bridge-panel details { margin-top: 14px; color: #756764; font-size: 13px; line-height: 1.6; }
  #style-bridge-panel .bridge-status { min-height: 24px; margin: 10px 0; color: #4e4568; }
  #style-bridge-panel .candidate-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
  #style-bridge-panel .candidate-card { min-width: 0; padding: 12px; background: #fff; border-radius: 14px; }
  #style-bridge-panel .candidate-card img { width: 100%; max-height: 340px; object-fit: contain; }
  #style-bridge-panel .candidate-actions { display: flex; gap: 8px; flex-wrap: wrap; margin: 12px 0; }
  #style-bridge-panel .candidate-actions button { white-space: normal; min-height: 44px; }
  #style-candidate-reviews section { margin-top: 24px; overflow-wrap: anywhere; }
  @media (max-width: 680px) { #style-bridge-panel .candidate-grid { grid-template-columns: 1fr; } }
  @media (max-width: 380px) { #style-bridge-panel { width: calc(100% - 24px); } #style-bridge-panel .bridge-card { padding: 16px; } }
</style>
<section class="panel" id="style-bridge-panel" aria-labelledby="style-bridge-title">
  <div class="bridge-card">
    <p class="eyebrow">仅限脱敏本地测试</p>
    <h2 id="style-bridge-title">商品确认后生成风格图</h2>
    <p class="intro">图片上传后系统先自动归类；只有黄色提示或无法打开的图片需要你处理。这里不是上架、发布或真实客户生成入口。</p>
    <p id="style-bridge-status" class="bridge-status" role="status" aria-live="polite">等待选择或恢复一个上传批次。</p>
    <label>脱敏测试商品（只显示启动时白名单）<select id="style-bridge-product"><option value="">请选择测试商品</option></select></label>
    <label>主图<select id="style-bridge-main-photo"><option value="">请选择已上传主图</option></select></label>
    <label><input id="style-bridge-permission" type="checkbox"> 我确认本批图片仅用于脱敏本地测试转换</label>
    <div id="style-bridge-items" class="bridge-items"></div>
    <button class="primary" id="style-bridge-generate" type="button" disabled>确认商品并生成风格图</button>
    <button class="bridge-action" id="style-bridge-retry-progress" type="button" hidden>重试查看进度</button>
    <div id="style-candidate-reviews" aria-label="风格候选图审核"></div>
    <p class="bridge-meta">仅支持 JPEG、PNG；HEIC 仅在本机有解码器时可用。视频不支持，上传接口会返回 422。</p>
    <details id="style-bridge-details">
      <summary>查看详情</summary>
      <pre id="style-bridge-detail-text"></pre>
    </details>
  </div>
</section>
<script type="module">
  import { mountCandidateReview } from '/local/assets/style-candidate-review.mjs';
  import {
    bridgeErrorMessage,
    createBatchBridgeController,
    createTargetBatchDirectory,
    eligibleMainPhotoItems,
    readinessStatusMessage,
    recoveryActionNames,
    selectMainPhotoId,
    technicalDetails,
  } from '/local/assets/photo-usage-preview.mjs';
  const panel = document.querySelector('#style-bridge-panel');
  const status = document.querySelector('#style-bridge-status');
  const items = document.querySelector('#style-bridge-items');
  const generate = document.querySelector('#style-bridge-generate');
  const retryProgress = document.querySelector('#style-bridge-retry-progress');
  const productSelect = document.querySelector('#style-bridge-product');
  const mainPhotoSelect = document.querySelector('#style-bridge-main-photo');
  const permission = document.querySelector('#style-bridge-permission');
  const detail = document.querySelector('#style-bridge-detail-text');
  const reviewRoot = document.querySelector('#style-candidate-reviews');
  const reviewPanels = new Map();

  function showReviewJobs(payload) {
    const handoff = payload.handoff || payload;
    const summaries = handoff.output_jobs || [];
    if (!summaries.length && (handoff.output_job_ids || []).length) {
      reviewRoot.textContent = '暂时无法确认任务类型。';
      return;
    }
    for (const job of summaries) {
      const jobId = job.job_id;
      if (!(handoff.output_job_ids || []).includes(jobId)) continue;
      if (reviewPanels.has(jobId)) continue;
      const section = document.createElement('section');
      reviewRoot.append(section);
      if (job.output_type === 'real_photo') {
        const message = document.createElement('p');
        message.textContent = ['awaiting_customer_review', 'customer_approved'].includes(job.status)
          ? '实拍图已整理，作为实物原图保留。' : '实拍图仍需处理，请查看生成进度。';
        section.append(message);
        reviewPanels.set(jobId, null);
        continue;
      }
      if (!['scene', 'multi_product'].includes(job.output_type)) continue;
      if (!['awaiting_customer_review', 'customer_approved', 'customer_rejected'].includes(job.status)) {
        const message = document.createElement('p');
        message.textContent = '风格图尚未通过检查，暂时不能审核。';
        section.append(message);
        reviewPanels.set(jobId, null);
        continue;
      }
      const reviewPanel = mountCandidateReview(section);
      reviewPanels.set(jobId, reviewPanel);
      void reviewPanel.load(jobId);
    }
  }
  const categoryLabels = { real_photo: '实拍商品图', wear_photo: '佩戴图', scene_photo: '场景图', multi_product_photo: '多商品图' };
  let knownTargets = [];
  let current = null;
  let boundProductId = '';
  let retryPoll = null;

  async function responsePayload(response) {
    let payload;
    try {
      payload = await response.json();
    } catch (_) {
      return { ok: false, message_zh: '服务返回内容无法读取，请稍后重试。' };
    }
    return response.ok ? payload : { ...payload, ok: false };
  }

  const targetDirectory = createTargetBatchDirectory(async signal => responsePayload(
    await fetch('/local/api/style-test-target-batches', { signal })
  ));

  function resetBatchSelection() {
    clearPollRetry();
    for (const reviewPanel of reviewPanels.values()) reviewPanel?.reset();
    reviewPanels.clear();
    reviewRoot.replaceChildren();
    productSelect.value = '';
    productSelect.disabled = false;
    boundProductId = '';
    mainPhotoSelect.value = '';
    permission.checked = false;
  }

  function recoveryTargetSelector(sourceBatchId) {
    const label = document.createElement('label');
    label.textContent = '选择目标批次';
    const select = document.createElement('select');
    select.setAttribute('aria-label', '选择目标批次');
    select.add(new Option('请选择目标批次', ''));
    for (const target of knownTargets) {
      const option = new Option(`${target.label} · ${target.batchId}`, target.batchId);
      option.dataset.productId = target.productId;
      select.add(option);
    }
    label.append(select);
    return { label, select };
  }

  function clearPollRetry() {
    retryPoll = null;
    retryProgress.hidden = true;
  }

  function showPollRetry(retry) {
    retryPoll = retry;
    retryProgress.hidden = false;
  }

  retryProgress.addEventListener('click', () => {
    const retry = retryPoll;
    clearPollRetry();
    if (retry) void retry();
  });

  async function activateBatch(batchId) {
    resetBatchSelection();
    const readiness = controller.switchBatch(batchId);
    try {
      knownTargets = await targetDirectory.refresh(batchId);
    } catch (_) {
      if (controller.snapshot().batchId === batchId) knownTargets = [];
    }
    await readiness;
    if (controller.snapshot().batchId === batchId && current) {
      render(current, controller.visibleState(current.batch?.items?.length || 0));
    }
  }

  async function restoreLatestStyleHandoff(expectedBatchId = '') {
    const payload = await responsePayload(await fetch('/local/api/latest-style-handoff'));
    const handoff = payload.handoff;
    if (!payload.ok || !handoff?.batch_id) return;
    if (expectedBatchId && handoff.batch_id !== expectedBatchId) return;
    if (!expectedBatchId) await activateBatch(handoff.batch_id);
    showReviewJobs(payload);
    status.textContent = handoff.current_status === 'awaiting_review'
      ? '已恢复最近一次风格图结果，可以继续审核。'
      : '已恢复最近一次风格任务。';
  }

  function currentSelection() {
    return {
      productId: productSelect.value,
      mainPhotoId: mainPhotoSelect.value,
      permission: permission.checked,
    };
  }

  function syncSelection() {
    clearPollRetry();
    controller.updateSelection(currentSelection());
    if (current) render(current, controller.visibleState(current.batch?.items?.length || 0));
  }

  function appendRecoveryActions(body, view, payload) {
    const item = view.item;
    const batchId = payload.batch.batch_id;
    const actionNames = recoveryActionNames(view);
    if (!actionNames.length) return;
    let actionBody = body;
    if (!['failed', 'duplicate'].includes(view.state)) {
      const details = document.createElement('details');
      const summary = document.createElement('summary');
      summary.textContent = '调整归组';
      details.append(summary);
      body.append(details);
      actionBody = details;
    }

    if (actionNames.includes('reupload')) {
      const retry = document.createElement('button');
      retry.type = 'button';
      retry.className = 'bridge-action';
      retry.textContent = '重新上传';
      retry.addEventListener('click', () => {
        const picker = document.createElement('input');
        picker.type = 'file';
        picker.accept = 'image/jpeg,image/png,image/heic,.heic';
        picker.onchange = () => {
          const file = picker.files?.[0];
          if (!file) return;
          void controller.runBatchAction({
            batchId,
            request: async signal => {
              const form = new FormData();
              form.append('position', item.position);
              form.append('file', file);
              return responsePayload(await fetch(`/local/api/upload-batches/${batchId}/images`, { method: 'POST', body: form, signal }));
            },
            refresh: true,
            onError: message => { status.textContent = message; },
          });
        };
        picker.click();
      });
      actionBody.append(retry);
    }

    if (actionNames.includes('delete')) {
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'bridge-action';
      remove.textContent = '移除图片';
      remove.addEventListener('click', () => {
        void controller.runBatchAction({
          batchId,
          request: async signal => responsePayload(await fetch(`/local/api/upload-batches/${batchId}/items/${item.item_id}`, { method: 'DELETE', signal })),
          refresh: true,
          onError: message => { status.textContent = message; },
        });
      });
      actionBody.append(remove);
    }

    if (actionNames.includes('move')) {
      const target = recoveryTargetSelector(batchId);
      const move = document.createElement('button');
      move.type = 'button';
      move.className = 'bridge-action';
      move.textContent = '移到目标批次';
      move.disabled = true;
      target.select.addEventListener('change', () => { move.disabled = !target.select.value; });
      move.addEventListener('click', () => {
        const targetBatchId = target.select.value;
        const targetProductId = target.select.selectedOptions[0]?.dataset.productId;
        if (!targetBatchId || !targetProductId) return;
        void controller.runBatchAction({
          batchId,
          request: async signal => responsePayload(await fetch(`/local/api/upload-batches/${batchId}/items/${item.item_id}/move-to-batch`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ target_batch_id: targetBatchId, target_product_id: targetProductId }),
            signal,
          })),
          onSuccess: () => activateBatch(targetBatchId),
          onError: message => { status.textContent = message; },
        });
      });
      actionBody.append(target.label, move);
    }
  }

  function render(payload, state) {
    current = payload;
    const productBinding = payload?.product_confirmation || payload?.product_binding;
    boundProductId = productBinding?.product_id || '';
    if (boundProductId) productSelect.value = boundProductId;
    productSelect.disabled = Boolean(boundProductId);
    const selectedMainPhoto = mainPhotoSelect.value;
    mainPhotoSelect.replaceChildren(new Option('请选择已上传主图', ''));
    const mainPhotoChoices = eligibleMainPhotoItems(payload?.items || []);
    for (const view of mainPhotoChoices) {
      mainPhotoSelect.add(new Option(
        `${view.item.filename}${view.recommended ? '（推荐）' : ''}`,
        view.item.item_id,
      ));
    }
    mainPhotoSelect.value = selectMainPhotoId(mainPhotoChoices, selectedMainPhoto);
    const selectionComplete = Boolean(
      productSelect.value && mainPhotoSelect.value && permission.checked
    );
    const submitting = controller.snapshot().submitting;
    const visible = submitting ? 'generating' : state;
    status.textContent = visible === 'awaiting_selection' ? '等待选择或恢复一个上传批次。' :
      visible === 'organizing' ? '正在整理图片用途…' :
      visible === 'generating' ? '正在读取本地测试任务状态…' :
      readinessStatusMessage(payload?.missing_codes || [], {
        canConfirm: payload?.can_confirm === true,
        selectionComplete,
      });
    generate.disabled = !payload?.can_confirm || !selectionComplete || submitting;
    detail.textContent = JSON.stringify(technicalDetails(payload), null, 2);
    items.replaceChildren(...(payload?.items || []).map(view => {
      const item = view.item; const card = document.createElement('article'); card.className = 'bridge-item';
      const image = document.createElement('img'); image.alt = view.state === 'failed' ? '图片无法预览' : ''; if (view.state !== 'failed') image.src = `/local/api/upload-batches/${payload.batch.batch_id}/items/${item.item_id}/image`;
      const body = document.createElement('div'); const name = document.createElement('strong'); name.textContent = item.filename;
      const meta = document.createElement('p'); meta.className = 'bridge-meta'; meta.textContent = `图片 ID：${item.item_id}`;
      body.append(name, meta);
      if (view.state === 'accepted') {
        const accepted = document.createElement('p'); accepted.className = 'bridge-meta'; accepted.textContent = `自动归类：${categoryLabels[view.automatic_category]}（高置信度，已收起）`;
        body.append(accepted);
      } else if (view.state === 'exception') {
        const label = document.createElement('label'); label.textContent = `建议：${categoryLabels[view.automatic_category]}；如不正确才更正`;
        const select = document.createElement('select');
        for (const [code, text] of Object.entries(categoryLabels)) { const option = new Option(text, code, false, code === view.automatic_category); select.add(option); }
        const save = document.createElement('button'); save.type = 'button'; save.textContent = '保存更正';
        save.addEventListener('click', () => {
          const batchId = payload.batch.batch_id;
          void controller.runBatchAction({
            batchId,
            request: async signal => responsePayload(await fetch(`/local/api/upload-batches/${batchId}/items/${item.item_id}/photo-category-corrections`, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ corrected_category: select.value, reviewer_note: '页面人工更正' }),
              signal,
            })),
            refresh: true,
            onError: message => { status.textContent = message; },
          });
        }); label.append(select); body.append(label, save);
      } else if (['failed', 'duplicate'].includes(view.state)) {
        const failure = document.createElement('p');
        failure.className = 'bridge-meta';
        failure.textContent = view.state === 'failed'
          ? bridgeErrorMessage({ error_code: view.reason_code }, '图片无法处理，请重新上传可用图片。')
          : '检测到重复图片，请重新上传、移除或移动到正确商品批次。';
        body.append(failure);
      }
      if (recoveryActionNames(view).length) appendRecoveryActions(body, view, payload);
      card.append(image, body); return card;
    }));
  }
  const controller = createBatchBridgeController({
    fetchReadiness: async (batchId, signal) => { const response = await fetch(`/local/api/upload-batches/${batchId}/style-readiness`, { signal }); const payload = await response.json(); if (!response.ok) throw payload; return payload; },
    render,
  });
  window.addEventListener('style-bridge-batch-changed', event => { void activateBatch(event.detail.batchId); });
  fetch('/local/api/style-test-products').then(response => response.json()).then(payload => {
    for (const entry of payload.products || []) { const option = new Option(entry.display_label || entry.product_id, entry.product_id); option.dataset.customerTestId = entry.customer_test_id; productSelect.add(option); }
    if (boundProductId) productSelect.value = boundProductId;
    if (current) render(current, controller.visibleState(current.batch?.items?.length || 0));
    const restoredBatchId = localStorage.getItem('latestUploadBatchId');
    if (restoredBatchId) {
      void activateBatch(restoredBatchId).then(() => restoreLatestStyleHandoff(restoredBatchId));
    }
    else void restoreLatestStyleHandoff();
  }).catch(() => {});
  productSelect.addEventListener('change', syncSelection);
  mainPhotoSelect.addEventListener('change', syncSelection);
  permission.addEventListener('change', syncSelection);
  generate.addEventListener('click', () => {
    if (!current?.batch?.batch_id || generate.disabled) return;
    const batchId = current.batch.batch_id;
    const entry = productSelect.selectedOptions[0];
    controller.updateSelection(currentSelection());
    clearPollRetry();
    generate.disabled = true;
    status.textContent = '正在提交脱敏本地测试任务…';
    void controller.runBatchAction({
      batchId,
      selectionSensitive: true,
      request: async signal => responsePayload(await fetch(`/local/api/upload-batches/${batchId}/confirm-and-generate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ customer_test_id: entry.dataset.customerTestId, product_id: productSelect.value, main_photo_id: mainPhotoSelect.value, transform_permission: permission.checked }),
        signal,
      })),
      onError: message => { status.textContent = message; generate.disabled = false; },
      onSuccess: payload => {
        clearPollRetry();
        status.textContent = '已交给本机测试服务，正在等待结果。';
        void controller.pollHandoff(
          payload.handoff_id,
          async (handoffId, signal) => responsePayload(await fetch(`/local/api/style-handoffs/${handoffId}`, { signal })),
          (handoff, state) => {
            clearPollRetry();
            const labels = { generating: '正在生成风格图…', needs_confirmation: '仍需你确认。', organizing: '正在整理图片用途…', awaiting_selection: '等待你选图。' };
            status.textContent = labels[state];
            detail.textContent = JSON.stringify(handoff.handoff || handoff, null, 2);
            if ((handoff.handoff || handoff).current_status !== 'processing') showReviewJobs(handoff);
          },
          message => { clearPollRetry(); status.textContent = message; },
          showPollRetry,
        );
      },
    });
  });
</script>
"""


def build_employee_package_template() -> bytes:
    """Build an empty, self-contained staff package template for local download."""
    workbook = BytesIO()
    with ZipFile(workbook, "w") as archive:
        archive.writestr(
            "[Content_Types].xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>
</Types>""",
        )
        archive.writestr(
            "_rels/.rels",
            """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>""",
        )
        archive.writestr(
            "xl/workbook.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="商品资料" sheetId="1" r:id="rId1"/></sheets></workbook>""",
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/>
</Relationships>""",
        )
        archive.writestr(
            "xl/sharedStrings.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="7" uniqueCount="7">
<si><t>主图文件名（必填）</t></si><si><t>商品名称（选填）</t></si><si><t>材质或品类（选填）</t></si>
<si><t>商品形态（选填）</t></si><si><t>可见特点（选填）</t></si><si><t>销售要点（选填）</t></si>
<si><t>建议价格区间（选填）</t></si></sst>""",
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1">
<c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c><c r="C1" t="s"><v>2</v></c>
<c r="D1" t="s"><v>3</v></c><c r="E1" t="s"><v>4</v></c><c r="F1" t="s"><v>5</v></c>
<c r="G1" t="s"><v>6</v></c></row></sheetData></worksheet>""",
        )

    package = BytesIO()
    with ZipFile(package, "w") as archive:
        archive.writestr("员工商品资料包/商品资料批量上传模板-简版.xlsx", workbook.getvalue())
        archive.writestr(
            "员工商品资料包/填写与打包说明.txt",
            "1. 将商品图片放入“商品图片”文件夹。\n2. 在表格中填写每张主图的文件名；其他六项可按实际情况选填。\n3. 压缩整个“员工商品资料包”文件夹后上传。\n4. 商品编码和商品序号由系统自动生成。",
        )
        archive.writestr(
            "员工商品资料包/商品图片/请把商品图片放在这里.txt",
            "请删除本提示文件后放入 JPEG、PNG 或 HEIC 商品图片。",
        )
    return package.getvalue()


def create_local_app(
    data_root: Path = DEFAULT_DATA_ROOT,
    vision_provider: VisionProvider | None = None,
    album_review_inbox: AlbumReviewInboxReader | None = None,
    album_sync_tenant_id: UUID | None = None,
    style_test_bridge_enabled: bool | None = None,
    style_test_catalog: Mapping[str, Mapping[str, object]] | None = None,
    style_bridge_client: StyleBridgeClient | None = None,
    classification_confirmation_writer: ClassificationConfirmationWriter | None = None,
    sales_handoff_service: SalesHandoffService | None = None,
    style_handoff_service: StyleHandoffService | None = None,
    material_evidence_runtime: MaterialEvidenceRuntime | None = None,
    warehouse_v3_runtime: WarehouseV3Runtime | None = None,
    employee_batch_promotion_service: EmployeeBatchPromotionRuntime | None = None,
) -> FastAPI:
    storage = LocalObjectStorage(data_root / "objects")
    repository = LocalTemplateRepository(data_root / "templates.json")
    upload_service = LocalUploadService(storage, repository)
    if material_evidence_runtime is None:
        raw_evidence_tenant = os.environ.get("LOCAL_ALBUM_SYNC_TENANT_ID")
        if raw_evidence_tenant is not None:
            try:
                evidence_tenant_id = UUID(raw_evidence_tenant)
                evidence_settings = Settings()  # type: ignore[call-arg]
                material_evidence_runtime = RuntimeMaterialEvidenceService(
                    database=Database(evidence_settings.database_url),
                    tenant_id=evidence_tenant_id,
                    objects=storage,
                    actor=MaterialEvidenceActorContext(
                        declared_by_ref=os.environ.get(
                            "LOCAL_MATERIAL_DECLARER_REF",
                            "local-merchant",
                        ),
                        reviewer_ref=os.environ.get(
                            "LOCAL_MATERIAL_REVIEWER_REF",
                            "local-trainer",
                        ),
                    ),
                )
            except (ValueError, ValidationError):
                material_evidence_runtime = None
    if warehouse_v3_runtime is None:
        raw_v3_tenant = os.environ.get("LOCAL_ALBUM_SYNC_TENANT_ID")
        if raw_v3_tenant is not None:
            try:
                v3_settings = Settings()  # type: ignore[call-arg]
                warehouse_v3_runtime = RuntimeWarehouseV3Service(
                    database=Database(v3_settings.database_url),
                    tenant_id=UUID(raw_v3_tenant),
                    style_secret=v3_settings.style_signing_secret.get_secret_value().encode(),
                    now=lambda: int(datetime.now(UTC).timestamp()),
                )
            except (ValueError, ValidationError):
                warehouse_v3_runtime = None
    training_labels_path = data_root / "training-labels.jsonl"
    training_version_store = TrainingLabelVersionStore(
        data_root / "training-label-versions.jsonl",
        training_labels_path,
    )
    pilot_acceptance_store = LocalPilotAcceptanceStore(data_root / "pilot-acceptances.jsonl")
    training_version_store.backfill_exact_matches()
    batch_store = LocalUploadBatchStore(data_root / "upload-batches.json")
    album_review_imports_path = data_root / "album-review-imports.json"
    employee_handoff_receipts = EmployeeHandoffReceiptStore(
        data_root / "employee-formal-handoffs.json"
    )
    if employee_batch_promotion_service is None:
        employee_batch_promotion_service = build_runtime_employee_batch_promotion_service(
            data_root,
            storage,
            batch_store,
            training_version_store,
        )

    def employee_handoff_error(error_code: str) -> JSONResponse:
        status_codes = {
            "employee_batch_review_incomplete": 409,
            "employee_batch_already_changed": 409,
            "employee_handoff_request_conflict": 409,
            "employee_handoff_ledger_conflict": 409,
            "employee_product_main_image_invalid": 422,
            "employee_product_form_unconfirmed": 422,
            "employee_source_image_unreadable": 422,
            "employee_handoff_storage_unavailable": 503,
        }
        messages = {
            "employee_batch_review_incomplete": "批次仍有图片未完成人工审核。",
            "employee_batch_already_changed": "审核后的图片内容已经变化，请重新核对批次。",
            "employee_handoff_request_conflict": "本次请求与已经记录的转正内容冲突。",
            "employee_handoff_ledger_conflict": "正式交接记录出现冲突，已停止写入。",
            "employee_product_main_image_invalid": "每件商品必须且只能有一张主图。",
            "employee_product_form_unconfirmed": "商品形态尚未完成人工确认。",
            "employee_source_image_unreadable": "本机审核图片无法读取或校验。",
            "employee_handoff_storage_unavailable": "私有图片存储暂时不可用。",
        }
        return JSONResponse(
            status_code=status_codes.get(error_code, 409),
            content={
                "ok": False,
                "error_code": error_code,
                "message_zh": messages.get(error_code, "正式交接包未生成，请核对错误代码。"),
            },
        )

    def require_batch_structure_mutable(batch_id: str) -> JSONResponse | None:
        try:
            receipt = employee_handoff_receipts.latest(batch_id)
        except EmployeeHandoffReceiptFailure as error:
            return employee_handoff_error(str(error))
        if receipt is None:
            return None
        return JSONResponse(
            status_code=409,
            content={
                "ok": False,
                "error_code": "employee_batch_promoted_immutable",
                "message_zh": "该批次已生成正式图片交接包，图片结构不能再修改。",
            },
        )
    if classification_confirmation_writer is None:
        raw_classification_tenant = os.environ.get("LOCAL_ALBUM_SYNC_TENANT_ID")
        if raw_classification_tenant is not None:
            try:
                classification_tenant_id = UUID(raw_classification_tenant)
                classification_settings = Settings()  # type: ignore[call-arg]
                classification_confirmation_writer = RuntimeClassificationConfirmationWriter(
                    database=Database(classification_settings.database_url),
                    tenant_id=classification_tenant_id,
                    batch_store=batch_store,
                    training_version_store=training_version_store,
                    album_review_imports_path=album_review_imports_path,
                )
            except (ValueError, ValidationError):
                classification_confirmation_writer = None
    if sales_handoff_service is None:
        raw_sales_tenant = os.environ.get("LOCAL_ALBUM_SYNC_TENANT_ID")
        if raw_sales_tenant is not None:
            try:
                sales_tenant_id = UUID(raw_sales_tenant)
                sales_settings = Settings()  # type: ignore[call-arg]
                sales_handoff_service = RuntimeSalesHandoffService(
                    database=Database(sales_settings.database_url),
                    tenant_id=sales_tenant_id,
                    secret=sales_settings.sales_signing_secret.get_secret_value().encode(),
                    now=lambda: int(datetime.now(UTC).timestamp()),
                )
            except (ValueError, ValidationError):
                sales_handoff_service = None
    if style_handoff_service is None:
        raw_style_tenant = os.environ.get("LOCAL_ALBUM_SYNC_TENANT_ID")
        if raw_style_tenant is not None:
            try:
                style_tenant_id = UUID(raw_style_tenant)
                style_settings = Settings()  # type: ignore[call-arg]
                style_handoff_service = RuntimeStyleHandoffService(
                    database=Database(style_settings.database_url),
                    tenant_id=style_tenant_id,
                    secret=style_settings.style_signing_secret.get_secret_value().encode(),
                    now=lambda: int(datetime.now(UTC).timestamp()),
                )
            except (ValueError, ValidationError):
                style_handoff_service = None
    recognition_store = RecognitionStore(data_root / "image-recognitions.jsonl")
    recognition_service = RecognitionService(
        recognition_store,
        vision_provider or build_default_vision_provider(),
    )
    bridge_enabled = (
        style_test_bridge_enabled
        if style_test_bridge_enabled is not None
        else (
            os.environ.get("STYLE_TEST_BRIDGE_ENABLED", "").casefold() == "true"
            or os.environ.get("LOCAL_CLASSIFICATION_BRIDGE", "").casefold() == "true"
        )
    )
    raw_catalog: Mapping[str, Mapping[str, object]] = style_test_catalog or {}
    if (
        bridge_enabled
        and style_test_catalog is None
        and os.environ.get("LOCAL_CLASSIFICATION_TEST_PRODUCT_CATALOG")
    ):
        try:
            loaded_catalog = json.loads(os.environ["LOCAL_CLASSIFICATION_TEST_PRODUCT_CATALOG"])
            raw_catalog = cast(Mapping[str, Mapping[str, object]], loaded_catalog)
        except json.JSONDecodeError:
            raw_catalog = {}
    test_catalog: dict[str, StyleTestCatalogEntry] = {}
    for catalog_key, value in raw_catalog.items() if bridge_enabled else ():
        entry = StyleTestCatalogEntry.model_validate(value)
        if catalog_key != entry.product_id:
            raise ValueError("style_test_catalog_key_mismatch")
        test_catalog[catalog_key] = entry
    bridge_client: StyleBridgeClient | None = style_bridge_client
    if bridge_enabled and bridge_client is None:
        nonce = os.environ.get("LOCAL_CLASSIFICATION_BRIDGE_NONCE")
        url = os.environ.get("LOCAL_CLASSIFICATION_BRIDGE_URL")
        if nonce and url:
            try:
                bridge_client = StyleBridgeClient(enabled=True, base_url=url, session_nonce=nonce)
            except StyleBridgeError:
                bridge_client = None
    handoff_registry = StyleHandoffIdempotencyRegistry()
    style_handoff_path = data_root / "style-test-handoffs.json"
    if bridge_enabled and style_handoff_path.exists():
        raw_handoffs = json.loads(style_handoff_path.read_text(encoding="utf-8"))
    else:
        raw_handoffs = []
    style_handoffs: dict[UUID, dict[str, object]] = {
        UUID(cast(str, entry["handoff_id"])): entry for entry in raw_handoffs
    }

    def save_style_handoffs() -> None:
        style_handoff_path.parent.mkdir(parents=True, exist_ok=True)
        style_handoff_path.write_text(
            json.dumps(list(style_handoffs.values()), ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )

    def style_fact_fingerprint(batch: UploadBatch) -> str:
        facts = {
            "items": [
                {
                    "id": str(item.item_id),
                    "sha256": item.sha256,
                    "facts": [
                        fact.model_dump(mode="json") for fact in item.photo_classification_facts
                    ],
                }
                for item in sorted(batch.items, key=lambda entry: str(entry.item_id))
            ],
            "permission": batch.style_test_permission.model_dump(mode="json")
            if batch.style_test_permission
            else None,
            "product_binding": (
                batch.style_test_product_draft_binding.model_dump(mode="json")
                if batch.style_test_product_draft_binding
                else None
            ),
            "product": batch.style_test_product_confirmation.model_dump(mode="json")
            if batch.style_test_product_confirmation
            else None,
        }
        return hashlib.sha256(
            json.dumps(facts, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def style_handoff_fingerprint_conflict() -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={
                "ok": False,
                "error_code": "style_handoff_fingerprint_conflict",
                "message_zh": "确认事实已变化，请建立新的测试批次。",
            },
        )

    def request_matches_submitted_batch(
        batch: UploadBatch,
        request: ConfirmAndGenerateRequest,
    ) -> bool:
        permission = batch.style_test_permission
        product = batch.style_test_product_confirmation
        product_binding = batch.style_test_product_draft_binding
        catalog_entry = test_catalog.get(request.product_id)
        return bool(
            permission
            and product
            and catalog_entry
            and request.transform_permission is True
            and permission.transform_permission is True
            and permission.customer_test_id == request.customer_test_id
            and permission.product_id == request.product_id
            and product.customer_test_id == request.customer_test_id
            and product.product_id == request.product_id
            and product.product_category == catalog_entry.product_category
            and product.product_form == catalog_entry.product_form
            and product.confirmation_version == permission.confirmation_version
            and product.main_photo_id == request.main_photo_id
            and (
                product_binding is None
                or (
                    product_binding.customer_test_id == request.customer_test_id
                    and product_binding.product_id == request.product_id
                    and product_binding.product_category == catalog_entry.product_category
                    and product_binding.product_form == catalog_entry.product_form
                )
            )
        )

    def existing_style_handoff(batch_id: str) -> dict[str, object] | None:
        return next(
            (record for record in style_handoffs.values() if record.get("batch_id") == batch_id),
            None,
        )

    photo_usage_classifier = LocalPhotoUsageClassifier() if bridge_enabled else None

    async def bridge_readiness_payload(
        batch: UploadBatch,
    ) -> tuple[dict[str, object], list[ConfirmedUploadItem]]:
        """Read stored facts and bounded service health without mutating either."""

        confirmed: list[ConfirmedUploadItem] = []
        item_views: list[dict[str, object]] = []
        decode_failed = False
        photo_conflict = False
        sealed = existing_style_handoff(batch.batch_id) is not None
        moved_item_ids = {audit.item_id for audit in batch.item_audit if audit.action == "moved"}

        def append_item_view(
            item: UploadBatchItem,
            state: str,
            **details: object,
        ) -> None:
            moved_into_batch = item.item_id in moved_item_ids
            recovery_actions: list[str] = []
            if not sealed:
                if state in {"failed", "duplicate"}:
                    recovery_actions = ["reupload", "delete", "move"]
                elif moved_into_batch:
                    recovery_actions = ["delete", "move"]
            item_views.append(
                {
                    "item": serialize_batch_item(item),
                    "state": state,
                    "moved_into_batch": moved_into_batch,
                    "recovery_actions": recovery_actions,
                    **details,
                }
            )

        permission = batch.style_test_permission
        product_binding = batch.style_test_product_draft_binding
        product_confirmation = batch.style_test_product_confirmation
        catalog_entry = (
            test_catalog.get(product_confirmation.product_id)
            if product_confirmation is not None
            else None
        )
        catalog_matches = bool(
            catalog_entry
            and product_confirmation
            and catalog_entry.customer_test_id == product_confirmation.customer_test_id
            and catalog_entry.product_id == product_confirmation.product_id
            and catalog_entry.product_category == product_confirmation.product_category
            and catalog_entry.product_form == product_confirmation.product_form
        )
        permission_matches = bool(
            permission
            and product_confirmation
            and permission.customer_test_id == product_confirmation.customer_test_id
            and permission.product_id == product_confirmation.product_id
            and permission.confirmation_version == product_confirmation.confirmation_version
        )
        service_available = False
        if bridge_client is not None:
            try:
                service_available = await bridge_client.health()
            except StyleBridgeError:
                service_available = False
        for item in batch.items:
            fact = item.photo_classification_facts[-1] if item.photo_classification_facts else None
            if item.status == "failed":
                append_item_view(
                    item,
                    "failed",
                    reason_code=item.error_code or "photo_decode_failed",
                )
                decode_failed = True
                continue
            if item.status == "duplicate":
                append_item_view(item, "duplicate")
                photo_conflict = True
                continue
            if (
                fact is None
                or item.object_key is None
                or item.sha256 is None
                or item.mime_type is None
            ):
                append_item_view(item, "pending")
                continue
            try:
                image_path = storage.root.joinpath(*PurePosixPath(item.object_key).parts)
                with Image.open(image_path) as image:
                    width, height = image.size
            except (OSError, UnidentifiedImageError, ValueError):
                append_item_view(item, "failed", reason_code="photo_decode_failed")
                decode_failed = True
                continue
            identity = ConfirmedProductIdentity(
                confirmed=catalog_matches,
                product_id=(product_confirmation.product_id if product_confirmation else ""),
                product_category=(catalog_entry.product_category if catalog_entry else None),
                product_form=(catalog_entry.product_form if catalog_entry else None),
            )
            confirmed.append(
                ConfirmedUploadItem(
                    photo_id=item.item_id,
                    content_sha256=item.sha256,
                    mime_type=cast(
                        Literal["image/jpeg", "image/png", "image/heic"], item.mime_type
                    ),
                    width=width,
                    height=height,
                    automatic_decision=fact.automatic_decision,
                    human_correction=fact.human_correction,
                    final_category=fact.final_category,
                    classifier_version=fact.classifier_version,
                    confirmation_version=fact.confirmation_version,
                    product_identity=identity,
                    is_main_photo=bool(
                        product_confirmation and item.item_id == product_confirmation.main_photo_id
                    ),
                    transform_permission=bool(
                        permission_matches and permission and permission.transform_permission
                    ),
                    conflict_free=True,
                    desensitized_test_only=True,
                    source="classification_local_page",
                    publishable=False,
                    production_eligible=False,
                    style_service_available=service_available,
                )
            )
            append_item_view(
                item,
                "exception" if fact.automatic_decision.confidence != "high" else "accepted",
                automatic_category=fact.automatic_decision.category,
                final_category=fact.final_category,
                confidence=fact.automatic_decision.confidence,
                classifier_version=fact.classifier_version,
                confirmation_version=fact.confirmation_version,
            )
        # These UUIDs are a preflight membership marker only: real receipt IDs are
        # supplied by Task 7 after staging, and are the only ones placed in a handoff.
        readiness = assess_style_handoff_readiness(
            confirmed,
            {entry.photo_id: entry.photo_id for entry in confirmed},
            product_id=(product_confirmation.product_id if product_confirmation else None),
        )
        missing = list(readiness.missing_codes)
        if decode_failed and "photo_classification_required" not in missing:
            missing.append("photo_classification_required")
        if photo_conflict and "photo_conflict_present" not in missing:
            missing.append("photo_conflict_present")
        photo_codes = {
            "photo_classification_required",
            "real_photo_required",
            "photo_conflict_present",
        }
        return {
            "ok": True,
            "ready": not missing,
            "can_confirm": not bool(photo_codes.intersection(missing)) and service_available,
            "batch_confirmation_version": batch_store.batch_confirmation_version(batch.batch_id),
            "missing_codes": missing,
            "sealed": sealed,
            "batch": serialize_batch(batch),
            "items": item_views,
            "permission": permission.model_dump(mode="json") if permission else None,
            "product_binding": (
                product_binding.model_dump(mode="json") if product_binding else None
            ),
            "product_confirmation": (
                product_confirmation.model_dump(mode="json") if product_confirmation else None
            ),
            "technical_details": {
                "classifier_version": "local-photo-usage-rules-v1",
                "receipt_ids": [],
                "output_job_ids": [],
                "handoff_id": None,
                "current_status": "needs_confirmation",
            },
        }, confirmed

    application = FastAPI(
        title="本地商品图片上传",
        description="只需选择图片，其余登记工作由系统自动完成。",
        version="0.1.0",
    )

    @application.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse("/local/templates")

    @application.get("/health", include_in_schema=False)
    async def health() -> JSONResponse:
        return JSONResponse(content={"status": "ok", "service": "product-engine-local"})

    @application.get(
        "/local/templates",
        response_class=HTMLResponse,
        include_in_schema=False,
    )
    async def upload_page() -> HTMLResponse:
        page = PAGE_PATH.read_text(encoding="utf-8")
        if bridge_enabled:
            page = page.replace("</body>", _style_bridge_panel() + "</body>")
        return HTMLResponse(page)

    @application.get(
        "/local/api/recognition-agent-status",
        summary="读取图片识别 Agent 连接状态",
    )
    async def recognition_agent_status() -> JSONResponse:
        provider = recognition_service.provider.provider
        connected = provider != "none"
        return JSONResponse(
            content={
                "status": "connected" if connected else "unavailable",
                "provider": provider,
                "label_zh": ("图片识别 Agent 已连接" if connected else "图片识别 Agent 未连接"),
                "external_upload": provider == "openai_vision",
            }
        )

    if bridge_enabled:

        def review_client() -> StyleBridgeClient:
            if bridge_client is None:
                raise StyleBridgeError("style_service_unavailable")
            return bridge_client

        @application.get(
            "/local/api/latest-style-handoff",
            summary="读取最近一次本地风格图任务",
        )
        async def latest_style_handoff() -> JSONResponse:
            if not style_handoffs:
                return JSONResponse(content={"ok": True, "handoff": None})
            latest = next(reversed(style_handoffs.values()))
            return JSONResponse(content={"ok": True, "handoff": latest})

        @application.get("/local/api/style-jobs/{job_id}")
        async def local_style_job(job_id: UUID) -> JSONResponse:
            try:
                result = await review_client().get_review_job(job_id)
                payload = cast(dict[str, object], jsonable_encoder(result))
                payload["status"] = result.session_status
                payload["synthetic_test_only"] = True
                payload["candidates"] = [
                    {
                        **jsonable_encoder(candidate),
                        "content_url": f"/local/api/style-jobs/{job_id}/candidates/{candidate.candidate_id}/image",
                        "synthetic_test_only": True,
                        "publishable": False,
                    }
                    for candidate in result.candidates
                ]
                return JSONResponse(content=payload)
            except StyleBridgeError as error:
                return _local_review_error(error)

        @application.get("/local/api/style-jobs/{job_id}/candidates/{candidate_id}/image")
        async def local_style_candidate_image(job_id: UUID, candidate_id: UUID) -> Response:
            try:
                image = await review_client().get_candidate_content(job_id, candidate_id)
                return StreamingResponse(
                    iter((image.content,)),
                    media_type=image.mime_type,
                    headers={"Cache-Control": "no-store"},
                )
            except StyleBridgeError as error:
                return _local_review_error(error)

        @application.get("/local/api/style-jobs/{job_id}/assets/{asset_id}/image")
        async def local_style_real_image(job_id: UUID, asset_id: UUID) -> Response:
            try:
                image = await review_client().get_real_content(job_id, asset_id)
                return StreamingResponse(
                    iter((image.content,)),
                    media_type=image.mime_type,
                    headers={"Cache-Control": "no-store"},
                )
            except StyleBridgeError as error:
                return _local_review_error(error)

        @application.post("/local/api/style-jobs/{job_id}/candidates/{candidate_id}/review")
        async def local_style_review(
            job_id: UUID, candidate_id: UUID, request: LocalReviewRequest
        ) -> JSONResponse:
            try:
                result = await review_client().review_candidate(
                    job_id,
                    candidate_id,
                    decision=request.decision,
                    reason=request.reason,
                    idempotency_key=request.idempotency_key,
                )
                payload = cast(dict[str, object], jsonable_encoder(result))
                payload["job_status"] = result.status
                payload["status"] = result.session_status
                payload["synthetic_test_only"] = True
                payload["product_associations"] = [
                    {
                        "product_id": product_id,
                        "approved_image": _local_product_image(
                            approved, fallback_job_id=result.job_id
                        ),
                        "real_photo": _local_product_image(real, fallback_job_id=result.job_id),
                    }
                    for product_id, approved, real in result.product_associations
                ]
                return JSONResponse(content=payload)
            except StyleBridgeError as error:
                return _local_review_error(error)

        @application.get("/local/api/style-products/{product_id}/images")
        async def local_style_product_images(
            product_id: ProductId, handoff_id: UUID, view: Literal["default", "real"] = "default"
        ) -> JSONResponse:
            try:
                result = await review_client().get_product_images(product_id, handoff_id, view=view)
                payload = cast(dict[str, object], jsonable_encoder(result))
                payload["synthetic_test_only"] = True
                payload["images"] = [_local_product_image(image) for image in result.images]
                return JSONResponse(content=payload)
            except StyleBridgeError as error:
                return _local_review_error(error)

        @application.get("/local/api/style-test-products", summary="读取脱敏测试商品白名单")
        async def style_test_products() -> JSONResponse:
            return JSONResponse(
                content={
                    "ok": True,
                    "products": [entry.model_dump(mode="json") for entry in test_catalog.values()],
                }
            )

        @application.get(
            "/local/api/style-test-target-batches",
            summary="读取尚未交接的脱敏测试目标批次",
        )
        async def style_test_target_batches() -> JSONResponse:
            targets: list[dict[str, str]] = []
            for batch in batch_store.list_batches():
                if existing_style_handoff(batch.batch_id) is not None:
                    continue
                product = (
                    batch.style_test_product_confirmation or batch.style_test_product_draft_binding
                )
                if product is None:
                    for catalog_entry in test_catalog.values():
                        targets.append(
                            {
                                "batch_id": batch.batch_id,
                                "product_id": catalog_entry.product_id,
                                "display_label": (
                                    catalog_entry.display_label or catalog_entry.product_id
                                ),
                            }
                        )
                    continue
                bound_catalog_entry = test_catalog.get(product.product_id)
                if (
                    bound_catalog_entry is None
                    or bound_catalog_entry.customer_test_id != product.customer_test_id
                    or bound_catalog_entry.product_category != product.product_category
                    or bound_catalog_entry.product_form != product.product_form
                ):
                    continue
                targets.append(
                    {
                        "batch_id": batch.batch_id,
                        "product_id": bound_catalog_entry.product_id,
                        "display_label": (
                            bound_catalog_entry.display_label or bound_catalog_entry.product_id
                        ),
                    }
                )
            return JSONResponse(content={"ok": True, "targets": targets})

        @application.get("/local/assets/photo-usage-preview.mjs", include_in_schema=False)
        async def photo_usage_preview_script() -> FileResponse:
            return FileResponse(PHOTO_USAGE_PREVIEW_PATH, media_type="text/javascript")

        @application.get("/local/assets/style-candidate-review.mjs", include_in_schema=False)
        async def style_candidate_review_script() -> FileResponse:
            return FileResponse(STYLE_CANDIDATE_REVIEW_PATH, media_type="text/javascript")

        @application.delete("/local/api/upload-batches/{batch_id}/items/{item_id}")
        async def delete_bridge_item(batch_id: str, item_id: UUID) -> JSONResponse:
            if blocked := require_batch_structure_mutable(batch_id):
                return blocked
            if existing_style_handoff(batch_id) is not None:
                return batch_error_response("style_test_batch_sealed")
            try:
                batch_store.remove_item(batch_id, item_id)
            except LocalUploadBatchFailure as error:
                return batch_error_response(str(error))
            batch = batch_store.get(batch_id)
            assert batch is not None
            payload, _ = await bridge_readiness_payload(batch)
            return JSONResponse(content=payload)

        @application.post("/local/api/upload-batches/{batch_id}/items/{item_id}/move")
        async def move_bridge_item(
            batch_id: str, item_id: UUID, request: MoveBatchItemRequest
        ) -> JSONResponse:
            if blocked := require_batch_structure_mutable(batch_id):
                return blocked
            if existing_style_handoff(batch_id) is not None:
                return batch_error_response("style_test_batch_sealed")
            try:
                batch_store.move_item(batch_id, item_id, request.to_position)
            except LocalUploadBatchFailure as error:
                return batch_error_response(str(error))
            batch = batch_store.get(batch_id)
            assert batch is not None
            payload, _ = await bridge_readiness_payload(batch)
            return JSONResponse(content=payload)

        @application.post("/local/api/upload-batches/{batch_id}/items/{item_id}/move-to-batch")
        async def move_bridge_item_to_batch(
            batch_id: str, item_id: UUID, request: MoveToBatchRequest
        ) -> JSONResponse:
            if blocked := require_batch_structure_mutable(batch_id):
                return blocked
            if blocked := require_batch_structure_mutable(request.target_batch_id):
                return blocked
            target = batch_store.get(request.target_batch_id)
            if target is None:
                return batch_error_response("target_batch_not_found")
            if (
                existing_style_handoff(batch_id) is not None
                or existing_style_handoff(request.target_batch_id) is not None
            ):
                return batch_error_response("style_test_batch_sealed")
            catalog_entry = test_catalog.get(request.target_product_id)
            if catalog_entry is None:
                return JSONResponse(
                    status_code=409,
                    content={
                        "ok": False,
                        "error_code": "test_product_not_allowlisted",
                        "message_zh": "请选择启动时提供的脱敏测试商品，不能手填商品身份。",
                    },
                )
            try:
                batch_store.move_item_to_batch(
                    batch_id,
                    item_id,
                    request.target_batch_id,
                    request.to_position,
                    target_customer_test_id=catalog_entry.customer_test_id,
                    target_product_id=catalog_entry.product_id,
                    target_product_category=catalog_entry.product_category,
                    target_product_form=catalog_entry.product_form,
                )
            except LocalUploadBatchFailure as error:
                return batch_error_response(str(error))
            target = batch_store.get(request.target_batch_id)
            assert target is not None
            payload, _ = await bridge_readiness_payload(target)
            return JSONResponse(content=payload)

        @application.get(
            "/local/api/upload-batches/{batch_id}/style-readiness",
            summary="查看本地风格测试放行状态",
        )
        async def style_readiness(batch_id: str) -> JSONResponse:
            batch = batch_store.get(batch_id)
            if batch is None:
                return batch_error_response("batch_not_found")
            payload, _ = await bridge_readiness_payload(batch)
            return JSONResponse(content=payload)

        @application.post(
            "/local/api/upload-batches/{batch_id}/items/{item_id}/photo-category-corrections",
            summary="追加低置信度图片用途更正事实",
        )
        async def correct_photo_category(
            batch_id: str,
            item_id: UUID,
            request: PhotoCategoryCorrectionRequest,
        ) -> JSONResponse:
            batch = batch_store.get(batch_id)
            if batch is None:
                return batch_error_response("batch_not_found")
            if existing_style_handoff(batch_id) is not None:
                return batch_error_response("style_test_batch_sealed")
            item = next((entry for entry in batch.items if entry.item_id == item_id), None)
            if item is None or item.status == "failed" or not item.photo_classification_facts:
                return batch_error_response("item_not_confirmable")
            if item.photo_classification_facts[0].automatic_decision.confidence == "high":
                return batch_error_response("high_confidence_photo_not_editable")
            try:
                batch_store.apply_photo_classification_correction(
                    batch_id,
                    item_id,
                    corrected_category=cast(
                        Literal["real_photo", "wear_photo", "scene_photo", "multi_product_photo"],
                        request.corrected_category,
                    ),
                    reviewer_note=request.reviewer_note,
                )
            except (LocalUploadBatchFailure, ValueError) as error:
                return batch_error_response(str(error))
            updated = batch_store.get(batch_id)
            assert updated is not None
            payload, _ = await bridge_readiness_payload(updated)
            return JSONResponse(content=payload)

        @application.post(
            "/local/api/upload-batches/{batch_id}/confirm-and-generate",
            summary="确认本地小试权限并生成风格图",
        )
        async def confirm_and_generate(
            batch_id: str,
            request: ConfirmAndGenerateRequest,
        ) -> JSONResponse:
            existing_batch = batch_store.get(batch_id)
            if existing_batch is None:
                return batch_error_response("batch_not_found")
            existing = existing_style_handoff(batch_id)
            if existing is not None:
                if existing.get("fingerprint") != style_fact_fingerprint(
                    existing_batch
                ) or not request_matches_submitted_batch(existing_batch, request):
                    return style_handoff_fingerprint_conflict()
                return JSONResponse(content={"ok": True, **existing, "created": False})
            catalog_entry = test_catalog.get(request.product_id)
            if catalog_entry is None or catalog_entry.customer_test_id != request.customer_test_id:
                return JSONResponse(
                    status_code=409,
                    content={
                        "ok": False,
                        "error_code": "test_product_not_allowlisted",
                        "message_zh": "请选择启动时提供的脱敏测试商品，不能手填商品身份。",
                    },
                )
            product_binding = existing_batch.style_test_product_draft_binding
            if product_binding is not None and (
                product_binding.customer_test_id != catalog_entry.customer_test_id
                or product_binding.product_id != catalog_entry.product_id
                or product_binding.product_category != catalog_entry.product_category
                or product_binding.product_form != catalog_entry.product_form
            ):
                return batch_error_response("target_product_binding_conflict")
            if request.transform_permission is not True:
                return JSONResponse(
                    status_code=409,
                    content={"ok": False, "missing_codes": ["transform_permission_required"]},
                )
            main_photo = next(
                (entry for entry in existing_batch.items if entry.item_id == request.main_photo_id),
                None,
            )
            if main_photo is None or main_photo.status not in {"pending_review", "reviewed"}:
                return JSONResponse(
                    status_code=409,
                    content={
                        "ok": False,
                        "error_code": "main_photo_required",
                        "message_zh": "请选择本批次中可正常使用的一张主图。",
                    },
                )
            try:
                batch_store.bind_style_test_product(
                    batch_id,
                    customer_test_id=catalog_entry.customer_test_id,
                    product_id=catalog_entry.product_id,
                    product_category=catalog_entry.product_category,
                    product_form=catalog_entry.product_form,
                )
                batch_store.confirm_style_test_batch(
                    batch_id,
                    customer_test_id=catalog_entry.customer_test_id,
                    product_id=catalog_entry.product_id,
                    product_category=catalog_entry.product_category,
                    product_form=catalog_entry.product_form,
                    main_photo_id=request.main_photo_id,
                    transform_permission=True,
                )
            except LocalUploadBatchFailure as error:
                return batch_error_response(str(error))
            batch = batch_store.get(batch_id)
            assert batch is not None
            readiness, confirmed = await bridge_readiness_payload(batch)
            if not readiness["ready"]:
                response_payload = {**readiness, "ok": False}
                if readiness["missing_codes"] == ["style_service_unavailable"]:
                    response_payload.update(
                        retryable=True,
                        error_code="style_service_unavailable",
                    )
                return JSONResponse(status_code=409, content=response_payload)
            existing = existing_style_handoff(batch_id)
            if existing is not None:
                if existing.get("fingerprint") != style_fact_fingerprint(
                    batch
                ) or not request_matches_submitted_batch(batch, request):
                    return style_handoff_fingerprint_conflict()
                return JSONResponse(content={"ok": True, **existing, "created": False})
            if bridge_client is None:
                return JSONResponse(
                    status_code=503,
                    content={
                        "ok": False,
                        "error_code": "style_service_unavailable",
                        "message_zh": "本地测试服务配置不可用。",
                    },
                )
            photos: list[ConfirmedLocalPhoto] = []
            for item in batch.items:
                if (
                    item.item_id not in {entry.photo_id for entry in confirmed}
                    or item.object_key is None
                ):
                    continue
                image_path = storage.root.joinpath(*PurePosixPath(item.object_key).parts)
                with Image.open(image_path) as image:
                    width, height = image.size

                def open_content(path: Path = image_path) -> BinaryIO:
                    return path.open("rb")

                assert item.sha256 is not None and item.mime_type is not None
                photos.append(
                    ConfirmedLocalPhoto(
                        photo_id=item.item_id,
                        content_sha256=item.sha256,
                        mime_type=cast(
                            Literal["image/jpeg", "image/png", "image/heic"], item.mime_type
                        ),
                        width=width,
                        height=height,
                        open_content=open_content,
                    )
                )
            try:
                result = await bridge_client.stage_and_submit(
                    photos,
                    lambda receipts: handoff_registry.build(
                        customer_test_id=catalog_entry.customer_test_id,
                        product_id=catalog_entry.product_id,
                        confirmed_items=confirmed,
                        media_receipts={
                            photo_id: receipt.receipt_id for photo_id, receipt in receipts.items()
                        },
                    ),
                )
            except StyleBridgeError as error:
                return JSONResponse(
                    status_code=503,
                    content={
                        "ok": False,
                        "error_code": error.code,
                        "message_zh": "本地测试服务暂时不可用。",
                    },
                )
            except StyleHandoffFailure as error:
                return JSONResponse(
                    status_code=409,
                    content={
                        "ok": False,
                        "error_code": str(error),
                        "message_zh": "本批商品版本与已有交接记录冲突，请刷新后新建批次。",
                    },
                )
            record: dict[str, object] = {
                "handoff_id": str(result.handoff_id),
                "batch_id": batch_id,
                "fingerprint": style_fact_fingerprint(batch),
                "current_status": result.status,
                "output_job_ids": [str(job_id) for job_id in result.output_job_ids],
                "created": result.created,
                "publishable": False,
            }
            result_output_jobs = getattr(result, "output_jobs", ())
            if result_output_jobs:
                record["output_jobs"] = jsonable_encoder(result_output_jobs)
            style_handoffs[result.handoff_id] = record
            save_style_handoffs()
            return JSONResponse(content={"ok": True, **record})

        @application.get(
            "/local/api/style-handoffs/{handoff_id}",
            summary="读取本地风格图任务状态",
        )
        async def get_style_handoff(handoff_id: UUID) -> JSONResponse:
            record = style_handoffs.get(handoff_id)
            if record is not None:
                output_jobs: object = []
                if isinstance(bridge_client, StyleBridgeClient):
                    try:
                        replay = await bridge_client.get_handoff(handoff_id)
                        record["current_status"] = replay.status
                        record["output_job_ids"] = [str(job_id) for job_id in replay.output_job_ids]
                        record["output_jobs"] = jsonable_encoder(replay.output_jobs)
                        output_jobs = jsonable_encoder(replay.output_jobs)
                        save_style_handoffs()
                    except StyleBridgeError as error:
                        return JSONResponse(
                            status_code=503,
                            content={
                                "ok": False,
                                "error_code": error.code,
                                "retryable": bool(getattr(error, "retryable", True)),
                                "message_zh": "暂时无法读取风格图任务，请稍后重试。",
                            },
                        )
                return JSONResponse(
                    content={"ok": True, "handoff": {**record, "output_jobs": output_jobs}}
                )
            return JSONResponse(
                status_code=404,
                content={"ok": False, "error_code": "style_handoff_not_found"},
            )

    @application.get(
        "/local/api/album-review-inbox",
        summary="查看相册整理送达的待审核商品",
    )
    async def album_review_products() -> JSONResponse:
        if album_review_inbox is None or album_sync_tenant_id is None:
            return JSONResponse(
                content={
                    "ok": True,
                    "status": "unconfigured",
                    "message_zh": "当前页面尚未连接正式相册收件箱。",
                    "products": [],
                    "summary": {
                        "product_count": 0,
                        "image_count": 0,
                        "latest_received_at": None,
                    },
                }
            )
        products = await album_review_inbox.list_latest(album_sync_tenant_id)
        payload: list[dict[str, object]] = []
        for product in products:
            product_payload = product.model_dump(mode="json")
            for image in product_payload["images"]:
                assert isinstance(image, dict)
                image["image_url"] = (
                    "/local/api/album-review-inbox/"
                    f"{quote(product.product_code, safe='')}/versions/"
                    f"{product.package_version}/assets/{image['asset_id']}/image"
                )
            payload.append(product_payload)
        latest_received_at = max(
            (product.received_at for product in products if product.received_at is not None),
            default=None,
        )
        return JSONResponse(
            content={
                "ok": True,
                "status": "connected",
                "message_zh": "已连接相册整理正式收件箱。",
                "products": payload,
                "summary": {
                    "product_count": len(products),
                    "image_count": sum(len(product.images) for product in products),
                    "latest_received_at": (
                        latest_received_at.isoformat().replace("+00:00", "Z")
                        if latest_received_at is not None
                        else None
                    ),
                },
            }
        )

    @application.get(
        "/local/api/album-review-inbox/{product_code}/versions/"
        "{package_version}/assets/{asset_id}/image",
        include_in_schema=False,
    )
    async def album_review_image(
        product_code: str,
        package_version: int,
        asset_id: UUID,
    ) -> Response:
        if album_review_inbox is None or album_sync_tenant_id is None:
            return JSONResponse(
                status_code=404,
                content={
                    "ok": False,
                    "error_code": "album_image_not_found",
                    "message_zh": "这张相册图片当前不可读取。",
                    "next_step_zh": "请回到相册整理确认图片仍然可用后重新同步。",
                },
            )
        try:
            image = await album_review_inbox.read_image(
                album_sync_tenant_id,
                product_code,
                package_version,
                asset_id,
            )
        except AlbumReviewInboxFailure:
            return JSONResponse(
                status_code=404,
                content={
                    "ok": False,
                    "error_code": "album_image_not_found",
                    "message_zh": "这张相册图片当前不可读取。",
                    "next_step_zh": "请回到相册整理确认图片仍然可用后重新同步。",
                },
            )
        return Response(content=image.content, media_type=image.mime_type)

    @application.post(
        "/local/api/formal-product-packages/{product_code}/versions/"
        "{package_version}/classification-confirmations",
        summary="将已完成的人工分类写入正式分类账本",
    )
    @application.post(
        "/local/api/album-review-inbox/{product_code}/versions/"
        "{package_version}/classification-confirmations",
        summary="将已完成的人工分类写入正式分类账本",
    )
    async def confirm_product_classification(
        product_code: str,
        package_version: int,
        request: ClassificationConfirmationRequest,
    ) -> JSONResponse:
        if classification_confirmation_writer is None:
            return JSONResponse(
                status_code=404,
                content={
                    "ok": False,
                    "error_code": "classification_writeback_unconfigured",
                    "message_zh": "当前本机尚未连接分类账本。",
                },
            )
        batch = batch_store.get(request.local_batch_id)
        product_items = (
            []
            if batch is None
            else [item for item in batch.items if item.product_code == product_code]
        )
        if not product_items or any(item.status != "reviewed" for item in product_items):
            return JSONResponse(
                status_code=409,
                content={
                    "ok": False,
                    "error_code": "product_review_incomplete",
                    "message_zh": "该商品仍有图片未完成人工审核。",
                },
            )
        try:
            receipt = await classification_confirmation_writer.confirm(
                product_code=product_code,
                package_version=package_version,
                request_id=request.request_id,
                local_batch_id=request.local_batch_id,
            )
        except ClassificationConfirmationFailure as error:
            return JSONResponse(
                status_code=409,
                content={
                    "ok": False,
                    "error_code": str(error),
                    "message_zh": "本次分类未写入，请按错误代码核对本机审核记录。",
                },
            )
        return JSONResponse(
            status_code=201 if receipt.disposition == "created" else 200,
            content={"ok": True, **receipt.model_dump(mode="json")},
        )

    material_evidence_conflicts = {
        "stale_classification",
        "material_evidence_request_conflict",
        "material_evidence_revocation_required",
        "material_evidence_invalid",
        "material_evidence_ledger_conflict",
    }
    material_evidence_validation_errors = {
        "unsafe_filename",
        "mime_type_mismatch",
        "unsupported_evidence_file",
        "material_evidence_file_too_large",
        "material_evidence_attachment_required",
        "material_evidence_upload_not_staged",
        "material_evidence_hash_mismatch",
        "material_evidence_kind_mismatch",
        "material_declaration_acknowledgement_required",
        "material_evidence_attachment_incomplete",
    }
    material_evidence_not_found = {
        "material_evidence_product_not_found",
        "material_evidence_classification_not_found",
        "material_evidence_not_found",
    }

    def material_evidence_error(error_code: str) -> JSONResponse:
        if error_code in material_evidence_conflicts:
            status_code = 409
            safe_code = error_code
        elif error_code in material_evidence_validation_errors:
            status_code = 422
            safe_code = error_code
        elif (
            error_code in material_evidence_not_found
            or error_code == "material_evidence_unconfigured"
        ):
            status_code = 404
            safe_code = error_code
        else:
            status_code = 503
            safe_code = "material_evidence_unavailable"
        return JSONResponse(
            status_code=status_code,
            content={
                "ok": False,
                "error_code": safe_code,
                "message_zh": "材质依据未写入，当前分类底账保持不变。",
                "next_step_zh": "请按错误代码核对最新分类版本、附件和人工确认。",
            },
        )

    @application.post(
        "/local/api/products/{product_code}/material-evidence-uploads",
        summary="暂存本机材质依据附件",
    )
    async def upload_material_evidence(
        product_code: str,
        file: Annotated[UploadFile | None, File()] = None,
    ) -> JSONResponse:
        if material_evidence_runtime is None:
            return material_evidence_error("material_evidence_unconfigured")
        if file is None or not file.filename:
            return material_evidence_error("unsupported_evidence_file")
        content = await file.read(MAX_EVIDENCE_BYTES + 1)
        try:
            receipt = await material_evidence_runtime.upload(
                product_code,
                file.filename,
                file.content_type or "application/octet-stream",
                content,
            )
        except (MaterialEvidenceFailure, MaterialEvidenceUploadFailure) as error:
            return material_evidence_error(str(error))
        return JSONResponse(status_code=201, content=receipt.model_dump(mode="json"))

    @application.post(
        "/local/api/products/{product_code}/material-evidence",
        summary="登记材质依据并追加分类版本",
    )
    async def register_material_evidence(
        product_code: str,
        request: MaterialEvidenceRegistrationInput,
    ) -> JSONResponse:
        if material_evidence_runtime is None:
            return material_evidence_error("material_evidence_unconfigured")
        try:
            receipt = await material_evidence_runtime.register(product_code, request)
        except (MaterialEvidenceFailure, MaterialEvidenceUploadFailure) as error:
            return material_evidence_error(str(error))
        except ValidationError:
            return material_evidence_error("material_evidence_invalid")
        return JSONResponse(
            status_code=201 if receipt.disposition == "created" else 200,
            content=receipt.model_dump(mode="json"),
        )

    @application.post(
        "/local/api/products/{product_code}/material-evidence/insufficient",
        summary="明确保持材质信息不足",
    )
    async def keep_material_evidence_insufficient(
        product_code: str,
        request: MaterialEvidenceInsufficientInput,
    ) -> JSONResponse:
        if material_evidence_runtime is None:
            return material_evidence_error("material_evidence_unconfigured")
        try:
            receipt = await material_evidence_runtime.keep_insufficient(
                product_code,
                request,
            )
        except MaterialEvidenceFailure as error:
            return material_evidence_error(str(error))
        return JSONResponse(
            status_code=201 if receipt.disposition == "created" else 200,
            content=receipt.model_dump(mode="json"),
        )

    @application.post(
        "/local/api/material-evidence/{evidence_id}/revocations",
        summary="撤回材质依据并追加拦截版本",
    )
    async def revoke_material_evidence(
        evidence_id: UUID,
        request: MaterialEvidenceRevocationInput,
    ) -> JSONResponse:
        if material_evidence_runtime is None:
            return material_evidence_error("material_evidence_unconfigured")
        try:
            receipt = await material_evidence_runtime.revoke(evidence_id, request)
        except MaterialEvidenceFailure as error:
            return material_evidence_error(str(error))
        return JSONResponse(
            status_code=201 if receipt.disposition == "created" else 200,
            content=receipt.model_dump(mode="json"),
        )

    @application.get(
        "/local/api/products/{product_code}/material-evidence",
        summary="读取商品最新材质依据状态",
    )
    async def read_material_evidence(product_code: str) -> JSONResponse:
        if material_evidence_runtime is None:
            return material_evidence_error("material_evidence_unconfigured")
        try:
            view = await material_evidence_runtime.read(product_code)
        except MaterialEvidenceFailure as error:
            return material_evidence_error(str(error))
        return JSONResponse(content=view.model_dump(mode="json"))

    def warehouse_v3_error(error_code: str) -> JSONResponse:
        status_code = (
            404
            if error_code
            in {
                "warehouse_v3_unconfigured",
                "warehouse_v3_receipt_not_found",
                "classification_not_found",
                "warehouse_handoff_not_found",
                "style_handoff_not_found",
            }
            else 409
        )
        return JSONResponse(
            status_code=status_code,
            content={
                "ok": False,
                "error_code": error_code,
                "message_zh": "V3 仓间动作未完成，原底账保持不变。",
            },
        )

    @application.post(
        "/local/api/v3/classification-versions/{classification_id}/prepare-sales-preview"
    )
    async def prepare_sales_preview_v3(classification_id: UUID) -> JSONResponse:
        if warehouse_v3_runtime is None:
            return warehouse_v3_error("warehouse_v3_unconfigured")
        try:
            value = await warehouse_v3_runtime.prepare_sales(classification_id)
        except RuntimeWarehouseV3Failure as error:
            return warehouse_v3_error(str(error))
        return JSONResponse(
            status_code=201, content={"ok": True, "result": jsonable_encoder(value)}
        )

    @application.post("/local/api/v3/sales-handoffs/{handoff_id}/deliver")
    async def deliver_sales_handoff_v3(handoff_id: UUID) -> JSONResponse:
        if warehouse_v3_runtime is None:
            return warehouse_v3_error("warehouse_v3_unconfigured")
        try:
            value = await warehouse_v3_runtime.deliver_sales(handoff_id)
        except RuntimeWarehouseV3Failure as error:
            return warehouse_v3_error(str(error))
        return JSONResponse(content={"ok": True, "receipt": jsonable_encoder(value)})

    @application.post(
        "/local/api/v3/classification-versions/{classification_id}/prepare-style-eligibility"
    )
    async def prepare_style_eligibility_v3(classification_id: UUID) -> JSONResponse:
        if warehouse_v3_runtime is None:
            return warehouse_v3_error("warehouse_v3_unconfigured")
        try:
            value = await warehouse_v3_runtime.prepare_style(classification_id)
        except RuntimeWarehouseV3Failure as error:
            return warehouse_v3_error(str(error))
        return JSONResponse(
            status_code=201, content={"ok": True, "result": jsonable_encoder(value)}
        )

    @application.post("/local/api/v3/style-handoffs/{handoff_id}/evaluate")
    async def evaluate_style_handoff_v3(handoff_id: UUID) -> JSONResponse:
        if warehouse_v3_runtime is None:
            return warehouse_v3_error("warehouse_v3_unconfigured")
        try:
            value = await warehouse_v3_runtime.evaluate_style(handoff_id)
        except RuntimeWarehouseV3Failure as error:
            return warehouse_v3_error(str(error))
        return JSONResponse(content={"ok": True, "receipt": jsonable_encoder(value)})

    @application.get("/local/api/v3/receipts/{receipt_id}")
    async def read_warehouse_receipt_v3(receipt_id: UUID) -> JSONResponse:
        if warehouse_v3_runtime is None:
            return warehouse_v3_error("warehouse_v3_unconfigured")
        try:
            value = await warehouse_v3_runtime.read_receipt(receipt_id)
        except RuntimeWarehouseV3Failure as error:
            return warehouse_v3_error(str(error))
        return JSONResponse(content={"ok": True, "receipt": jsonable_encoder(value)})

    def sales_handoff_error(error_code: str) -> JSONResponse:
        status_code = (
            404
            if error_code
            in {
                "classification_not_found",
                "warehouse_handoff_not_found",
                "sales_handoff_unconfigured",
            }
            else 409
        )
        return JSONResponse(
            status_code=status_code,
            content={
                "ok": False,
                "error_code": error_code,
                "message_zh": "销售仓交接未完成，已保留当前底账。",
                "next_step_zh": "请根据错误代码刷新最新分类版本后重试。",
            },
        )

    @application.post(
        "/local/api/classification-versions/{classification_id}/prepare-sales-preview",
        summary="从权威分类准备销售预览",
    )
    async def prepare_sales_preview(classification_id: UUID) -> JSONResponse:
        if sales_handoff_service is None:
            return sales_handoff_error("sales_handoff_unconfigured")
        try:
            result = await sales_handoff_service.prepare(classification_id)
        except RuntimeSalesHandoffFailure as error:
            return sales_handoff_error(str(error))
        return JSONResponse(
            status_code=201 if result.created else 200,
            content={
                "ok": True,
                "created": result.created,
                "preview": jsonable_encoder(result.payload),
                **jsonable_encoder(result.handoff),
            },
        )

    @application.post(
        "/local/api/warehouse-handoffs/{handoff_id}/deliver",
        summary="显式发送销售预览并等待销售仓签收",
    )
    async def deliver_sales_handoff(handoff_id: UUID) -> JSONResponse:
        if sales_handoff_service is None:
            return sales_handoff_error("sales_handoff_unconfigured")
        try:
            handoff = await sales_handoff_service.deliver(handoff_id)
        except RuntimeSalesHandoffFailure as error:
            return sales_handoff_error(str(error))
        return JSONResponse(content={"ok": True, **jsonable_encoder(handoff)})

    @application.get(
        "/local/api/warehouse-handoffs/{handoff_id}",
        summary="读取销售仓交接和独立回执",
    )
    async def read_sales_handoff(handoff_id: UUID) -> JSONResponse:
        if sales_handoff_service is None:
            return sales_handoff_error("sales_handoff_unconfigured")
        try:
            result = await sales_handoff_service.read(handoff_id)
        except RuntimeSalesHandoffFailure as error:
            return sales_handoff_error(str(error))
        return JSONResponse(
            content={
                "ok": True,
                "preview": jsonable_encoder(result.payload),
                **jsonable_encoder(result.handoff),
            }
        )

    style_error_codes = {
        "style_handoff_unconfigured",
        "classification_not_found",
        "style_handoff_not_found",
        "style_authorization_not_found",
        "classification_not_latest",
        "classification_ledger_conflict",
        "style_authorization_conflict",
        "style_authorization_expired",
        "style_eligibility_conflict",
        "style_handoff_conflict",
        "style_payload_hash_mismatch",
        "invalid_style_warehouse_signature",
    }

    def style_handoff_error(error_code: str) -> JSONResponse:
        safe_code = error_code if error_code in style_error_codes else "style_handoff_conflict"
        status_code = (
            404
            if safe_code
            in {
                "style_handoff_unconfigured",
                "classification_not_found",
                "style_handoff_not_found",
                "style_authorization_not_found",
            }
            else 409
        )
        return JSONResponse(
            status_code=status_code,
            content={
                "ok": False,
                "error_code": safe_code,
                "message_zh": "风格仓资格校验未完成，当前底账未被覆盖。",
                "next_step_zh": "请根据错误代码核对最新分类、授权和图片后重试。",
            },
        )

    @application.post(
        "/local/api/classification-versions/{classification_id}/prepare-style-eligibility",
        summary="从权威分类准备风格仓资格输入",
    )
    async def prepare_style_eligibility(classification_id: UUID) -> JSONResponse:
        if style_handoff_service is None:
            return style_handoff_error("style_handoff_unconfigured")
        try:
            result = await style_handoff_service.prepare(classification_id)
        except RuntimeStyleHandoffFailure as error:
            return style_handoff_error(str(error))
        return JSONResponse(
            status_code=201 if result.created else 200,
            content={
                "ok": True,
                "created": result.created,
                "eligibility_input_id": str(result.eligibility_input_id),
                "eligibility": jsonable_encoder(result.payload),
                **jsonable_encoder(result.handoff),
            },
        )

    @application.post(
        "/local/api/classification-versions/{classification_id}/style-transform-authorizations",
        summary="记录本机风格转换授权",
    )
    async def create_style_authorization(
        classification_id: UUID,
        request: StyleAuthorizationRequest,
    ) -> JSONResponse:
        if style_handoff_service is None:
            return style_handoff_error("style_handoff_unconfigured")
        try:
            value = await style_handoff_service.authorize(classification_id, request)
        except RuntimeStyleHandoffFailure as error:
            return style_handoff_error(str(error))
        return JSONResponse(status_code=201, content={"ok": True, **jsonable_encoder(value)})

    @application.post(
        "/local/api/style-transform-authorizations/{authorization_id}/revoke",
        summary="显式撤回本机风格转换授权",
    )
    async def revoke_style_authorization(authorization_id: UUID) -> JSONResponse:
        if style_handoff_service is None:
            return style_handoff_error("style_handoff_unconfigured")
        try:
            value = await style_handoff_service.revoke(authorization_id)
        except RuntimeStyleHandoffFailure as error:
            return style_handoff_error(str(error))
        return JSONResponse(content={"ok": True, **jsonable_encoder(value)})

    def style_handoff_response(result: StoredStyleHandoff) -> JSONResponse:
        decision = (
            result.handoff.status if result.handoff.status in {"accepted", "rejected"} else None
        )
        return JSONResponse(
            content={
                "ok": True,
                "eligibility_input_id": str(result.eligibility_input_id),
                "eligibility": jsonable_encoder(result.payload),
                "decision": decision,
                **jsonable_encoder(result.handoff),
            }
        )

    @application.post(
        "/local/api/style-warehouse-handoffs/{handoff_id}/evaluate",
        summary="显式发送风格仓资格校验",
    )
    async def evaluate_style_handoff(handoff_id: UUID) -> JSONResponse:
        if style_handoff_service is None:
            return style_handoff_error("style_handoff_unconfigured")
        try:
            result = await style_handoff_service.evaluate(handoff_id)
        except RuntimeStyleHandoffFailure as error:
            return style_handoff_error(str(error))
        return style_handoff_response(result)

    @application.get(
        "/local/api/style-warehouse-handoffs/{handoff_id}",
        summary="读取风格仓资格校验原回执",
    )
    async def read_style_handoff(handoff_id: UUID) -> JSONResponse:
        if style_handoff_service is None:
            return style_handoff_error("style_handoff_unconfigured")
        try:
            result = await style_handoff_service.read(handoff_id)
        except RuntimeStyleHandoffFailure as error:
            return style_handoff_error(str(error))
        return style_handoff_response(result)

    @application.post(
        "/local/api/album-review-inbox/{product_code}/versions/{package_version}/open-review",
        summary="将相册商品接入本地逐张审核",
    )
    async def open_album_product_review(
        product_code: str,
        package_version: int,
    ) -> JSONResponse:
        if album_review_inbox is None or album_sync_tenant_id is None:
            return JSONResponse(
                status_code=404,
                content={
                    "ok": False,
                    "error_code": "album_review_inbox_unconfigured",
                    "message_zh": "当前页面尚未连接相册整理收件箱。",
                },
            )

        import_key = f"{product_code}:v{package_version}"
        imports: dict[str, object] = {}
        if album_review_imports_path.exists():
            try:
                raw_imports = json.loads(album_review_imports_path.read_text(encoding="utf-8"))
                if isinstance(raw_imports, dict):
                    imports = {str(key): value for key, value in raw_imports.items()}
            except (OSError, json.JSONDecodeError):
                imports = {}
        existing_import = imports.get(import_key)
        existing_batch_id = (
            existing_import
            if isinstance(existing_import, str)
            else (
                existing_import.get("batch_id")
                if isinstance(existing_import, dict)
                and isinstance(existing_import.get("batch_id"), str)
                else None
            )
        )
        if existing_batch_id:
            existing_batch = batch_store.get(existing_batch_id)
            if existing_batch is not None:
                if photo_usage_classifier is not None:
                    for existing_item in existing_batch.items:
                        if (
                            existing_item.photo_classification_facts
                            or existing_item.object_key is None
                            or existing_item.status not in {"pending_review", "reviewed"}
                        ):
                            continue
                        image_path = storage.root.joinpath(
                            *PurePosixPath(existing_item.object_key).parts
                        )
                        try:
                            with Image.open(image_path) as decoded:
                                decoded.load()
                                decision = photo_usage_classifier.classify(
                                    image=decoded,
                                    product_count_hint=1,
                                    safe_filename=(
                                        "product-main.jpg"
                                        if existing_item.image_role == "main"
                                        else "product-detail.jpg"
                                    ),
                                )
                            batch_store.append_photo_classification_fact(
                                existing_batch.batch_id,
                                existing_item.item_id,
                                automatic_decision=decision,
                                human_correction=None,
                                final_category=decision.category,
                                confirmation_version=1,
                            )
                        except (OSError, UnidentifiedImageError, ValueError):
                            continue
                    existing_batch = batch_store.get(existing_batch_id)
                    assert existing_batch is not None
                if not (
                    isinstance(existing_import, dict)
                    and isinstance(existing_import.get("assets"), list)
                ):
                    current_products = await album_review_inbox.list_latest(album_sync_tenant_id)
                    current_product = next(
                        (
                            candidate
                            for candidate in current_products
                            if candidate.product_code == product_code
                            and candidate.package_version == package_version
                        ),
                        None,
                    )
                    ordered_items = sorted(existing_batch.items, key=lambda item: item.position)
                    if current_product is not None and len(ordered_items) == len(
                        current_product.images
                    ):
                        imports[import_key] = {
                            "batch_id": existing_batch.batch_id,
                            "assets": [
                                {
                                    "item_id": str(item.item_id),
                                    "asset_id": str(source.asset_id),
                                    "position": position,
                                    "role": source.role,
                                }
                                for position, (item, source) in enumerate(
                                    zip(
                                        ordered_items,
                                        current_product.images,
                                        strict=True,
                                    )
                                )
                            ],
                        }
                        album_review_imports_path.write_text(
                            json.dumps(
                                imports,
                                ensure_ascii=False,
                                indent=2,
                                sort_keys=True,
                            ),
                            encoding="utf-8",
                        )
                return JSONResponse(
                    content={
                        "ok": True,
                        "replayed": True,
                        "message_zh": "该版本已在审核区，已直接打开。",
                        "batch": serialize_batch(existing_batch),
                    }
                )

        products = await album_review_inbox.list_latest(album_sync_tenant_id)
        product = next(
            (
                candidate
                for candidate in products
                if candidate.product_code == product_code
                and candidate.package_version == package_version
            ),
            None,
        )
        if product is None:
            return JSONResponse(
                status_code=404,
                content={
                    "ok": False,
                    "error_code": "album_product_version_not_found",
                    "message_zh": "该商品版本已不是相册整理的最新版本。",
                },
            )

        batch = batch_store.create(expected_count=len(product.images))
        mime_suffixes = {"image/jpeg": ".jpg", "image/png": ".png", "image/heic": ".heic"}
        for position, source in enumerate(product.images):
            image = await album_review_inbox.read_image(
                album_sync_tenant_id,
                product.product_code,
                product.package_version,
                source.asset_id,
            )
            suffix = mime_suffixes.get(
                image.mime_type, mimetypes.guess_extension(image.mime_type) or ".image"
            )
            filename = (
                f"{product.product_code}{'' if position == 0 else f'-{position + 1}'}{suffix}"
            )
            record = await upload_service.upload(filename, image.mime_type, image.content)
            recognition_result = InitialRecognitionResult(
                material_label=product.category,
                product_form=cast(LocalProductFormCode, product.form),
                visual_observations=["待训练员确认图片可见特征"],
                selling_points=["待训练员补充可用销售卖点"],
            )
            photo_usage_decision = None
            if photo_usage_classifier is not None:
                try:
                    with Image.open(BytesIO(image.content)) as decoded:
                        decoded.load()
                        photo_usage_decision = photo_usage_classifier.classify(
                            image=decoded,
                            product_count_hint=1,
                            safe_filename=(
                                "product-main.jpg"
                                if source.role == "main_candidate"
                                else "product-detail.jpg"
                            ),
                        )
                except (OSError, UnidentifiedImageError, ValueError):
                    photo_usage_decision = None
            if photo_usage_decision is not None:
                item = batch_store.add_classified_image(
                    batch.batch_id,
                    position,
                    filename,
                    record,
                    automatic_decision=photo_usage_decision,
                    product_code=product.product_code,
                    product_sequence=1,
                    image_role="main" if source.role == "main_candidate" else "detail",
                    product_name=product.product_code,
                    source_system="album_organizer",
                    initial_result=recognition_result,
                )
            else:
                item = batch_store.add_image(
                    batch.batch_id,
                    position,
                    filename,
                    record,
                    product_code=product.product_code,
                    product_sequence=1,
                    image_role="main" if source.role == "main_candidate" else "detail",
                    product_name=product.product_code,
                    source_system="album_organizer",
                    initial_result=recognition_result,
                )
            initial_result = training_version_store.register_initial(
                record.template_code,
                TrainingLabelInput(
                    sha256=record.sha256,
                    material_label=product.category,
                    product_form=product.form,
                    visual_observations=recognition_result.visual_observations,
                    selling_points=recognition_result.selling_points,
                    specification_note="",
                    quality_note="",
                    defect_note="",
                    price_range=None,
                ),
                source="merchant_imported",
            )
            candidate = await recognition_service.recognize(
                template_code=record.template_code,
                sha256=record.sha256,
                content=image.content,
                mime_type=image.mime_type,
                historical_reference=historical_reference_for(initial_result),
            )
            batch_store.set_recognition_status(batch.batch_id, item.item_id, candidate.status)

        completed = batch_store.get(batch.batch_id)
        assert completed is not None
        ordered_items = sorted(completed.items, key=lambda item: item.position)
        imports[import_key] = {
            "batch_id": batch.batch_id,
            "assets": [
                {
                    "item_id": str(item.item_id),
                    "asset_id": str(source.asset_id),
                    "position": position,
                    "role": source.role,
                }
                for position, (item, source) in enumerate(
                    zip(ordered_items, product.images, strict=True)
                )
            ],
        }
        album_review_imports_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = album_review_imports_path.with_name(
            f".{album_review_imports_path.name}.writing"
        )
        temporary.write_text(
            json.dumps(imports, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        temporary.replace(album_review_imports_path)
        return JSONResponse(
            status_code=201,
            content={
                "ok": True,
                "replayed": False,
                "message_zh": "已接入逐张审核，商品编码与多图关系保持不变。",
                "batch": serialize_batch(completed),
            },
        )

    @application.get(
        "/local/assets/image-file-selection.mjs",
        include_in_schema=False,
    )
    async def image_file_selection_script() -> FileResponse:
        return FileResponse(IMAGE_FILE_SELECTION_PATH, media_type="text/javascript")

    @application.get(
        "/local/assets/voice-permissions.mjs",
        include_in_schema=False,
    )
    async def voice_permissions_script() -> FileResponse:
        return FileResponse(VOICE_PERMISSIONS_PATH, media_type="text/javascript")

    @application.get(
        "/local/assets/classification-writeback.mjs",
        include_in_schema=False,
    )
    async def classification_writeback_script() -> FileResponse:
        return FileResponse(CLASSIFICATION_WRITEBACK_PATH, media_type="text/javascript")

    @application.get(
        "/local/assets/employee-formal-handoff.mjs",
        include_in_schema=False,
    )
    async def employee_formal_handoff_script() -> FileResponse:
        return FileResponse(EMPLOYEE_FORMAL_HANDOFF_PATH, media_type="text/javascript")

    @application.get(
        "/local/assets/voice-transcript.mjs",
        include_in_schema=False,
    )
    async def voice_transcript_script() -> FileResponse:
        return FileResponse(VOICE_TRANSCRIPT_PATH, media_type="text/javascript")

    @application.get(
        "/local/assets/recognition-preview.mjs",
        include_in_schema=False,
    )
    async def recognition_preview_script() -> FileResponse:
        return FileResponse(RECOGNITION_PREVIEW_PATH, media_type="text/javascript")

    @application.get(
        "/local/assets/recognition-status-view.mjs",
        include_in_schema=False,
    )
    async def recognition_status_view_script() -> FileResponse:
        return FileResponse(
            RECOGNITION_STATUS_VIEW_PATH,
            media_type="text/javascript",
        )

    @application.get(
        "/local/assets/album-review-open.mjs",
        include_in_schema=False,
    )
    async def album_review_open_script() -> FileResponse:
        return FileResponse(ALBUM_REVIEW_OPEN_PATH, media_type="text/javascript")

    @application.get(
        "/local/assets/sales-delivery-status.mjs",
        include_in_schema=False,
    )
    async def sales_delivery_status_script() -> FileResponse:
        return FileResponse(SALES_DELIVERY_STATUS_PATH, media_type="text/javascript")

    @application.get(
        "/local/assets/sales-handoff.mjs",
        include_in_schema=False,
    )
    async def sales_handoff_script() -> FileResponse:
        return FileResponse(SALES_HANDOFF_PATH, media_type="text/javascript")

    @application.get(
        "/local/assets/style-eligibility-handoff.mjs",
        include_in_schema=False,
    )
    async def style_eligibility_handoff_script() -> FileResponse:
        return FileResponse(
            STYLE_ELIGIBILITY_HANDOFF_PATH,
            media_type="text/javascript",
        )

    @application.get(
        "/local/assets/material-evidence.mjs",
        include_in_schema=False,
    )
    async def material_evidence_script() -> FileResponse:
        return FileResponse(MATERIAL_EVIDENCE_PATH, media_type="text/javascript")

    @application.get(
        "/local/assets/product-passport-draft-view.mjs",
        include_in_schema=False,
    )
    async def product_passport_draft_view_script() -> FileResponse:
        return FileResponse(
            PRODUCT_PASSPORT_DRAFT_VIEW_PATH,
            media_type="text/javascript",
        )

    @application.get(
        "/local/assets/service-rule-form.mjs",
        include_in_schema=False,
    )
    async def service_rule_form_script() -> FileResponse:
        return FileResponse(SERVICE_RULE_FORM_PATH, media_type="text/javascript")

    @application.get(
        "/local/assets/pilot-review-request-guard.mjs",
        include_in_schema=False,
    )
    async def pilot_review_request_guard_script() -> FileResponse:
        return FileResponse(PILOT_REVIEW_REQUEST_GUARD_PATH, media_type="text/javascript")

    @application.get(
        "/local/assets/pilot-acceptance-form.mjs",
        include_in_schema=False,
    )
    async def pilot_acceptance_form_script() -> FileResponse:
        return FileResponse(PILOT_ACCEPTANCE_FORM_PATH, media_type="text/javascript")

    @application.get(
        "/local/api/training-results/latest",
        summary="查看最近商品图标注效果",
    )
    async def training_result() -> JSONResponse:
        result = training_version_store.latest()
        return JSONResponse(
            content={
                "ok": True,
                "result": serialize_training_result(result) if result else None,
            }
        )

    @application.get(
        "/local/api/training-results/{template_code}",
        summary="查看指定商品图的当前人工标准",
    )
    async def current_training_result(template_code: str) -> JSONResponse:
        result = training_version_store.current(template_code)
        return JSONResponse(
            content={
                "ok": True,
                "result": serialize_training_result(result) if result else None,
            }
        )

    @application.get(
        "/local/api/training-results/{template_code}/pilot-release-status",
        summary="查看脱敏本地小试状态",
    )
    async def pilot_release_status(template_code: str) -> JSONResponse:
        try:
            result = training_version_store.current(template_code)
        except TrainingLabelFailure as error:
            if str(error) != "forbidden_unsupported_claim":
                raise
            decision = PilotReleaseDecision(
                template_code=template_code,
                source_version=None,
                decision="blocked",
                reason_codes=["forbidden_unsupported_claim"],
            )
            status = build_pilot_release_status(decision)
            return JSONResponse(
                content={
                    "ok": True,
                    "decision": decision.model_dump(mode="json"),
                    "status": status.model_dump(mode="json"),
                }
            )
        if result is None:
            return product_passport_draft_error_response("training_result_not_found")
        decision = evaluate_pilot_release(result)
        status = build_pilot_release_status(decision)
        return JSONResponse(
            content={
                "ok": True,
                "decision": decision.model_dump(mode="json"),
                "status": status.model_dump(mode="json"),
            }
        )

    @application.get(
        "/local/api/training-results/{template_code}/pilot-review-package",
        summary="下载脱敏本地小试审阅包",
    )
    async def pilot_review_package(template_code: str) -> Response:
        try:
            result = training_version_store.current(template_code)
        except TrainingLabelFailure as error:
            if str(error) != "forbidden_unsupported_claim":
                raise
            return JSONResponse(
                status_code=422,
                content={
                    "ok": False,
                    "error_code": "forbidden_unsupported_claim",
                    "message_zh": "商品资料包含禁止承诺，不能生成小试审阅包。",
                },
            )
        if result is None:
            return product_passport_draft_error_response("training_result_not_found")
        try:
            package = build_local_pilot_review_package(result)
        except LocalPilotReviewPackageFailure as error:
            return JSONResponse(
                status_code=409,
                content={
                    "ok": False,
                    "error_code": str(error),
                    "message_zh": "当前资料尚未通过本地小试门禁。",
                },
            )
        safe_code = (
            "".join(
                character
                for character in package.template_code
                if character.isascii() and (character.isalnum() or character in {"-", "_"})
            ).strip("-_")
            or "product"
        )
        ascii_filename = f"local-pilot-review-{safe_code}-v{package.source_version}.json"
        utf8_filename = quote(
            f"local-pilot-review-{package.template_code}-v{package.source_version}.json",
            safe="",
        )
        return Response(
            content=package.model_dump_json(indent=2),
            media_type="application/json",
            headers={
                "Content-Disposition": (
                    f"attachment; filename=\"{ascii_filename}\"; filename*=UTF-8''{utf8_filename}"
                )
            },
        )

    @application.post(
        "/local/api/pilot-review-package/inspect",
        summary="验证本地小试审阅包封条",
    )
    async def inspect_pilot_review_package(
        file: Annotated[UploadFile | None, File()] = None,
    ) -> JSONResponse:
        if file is None:
            return JSONResponse(
                status_code=422,
                content={
                    "ok": False,
                    "error_code": "invalid_pilot_review_package",
                    "message_zh": "请选择一个已下载的 JSON 审阅包。",
                },
            )
        try:
            content = await file.read(1024 * 1024 + 1)
            if len(content) > 1024 * 1024:
                return JSONResponse(
                    status_code=413,
                    content={
                        "ok": False,
                        "error_code": "pilot_review_package_too_large",
                        "message_zh": "审阅包超过 1 MiB，请重新从本地页面下载。",
                    },
                )
            package = LocalPilotReviewPackage.model_validate_json(content)
        except (UnicodeDecodeError, ValidationError, ValueError):
            return JSONResponse(
                status_code=422,
                content={
                    "ok": False,
                    "error_code": "invalid_pilot_review_package",
                    "message_zh": "这不是有效的本地小试审阅包，请重新下载。",
                },
            )
        finally:
            await file.close()
        try:
            authoritative = training_version_store.version(
                package.template_code,
                package.source_version,
            )
        except TrainingLabelFailure as error:
            if str(error) == "forbidden_unsupported_claim":
                authoritative = None
            else:
                return JSONResponse(
                    status_code=422,
                    content={
                        "ok": False,
                        "error_code": "pilot_review_authority_unavailable",
                        "message_zh": "权威底账冲突或无法读取，请先修复本地资料。",
                    },
                )
        except (UnicodeDecodeError, json.JSONDecodeError, ValidationError):
            return JSONResponse(
                status_code=422,
                content={
                    "ok": False,
                    "error_code": "pilot_review_authority_unavailable",
                    "message_zh": "权威底账冲突或无法读取，请先修复本地资料。",
                },
            )
        current_blocked = False
        try:
            current = training_version_store.current(package.template_code)
        except TrainingLabelFailure as error:
            if str(error) != "forbidden_unsupported_claim":
                return JSONResponse(
                    status_code=422,
                    content={
                        "ok": False,
                        "error_code": "pilot_review_authority_unavailable",
                        "message_zh": "当前权威底账无法读取，请先修复本地资料。",
                    },
                )
            current = None
            current_blocked = True
        inspection = inspect_local_pilot_review_package(
            package,
            authoritative,
            current,
            current_blocked=current_blocked,
        )
        return JSONResponse(
            content={
                "ok": True,
                "inspection": inspection.model_dump(mode="json"),
            }
        )

    @application.post(
        "/local/api/pilot-review-package/acceptances",
        summary="记录脱敏本地小试人工验收回执",
    )
    async def create_pilot_acceptance(
        file: Annotated[UploadFile | None, File()] = None,
        request_id: Annotated[str | None, Form()] = None,
        decision: Annotated[str | None, Form()] = None,
        reviewer_id: Annotated[str | None, Form()] = None,
        note: Annotated[str | None, Form()] = None,
    ) -> JSONResponse:
        if (
            file is None
            or request_id is None
            or decision is None
            or reviewer_id is None
            or note is None
            or not request_id.strip()
            or not reviewer_id.strip()
            or not note.strip()
            or decision not in {"accepted", "needs_follow_up", "rejected"}
        ):
            return JSONResponse(
                status_code=422,
                content={
                    "ok": False,
                    "error_code": "invalid_pilot_acceptance",
                    "message_zh": "请选择审阅包，并填写结论、验收人编号和备注。",
                },
            )
        try:
            parsed_request_id = UUID(request_id)
            content = await file.read(1024 * 1024 + 1)
            if len(content) > 1024 * 1024:
                return JSONResponse(
                    status_code=413,
                    content={
                        "ok": False,
                        "error_code": "pilot_review_package_too_large",
                        "message_zh": "审阅包超过 1 MiB，请重新从本地页面下载。",
                    },
                )
            package = LocalPilotReviewPackage.model_validate_json(content)
        except (UnicodeDecodeError, ValidationError, ValueError):
            return JSONResponse(
                status_code=422,
                content={
                    "ok": False,
                    "error_code": "invalid_pilot_acceptance",
                    "message_zh": "提交内容或审阅包格式无效，请检查后重试。",
                },
            )
        finally:
            await file.close()

        try:
            authoritative = training_version_store.version(
                package.template_code,
                package.source_version,
            )
            current_blocked = False
            try:
                current = training_version_store.current(package.template_code)
            except TrainingLabelFailure as error:
                if str(error) != "forbidden_unsupported_claim":
                    raise
                current = None
                current_blocked = True
        except (TrainingLabelFailure, UnicodeDecodeError, json.JSONDecodeError, ValidationError):
            return JSONResponse(
                status_code=422,
                content={
                    "ok": False,
                    "error_code": "pilot_review_authority_unavailable",
                    "message_zh": "权威商品底账无法读取，请先修复本地资料。",
                },
            )

        inspection = inspect_local_pilot_review_package(
            package,
            authoritative,
            current,
            current_blocked=current_blocked,
        )
        try:
            result = pilot_acceptance_store.record(
                request_id=parsed_request_id,
                template_code=package.template_code,
                source_version=package.source_version,
                package_sha256=package.package_sha256,
                inspection_code=inspection.code,
                inspection_signal=inspection.signal,
                decision=cast(AcceptanceDecision, decision),
                reviewer_id=reviewer_id,
                note=note,
            )
        except LocalPilotAcceptanceFailure as error:
            error_code = str(error)
            status_code = (
                409
                if error_code
                in {
                    "acceptance_not_allowed",
                    "acceptance_request_conflict",
                }
                else 422
            )
            return JSONResponse(
                status_code=status_code,
                content={
                    "ok": False,
                    "error_code": error_code,
                    "message_zh": (
                        "当前验封状态不允许所选结论，请按页面提示处理。"
                        if error_code == "acceptance_not_allowed"
                        else "验收请求无效或底账无法安全读取，请检查后重试。"
                    ),
                },
            )
        return JSONResponse(
            status_code=201 if result.created else 200,
            content={
                "ok": True,
                "created": result.created,
                "receipt": result.receipt.model_dump(mode="json"),
                "message_zh": "已记录；仅可进入脱敏本地小试，不代表发布许可。",
            },
        )

    @application.get(
        "/local/api/training-results/{template_code}/pilot-acceptances",
        summary="读取本地小试人工验收历史",
    )
    async def pilot_acceptance_history(template_code: str) -> JSONResponse:
        try:
            current = training_version_store.current(template_code)
            history = pilot_acceptance_store.history(
                template_code,
                current.version if current is not None else None,
            )
        except (TrainingLabelFailure, UnicodeDecodeError, json.JSONDecodeError, ValidationError):
            return JSONResponse(
                status_code=422,
                content={
                    "ok": False,
                    "error_code": "pilot_review_authority_unavailable",
                    "message_zh": "权威商品底账无法读取，请先修复本地资料。",
                },
            )
        except LocalPilotAcceptanceFailure:
            return JSONResponse(
                status_code=422,
                content={
                    "ok": False,
                    "error_code": "pilot_acceptance_ledger_unavailable",
                    "message_zh": "验收底账损坏或冲突，已停止读取。",
                },
            )
        return JSONResponse(
            content={
                "ok": True,
                "history": history.model_dump(mode="json"),
                "message_zh": "仅展示脱敏本地小试记录，不代表发布许可。",
            }
        )

    @application.get(
        "/local/api/product-passport-drafts/{template_code}",
        summary="生成不可发布的商品资料草稿",
    )
    async def product_passport_draft(template_code: str) -> JSONResponse:
        training_result = training_version_store.current(template_code)
        if training_result is None:
            return product_passport_draft_error_response("training_result_not_found")
        try:
            draft = build_product_passport_draft(training_result)
        except ProductPassportDraftFailure as error:
            return product_passport_draft_error_response(str(error))
        return JSONResponse(
            content={
                "ok": True,
                "draft": draft.model_dump(mode="json"),
            }
        )

    @application.get(
        "/local/api/mall-product-data/{template_code}",
        summary="读取商城可用的已确认材质类目",
    )
    async def mall_product_data(template_code: str) -> JSONResponse:
        training_result = training_version_store.current(template_code)
        if training_result is None:
            return product_passport_draft_error_response("training_result_not_found")
        try:
            draft = build_product_passport_draft(training_result)
            handoff = build_mall_classification_handoff(draft)
        except ProductPassportDraftFailure as error:
            return product_passport_draft_error_response(str(error))
        except MallClassificationHandoffFailure as error:
            return product_passport_draft_error_response(str(error))
        return JSONResponse(
            content={
                "ok": True,
                "product_data": handoff.model_dump(mode="json"),
            }
        )

    @application.get(
        "/local/api/mall-candidate-imports/{template_code}",
        summary="生成不可自动上架的商城候选导入包",
    )
    async def mall_candidate_import(template_code: str) -> JSONResponse:
        training_result = training_version_store.current(template_code)
        if training_result is None:
            return product_passport_draft_error_response("training_result_not_found")
        try:
            draft = build_product_passport_draft(training_result)
            candidate_import = build_mall_candidate_import(draft)
        except ProductPassportDraftFailure as error:
            return product_passport_draft_error_response(str(error))
        except MallClassificationHandoffFailure as error:
            return product_passport_draft_error_response(str(error))
        except MallCandidateImportFailure as error:
            return JSONResponse(
                status_code=409,
                content={
                    "ok": False,
                    "error_code": str(error),
                    "message_zh": "商品资料尚未通过完整性审核。",
                    "next_step_zh": "请按缺失项补全人工资料后重新生成候选包。",
                    "remaining_blockers": error.remaining_blockers,
                },
            )
        return JSONResponse(
            content={
                "ok": True,
                "candidate_import": candidate_import.model_dump(mode="json"),
            }
        )

    def historical_reference_for(
        training_result: TrainingLabelVersion | None,
    ) -> HistoricalRecognitionReference | None:
        if (
            training_result is None
            or training_result.match_method != "exact_sha256"
            or training_result.matched_from_template_code is None
        ):
            return None
        source = training_version_store.current(training_result.matched_from_template_code)
        if source is None:
            return None
        return HistoricalRecognitionReference(
            source_template_code=source.template_code,
            material_label=source.material_label,
            product_form=source.product_form,
            visual_observations=source.visual_observations,
            selling_points=source.selling_points,
            specification_note=source.specification_note,
        )

    def recognition_payload(template_code: str) -> dict[str, object]:
        candidate = recognition_store.current(template_code)
        training_result = training_version_store.current(template_code)
        historical_reference = historical_reference_for(training_result)
        current_human_version = None
        if training_result is not None and training_result.change_source in {
            "trainer_confirmed",
            "trainer_rollback",
        }:
            current_human_version = serialize_training_result(training_result)
        return {
            "ok": True,
            "recognition_runtime": {
                "provider": recognition_service.provider.provider,
                "model": recognition_service.provider.model,
                "available": recognition_service.provider.provider != "none",
            },
            "ai_candidate": (serialize_recognition_result(candidate) if candidate else None),
            "historical_reference": (
                historical_reference.model_dump(mode="json") if historical_reference else None
            ),
            "current_human_version": current_human_version,
        }

    @application.get(
        "/local/api/recognitions/{template_code}",
        summary="查看商品图识别候选、历史参考和人工版本",
    )
    async def current_recognition(template_code: str) -> JSONResponse:
        return JSONResponse(content=recognition_payload(template_code))

    @application.post(
        "/local/api/recognitions/{template_code}/retry",
        status_code=201,
        summary="重试商品图识别候选",
    )
    async def retry_recognition(template_code: str) -> JSONResponse:
        record = await repository.get_by_code(template_code)
        if record is None:
            return batch_error_response("image_not_found")
        content = await storage.read(record.object_key)
        training_result = training_version_store.current(template_code)
        await recognition_service.recognize(
            template_code=record.template_code,
            sha256=record.sha256,
            content=content,
            mime_type=record.mime_type,
            historical_reference=historical_reference_for(training_result),
            force=True,
        )
        return JSONResponse(
            status_code=201,
            content=recognition_payload(template_code),
        )

    @application.post(
        "/local/api/training-results/{template_code}/versions",
        status_code=201,
        summary="保存人工确认的新版本",
    )
    async def save_training_result(
        template_code: str,
        request: TrainingLabelSaveRequest,
    ) -> JSONResponse:
        try:
            saved = training_version_store.save(
                template_code,
                request.base_version,
                TrainingLabelInput(**request.model_dump(exclude={"base_version"})),
            )
        except TrainingLabelFailure as error:
            return training_error_response(str(error))
        return JSONResponse(
            status_code=201,
            content={
                "ok": True,
                "message_zh": "人工标准已更新，模型尚未重新训练。",
                "result": serialize_training_result(saved),
            },
        )

    @application.post(
        "/local/api/training-results/{template_code}/confirm-price-candidate",
        status_code=201,
        summary="人工确认候选价格并生成新版本",
    )
    async def confirm_price_candidate(
        template_code: str,
        request: PriceCandidateConfirmRequest,
    ) -> JSONResponse:
        current = training_version_store.current(template_code)
        if current is None:
            return product_passport_draft_error_response("training_result_not_found")
        confirmation_time = datetime.now(UTC)
        try:
            draft = build_product_passport_draft(current)
            anchor = PriceAnchor.model_validate(
                {
                    "standard_code": request.standard_code,
                    "category_code": draft.material_classification.category_code,
                    "form": draft.approved_attributes.form,
                    "specification_summary": draft.specification_note or "",
                    "quality_summary": draft.quality_note or "",
                    "defect_summary": draft.defect_note or "",
                    "minimum": request.minimum,
                    "maximum": request.maximum,
                    "basis": request.basis,
                    "anchor_version": request.anchor_version,
                    "confirmation_source": "trainer_confirmed",
                    "confirmed_by": request.anchor_confirmed_by,
                    "confirmed_at": confirmation_time,
                }
            )
            candidate = build_conservative_price_candidate(draft, anchor)
            confirmed_packages = []
            saved = confirm_conservative_price_candidate(
                store=training_version_store,
                draft=draft,
                price_candidate=candidate,
                price_anchor=anchor,
                base_version=request.base_version,
                confirmed_by=request.confirmed_by,
                confirmed_at=confirmation_time,
                before_append=lambda record: confirmed_packages.append(
                    build_confirmed_product_content_package(record)
                ),
            )
            content_package = confirmed_packages[0]
        except ProductPassportDraftFailure as error:
            return product_passport_draft_error_response(str(error))
        except ValidationError:
            return price_confirmation_error_response("invalid_price_anchor")
        except (ConservativePricingFailure, PriceCandidateConfirmationFailure) as error:
            return price_confirmation_error_response(str(error))
        return JSONResponse(
            status_code=201,
            content={
                "ok": True,
                "message_zh": "候选价格已由人工确认并生成新版本，尚未自动发布。",
                "result": serialize_training_result(saved),
                "content_package": content_package.model_dump(mode="json"),
                "publication_status": "blocked",
                "automatic_publication_allowed": False,
            },
        )

    @application.post(
        "/local/api/training-results/{template_code}/rollback",
        status_code=201,
        summary="撤销上一次人工修改",
    )
    async def rollback_training_result(
        template_code: str,
        request: RollbackRequest,
    ) -> JSONResponse:
        try:
            saved = training_version_store.rollback(template_code, request.base_version)
        except TrainingLabelFailure as error:
            return training_error_response(str(error))
        return JSONResponse(
            status_code=201,
            content={
                "ok": True,
                "message_zh": "已恢复上一版人工标准，模型尚未重新训练。",
                "result": serialize_training_result(saved),
            },
        )

    @application.post(
        "/local/api/voice-commands/parse",
        summary="解析训练员语音文字指令",
    )
    async def parse_trainer_voice_command(request: VoiceCommandRequest) -> JSONResponse:
        try:
            result = parse_voice_command(request.transcript)
        except VoiceCommandFailure:
            return JSONResponse(
                status_code=422,
                content={
                    "ok": False,
                    "error_code": "empty_voice_command",
                    "message_zh": "还没有收到语音或文字指令。",
                    "next_step_zh": "请说出明确指令，或在文字框中输入后再应用。",
                },
            )
        return JSONResponse(content={"ok": True, **result.model_dump(mode="json")})

    @application.post(
        "/local/api/upload-batches",
        status_code=201,
        summary="创建本地图片上传批次",
    )
    async def create_upload_batch(request: CreateBatchRequest) -> JSONResponse:
        try:
            batch = batch_store.create(request.expected_count)
        except LocalUploadBatchFailure as error:
            return batch_error_response(str(error))
        return JSONResponse(
            status_code=201,
            content={"ok": True, "batch": serialize_batch(batch)},
        )

    @application.post(
        "/local/api/upload-batches/{batch_id}/formal-handoffs",
        summary="将已审核员工批次生成正式图片交接包",
    )
    async def create_employee_formal_handoff(
        batch_id: str,
        request: EmployeeFormalHandoffRequest,
    ) -> JSONResponse:
        if employee_batch_promotion_service is None:
            return JSONResponse(
                status_code=404,
                content={
                    "ok": False,
                    "error_code": "employee_handoff_unconfigured",
                    "message_zh": "当前本机尚未连接正式图片素材仓。",
                },
            )
        try:
            receipt = await employee_batch_promotion_service.promote(
                batch_id,
                request.request_id,
            )
        except EmployeeBatchPromotionFailure as error:
            return employee_handoff_error(str(error))
        return JSONResponse(
            status_code=201 if receipt.disposition == "created" else 200,
            content={"ok": True, **receipt.model_dump(mode="json")},
        )

    @application.get(
        "/local/api/upload-batches/{batch_id}/formal-handoffs/latest",
        summary="读取员工批次最近一次正式交接回执",
    )
    async def latest_employee_formal_handoff(batch_id: str) -> JSONResponse:
        try:
            receipt = employee_handoff_receipts.latest(batch_id)
        except EmployeeHandoffReceiptFailure as error:
            return employee_handoff_error(str(error))
        if receipt is None:
            return JSONResponse(
                status_code=404,
                content={
                    "ok": False,
                    "error_code": "employee_handoff_not_found",
                    "message_zh": "该批次尚未生成正式图片交接包。",
                },
            )
        return JSONResponse(content={"ok": True, **receipt.model_dump(mode="json")})

    @application.get(
        "/local/api/upload-batches/latest",
        summary="找回最近一个仍有本地图片的上传批次",
    )
    async def latest_upload_batch() -> JSONResponse:
        for batch in reversed(batch_store.list_batches()):
            has_local_image = False
            for item in batch.items:
                if item.object_key is None:
                    continue
                relative = PurePosixPath(item.object_key)
                if relative.is_absolute() or ".." in relative.parts:
                    continue
                image_path = storage.root.joinpath(*relative.parts).resolve()
                if image_path.is_relative_to(storage.root) and image_path.is_file():
                    has_local_image = True
                    break
            if has_local_image:
                return JSONResponse(content={"ok": True, "batch": serialize_batch(batch)})
        return batch_error_response("batch_not_found")

    @application.get(
        "/local/api/upload-batches/{batch_id}",
        summary="查看本地图片上传批次",
    )
    async def get_upload_batch(batch_id: str) -> JSONResponse:
        batch = batch_store.get(batch_id)
        if batch is None:
            return batch_error_response("batch_not_found")
        return JSONResponse(content={"ok": True, "batch": serialize_batch(batch)})

    @application.get(
        "/local/api/upload-batches/{batch_id}/items/{item_id}/image",
        include_in_schema=False,
    )
    async def get_batch_item_image(
        batch_id: str,
        item_id: UUID,
    ) -> Response:
        batch = batch_store.get(batch_id)
        if batch is None:
            return batch_error_response("batch_not_found")
        item = next((entry for entry in batch.items if entry.item_id == item_id), None)
        if item is None or item.object_key is None or item.mime_type is None:
            return batch_error_response("image_not_found")
        relative = PurePosixPath(item.object_key)
        if relative.is_absolute() or ".." in relative.parts:
            return batch_error_response("image_not_found")
        image_path = storage.root.joinpath(*relative.parts).resolve()
        if not image_path.is_relative_to(storage.root) or not image_path.is_file():
            return batch_error_response("image_not_found")
        return FileResponse(image_path, media_type=item.mime_type)

    @application.post(
        "/local/api/upload-batches/{batch_id}/items/{item_id}/reviewed",
        summary="将图片标记为已审核",
    )
    async def mark_batch_item_reviewed(batch_id: str, item_id: UUID) -> JSONResponse:
        try:
            item = batch_store.mark_reviewed(batch_id, item_id)
        except LocalUploadBatchFailure as error:
            return batch_error_response(str(error))
        return JSONResponse(
            content={
                "ok": True,
                "message_zh": "这张图片已完成审核。",
                "item": serialize_batch_item(item),
            }
        )

    @application.post(
        "/local/api/upload-batches/{batch_id}/products/{product_code}/reviewed",
        summary="确认当前商品的全部图片",
    )
    async def mark_product_images_reviewed(batch_id: str, product_code: str) -> JSONResponse:
        try:
            items = batch_store.mark_product_reviewed(batch_id, product_code)
        except LocalUploadBatchFailure as error:
            return batch_error_response(str(error))
        return JSONResponse(
            content={
                "ok": True,
                "message_zh": "这件商品的全部图片已确认，可进入销售策略预览。",
                "reviewed_count": len(items),
            }
        )

    @application.post(
        "/local/api/upload-batches/{batch_id}/images",
        status_code=201,
        summary="上传批次中的一张图片",
    )
    async def upload_batch_image(
        batch_id: str,
        position: Annotated[int, Form()],
        file: Annotated[UploadFile | None, File()] = None,
    ) -> JSONResponse:
        if blocked := require_batch_structure_mutable(batch_id):
            return blocked
        batch = batch_store.get(batch_id)
        if batch is None:
            return batch_error_response("batch_not_found")
        if file is None:
            return error_response("missing_file")
        content = await file.read(MAX_UPLOAD_BYTES + 1)
        filename = file.filename or "未命名图片"
        digest = hashlib.sha256(content).hexdigest() if content else None
        existing_position = next(
            (item for item in batch.items if item.position == position),
            None,
        )
        replacing_terminal = bool(
            existing_position is not None and existing_position.status in {"failed", "duplicate"}
        )
        try:
            if existing_position is not None:
                if not replacing_terminal and existing_position.sha256 == digest:
                    return JSONResponse(
                        status_code=200,
                        content={
                            "ok": True,
                            "message_zh": "这张图片已经上传，无需重复处理。",
                            "item": serialize_batch_item(existing_position),
                        },
                    )
                if not replacing_terminal:
                    return batch_error_response("position_conflict")

            duplicate = next(
                (
                    item
                    for item in batch.items
                    if item.item_id != (existing_position.item_id if existing_position else None)
                    and item.sha256 == (digest or "")
                    and item.status != "failed"
                ),
                None,
            )
            if duplicate is not None and digest is not None:
                item = batch_store.add_duplicate(
                    batch_id,
                    position,
                    filename,
                    digest,
                    duplicate.mime_type or "image/jpeg",
                    duplicate,
                    replace_existing=replacing_terminal,
                )
                return JSONResponse(
                    status_code=200,
                    content={
                        "ok": True,
                        "message_zh": "检测到完全相同的图片，已自动跳过重复处理。",
                        "item": serialize_batch_item(item),
                    },
                )

            record = await upload_service.upload(
                filename,
                file.content_type or "",
                content,
            )
            if photo_usage_classifier is not None:
                try:
                    with Image.open(BytesIO(content)) as decoded:
                        decoded.load()
                        decision = photo_usage_classifier.classify(
                            image=decoded,
                            product_count_hint=None,
                            safe_filename=filename,
                        )
                    item = batch_store.add_classified_image(
                        batch_id,
                        position,
                        filename,
                        record,
                        automatic_decision=decision,
                        replace_existing=replacing_terminal,
                    )
                except (OSError, UnidentifiedImageError, ValueError):
                    if not replacing_terminal:
                        batch_store.mark_failed(
                            batch_id,
                            position,
                            filename,
                            digest,
                            "photo_decode_failed",
                            replace_existing=True,
                        )
                    return JSONResponse(
                        status_code=422,
                        content={
                            "ok": False,
                            "error_code": "photo_decode_failed",
                            "message_zh": "图片文件无法解码，未生成分类卡片。",
                            "next_step_zh": "请删除或移走原文件后，重新选择可正常打开的 JPEG、PNG；HEIC 需本机具备解码器。",
                        },
                    )
            else:
                item = batch_store.add_image(
                    batch_id,
                    position,
                    filename,
                    record,
                    replace_existing=replacing_terminal,
                )
            initial_result = training_version_store.register_initial(
                record.template_code,
                TrainingLabelInput(
                    sha256=record.sha256,
                    material_label="待确认",
                    product_form="待确认",
                    visual_observations=["待训练员确认图片可见特征"],
                    selling_points=["待训练员补充可用销售卖点"],
                    specification_note="",
                    quality_note="",
                    defect_note="",
                    price_range=None,
                ),
            )
            candidate = await recognition_service.recognize(
                template_code=record.template_code,
                sha256=record.sha256,
                content=content,
                mime_type=record.mime_type,
                historical_reference=historical_reference_for(initial_result),
            )
            item = batch_store.set_recognition_status(
                batch_id,
                item.item_id,
                candidate.status,
            )
        except LocalUploadRejected as error:
            if not replacing_terminal:
                batch_store.mark_failed(
                    batch_id,
                    position,
                    filename,
                    digest,
                    str(error),
                )
            return error_response(str(error))
        except LocalUploadBatchFailure as error:
            return batch_error_response(str(error))
        finally:
            await file.close()
        return JSONResponse(
            status_code=201,
            content={
                "ok": True,
                "message_zh": (
                    "图片已上传，已匹配历史人工确认结果，请复核。"
                    if initial_result.match_method == "exact_sha256"
                    else "图片已上传，等待逐张审核。"
                ),
                "item": serialize_batch_item(item),
            },
        )

    @application.post(
        "/local/api/templates/upload",
        status_code=201,
        summary="本地一键上传图片",
    )
    async def upload_template(
        file: Annotated[UploadFile | None, File()] = None,
    ) -> JSONResponse:
        if file is None:
            return error_response("missing_file")
        try:
            content = await file.read(MAX_UPLOAD_BYTES + 1)
            record = await upload_service.upload(
                file.filename or "未命名图片",
                file.content_type or "",
                content,
            )
        except LocalUploadRejected as error:
            return error_response(str(error))
        finally:
            await file.close()
        serialized = serialize_template(record)
        serialized["id"] = str(record.id)
        serialized["tenant_id"] = str(record.tenant_id)
        return JSONResponse(
            status_code=201,
            content={
                "ok": True,
                "message_zh": "图片上传并登记成功",
                "template": serialized,
            },
        )

    @application.post(
        "/local/api/employee-package/validate",
        summary="校验员工商品资料压缩包",
    )
    async def validate_uploaded_employee_package(
        file: Annotated[UploadFile | None, File()] = None,
    ) -> JSONResponse:
        if file is None:
            return employee_package_error_response("invalid_zip")
        try:
            result = validate_employee_package(await file.read())
        except EmployeePackageValidationFailure as error:
            return employee_package_error_response(str(error), error.details)
        finally:
            await file.close()
        return JSONResponse(
            content={
                "ok": True,
                "template_file": result.template_file,
                "workbook_count": result.workbook_count,
                "image_count": result.image_count,
                "video_count": result.video_count,
                "product_count": result.product_count,
                "filename_warnings": list(result.filename_warnings),
                "spreadsheet_warning_zh": result.spreadsheet_warning_zh,
                "next_step_zh": result.next_step_zh,
            }
        )

    @application.get(
        "/local/api/employee-package/template",
        summary="下载员工商品资料包模板",
    )
    async def download_employee_package_template() -> Response:
        return Response(
            content=build_employee_package_template(),
            media_type="application/zip",
            headers={
                "Content-Disposition": (
                    "attachment; filename=employee-product-package-template.zip"
                )
            },
        )

    async def import_employee_package_content(
        package_content: bytes,
        message_zh: str,
        batch_id: str | None = None,
    ) -> JSONResponse:
        validation = validate_employee_package(package_content)
        with ZipFile(BytesIO(package_content)) as archive:
            image_names_by_filename = {
                PurePosixPath(name).name: name
                for name in archive.namelist()
                if is_employee_package_image(name)
            }
            grouped_images = [
                (product, image) for product in validation.products for image in product.images
            ]
            if batch_id is None:
                batch = batch_store.create(expected_count=len(grouped_images))
            else:
                existing_batch = batch_store.get(batch_id)
                if existing_batch is None:
                    raise LocalUploadBatchFailure("batch_not_found")
                batch = existing_batch
                if batch.expected_count != len(grouped_images):
                    raise LocalUploadBatchFailure("batch_not_empty")
            for position, (product, image) in enumerate(grouped_images):
                filename = image.filename
                name = image_names_by_filename[filename]
                content = archive.read(name)
                try:
                    record = await upload_service.upload(
                        filename,
                        mimetypes.guess_type(filename)[0] or "image/jpeg",
                        content,
                    )
                    material_label = product.material_label or "待确认"
                    product_form = product.product_form or "待确认"
                    visual_observations = list(product.visual_observations) or [
                        "待训练员确认图片可见特征"
                    ]
                    selling_points = list(product.selling_points) or ["待训练员补充可用销售卖点"]
                    price_reason = (
                        f"Excel 建议价格 ¥{product.suggested_price}，仍需人工确认价格区间。"
                        if product.suggested_price
                        else "待录入该品类的价格标准"
                    )
                    recognition_result = InitialRecognitionResult(
                        material_label=material_label,
                        product_form=cast(LocalProductFormCode, product_form),
                        visual_observations=visual_observations,
                        selling_points=selling_points,
                        price_reason=price_reason,
                    )
                    if photo_usage_classifier is not None:
                        with Image.open(BytesIO(content)) as decoded:
                            decoded.load()
                            classifier_image = prepare_classification_image(decoded)
                            try:
                                photo_usage_decision = photo_usage_classifier.classify(
                                    image=classifier_image,
                                    product_count_hint=None,
                                    safe_filename=filename,
                                )
                            finally:
                                if classifier_image is not decoded:
                                    classifier_image.close()
                        item = batch_store.add_classified_image(
                            batch.batch_id,
                            position,
                            filename,
                            record,
                            automatic_decision=photo_usage_decision,
                            product_code=product.product_code,
                            product_sequence=product.sequence,
                            image_role=image.role,
                            product_name=product.product_name or None,
                            merchant_price_reference=product.suggested_price or None,
                            initial_result=recognition_result,
                        )
                    else:
                        item = batch_store.add_image(
                            batch.batch_id,
                            position,
                            filename,
                            record,
                            product_code=product.product_code,
                            product_sequence=product.sequence,
                            image_role=image.role,
                            product_name=product.product_name or None,
                            merchant_price_reference=product.suggested_price or None,
                            initial_result=recognition_result,
                        )
                    initial_result = training_version_store.register_initial(
                        record.template_code,
                        TrainingLabelInput(
                            sha256=record.sha256,
                            material_label=material_label,
                            product_form=product_form,
                            visual_observations=visual_observations,
                            selling_points=selling_points,
                            specification_note="",
                            quality_note="",
                            defect_note="",
                            price_range=None,
                        ),
                        source="merchant_imported",
                    )
                    candidate = await recognition_service.recognize(
                        template_code=record.template_code,
                        sha256=record.sha256,
                        content=content,
                        mime_type=record.mime_type,
                        historical_reference=historical_reference_for(initial_result),
                    )
                    batch_store.set_recognition_status(
                        batch.batch_id,
                        item.item_id,
                        candidate.status,
                    )
                except LocalUploadRejected as error:
                    batch_store.mark_failed(
                        batch.batch_id,
                        position,
                        filename,
                        hashlib.sha256(content).hexdigest(),
                        str(error),
                    )
        imported = batch_store.get(batch.batch_id)
        assert imported is not None
        return JSONResponse(
            status_code=201,
            content={
                "ok": True,
                "message_zh": message_zh,
                "batch": serialize_batch(imported),
                "workbook_count": validation.workbook_count,
                "image_count": validation.image_count,
                "video_count": validation.video_count,
                "filename_warnings": list(validation.filename_warnings),
                "spreadsheet_warning_zh": validation.spreadsheet_warning_zh,
            },
        )

    @application.post(
        "/local/api/employee-package/import",
        status_code=201,
        summary="导入员工商品资料压缩包到本地审核批次",
    )
    async def import_uploaded_employee_package(
        file: Annotated[UploadFile | None, File()] = None,
        batch_id: Annotated[str | None, Form()] = None,
    ) -> JSONResponse:
        if batch_id is not None and (blocked := require_batch_structure_mutable(batch_id)):
            return blocked
        if file is None:
            return employee_package_error_response("invalid_zip")
        try:
            return await import_employee_package_content(
                await file.read(),
                "资料包已导入本地审核批次，请逐张复核系统候选。",
                batch_id,
            )
        except EmployeePackageValidationFailure as error:
            return employee_package_error_response(str(error), error.details)
        except LocalUploadBatchFailure as error:
            return batch_error_response(str(error))
        finally:
            await file.close()

    @application.post(
        "/local/api/upload-batches/{batch_id}/workbook",
        status_code=201,
        summary="为已上传图片的批次补充商品资料表",
    )
    async def attach_workbook_to_upload_batch(
        batch_id: str,
        file: Annotated[UploadFile | None, File()] = None,
    ) -> JSONResponse:
        if blocked := require_batch_structure_mutable(batch_id):
            return blocked
        source_batch = batch_store.get(batch_id)
        if source_batch is None:
            return batch_error_response("batch_not_found")
        if file is None:
            return employee_package_error_response("template_file_missing")
        try:
            package = BytesIO()
            with ZipFile(package, "w", ZIP_DEFLATED) as archive:
                archive.writestr("商品资料.xlsx", await file.read())
                for item in source_batch.items:
                    if item.status not in {"pending_review", "reviewed"} or item.object_key is None:
                        continue
                    archive.writestr(
                        f"商品图片/{PurePosixPath(item.filename).name}",
                        await storage.read(item.object_key),
                    )
            package_content = package.getvalue()
            validation = validate_employee_package(package_content)
            if validation.spreadsheet_warning_zh:
                return JSONResponse(
                    status_code=422,
                    content={
                        "ok": False,
                        "error_code": "workbook_does_not_match_batch",
                        "message_zh": "Excel 与当前图片的商品编码未对上。",
                        "next_step_zh": "请选择包含当前图片编码的 Excel。",
                    },
                )
            response = await import_employee_package_content(
                package_content,
                "Excel 已补充，已生成新的商品审核批次。",
            )
            response_body = json.loads(bytes(response.body))
            response_body["source_batch_id"] = batch_id
            return JSONResponse(status_code=201, content=response_body)
        except EmployeePackageValidationFailure as error:
            return employee_package_error_response(str(error), error.details)
        finally:
            await file.close()

    @application.post(
        "/local/api/employee-package/import-parts",
        status_code=201,
        summary="直接配对员工表格和图片文件夹",
    )
    async def import_employee_package_parts(
        workbook: Annotated[UploadFile | None, File()] = None,
        images: Annotated[list[UploadFile] | None, File()] = None,
    ) -> JSONResponse:
        uploads = images or []
        if workbook is None:
            return employee_package_error_response("template_file_missing")
        try:
            package = BytesIO()
            with ZipFile(package, "w", ZIP_DEFLATED) as archive:
                archive.writestr(
                    "员工商品资料包/商品资料批量上传模板-简版.xlsx",
                    await workbook.read(),
                )
                for image in uploads:
                    filename = PurePosixPath(image.filename or "").name
                    if PurePosixPath(filename).suffix.lower() in IMAGE_SUFFIXES:
                        archive.writestr(
                            f"员工商品资料包/商品图片/{filename}",
                            await image.read(),
                        )
            return await import_employee_package_content(
                package.getvalue(),
                "表格和图片已配对导入，请逐张复核系统候选。",
            )
        except EmployeePackageValidationFailure as error:
            return employee_package_error_response(str(error), error.details)
        finally:
            await workbook.close()
            for image in uploads:
                await image.close()

    return application


runtime_album_review_inbox, runtime_album_sync_tenant_id = build_runtime_album_review_inbox()
app = create_local_app(
    Path(os.environ.get("LOCAL_DATA_ROOT", DEFAULT_DATA_ROOT)),
    album_review_inbox=runtime_album_review_inbox,
    album_sync_tenant_id=runtime_album_sync_tenant_id,
)
