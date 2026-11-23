from __future__ import annotations

import json
from hashlib import sha256

from pydantic import ValidationError

from photo_organizer.categorization.contracts import ProductSourceType
from photo_organizer.schemas.real_sku_pilot import (
    RealSkuPilotIntakeManifest,
    RealSkuPilotReadinessReceipt,
    ValidatedPilotImage,
)


class RealSkuPilotError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def build_real_sku_pilot_readiness(
    manifest: RealSkuPilotIntakeManifest,
    images: tuple[ValidatedPilotImage, ...],
) -> RealSkuPilotReadinessReceipt:
    manifest_payload = _manifest_payload(manifest)
    _require_safe_boundaries(manifest_payload)
    checked_manifest = _manifest(manifest_payload)
    if checked_manifest.source_type is not ProductSourceType.FIRST_SALE_BEAD:
        raise RealSkuPilotError("unsupported_source_context")
    checked_images = _images(images)
    intended = tuple(
        (
            image.local_image_id,
            image.role,
            image.position,
            image.captured_at,
            image.human_privacy_reviewed,
        )
        for image in checked_manifest.images
    )
    actual = tuple(
        (
            image.local_image_id,
            image.role,
            image.position,
            image.captured_at,
            image.human_privacy_reviewed,
        )
        for image in checked_images
    )
    if intended != actual:
        raise RealSkuPilotError("image_set_mismatch")

    canonical = json.dumps(
        {
            "manifest": checked_manifest.model_dump(mode="json"),
            "images": [image.model_dump(mode="json") for image in checked_images],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    readiness_id = f"opc-real-sku-pilot-readiness:{sha256(canonical).hexdigest()}"
    try:
        return RealSkuPilotReadinessReceipt(
            schema_version="opc-real-sku-pilot-readiness/1.0",
            readiness_id=readiness_id,
            status="eligible_for_controlled_import",
            opc_id=checked_manifest.opc_id,
            album_product_id=checked_manifest.album_product_id,
            source_type=ProductSourceType.FIRST_SALE_BEAD,
            target_chain="sellable_product_material",
            data_classification="opc_owned_product_material_no_customer_data",
            material_owner="opc",
            authorization_basis="opc_self_owned_product_material",
            authorized_by=checked_manifest.authorized_by,
            authorized_at=checked_manifest.authorized_at,
            privacy_review_confirmed=True,
            customer_data_present=False,
            images=checked_images,
            claims_not_evaluated=checked_manifest.claims_not_evaluated,
            next_action="upload_to_controlled_ingestion",
            mall_product_id=None,
            bead_id=None,
            publication_state="not_created",
            auto_publish=False,
            shared_market_opt_in=False,
        )
    except ValidationError:
        raise RealSkuPilotError("invalid_manifest") from None


def _manifest_payload(value: RealSkuPilotIntakeManifest) -> dict[str, object]:
    try:
        payload = value.model_dump(mode="json", warnings=False)
    except (AttributeError, TypeError, ValueError):
        raise RealSkuPilotError("invalid_manifest") from None
    if not isinstance(payload, dict):
        raise RealSkuPilotError("invalid_manifest")
    return payload


def _require_safe_boundaries(manifest: dict[str, object]) -> None:
    if "material_owner" not in manifest or "authorization_basis" not in manifest:
        raise RealSkuPilotError("invalid_manifest")
    if (
        manifest["material_owner"] != "opc"
        or manifest["authorization_basis"] != "opc_self_owned_product_material"
    ):
        raise RealSkuPilotError("unauthorized_material")
    if (
        "privacy_review_confirmed" not in manifest
        or "customer_data_present" not in manifest
    ):
        raise RealSkuPilotError("invalid_manifest")
    if (
        manifest["privacy_review_confirmed"] is not True
        or manifest["customer_data_present"] is not False
    ):
        raise RealSkuPilotError("privacy_boundary_failed")
    manifest_images = manifest.get("images")
    if not isinstance(manifest_images, list) or any(
        not isinstance(image, dict) or "human_privacy_reviewed" not in image
        for image in manifest_images
    ):
        raise RealSkuPilotError("invalid_manifest")
    if any(image["human_privacy_reviewed"] is not True for image in manifest_images):
        raise RealSkuPilotError("privacy_boundary_failed")


def _manifest(value: object) -> RealSkuPilotIntakeManifest:
    try:
        return RealSkuPilotIntakeManifest.model_validate(value)
    except (ValidationError, ValueError, TypeError):
        raise RealSkuPilotError("invalid_manifest") from None


def _images(
    values: tuple[ValidatedPilotImage, ...],
) -> tuple[ValidatedPilotImage, ...]:
    try:
        checked = tuple(
            ValidatedPilotImage.model_validate(value.model_dump(mode="json"))
            for value in values
        )
    except (AttributeError, ValidationError, ValueError, TypeError):
        raise RealSkuPilotError("image_set_mismatch") from None
    ordered = tuple(sorted(checked, key=lambda image: image.position))
    if not 2 <= len(ordered) <= 4:
        raise RealSkuPilotError("image_set_mismatch")
    return ordered
