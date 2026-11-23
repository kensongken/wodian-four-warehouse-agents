from enum import StrEnum


class TaskStatus(StrEnum):
    WAITING_UPLOAD = "waiting_upload"
    UPLOADING = "uploading"
    AI_PROCESSING = "ai_processing"
    WAITING_REVIEW = "waiting_review"
    COMPLETED = "completed"
    PAUSED = "paused"
    PARTIAL_FAILURE = "partial_failure"
    CANCELLED = "cancelled"


class AssetStatus(StrEnum):
    PENDING = "pending"
    UPLOADING = "uploading"
    UPLOADED = "uploaded"
    FAILED = "failed"
    SKIPPED_UNSUPPORTED = "skipped_unsupported"
    SKIPPED_DUPLICATE = "skipped_duplicate"
