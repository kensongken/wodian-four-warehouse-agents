"""Persistence models for the Photo Organizer domain."""

from photo_organizer.models.album_product import AlbumProductSnapshot
from photo_organizer.models.asset import Asset
from photo_organizer.models.auth import Invite, OtpChallenge, RefreshToken
from photo_organizer.models.blob import StoredBlob
from photo_organizer.models.categorization_delivery import CategorizationDelivery
from photo_organizer.models.classification import ProductClassification
from photo_organizer.models.cleanup_job import StorageCleanupJob
from photo_organizer.models.evidence_asset import OpcEvidenceAsset
from photo_organizer.models.handoff_authority import (
    OpcExternalTenantBindingRecord,
    PreparedImageHandoffRecord,
    ProductCodeBindingRecord,
)
from photo_organizer.models.opc import OpcMembership, OpcWorkspace
from photo_organizer.models.outbox import OutboxEvent
from photo_organizer.models.product_image_set_delivery import (
    ProductImageSetDelivery,
    ProductImageSetDeliveryImage,
)
from photo_organizer.models.review import ClassificationReviewItem
from photo_organizer.models.task import ImportTask
from photo_organizer.models.upload_binding import ControlledUploadBinding
from photo_organizer.models.user import User

__all__ = [
    "AlbumProductSnapshot",
    "Asset",
    "CategorizationDelivery",
    "ClassificationReviewItem",
    "ControlledUploadBinding",
    "ImportTask",
    "Invite",
    "OpcEvidenceAsset",
    "OpcExternalTenantBindingRecord",
    "OpcMembership",
    "OpcWorkspace",
    "OtpChallenge",
    "OutboxEvent",
    "PreparedImageHandoffRecord",
    "ProductClassification",
    "ProductCodeBindingRecord",
    "ProductImageSetDelivery",
    "ProductImageSetDeliveryImage",
    "RefreshToken",
    "StorageCleanupJob",
    "StoredBlob",
    "User",
]
