from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol
from uuid import UUID, uuid4

from product_engine.contracts.screenshots import ScreenshotImportedV1
from product_engine.domain.errors import MaterialUsageReferenceNotFound, ObjectValidationFailed
from product_engine.repositories.events import ClaimResult
from product_engine.storage.protocol import ObjectStorage, PresignedUpload

MAX_SCREENSHOT_BYTES = 20_971_520
MIME_EXTENSIONS = {"image/heic": "heic", "image/jpeg": "jpg", "image/png": "png"}
Clock = Callable[[], datetime]


@dataclass(frozen=True)
class ScreenshotImportResult:
    screenshot_import_id: UUID
    disposition: Literal["accepted", "duplicate"]


class ScreenshotStore(Protocol):
    async def find_by_sha256(self, tenant_id: UUID, sha256: str) -> UUID | None: ...

    async def find_by_event_id(self, tenant_id: UUID, event_id: UUID) -> UUID | None: ...

    async def create(self, event: ScreenshotImportedV1) -> UUID: ...


class InboxStore(Protocol):
    async def claim_inbox(
        self, tenant_id: UUID, source_system: str, event_id: UUID, payload_sha256: str
    ) -> ClaimResult: ...


class OutboxStore(Protocol):
    async def append_outbox(
        self, tenant_id: UUID, topic: str, aggregate_key: str, payload: dict[str, object]
    ) -> UUID: ...


class ProductStore(Protocol):
    async def exists(self, tenant_id: UUID, product_code: str) -> bool: ...


class ScreenshotImportService:
    def __init__(
        self,
        storage: ObjectStorage,
        store: ScreenshotStore | None = None,
        inbox: InboxStore | None = None,
        outbox: OutboxStore | None = None,
        products: ProductStore | None = None,
        *,
        now: Clock = lambda: datetime.now(UTC),
    ) -> None:
        self.storage = storage
        self.now = now
        self.store = store
        self.inbox = inbox
        self.outbox = outbox
        self.products = products

    async def create_upload(
        self,
        *,
        tenant_id: UUID,
        mime_type: str,
        byte_size: int,
        sha256: str,
    ) -> PresignedUpload:
        if mime_type not in MIME_EXTENSIONS or not 1 <= byte_size <= MAX_SCREENSHOT_BYTES:
            raise ValueError("invalid_screenshot_upload")
        timestamp = self.now().astimezone(UTC)
        extension = MIME_EXTENSIONS[mime_type]
        object_key = (
            f"tenants/{tenant_id}/screenshots/{timestamp:%Y}/{timestamp:%m}/{uuid4()}.{extension}"
        )
        upload = await self.storage.create_upload(
            object_key,
            mime_type,
            MAX_SCREENSHOT_BYTES,
            sha256,
        )
        return PresignedUpload(
            object_key=upload.object_key,
            url=upload.url,
            headers={**upload.headers, "x-amz-meta-sha256": sha256},
            expires_in_seconds=upload.expires_in_seconds,
        )

    async def finalize(
        self, event: ScreenshotImportedV1, payload_sha256: str
    ) -> ScreenshotImportResult:
        if self.store is None or self.inbox is None or self.outbox is None:
            raise RuntimeError("screenshot_import_storage_not_configured")
        if event.product_code is not None and (
            self.products is None
            or not await self.products.exists(event.tenant_id, event.product_code)
        ):
            raise MaterialUsageReferenceNotFound("product_not_found")
        expected_prefix = f"tenants/{event.tenant_id}/screenshots/"
        if not event.object_key.startswith(expected_prefix):
            raise ObjectValidationFailed("object_outside_tenant_prefix")
        stored = await self.storage.inspect(event.object_key)
        if (
            stored.mime_type != event.mime_type
            or stored.byte_size != event.byte_size
            or stored.sha256 != event.sha256
            or stored.byte_size > MAX_SCREENSHOT_BYTES
        ):
            raise ObjectValidationFailed("stored_object_mismatch")
        claim = await self.inbox.claim_inbox(
            event.tenant_id, "sales", event.event_id, payload_sha256
        )
        if claim.disposition == "duplicate":
            screenshot_id = await self.store.find_by_event_id(event.tenant_id, event.event_id)
            if screenshot_id is None:
                screenshot_id = await self.store.find_by_sha256(event.tenant_id, event.sha256)
            if screenshot_id is None:
                raise ObjectValidationFailed("screenshot_import_not_found")
            return ScreenshotImportResult(screenshot_id, "duplicate")
        existing = await self.store.find_by_sha256(event.tenant_id, event.sha256)
        if existing is not None:
            return ScreenshotImportResult(existing, "duplicate")
        screenshot_id = await self.store.create(event)
        await self.outbox.append_outbox(
            event.tenant_id,
            "screenshot.imported.v1",
            str(screenshot_id),
            event.model_dump(mode="json"),
        )
        return ScreenshotImportResult(screenshot_id, "accepted")
