import json
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from product_engine.contracts.style_warehouse_v2 import (
    StyleTransformAuthorizationCommand,
)
from product_engine.db import Database
from product_engine.repositories.style_handoffs import (
    PreparedStyleEligibility,
    StoredStyleHandoff,
    StyleHandoffRepository,
    StyleHandoffRepositoryFailure,
    StyleTransformAuthorizationView,
)
from product_engine.security.signatures import sign_request
from product_engine.services.style_eligibility_v2 import (
    StyleEligibilityFailure,
    StyleEligibilityPreparer,
)
from product_engine.services.style_warehouse_receiver import (
    DELIVERY_PATH,
    SignedStyleDelivery,
    StyleReceiverFailure,
    StyleWarehouseReceiver,
)

Clock = Callable[[], int]


class RuntimeStyleHandoffFailure(ValueError):
    pass


class RuntimeStyleHandoffService:
    def __init__(
        self,
        *,
        database: Database,
        tenant_id: UUID,
        secret: bytes,
        now: Clock,
    ) -> None:
        self.database = database
        self.tenant_id = tenant_id
        self.secret = secret
        self.now = now

    def _now_datetime(self) -> datetime:
        return datetime.fromtimestamp(self.now(), UTC)

    async def authorize(
        self,
        classification_id: UUID,
        command: StyleTransformAuthorizationCommand,
    ) -> StyleTransformAuthorizationView:
        if command.expires_at <= self._now_datetime():
            raise RuntimeStyleHandoffFailure("style_authorization_expired")
        try:
            async with self.database.session() as session:
                return await StyleHandoffRepository(
                    session, now=self._now_datetime
                ).append_authorization(
                    self.tenant_id,
                    classification_id,
                    authorization_request_id=command.authorization_request_id,
                    expires_at=command.expires_at,
                )
        except ValueError as error:
            raise RuntimeStyleHandoffFailure(str(error)) from error

    async def revoke(self, authorization_id: UUID) -> StyleTransformAuthorizationView:
        try:
            async with self.database.session() as session:
                return await StyleHandoffRepository(
                    session, now=self._now_datetime
                ).revoke_authorization(self.tenant_id, authorization_id)
        except ValueError as error:
            raise RuntimeStyleHandoffFailure(str(error)) from error

    async def prepare(self, classification_id: UUID) -> PreparedStyleEligibility:
        try:
            async with self.database.session() as session:
                repository = StyleHandoffRepository(session, now=self._now_datetime)
                return await StyleEligibilityPreparer(repository, now=self._now_datetime).prepare(
                    self.tenant_id, classification_id
                )
        except (StyleEligibilityFailure, StyleHandoffRepositoryFailure) as error:
            raise RuntimeStyleHandoffFailure(str(error)) from error

    async def evaluate(self, handoff_id: UUID) -> StoredStyleHandoff:
        try:
            async with self.database.session() as sender_session:
                delivery = await StyleHandoffRepository(
                    sender_session, now=self._now_datetime
                ).mark_pending_and_build_delivery(self.tenant_id, handoff_id)
            body = json.dumps(
                delivery.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            timestamp = self.now()
            signed = SignedStyleDelivery(
                body=body,
                tenant_id=self.tenant_id,
                timestamp=timestamp,
                signature=sign_request(
                    self.secret,
                    timestamp,
                    "classification",
                    self.tenant_id,
                    "POST",
                    DELIVERY_PATH,
                    body,
                ),
            )
            async with self.database.session() as receiver_session:
                receipt = await StyleWarehouseReceiver(
                    StyleHandoffRepository(receiver_session, now=self._now_datetime),
                    self.secret,
                    now_seconds=self.now,
                    now_datetime=self._now_datetime,
                ).receive(signed)
            async with self.database.session() as sender_session:
                return await StyleHandoffRepository(
                    sender_session, now=self._now_datetime
                ).settle_handoff(self.tenant_id, handoff_id, receipt)
        except (StyleReceiverFailure, StyleHandoffRepositoryFailure) as error:
            raise RuntimeStyleHandoffFailure(str(error)) from error

    async def read(self, handoff_id: UUID) -> StoredStyleHandoff:
        try:
            async with self.database.session() as session:
                value = await StyleHandoffRepository(session).read_handoff(
                    self.tenant_id, handoff_id
                )
        except StyleHandoffRepositoryFailure as error:
            raise RuntimeStyleHandoffFailure(str(error)) from error
        if value is None:
            raise RuntimeStyleHandoffFailure("style_handoff_not_found")
        return value
