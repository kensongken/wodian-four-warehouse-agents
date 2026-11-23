"""Read-only Mall style catalogue and byte-bearing product-photo import."""

from __future__ import annotations

import io
import warnings
from collections import defaultdict
from datetime import UTC, datetime
from hashlib import sha256
from threading import Lock
from typing import Literal, Protocol
from uuid import UUID, uuid4

from PIL import Image, UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from opc_style.contracts.mall_style_handoff_v1 import (
    MallProductAssetImportReceiptV1,
    MallStyleAvailableSelectionV1,
    MallStyleSelectionListV1,
    MallStyleSelectionV1,
)
from opc_style.domain.errors import DomainRuleError
from opc_style.models import (
    MallStyleSourceAssetBinding,
    ProductAsset,
    ProductAssetPermissionEvent,
    SceneStyle,
    StyleCard,
    StyleCardSampleLink,
    StyleCardVersion,
    StyleSample,
    StyleTemplateSlot,
)
from opc_style.schemas import ProductAssetIntakeRequest
from opc_style.services.assets import ProductAssetService
from opc_style.services.local_media_stage import LocalMediaStage
from opc_style.services.real_photos import AssetContentStoreError

_IMPORT_LOCKS_GUARD = Lock()
_IMPORT_LOCKS: dict[tuple[str, str, str], Lock] = defaultdict(Lock)


class MallProductAssetStore(Protocol):
    """Existing private ProductAsset byte store operations used by this bridge."""

    def persist_original(
        self,
        customer_id: UUID,
        asset_id: UUID,
        content: bytes,
        expected_sha256: str,
    ) -> None: ...

    def read_original(
        self, customer_id: UUID, asset_id: UUID, expected_sha256: str
    ) -> bytes: ...

    def discard_uncommitted_asset(
        self,
        customer_id: UUID,
        kind: Literal["originals", "style-samples"],
        asset_id: UUID,
        expected_sha256: str,
    ) -> None: ...


class MallStyleIntegrationConflict(ValueError):
    """A stable source identity was reused with different immutable facts."""


class MallStyleIntegrationRejected(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _import_lock(merchant_id: UUID, opc_id: UUID, source_asset_id: UUID) -> Lock:
    key = (str(merchant_id), str(opc_id), str(source_asset_id))
    with _IMPORT_LOCKS_GUARD:
        return _IMPORT_LOCKS[key]


class MallStyleSelectionService:
    """Expose only current, confirmed, evidence-backed customer styles."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def list_available(
        self, *, merchant_id: UUID, opc_id: UUID
    ) -> MallStyleSelectionListV1:
        with self._session.no_autoflush:
            selections = self._current_selections(merchant_id)
        explicit = [row for row in selections if row.is_default]
        if len(selections) == 1:
            only = selections[0].model_copy(update={"is_default": True})
            selections = [only]
            status = "ready"
            reason = "sole_selection"
            default = _selection_identity(only)
        elif len(explicit) == 1:
            status = "ready"
            reason = "explicit_default"
            default = _selection_identity(explicit[0])
        elif not selections:
            status = "unavailable"
            reason = "no_confirmed_style"
            default = None
        elif not explicit:
            status = "selection_required"
            reason = "multiple_without_default"
            default = None
        else:
            status = "selection_required"
            reason = "multiple_defaults"
            default = None
        return MallStyleSelectionListV1.model_validate(
            {
                "merchant_id": merchant_id,
                "opc_id": opc_id,
                "selection_status": status,
                "selection_reason": reason,
                "default_selection": default,
                "selections": tuple(selections),
            }
        )

    def _current_selections(
        self, merchant_id: UUID
    ) -> list[MallStyleAvailableSelectionV1]:
        scenes = list(
            self._session.scalars(
                select(SceneStyle)
                .join(
                    StyleCard,
                    (StyleCard.customer_id == SceneStyle.customer_id)
                    & (StyleCard.id == SceneStyle.style_card_id),
                )
                .where(
                    SceneStyle.customer_id == merchant_id,
                    SceneStyle.active.is_(True),
                    SceneStyle.style_origin == "customer_upload",
                    StyleCard.customer_id == merchant_id,
                    StyleCard.kind == "scene",
                    StyleCard.active.is_(True),
                )
                .order_by(SceneStyle.name, SceneStyle.id)
            )
        )
        rows: list[MallStyleAvailableSelectionV1] = []
        for scene in scenes:
            if scene.style_card_id is None:
                continue
            version = self._session.scalar(
                select(StyleCardVersion)
                .where(
                    StyleCardVersion.customer_id == merchant_id,
                    StyleCardVersion.style_card_id == scene.style_card_id,
                )
                .order_by(StyleCardVersion.version.desc())
                .limit(1)
            )
            if version is None or not self._confirmed_version(merchant_id, version):
                continue
            main_version_id = self._valid_main_version_id(merchant_id, version)
            raw_main_id = version.content.get("main_style_card_version_id")
            if raw_main_id is not None and main_version_id is None:
                continue
            slots = list(
                self._session.scalars(
                    select(StyleTemplateSlot)
                    .where(
                        StyleTemplateSlot.customer_id == merchant_id,
                        StyleTemplateSlot.scene_style_id == scene.id,
                        StyleTemplateSlot.style_card_version_id == version.id,
                    )
                    .order_by(StyleTemplateSlot.position, StyleTemplateSlot.slot_key)
                )
            )
            sealed_slots = version.content.get("slots")
            if not isinstance(sealed_slots, list) or len(slots) != len(sealed_slots):
                continue
            default_slot = version.content.get("mall_default_slot_key")
            for slot in slots:
                snapshot = next(
                    (
                        item
                        for item in sealed_slots
                        if isinstance(item, dict) and item.get("id") == str(slot.id)
                    ),
                    None,
                )
                if snapshot != _slot_snapshot(merchant_id, scene, version, slot):
                    rows = [row for row in rows if row.scene_style_id != scene.id]
                    break
                if not slot.active:
                    continue
                rows.append(
                    MallStyleAvailableSelectionV1(
                        scene_style_id=scene.id,
                        style_card_version_id=version.id,
                        slot_key=slot.slot_key,
                        main_style_card_version_id=main_version_id,
                        scene_style_card_id=scene.style_card_id,
                        scene_name=scene.name,
                        style_version=version.version,
                        slot_id=slot.id,
                        slot_label=slot.label,
                        slot_rules=slot.rules,
                        is_default=default_slot == slot.slot_key,
                    )
                )
        return rows

    def _confirmed_version(self, merchant_id: UUID, version: StyleCardVersion) -> bool:
        if (
            version.content.get("historical_style") is not True
            or version.content.get("style_origin") != "customer_upload"
            or version.content.get("stability") != "customer_confirmed"
        ):
            return False
        links = list(
            self._session.scalars(
                select(StyleCardSampleLink).where(
                    StyleCardSampleLink.customer_id == merchant_id,
                    StyleCardSampleLink.style_card_version_id == version.id,
                )
            )
        )
        if not links:
            return False
        for link in links:
            sample = self._session.scalar(
                select(StyleSample).where(
                    StyleSample.customer_id == merchant_id,
                    StyleSample.id == link.style_sample_id,
                )
            )
            if (
                sample is None
                or sample.withdrawn_at is not None
                or not sample.has_complete_content_seal
                or sample.consent_provenance == "local_synthetic_acceptance"
            ):
                return False
        return True

    def _valid_main_version_id(
        self, merchant_id: UUID, version: StyleCardVersion
    ) -> UUID | None:
        raw = version.content.get("main_style_card_version_id")
        if raw is None:
            return None
        try:
            version_id = UUID(str(raw))
        except ValueError:
            return None
        main = self._session.scalar(
            select(StyleCardVersion)
            .join(
                StyleCard,
                (StyleCard.customer_id == StyleCardVersion.customer_id)
                & (StyleCard.id == StyleCardVersion.style_card_id),
            )
            .where(
                StyleCardVersion.customer_id == merchant_id,
                StyleCardVersion.id == version_id,
                StyleCard.customer_id == merchant_id,
                StyleCard.kind == "main",
                StyleCard.active.is_(True),
            )
        )
        return main.id if main is not None else None


class MallProductAssetImportService:
    """Persist one verified real product photo without starting a job."""

    def __init__(self, session: Session, store: MallProductAssetStore) -> None:
        self._session = session
        self._store = store

    def import_product_photo(
        self,
        *,
        merchant_id: UUID,
        opc_id: UUID,
        product_id: str,
        source_asset_id: UUID,
        claimed_sha256: str,
        authorization_reference: str,
        content: bytes,
        mime_type: str,
        width: int,
        height: int,
    ) -> MallProductAssetImportReceiptV1:
        with _import_lock(merchant_id, opc_id, source_asset_id):
            existing = self._binding(merchant_id, opc_id, source_asset_id)
            if existing is not None:
                return self._replay(
                    existing,
                    product_id=product_id,
                    claimed_sha256=claimed_sha256,
                    authorization_reference=authorization_reference,
                    content=content,
                )
            asset_id = uuid4()
            request = ProductAssetIntakeRequest.model_validate(
                {
                    "product_id": product_id,
                    "photo_id": f"mall:{opc_id}:{source_asset_id}",
                    "confirmed": True,
                    "source_type": "mall_authorized_upload",
                    "quality_flags": [],
                    "occurred_at": datetime.now(UTC).isoformat(),
                    "transform_permission": True,
                    "content_sha256": claimed_sha256,
                    "mime_type": mime_type,
                    "width": width,
                    "height": height,
                }
            )
            created = False
            persisted = False
            try:
                intake = ProductAssetService(self._session).intake(
                    merchant_id,
                    request,
                    commit=False,
                    asset_id=asset_id,
                    permission_provenance=_permission_provenance(
                        authorization_reference
                    ),
                )
                asset = intake.asset
                created = intake.created
                self._store.persist_original(
                    merchant_id, asset.id, content, claimed_sha256
                )
                persisted = True
                self._session.add(
                    MallStyleSourceAssetBinding(
                        merchant_id=merchant_id,
                        opc_id=opc_id,
                        source_asset_id=source_asset_id,
                        source_content_sha256=claimed_sha256,
                        asset_role="product_photo",
                        style_product_asset_id=asset.id,
                    )
                )
                self._session.commit()
            except IntegrityError as error:
                self._session.rollback()
                self._discard_new(
                    created, persisted, merchant_id, asset_id, claimed_sha256
                )
                winner = self._binding(merchant_id, opc_id, source_asset_id)
                if winner is None:
                    raise
                try:
                    return self._replay(
                        winner,
                        product_id=product_id,
                        claimed_sha256=claimed_sha256,
                        authorization_reference=authorization_reference,
                        content=content,
                    )
                except MallStyleIntegrationConflict as conflict:
                    raise conflict from error
            except AssetContentStoreError:
                self._session.rollback()
                self._discard_new(
                    created, persisted, merchant_id, asset_id, claimed_sha256
                )
                raise
            except DomainRuleError as error:
                self._session.rollback()
                self._discard_new(
                    created, persisted, merchant_id, asset_id, claimed_sha256
                )
                raise MallStyleIntegrationConflict(error.code) from error
            return _import_receipt(
                merchant_id=merchant_id,
                opc_id=opc_id,
                source_asset_id=source_asset_id,
                product_id=product_id,
                authorization_reference=authorization_reference,
                asset=asset,
                disposition="accepted",
            )

    def _replay(
        self,
        binding: MallStyleSourceAssetBinding,
        *,
        product_id: str,
        claimed_sha256: str,
        authorization_reference: str,
        content: bytes,
    ) -> MallProductAssetImportReceiptV1:
        if (
            binding.asset_role != "product_photo"
            or binding.source_content_sha256 != claimed_sha256
            or binding.style_product_asset_id is None
        ):
            raise MallStyleIntegrationConflict("mall_product_asset_source_conflict")
        asset = self._session.scalar(
            select(ProductAsset).where(
                ProductAsset.customer_id == binding.merchant_id,
                ProductAsset.id == binding.style_product_asset_id,
                ProductAsset.product_id == product_id,
                ProductAsset.content_sha256 == claimed_sha256,
                ProductAsset.identity == "original_real_photo",
                ProductAsset.confirmed.is_(True),
            )
        )
        if asset is None:
            raise MallStyleIntegrationConflict("mall_product_asset_source_conflict")
        permission = self._session.scalar(
            select(ProductAssetPermissionEvent).where(
                ProductAssetPermissionEvent.customer_id == binding.merchant_id,
                ProductAssetPermissionEvent.product_asset_id == asset.id,
                ProductAssetPermissionEvent.occurred_at == asset.occurred_at,
                ProductAssetPermissionEvent.transform_permission.is_(True),
                ProductAssetPermissionEvent.provenance
                == _permission_provenance(authorization_reference),
            )
        )
        if permission is None:
            raise MallStyleIntegrationConflict("mall_product_asset_source_conflict")
        try:
            stored = self._store.read_original(
                binding.merchant_id, asset.id, claimed_sha256
            )
        except AssetContentStoreError as error:
            raise MallStyleIntegrationRejected(
                "mall_product_asset_storage_unavailable"
            ) from error
        if stored != content:
            raise MallStyleIntegrationConflict("mall_product_asset_source_conflict")
        return _import_receipt(
            merchant_id=binding.merchant_id,
            opc_id=binding.opc_id,
            source_asset_id=binding.source_asset_id,
            product_id=product_id,
            authorization_reference=authorization_reference,
            asset=asset,
            disposition="duplicate",
        )

    def _binding(
        self, merchant_id: UUID, opc_id: UUID, source_asset_id: UUID
    ) -> MallStyleSourceAssetBinding | None:
        return self._session.scalar(
            select(MallStyleSourceAssetBinding).where(
                MallStyleSourceAssetBinding.merchant_id == merchant_id,
                MallStyleSourceAssetBinding.opc_id == opc_id,
                MallStyleSourceAssetBinding.source_asset_id == source_asset_id,
            )
        )

    def _discard_new(
        self,
        created: bool,
        persisted: bool,
        merchant_id: UUID,
        asset_id: UUID,
        claimed_sha256: str,
    ) -> None:
        if not created or not persisted:
            return
        self._store.discard_uncommitted_asset(
            merchant_id, "originals", asset_id, claimed_sha256
        )


def validate_product_image(
    content: bytes,
    *,
    claimed_sha256: str,
    declared_mime_type: str,
    max_bytes: int,
    max_width: int,
    max_height: int,
    max_pixels: int,
) -> tuple[str, int, int]:
    """Validate digest, exact MIME, complete decode, dimensions, and one frame."""
    if len(content) > max_bytes:
        raise MallStyleIntegrationRejected("mall_product_asset_too_large")
    if sha256(content).hexdigest() != claimed_sha256:
        raise MallStyleIntegrationRejected("mall_product_asset_digest_mismatch")
    formats = {"JPEG": "image/jpeg", "PNG": "image/png"}
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as image:
                image_format = image.format
                width = image.width
                height = image.height
                frames = getattr(image, "n_frames", 1)
                if (
                    width > max_width
                    or height > max_height
                    or width * height > max_pixels
                    or frames != 1
                ):
                    raise MallStyleIntegrationRejected(
                        "mall_product_asset_decode_limits_exceeded"
                    )
                image.verify()
            if image_format not in formats:
                raise MallStyleIntegrationRejected(
                    "mall_product_asset_mime_unsupported"
                )
            actual_mime = formats[image_format]
            if declared_mime_type != actual_mime:
                raise MallStyleIntegrationRejected(
                    "mall_product_asset_mime_mismatch"
                )
            if not _canonical_image_end(image_format, content):
                raise MallStyleIntegrationRejected(
                    "mall_product_asset_decode_invalid"
                )
            with Image.open(io.BytesIO(content)) as image:
                image.load()
    except MallStyleIntegrationRejected:
        raise
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        OSError,
        UnidentifiedImageError,
    ) as error:
        raise MallStyleIntegrationRejected(
            "mall_product_asset_decode_invalid"
        ) from error
    return actual_mime, width, height


def _canonical_image_end(image_format: str, data: bytes) -> bool:
    return LocalMediaStage._canonical(image_format, data)


def _selection_identity(
    value: MallStyleAvailableSelectionV1,
) -> MallStyleSelectionV1:
    return MallStyleSelectionV1(
        scene_style_id=value.scene_style_id,
        style_card_version_id=value.style_card_version_id,
        slot_key=value.slot_key,
    )


def _slot_snapshot(
    merchant_id: UUID,
    scene: SceneStyle,
    version: StyleCardVersion,
    slot: StyleTemplateSlot,
) -> dict[str, object]:
    return {
        "id": str(slot.id),
        "customer_id": str(merchant_id),
        "scene_style_id": str(scene.id),
        "style_card_version_id": str(version.id),
        "slot_key": slot.slot_key,
        "label": slot.label,
        "rules": slot.rules,
        "active": slot.active,
        "position": slot.position,
    }


def _import_receipt(
    *,
    merchant_id: UUID,
    opc_id: UUID,
    source_asset_id: UUID,
    product_id: str,
    authorization_reference: str,
    asset: ProductAsset,
    disposition: str,
) -> MallProductAssetImportReceiptV1:
    return MallProductAssetImportReceiptV1.model_validate(
        {
            "merchant_id": merchant_id,
            "opc_id": opc_id,
            "source_asset_id": source_asset_id,
            "source_content_sha256": asset.content_sha256,
            "authorization_reference": authorization_reference,
            "product_id": product_id,
            "style_product_asset_id": asset.id,
            "mime_type": asset.mime_type,
            "width": asset.width,
            "height": asset.height,
            "disposition": disposition,
        }
    )


def _permission_provenance(authorization_reference: str) -> str:
    return f"mall_product_asset_import:{authorization_reference}"
