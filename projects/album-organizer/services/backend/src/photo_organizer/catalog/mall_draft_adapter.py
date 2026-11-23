from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from types import TracebackType
from typing import TYPE_CHECKING, BinaryIO, Literal, cast

from pydantic import ValidationError

from photo_organizer.catalog.mall_draft_gateway import (
    DraftImageSource,
    MallDraftCreateResult,
    MallDraftGateway,
    MallDraftLookupResult,
)
from photo_organizer.catalog.package_image_streams import (
    PackageImageStreamError,
    PackageImageStreamProvider,
)
from photo_organizer.catalog.standard_product_package import (
    StandardProductPackageError,
    build_standard_product_package,
)
from photo_organizer.schemas.mall_draft_adapter import (
    MallDraftImageEvidence,
    MallDraftImportItem,
    MallDraftImportPlan,
    MallDraftImportReceipt,
    MallDraftItemErrorCode,
    MallDraftItemResult,
    plan_id_for_body,
)
from photo_organizer.schemas.mall_draft_export import (
    MallProductDraftBatch,
    MallProductDraftRequest,
)
from photo_organizer.schemas.standard_product_package import (
    StandardPackageProduct,
    StandardProductPackageManifest,
)
from photo_organizer.schemas.standard_product_package_acceptance import (
    StandardProductPackageAcceptanceReceipt,
)

if TYPE_CHECKING:
    from photo_organizer.catalog.mall_draft_execution_store import (
        LocalMallDraftExecutionStore,
    )

MallDraftAdapterErrorCode = Literal[
    "invalid_input",
    "package_receipt_mismatch",
    "batch_package_mismatch",
    "unsafe_material",
    "gateway_unavailable",
    "gateway_protocol_error",
    "resume_state_conflict",
    "output_unavailable",
]
EXPECTED_CLAIMS = (
    "authenticity",
    "origin",
    "quality",
    "warehouse_acceptance",
)
EXPECTED_BUSINESS_FIELDS = (
    "title",
    "selling_points",
    "price",
    "inventory",
    "sku",
    "customization",
    "origin_evidence",
)


class MallDraftAdapterError(Exception):
    def __init__(self, code: MallDraftAdapterErrorCode) -> None:
        self.code = code
        super().__init__(code)


async def execute_mall_draft_import(
    plan: MallDraftImportPlan,
    streams: PackageImageStreamProvider,
    gateway: MallDraftGateway,
    *,
    prior_receipt: MallDraftImportReceipt | None = None,
    dry_run: bool = False,
) -> MallDraftImportReceipt:
    checked_plan = _revalidate_plan(plan)
    prior_results = _revalidate_prior_receipt(checked_plan, prior_receipt)
    ordered_items = tuple(
        sorted(checked_plan.items, key=lambda item: item.draft_request_id)
    )

    if dry_run:
        return _receipt(
            checked_plan,
            "prepared",
            tuple(
                MallDraftItemResult(
                    draft_request_id=item.draft_request_id,
                    attempt_generation=1,
                    status="prepared",
                    mall_product_id=None,
                    error_code=None,
                )
                for item in ordered_items
            ),
        )

    results: list[MallDraftItemResult] = []
    for item in ordered_items:
        previous = prior_results.get(item.draft_request_id)
        if previous is not None and previous.status in _TERMINAL_STATUSES:
            results.append(previous)
            continue
        generation = 1 if previous is None else previous.attempt_generation + 1
        results.append(
            await _execute_item(checked_plan, item, streams, gateway, generation)
        )
    execution_status: Literal["in_progress", "completed"] = (
        "completed"
        if all(result.status in _TERMINAL_STATUSES for result in results)
        else "in_progress"
    )
    return _receipt(checked_plan, execution_status, tuple(results))


async def execute_mall_draft_import_with_store(
    plan: MallDraftImportPlan,
    streams: PackageImageStreamProvider,
    gateway: MallDraftGateway,
    store: LocalMallDraftExecutionStore,
) -> MallDraftImportReceipt:
    checked_plan = _revalidate_plan(plan)
    ordered_items = tuple(
        sorted(checked_plan.items, key=lambda item: item.draft_request_id)
    )
    async with store.execution(checked_plan) as transaction:
        for item in ordered_items:
            current = {
                result.draft_request_id: result
                for result in transaction.current_receipt.results
            }[item.draft_request_id]
            if current.status in _TERMINAL_STATUSES:
                continue
            result = await _execute_item(
                checked_plan,
                item,
                streams,
                gateway,
                transaction.next_generation(item.draft_request_id),
            )
            await transaction.settle(result)
        return transaction.current_receipt


_TERMINAL_STATUSES = frozenset(
    {"created", "change_proposal_required", "permanently_blocked"}
)


def _revalidate_plan(plan: MallDraftImportPlan) -> MallDraftImportPlan:
    try:
        return MallDraftImportPlan.model_validate(plan.model_dump(mode="python"))
    except (AttributeError, TypeError, ValueError, ValidationError):
        raise MallDraftAdapterError("invalid_input") from None


def _revalidate_prior_receipt(
    plan: MallDraftImportPlan,
    prior_receipt: MallDraftImportReceipt | None,
) -> dict[str, MallDraftItemResult]:
    if prior_receipt is None:
        return {}
    try:
        checked = MallDraftImportReceipt.model_validate(
            prior_receipt.model_dump(mode="python")
        )
    except (AttributeError, TypeError, ValueError, ValidationError):
        raise MallDraftAdapterError("resume_state_conflict") from None
    expected_ids = tuple(sorted(item.draft_request_id for item in plan.items))
    actual_ids = tuple(result.draft_request_id for result in checked.results)
    if (
        checked.plan_id != plan.plan_id
        or checked.opc_id != plan.opc_id
        or checked.item_count != len(plan.items)
        or actual_ids != expected_ids
    ):
        raise MallDraftAdapterError("resume_state_conflict")
    return {result.draft_request_id: result for result in checked.results}


async def _execute_item(
    plan: MallDraftImportPlan,
    item: MallDraftImportItem,
    streams: PackageImageStreamProvider,
    gateway: MallDraftGateway,
    generation: int,
) -> MallDraftItemResult:
    try:
        lookup = await gateway.lookup_by_source_album_product(
            opc_id=plan.opc_id,
            source_album_product_id=item.source_album_product_id,
        )
    except Exception:  # noqa: BLE001 - gateway exceptions must not escape receipts.
        return _failure(item, generation, "retryable_failed", "gateway_unavailable")
    try:
        lookup_status = MallDraftLookupResult.model_validate(
            lookup.model_dump(mode="json")
        ).status
    except (AttributeError, TypeError, ValueError, ValidationError):
        return _failure(
            item, generation, "permanently_blocked", "gateway_protocol_error"
        )

    if lookup_status in {"existing_draft", "existing_published"}:
        return _failure(item, generation, "change_proposal_required", None)
    if lookup_status == "ambiguous":
        return _failure(
            item, generation, "permanently_blocked", "gateway_protocol_error"
        )
    if lookup_status == "temporarily_unavailable":
        return _failure(item, generation, "retryable_failed", "gateway_unavailable")
    if lookup_status != "not_found":
        return _failure(
            item, generation, "permanently_blocked", "gateway_protocol_error"
        )

    tracker, image_sources = _draft_image_sources(item, streams)
    try:
        created = await gateway.create_draft(
            idempotency_key=item.draft_request_id,
            command=item,
            image_sources=image_sources,
        )
    except (_ImageSourceProtocolError, PackageImageStreamError):
        return _failure(
            item, generation, "permanently_blocked", "gateway_protocol_error"
        )
    except Exception:  # noqa: BLE001 - gateway exceptions must not escape receipts.
        return _failure(item, generation, "retryable_failed", "gateway_unavailable")
    else:
        streams_completed = tracker.completed
    finally:
        tracker.invalidate()
    if not streams_completed:
        return _failure(
            item, generation, "permanently_blocked", "gateway_protocol_error"
        )
    try:
        checked_created = MallDraftCreateResult.model_validate(
            created.model_dump(mode="json")
        )
        if (
            checked_created.opc_id != plan.opc_id
            or checked_created.draft_request_id != item.draft_request_id
            or checked_created.source_album_product_id != item.source_album_product_id
            or checked_created.publication_state != "draft"
        ):
            raise ValueError
        return MallDraftItemResult(
            draft_request_id=item.draft_request_id,
            attempt_generation=generation,
            status="created",
            mall_product_id=checked_created.mall_product_id,
            error_code=None,
        )
    except (AttributeError, TypeError, ValueError, ValidationError):
        return _failure(
            item, generation, "permanently_blocked", "gateway_protocol_error"
        )


class _ImageSourceProtocolError(Exception):
    pass


class _ImageSourceTracker:
    def __init__(self) -> None:
        self._sources: list[_TrackedImageSource] = []
        self._next_index = 0
        self._active: _TrackedImageSource | None = None
        self._invalidated = False

    @property
    def completed(self) -> bool:
        return (
            not self._invalidated
            and self._active is None
            and self._next_index == len(self._sources)
            and all(source.closed and source.verified for source in self._sources)
        )

    def add_source(
        self,
        open_verified: Callable[[], AbstractContextManager[BinaryIO]],
    ) -> _TrackedImageSource:
        source = _TrackedImageSource(self, len(self._sources), open_verified)
        self._sources.append(source)
        return source

    def begin(self, source: _TrackedImageSource) -> None:
        if (
            self._invalidated
            or self._active is not None
            or source.index != self._next_index
            or source.opened
        ):
            raise _ImageSourceProtocolError
        source.opened = True
        self._next_index += 1
        self._active = source

    def finish(self, source: _TrackedImageSource, *, verified: bool) -> None:
        if self._active is source:
            self._active = None
        source.closed = True
        source.verified = verified

    def invalidate(self) -> None:
        self._invalidated = True
        if self._active is not None:
            try:
                self._active.force_close()
            except Exception:  # noqa: BLE001, S110 - cleanup must not leak details.
                pass


class _TrackedImageSource:
    def __init__(
        self,
        tracker: _ImageSourceTracker,
        index: int,
        open_verified: Callable[[], AbstractContextManager[BinaryIO]],
    ) -> None:
        self._tracker = tracker
        self.index = index
        self._open_verified = open_verified
        self.opened = False
        self.closed = False
        self.verified = False

    def open(self) -> AbstractContextManager[BinaryIO]:
        if self._tracker._invalidated:
            raise _ImageSourceProtocolError
        return _TrackedImageContext(self)

    def enter(self, context: _TrackedImageContext) -> BinaryIO:
        self._tracker.begin(self)
        try:
            return context.enter_inner(self._open_verified)
        except BaseException:
            self._tracker.finish(self, verified=False)
            raise

    def exit(
        self,
        context: _TrackedImageContext,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        try:
            suppressed = context.exit_inner(exception_type, exception, traceback)
        except BaseException:
            self._tracker.finish(self, verified=False)
            raise
        self._tracker.finish(self, verified=exception_type is None)
        return suppressed

    def force_close(self) -> None:
        context = self._tracker_context
        context.force_close()

    @property
    def _tracker_context(self) -> _TrackedImageContext:
        if not hasattr(self, "_context"):
            raise _ImageSourceProtocolError
        return self._context

    def set_context(self, context: _TrackedImageContext) -> None:
        self._context = context


class _TrackedImageContext(AbstractContextManager[BinaryIO]):
    def __init__(self, source: _TrackedImageSource) -> None:
        self._source = source
        self._inner: AbstractContextManager[BinaryIO] | None = None
        self._entered = False
        self._closed = False

    def __enter__(self) -> BinaryIO:
        self._source.set_context(self)
        stream = self._source.enter(self)
        self._entered = True
        return stream

    def enter_inner(
        self,
        open_verified: Callable[[], AbstractContextManager[BinaryIO]],
    ) -> BinaryIO:
        self._inner = open_verified()
        return self._inner.__enter__()

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        if self._closed:
            return None
        self._closed = True
        return self._source.exit(self, exception_type, exception, traceback)

    def exit_inner(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        if self._inner is None:
            raise _ImageSourceProtocolError
        return self._inner.__exit__(exception_type, exception, traceback)

    def force_close(self) -> None:
        if self._entered and not self._closed:
            self.__exit__(None, None, None)


def _draft_image_sources(
    item: MallDraftImportItem,
    streams: PackageImageStreamProvider,
) -> tuple[_ImageSourceTracker, tuple[DraftImageSource, ...]]:
    tracker = _ImageSourceTracker()
    return tracker, tuple(
        _draft_image_source(item, image, streams, tracker) for image in item.images
    )


def _draft_image_source(
    item: MallDraftImportItem,
    image: MallDraftImageEvidence,
    streams: PackageImageStreamProvider,
    tracker: _ImageSourceTracker,
) -> DraftImageSource:
    def open_verified() -> AbstractContextManager[BinaryIO]:
        return cast(
            AbstractContextManager[BinaryIO],
            streams.open_verified_image(
                draft_request_id=item.draft_request_id,
                asset_id=image.asset_id,
            ),
        )

    source = tracker.add_source(open_verified)
    return DraftImageSource(
        asset_id=image.asset_id,
        evidence_asset_id=image.evidence_asset_id,
        media_type=image.media_type,
        byte_size=image.byte_size,
        open=source.open,
    )


def _failure(
    item: MallDraftImportItem,
    generation: int,
    status: Literal[
        "change_proposal_required", "retryable_failed", "permanently_blocked"
    ],
    error_code: MallDraftItemErrorCode | None,
) -> MallDraftItemResult:
    return MallDraftItemResult(
        draft_request_id=item.draft_request_id,
        attempt_generation=generation,
        status=status,
        mall_product_id=None,
        error_code=error_code,
    )


def _receipt(
    plan: MallDraftImportPlan,
    execution_status: Literal["prepared", "in_progress", "completed"],
    results: tuple[MallDraftItemResult, ...],
) -> MallDraftImportReceipt:
    return MallDraftImportReceipt(
        schema_version="opc-mall-draft-import-receipt/1.0",
        plan_id=plan.plan_id,
        opc_id=plan.opc_id,
        execution_status=execution_status,
        item_count=len(results),
        results=tuple(sorted(results, key=lambda result: result.draft_request_id)),
    )


def build_mall_draft_import_plan(
    batch: MallProductDraftBatch,
    manifest: StandardProductPackageManifest,
    acceptance: StandardProductPackageAcceptanceReceipt,
) -> MallDraftImportPlan:
    if (
        not isinstance(
            batch,
            MallProductDraftBatch,
        )
        or not isinstance(manifest, StandardProductPackageManifest)
        or not isinstance(acceptance, StandardProductPackageAcceptanceReceipt)
    ):
        raise MallDraftAdapterError("invalid_input")
    try:
        if _raw_material_is_unsafe(batch, manifest, acceptance):
            raise MallDraftAdapterError("unsafe_material")
    except MallDraftAdapterError:
        raise
    except (AttributeError, TypeError, ValueError):
        raise MallDraftAdapterError("invalid_input") from None
    try:
        checked_batch = MallProductDraftBatch.model_validate(
            batch.model_dump(mode="python")
        )
    except (TypeError, ValueError, ValidationError):
        raise MallDraftAdapterError("batch_package_mismatch") from None
    try:
        checked_manifest = StandardProductPackageManifest.model_validate(
            manifest.model_dump(mode="python")
        )
    except (TypeError, ValueError, ValidationError):
        raise MallDraftAdapterError("batch_package_mismatch") from None
    try:
        checked_acceptance = StandardProductPackageAcceptanceReceipt.model_validate(
            acceptance.model_dump(mode="python")
        )
    except (TypeError, ValueError, ValidationError):
        raise MallDraftAdapterError("package_receipt_mismatch") from None

    try:
        _validate_material_policy(checked_batch, checked_manifest, checked_acceptance)
        _validate_batch_manifest(checked_batch, checked_manifest)
        _validate_package_receipt(checked_manifest, checked_acceptance)
        items = tuple(
            MallDraftImportItem(
                draft_request_id=item.draft_request_id,
                source_acceptance_id=item.source_acceptance_id,
                source_group_confirmation_event_id=(
                    item.source_group_confirmation_event_id
                ),
                source_album_product_id=item.source_album_product_id,
                source_snapshot_version=item.source_snapshot_version,
                source_type=item.source_type,
                classification=item.classification,
                images=tuple(
                    MallDraftImageEvidence(
                        asset_id=image.asset_id,
                        evidence_asset_id=image.evidence_asset_id,
                        role=image.role,
                        sha256=image.sha256,
                        byte_size=image.byte_size,
                        media_type=image.media_type,
                        captured_at=image.captured_at,
                        imported_at=image.imported_at,
                    )
                    for image in item.images
                ),
                required_business_fields=item.required_business_fields,
                material_authorization=("opc_human_confirmed_for_draft_material_use"),
                claims_not_evaluated=item.claims_not_evaluated,
                mall_product_id=None,
                bead_id=None,
                publication_state="draft",
                auto_publish=False,
                shared_market_opt_in=False,
            )
            for item in checked_batch.items
        )
        body: dict[str, object] = {
            "schema_version": "opc-mall-draft-import-plan/1.0",
            "opc_id": checked_batch.opc_id,
            "source_batch_id": checked_batch.batch_id,
            "package_id": checked_manifest.package_id,
            "package_acceptance_id": checked_acceptance.acceptance_id,
            "package_sha256": checked_acceptance.package_sha256,
            "data_classification": checked_batch.data_classification,
            "operation": "create_draft_only",
            "existing_product_policy": "change_proposal_required",
            "auto_publish": False,
            "shared_market_opt_in": False,
            "items": [item.model_dump(mode="json") for item in items],
        }
        return MallDraftImportPlan.model_validate(
            {"plan_id": plan_id_for_body(body), **body}
        )
    except MallDraftAdapterError:
        raise
    except (TypeError, ValueError, ValidationError):
        raise MallDraftAdapterError("invalid_input") from None


def _raw_material_is_unsafe(
    batch: MallProductDraftBatch,
    manifest: StandardProductPackageManifest,
    acceptance: StandardProductPackageAcceptanceReceipt,
) -> bool:
    values = (batch, manifest, acceptance)
    for value in values:
        if (
            value.auto_publish is not False
            or value.shared_market_opt_in is not False
            or value.data_classification
            not in {
                "synthetic_no_customer_data",
                "opc_owned_product_material_no_customer_data",
            }
        ):
            return True
    if len({value.data_classification for value in values}) != 1:
        return True
    if any(_item_material_is_unsafe(item) for item in batch.items) or any(
        _item_material_is_unsafe(item) for item in manifest.items
    ):
        return True
    return (
        acceptance.material_authorization
        != "opc_human_confirmed_for_draft_material_use"
        or acceptance.auto_publish is not False
        or acceptance.shared_market_opt_in is not False
        or acceptance.mall_product_ids != ()
        or acceptance.bead_ids != ()
        or acceptance.claims_not_evaluated != EXPECTED_CLAIMS
    )


def _item_material_is_unsafe(
    item: MallProductDraftRequest | StandardPackageProduct,
) -> bool:
    return (
        item.mall_product_id is not None
        or item.bead_id is not None
        or item.publication_state != "draft"
        or item.auto_publish is not False
        or item.shared_market_opt_in is not False
        or item.claims_not_evaluated != EXPECTED_CLAIMS
        or item.required_business_fields != EXPECTED_BUSINESS_FIELDS
    )


def _validate_material_policy(
    batch: MallProductDraftBatch,
    manifest: StandardProductPackageManifest,
    acceptance: StandardProductPackageAcceptanceReceipt,
) -> None:
    if (
        batch.operation != "create_draft_only"
        or batch.existing_product_policy != "emit_change_proposal_do_not_overwrite"
        or manifest.operation != "create_draft_only"
        or manifest.existing_product_policy != "emit_change_proposal_do_not_overwrite"
        or acceptance.acceptance_status
        != "package_ready_for_mall_draft_import_not_listed"
        or acceptance.operation != "validate_only"
        or acceptance.publication_state != "draft"
    ):
        raise MallDraftAdapterError("unsafe_material")


def _validate_batch_manifest(
    batch: MallProductDraftBatch,
    manifest: StandardProductPackageManifest,
) -> None:
    if (
        batch.batch_id != manifest.source_batch_id
        or batch.opc_id != manifest.opc_id
        or batch.data_classification != manifest.data_classification
        or len(batch.items) != len(manifest.items)
    ):
        raise MallDraftAdapterError("batch_package_mismatch")
    try:
        rebuilt = build_standard_product_package(batch).manifest
    except StandardProductPackageError:
        raise MallDraftAdapterError("batch_package_mismatch") from None
    if rebuilt != manifest:
        raise MallDraftAdapterError("batch_package_mismatch")
    for batch_item, packaged_item in zip(batch.items, manifest.items, strict=True):
        expected = packaged_item.model_dump(mode="json")
        images = []
        lookups = []
        for image in expected["images"]:
            packaged_image = dict(image)
            confirmation_event_id = packaged_image.pop("download_confirmation_event_id")
            packaged_image.pop("package_path")
            images.append(packaged_image)
            lookups.append(
                {
                    "confirmation_event_id": confirmation_event_id,
                    "asset_id": packaged_image["asset_id"],
                }
            )
        expected["images"] = images
        expected["download_lookup"] = lookups
        if batch_item.model_dump(mode="json") != expected:
            raise MallDraftAdapterError("batch_package_mismatch")


def _validate_package_receipt(
    manifest: StandardProductPackageManifest,
    acceptance: StandardProductPackageAcceptanceReceipt,
) -> None:
    product_sources = tuple(
        source.model_dump(mode="json") for source in acceptance.product_sources
    )
    expected_sources = tuple(
        {
            "album_product_id": item.source_album_product_id,
            "snapshot_version": item.source_snapshot_version,
            "source_type": item.source_type.value,
            "confirmation_event_id": str(item.source_group_confirmation_event_id),
        }
        for item in manifest.items
    )
    if (
        acceptance.package_id != manifest.package_id
        or acceptance.source_batch_id != manifest.source_batch_id
        or acceptance.opc_id != manifest.opc_id
        or acceptance.data_classification != manifest.data_classification
        or acceptance.product_count != len(manifest.items)
        or acceptance.image_count != sum(len(item.images) for item in manifest.items)
        or acceptance.album_product_ids
        != tuple(item.source_album_product_id for item in manifest.items)
        or product_sources != expected_sources
    ):
        raise MallDraftAdapterError("package_receipt_mismatch")
