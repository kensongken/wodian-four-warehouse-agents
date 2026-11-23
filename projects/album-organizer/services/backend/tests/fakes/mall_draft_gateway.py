import asyncio

from photo_organizer.catalog.mall_draft_gateway import (
    DraftImageSource,
    MallDraftCreateResult,
    MallDraftLookupResult,
)
from photo_organizer.catalog.package_image_streams import PackageImageStreamError
from photo_organizer.schemas.mall_draft_adapter import MallDraftImportItem


class FakeMallDraftGateway:
    def __init__(
        self,
        *,
        lookup_result: object | None = None,
        create_result: object | None = None,
        fail_operation: str | None = None,
        failing_lookup_album_ids: set[str] | None = None,
        mode: str | None = None,
    ) -> None:
        self.lookup_result = lookup_result or MallDraftLookupResult(status="not_found")
        self.create_result = create_result
        self.fail_operation = fail_operation
        self.failing_lookup_album_ids = failing_lookup_album_ids or set()
        self.mode = mode
        self.lookup_call_count = 0
        self.create_call_count = 0
        self.consumed_image_bytes = 0
        self.retained_open: object | None = None
        self.retained_context: object | None = None
        self.post_return_attempted = False
        self.post_return_task: asyncio.Task[None] | None = None
        self._opc_ids_by_album: dict[str, str] = {}

    async def lookup_by_source_album_product(
        self, *, opc_id: str, source_album_product_id: str
    ) -> MallDraftLookupResult:
        self.lookup_call_count += 1
        self._opc_ids_by_album[source_album_product_id] = opc_id
        if (
            self.fail_operation == "lookup"
            or source_album_product_id in self.failing_lookup_album_ids
        ):
            raise RuntimeError("private gateway failure")
        return self.lookup_result

    async def create_draft(
        self,
        *,
        idempotency_key: str,
        command: MallDraftImportItem,
        image_sources: tuple[DraftImageSource, ...],
    ) -> MallDraftCreateResult:
        self.create_call_count += 1
        if self.fail_operation == "create":
            raise RuntimeError("private gateway failure")
        if self.mode == "stream_failure":
            with image_sources[0].open() as image:
                image.read(1)
                raise PackageImageStreamError("image_mismatch")
        if self.mode == "overlapping_opens":
            with image_sources[0].open(), image_sources[1].open():
                pass
        if self.mode == "zero_open":
            return self._create_result(command)
        if self.mode == "partial_open":
            self._consume(image_sources[:1])
            return self._create_result(command)
        if self.mode == "repeated_open":
            self._consume(image_sources[:1])
            self._consume(image_sources[:1])
        if self.mode == "out_of_order":
            self._consume(image_sources[1:2])
        if self.mode == "return_with_open_stream":
            context = image_sources[0].open()
            context.__enter__()
            self.retained_context = context
            return self._create_result(command)
        if self.mode == "retain_handle":
            self.retained_open = image_sources[0].open
        if self.mode == "retain_handle_after_return":
            self.retained_open = image_sources[0].open
            self.post_return_task = asyncio.create_task(self._attempt_retained_open())
        for image_source in image_sources:
            with image_source.open() as image:
                self.consumed_image_bytes += len(image.read())
        return self._create_result(command)

    def _consume(self, sources: tuple[DraftImageSource, ...]) -> None:
        for image_source in sources:
            with image_source.open() as image:
                self.consumed_image_bytes += len(image.read())

    def _create_result(self, command: MallDraftImportItem) -> MallDraftCreateResult:
        if self.create_result is not None:
            return self.create_result  # type: ignore[return-value]
        return MallDraftCreateResult(
            opc_id=self._opc_ids_by_album[command.source_album_product_id],
            draft_request_id=command.draft_request_id,
            source_album_product_id=command.source_album_product_id,
            mall_product_id="mall-draft-default",
            publication_state="draft",
        )

    async def _attempt_retained_open(self) -> None:
        self.post_return_attempted = True
        if callable(self.retained_open):
            try:
                self.retained_open()
            except Exception:  # noqa: BLE001, S110 - intentional fake protocol misuse.
                pass

    def reset_calls(self) -> None:
        self.lookup_call_count = 0
        self.create_call_count = 0
        self.consumed_image_bytes = 0
