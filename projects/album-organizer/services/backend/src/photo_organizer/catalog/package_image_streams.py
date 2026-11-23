from __future__ import annotations

import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from typing import IO, BinaryIO, Literal, Protocol
from uuid import UUID

from pydantic import ValidationError

from photo_organizer.schemas.mall_draft_adapter import (
    MallDraftImportItem,
    MallDraftImportPlan,
)
from photo_organizer.schemas.standard_product_package import (
    PackagedImageEvidence,
    StandardPackageProduct,
    StandardProductPackageManifest,
)
from photo_organizer.schemas.standard_product_package_acceptance import (
    StandardProductPackageAcceptanceReceipt,
)

PackageImageStreamErrorCode = Literal[
    "snapshot_closed",
    "image_not_in_plan",
    "stream_in_use",
    "image_mismatch",
]


class PackageImageStreamError(Exception):
    def __init__(self, code: PackageImageStreamErrorCode) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class _VerifiedImage:
    package_path: str
    sha256: str
    byte_size: int


class _StreamState(Protocol):
    def try_open(self) -> bool: ...

    def close(self) -> None: ...


class VerifiedImageStream(Protocol):
    def read(self, size: int = -1) -> bytes: ...


class PackageImageStreamProvider:
    def __init__(
        self,
        snapshot: BinaryIO,
        manifest: StandardProductPackageManifest,
        receipt: StandardProductPackageAcceptanceReceipt,
        plan: MallDraftImportPlan,
        stream_state: _StreamState,
    ) -> None:
        self._snapshot = snapshot
        self._stream_state = stream_state
        self._images = self._images_for_verified_plan(manifest, receipt, plan)

    @contextmanager
    def open_verified_image(
        self,
        *,
        draft_request_id: str,
        asset_id: UUID,
    ) -> Iterator[VerifiedImageStream]:
        if self._snapshot.closed:
            raise PackageImageStreamError("snapshot_closed")
        image = self._images.get((draft_request_id, asset_id))
        if image is None:
            raise PackageImageStreamError("image_not_in_plan")
        if not self._stream_state.try_open():
            raise PackageImageStreamError("stream_in_use")

        archive: zipfile.ZipFile | None = None
        source: IO[bytes] | None = None
        verified: _VerifiedImageStream | None = None
        try:
            try:
                self._snapshot.seek(0)
                archive = zipfile.ZipFile(self._snapshot)
                source = archive.open(image.package_path)
                verified = _VerifiedImageStream(source, image)
            except (
                KeyError,
                OSError,
                RuntimeError,
                zipfile.BadZipFile,
                zipfile.LargeZipFile,
            ):
                raise PackageImageStreamError("image_mismatch") from None
            try:
                yield verified
            except BaseException:
                try:
                    verified.finish()
                except PackageImageStreamError:
                    pass
                raise
            else:
                verified.finish()
        finally:
            if source is not None:
                source.close()
            if archive is not None:
                archive.close()
            self._stream_state.close()

    @staticmethod
    def _images_for_verified_plan(
        manifest: StandardProductPackageManifest,
        receipt: StandardProductPackageAcceptanceReceipt,
        plan: MallDraftImportPlan,
    ) -> dict[tuple[str, UUID], _VerifiedImage]:
        try:
            checked_plan = MallDraftImportPlan.model_validate(
                plan.model_dump(mode="python")
            )
        except (AttributeError, TypeError, ValueError, ValidationError):
            raise PackageImageStreamError("image_not_in_plan") from None
        if (
            checked_plan.package_id != manifest.package_id
            or checked_plan.package_acceptance_id != receipt.acceptance_id
            or checked_plan.package_sha256 != receipt.package_sha256
            or checked_plan.source_batch_id != manifest.source_batch_id
            or checked_plan.opc_id != manifest.opc_id
            or checked_plan.data_classification != manifest.data_classification
        ):
            raise PackageImageStreamError("image_not_in_plan")

        if len(checked_plan.items) != len(manifest.items) or any(
            not _same_item_projection(planned, packaged)
            for planned, packaged in zip(
                checked_plan.items,
                manifest.items,
                strict=True,
            )
        ):
            raise PackageImageStreamError("image_not_in_plan")

        images: dict[tuple[str, UUID], _VerifiedImage] = {}
        for planned_item, packaged_item in zip(
            checked_plan.items,
            manifest.items,
            strict=True,
        ):
            for image, packaged in zip(
                planned_item.images,
                packaged_item.images,
                strict=True,
            ):
                key = (planned_item.draft_request_id, image.asset_id)
                images[key] = _VerifiedImage(
                    package_path=packaged.package_path,
                    sha256=image.sha256,
                    byte_size=image.byte_size,
                )
        return images


class _VerifiedImageStream:
    def __init__(self, source: IO[bytes], image: _VerifiedImage) -> None:
        self._source = source
        self._image = image
        self._digest = sha256()
        self._byte_size = 0
        self._finished = False

    def read(self, size: int = -1) -> bytes:
        if self._finished:
            return b""
        try:
            chunk = self._source.read(size)
        except (OSError, RuntimeError, ValueError, zipfile.BadZipFile):
            raise PackageImageStreamError("image_mismatch") from None
        if not isinstance(chunk, bytes):
            raise PackageImageStreamError("image_mismatch")
        self._byte_size += len(chunk)
        self._digest.update(chunk)
        return chunk

    def finish(self) -> None:
        if self._finished:
            return
        try:
            while self.read(128 * 1024):
                pass
        except PackageImageStreamError:
            self._finished = True
            raise
        self._finished = True
        if (
            self._byte_size != self._image.byte_size
            or self._digest.hexdigest() != self._image.sha256
        ):
            raise PackageImageStreamError("image_mismatch")


def _same_image_evidence(
    planned: object,
    packaged: PackagedImageEvidence,
) -> bool:
    return (
        getattr(planned, "asset_id", None) == packaged.asset_id
        and getattr(planned, "evidence_asset_id", None) == packaged.evidence_asset_id
        and getattr(planned, "role", None) == packaged.role
        and getattr(planned, "sha256", None) == packaged.sha256
        and getattr(planned, "byte_size", None) == packaged.byte_size
        and getattr(planned, "media_type", None) == packaged.media_type
        and getattr(planned, "captured_at", None) == packaged.captured_at
        and getattr(planned, "imported_at", None) == packaged.imported_at
    )


def _same_item_projection(
    planned: MallDraftImportItem,
    packaged: StandardPackageProduct,
) -> bool:
    return (
        planned.draft_request_id == packaged.draft_request_id
        and planned.source_acceptance_id == packaged.source_acceptance_id
        and planned.source_group_confirmation_event_id
        == packaged.source_group_confirmation_event_id
        and planned.source_album_product_id == packaged.source_album_product_id
        and planned.source_snapshot_version == packaged.source_snapshot_version
        and planned.source_type == packaged.source_type
        and planned.classification == packaged.classification
        and len(planned.images) == len(packaged.images)
        and all(
            _same_image_evidence(planned_image, packaged_image)
            for planned_image, packaged_image in zip(
                planned.images,
                packaged.images,
                strict=True,
            )
        )
        and planned.required_business_fields == packaged.required_business_fields
        and planned.claims_not_evaluated == packaged.claims_not_evaluated
        and planned.mall_product_id == packaged.mall_product_id
        and planned.bead_id == packaged.bead_id
        and planned.publication_state == packaged.publication_state
        and planned.auto_publish == packaged.auto_publish
        and planned.shared_market_opt_in == packaged.shared_market_opt_in
    )
