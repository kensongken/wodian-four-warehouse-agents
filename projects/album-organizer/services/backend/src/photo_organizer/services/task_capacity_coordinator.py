from dataclasses import dataclass
from typing import Literal

from photo_organizer.domain.enums import AssetStatus
from photo_organizer.domain.limits import MAX_TASK_ACCEPTED_ASSETS
from photo_organizer.models.asset import Asset
from photo_organizer.repositories.asset_repository import AssetRepository

AdmissionDisposition = Literal["existing", "accepted", "duplicate"]


class TaskCapacityExceeded(Exception):
    """A new unique canonical cannot reserve another task slot."""


@dataclass(frozen=True)
class TaskCapacityAdmission:
    asset: Asset
    disposition: AdmissionDisposition


class TaskCapacityCoordinator:
    """Serialize task-local idempotency, content deduplication, and capacity.

    The caller owns the transaction and must hold the task row lock. This method
    performs database work only; object-storage operations belong after commit.
    """

    def __init__(
        self,
        repository: AssetRepository,
        *,
        max_accepted_assets: int = MAX_TASK_ACCEPTED_ASSETS,
    ) -> None:
        if max_accepted_assets < 1:
            raise ValueError("max_accepted_assets must be positive")
        self._repository = repository
        self._max_accepted_assets = max_accepted_assets

    async def admit(self, proposed: Asset) -> TaskCapacityAdmission:
        existing = await self._repository.for_task_local(
            proposed.user_id, proposed.task_id, proposed.local_asset_id
        )
        if existing is not None:
            return TaskCapacityAdmission(existing, "existing")

        if proposed.sha256 is None or proposed.byte_size is None:
            raise ValueError("accepted proposals require content metadata")
        canonical = await self._repository.for_task_content(
            proposed.user_id,
            proposed.task_id,
            proposed.sha256,
            proposed.byte_size,
        )
        if canonical is not None:
            proposed.blob_id = None
            proposed.canonical_asset_id = canonical.id
            proposed.status = AssetStatus.SKIPPED_DUPLICATE
            proposed.failure_code = None
            proposed.failure_message = None
            proposed.unsupported_reason = None
            await self._repository.add(proposed)
            return TaskCapacityAdmission(proposed, "duplicate")

        if (
            await self._repository.accepted_count(proposed.user_id, proposed.task_id)
            >= self._max_accepted_assets
        ):
            raise TaskCapacityExceeded

        proposed.canonical_asset_id = None
        await self._repository.add(proposed)
        return TaskCapacityAdmission(proposed, "accepted")
