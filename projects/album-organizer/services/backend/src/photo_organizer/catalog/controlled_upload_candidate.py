from __future__ import annotations

from pydantic import ValidationError

from photo_organizer.catalog.real_sku_pilot import (
    RealSkuPilotError,
    build_real_sku_pilot_readiness,
)
from photo_organizer.schemas.controlled_upload_candidate import (
    ControlledUploadCandidate,
    controlled_upload_candidate_id,
)
from photo_organizer.schemas.real_sku_pilot import (
    PilotImageIntent,
    RealSkuPilotIntakeManifest,
    RealSkuPilotReadinessReceipt,
    ValidatedPilotImage,
)

_EXTENSIONS = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/heic": "heic",
}


class ControlledUploadCandidateError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def build_controlled_upload_candidate(
    readiness: RealSkuPilotReadinessReceipt,
    images: tuple[ValidatedPilotImage, ...],
) -> ControlledUploadCandidate:
    checked_readiness = validate_controlled_upload_readiness(readiness)
    checked_images = _images(images)
    intended = tuple(_image_facts(image) for image in checked_readiness.images)
    actual = tuple(_image_facts(image) for image in checked_images)
    if intended != actual:
        raise ControlledUploadCandidateError("readiness_image_mismatch")
    if any(image.media_type not in _EXTENSIONS for image in checked_images):
        raise ControlledUploadCandidateError("unsupported_upload_media_type")

    items = [
        {
            "local_asset_id": str(image.local_image_id),
            "role": image.role,
            "position": image.position,
            "filename": (f"{image.local_image_id}.{_EXTENSIONS[image.media_type]}"),
            "media_type": image.media_type,
            "byte_size": image.byte_size,
            "sha256": image.sha256,
            "captured_at": image.captured_at.isoformat().replace("+00:00", "Z"),
            "human_privacy_reviewed": True,
            "privacy_metadata_status": image.privacy_metadata_status,
            "retry": False,
        }
        for image in checked_images
    ]
    body: dict[str, object] = {
        "schema_version": "opc-controlled-upload-candidate/1.0",
        "status": "ready_for_authenticated_task_binding",
        "source_readiness_id": checked_readiness.readiness_id,
        "opc_id": checked_readiness.opc_id,
        "opc_authority_state": "requires_authenticated_revalidation",
        "album_product_id": checked_readiness.album_product_id,
        "source_type": checked_readiness.source_type,
        "target_chain": checked_readiness.target_chain,
        "data_classification": checked_readiness.data_classification,
        "material_owner": checked_readiness.material_owner,
        "authorization_basis": checked_readiness.authorization_basis,
        "authorized_by": checked_readiness.authorized_by,
        "authorized_at": checked_readiness.authorized_at.isoformat().replace(
            "+00:00", "Z"
        ),
        "privacy_review_confirmed": checked_readiness.privacy_review_confirmed,
        "customer_data_present": checked_readiness.customer_data_present,
        "items": items,
        "claims_not_evaluated": checked_readiness.claims_not_evaluated,
        "target_task_id": None,
        "task_binding_state": "not_bound",
        "upload_state": "not_started",
        "next_action": "bind_to_verified_opc_upload_task",
        "mall_product_id": None,
        "bead_id": None,
        "publication_state": "not_created",
        "auto_publish": False,
        "shared_market_opt_in": False,
    }
    body["candidate_id"] = controlled_upload_candidate_id(body)
    try:
        return ControlledUploadCandidate.model_validate(body)
    except ValidationError:
        raise ControlledUploadCandidateError("invalid_readiness") from None


def validate_controlled_upload_readiness(
    readiness: RealSkuPilotReadinessReceipt,
) -> RealSkuPilotReadinessReceipt:
    """Validate content consistency without claiming OPC identity authority."""
    readiness_payload = _readiness_payload(readiness)
    _require_safe_boundaries(readiness_payload)
    checked_readiness = _readiness(readiness_payload)
    _require_consistent_readiness_identity(checked_readiness)
    return checked_readiness


def _readiness_payload(value: RealSkuPilotReadinessReceipt) -> dict[str, object]:
    try:
        payload = value.model_dump(mode="json", warnings=False)
    except (AttributeError, TypeError, ValueError):
        raise ControlledUploadCandidateError("invalid_readiness") from None
    if not isinstance(payload, dict):
        raise ControlledUploadCandidateError("invalid_readiness")
    return payload


def _require_safe_boundaries(readiness: dict[str, object]) -> None:
    if "material_owner" not in readiness or "authorization_basis" not in readiness:
        raise ControlledUploadCandidateError("invalid_readiness")
    if (
        readiness["material_owner"] != "opc"
        or readiness["authorization_basis"] != "opc_self_owned_product_material"
    ):
        raise ControlledUploadCandidateError("unauthorized_material")
    if (
        "privacy_review_confirmed" not in readiness
        or "customer_data_present" not in readiness
    ):
        raise ControlledUploadCandidateError("invalid_readiness")
    if (
        readiness["privacy_review_confirmed"] is not True
        or readiness["customer_data_present"] is not False
    ):
        raise ControlledUploadCandidateError("privacy_boundary_failed")
    readiness_images = readiness.get("images")
    if not isinstance(readiness_images, list) or any(
        not isinstance(image, dict) or "human_privacy_reviewed" not in image
        for image in readiness_images
    ):
        raise ControlledUploadCandidateError("invalid_readiness")
    if any(image["human_privacy_reviewed"] is not True for image in readiness_images):
        raise ControlledUploadCandidateError("privacy_boundary_failed")


def _readiness(value: object) -> RealSkuPilotReadinessReceipt:
    try:
        return RealSkuPilotReadinessReceipt.model_validate(value)
    except (ValidationError, ValueError, TypeError):
        raise ControlledUploadCandidateError("invalid_readiness") from None


def _images(
    values: tuple[ValidatedPilotImage, ...],
) -> tuple[ValidatedPilotImage, ...]:
    try:
        checked = tuple(
            ValidatedPilotImage.model_validate(
                value.model_dump(mode="json", warnings=False)
            )
            for value in values
        )
    except (AttributeError, ValidationError, ValueError, TypeError):
        raise ControlledUploadCandidateError("readiness_image_mismatch") from None
    return tuple(sorted(checked, key=lambda image: image.position))


def _require_consistent_readiness_identity(
    readiness: RealSkuPilotReadinessReceipt,
) -> None:
    try:
        manifest = RealSkuPilotIntakeManifest(
            schema_version="opc-real-sku-pilot-intake/1.0",
            opc_id=readiness.opc_id,
            album_product_id=readiness.album_product_id,
            source_type=readiness.source_type,
            target_chain=readiness.target_chain,
            data_classification=readiness.data_classification,
            material_owner=readiness.material_owner,
            authorization_basis=readiness.authorization_basis,
            authorized_by=readiness.authorized_by,
            authorized_at=readiness.authorized_at,
            privacy_review_confirmed=readiness.privacy_review_confirmed,
            customer_data_present=readiness.customer_data_present,
            images=tuple(
                PilotImageIntent(
                    local_image_id=image.local_image_id,
                    role=image.role,
                    position=image.position,
                    captured_at=image.captured_at,
                    human_privacy_reviewed=True,
                )
                for image in readiness.images
            ),
            claims_not_evaluated=readiness.claims_not_evaluated,
            mall_product_id=readiness.mall_product_id,
            bead_id=readiness.bead_id,
            auto_publish=readiness.auto_publish,
            shared_market_opt_in=readiness.shared_market_opt_in,
        )
        rebuilt = build_real_sku_pilot_readiness(manifest, readiness.images)
    except (RealSkuPilotError, ValidationError, ValueError, TypeError):
        raise ControlledUploadCandidateError("invalid_readiness") from None
    if rebuilt != readiness:
        raise ControlledUploadCandidateError("invalid_readiness")


def _image_facts(image: ValidatedPilotImage) -> tuple[object, ...]:
    return (
        image.local_image_id,
        image.role,
        image.position,
        image.captured_at,
        image.human_privacy_reviewed,
        image.sha256,
        image.byte_size,
        image.media_type,
        image.privacy_metadata_status,
    )
