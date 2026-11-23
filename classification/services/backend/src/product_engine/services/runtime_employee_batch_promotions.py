from collections.abc import Sequence
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from product_engine.contracts.album import ProductImageSetReadyV1
from product_engine.contracts.employee_formal_handoff import EmployeePromotionProductReceipt
from product_engine.db import Database
from product_engine.domain.errors import EventConflict
from product_engine.repositories.events import EventRepository
from product_engine.repositories.products import ProductRepository
from product_engine.services.employee_batch_promotions import EmployeeBatchPromotionFailure
from product_engine.services.sales_strategy_inputs import canonical_album_sha256

EMPLOYEE_PROMOTION_REQUEST_SOURCE = "employee_handoff_request"
EMPLOYEE_PROMOTION_EVENT_SOURCE = "employee_import_promotion"


class RuntimeEmployeePromotionWriter:
    def __init__(self, database: Database, tenant_id: UUID) -> None:
        self.database = database
        self.tenant_id = tenant_id

    async def commit_batch(
        self,
        *,
        request_id: UUID,
        batch_id: str,
        content_sha256: str,
        events: Sequence[ProductImageSetReadyV1],
    ) -> tuple[EmployeePromotionProductReceipt, ...]:
        del batch_id
        if (
            not events
            or len({event.product_code for event in events}) != len(events)
            or any(event.tenant_id != self.tenant_id for event in events)
        ):
            raise EmployeeBatchPromotionFailure("employee_handoff_ledger_conflict")
        try:
            async with self.database.session() as session:
                event_repository = EventRepository(session)
                product_repository = ProductRepository(session)
                await event_repository.claim_inbox(
                    self.tenant_id,
                    EMPLOYEE_PROMOTION_REQUEST_SOURCE,
                    request_id,
                    content_sha256,
                )
                results: list[EmployeePromotionProductReceipt] = []
                for event in events:
                    claim = await event_repository.claim_inbox(
                        self.tenant_id,
                        EMPLOYEE_PROMOTION_EVENT_SOURCE,
                        event.event_id,
                        canonical_album_sha256(event),
                    )
                    if claim.disposition == "new":
                        package_version = await product_repository.upsert_image_set(
                            event,
                            package_event_id=event.event_id,
                        )
                    else:
                        package_version = (
                            await product_repository.required_material_package_version_by_event(
                                self.tenant_id,
                                event.event_id,
                                event.product_code,
                                event,
                            )
                        )
                    results.append(
                        EmployeePromotionProductReceipt.from_event(event, package_version)
                    )
                return tuple(results)
        except EventConflict as error:
            error_code = (
                "employee_handoff_request_conflict"
                if str(error) == "event_id_reused_with_different_payload"
                else "employee_handoff_ledger_conflict"
            )
            raise EmployeeBatchPromotionFailure(error_code) from error
        except IntegrityError as error:
            raise EmployeeBatchPromotionFailure("employee_handoff_ledger_conflict") from error
