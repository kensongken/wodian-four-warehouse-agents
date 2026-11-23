"""Process-local capability for one explicitly confirmed paid pilot scope."""

from __future__ import annotations

import hmac
import json
from dataclasses import dataclass, field
from decimal import Decimal
from hashlib import sha256
from secrets import token_bytes
from uuid import UUID

_ISSUER_SECRET = token_bytes(32)


@dataclass(frozen=True)
class RealPilotAuthorizationCapability:
    """Bind cost approval to one customer, source, style, slot, and run key."""

    customer_id: UUID
    product_id: str
    source_asset_id: UUID
    scene_style_version_id: UUID
    slot_key: str
    idempotency_key: str
    authorized_usd: Decimal
    _signature: bytes = field(repr=False, compare=False)

    def authorizes_suite(
        self,
        *,
        customer_id: UUID,
        product_id: str,
        source_asset_id: UUID,
        scene_style_version_id: UUID,
        slot_key: str,
        idempotency_key: str,
        generator_max_usd: object,
    ) -> bool:
        """Verify the complete user-visible run scope."""
        return self._is_authentic() and (
            self.customer_id == customer_id
            and self.product_id == product_id
            and self.source_asset_id == source_asset_id
            and self.scene_style_version_id == scene_style_version_id
            and self.slot_key == slot_key
            and self.idempotency_key == idempotency_key
            and self.authorized_usd == generator_max_usd
        )

    def authorizes_image(
        self,
        *,
        customer_id: UUID,
        product_id: str,
        source_asset_id: UUID,
        scene_style_version_id: UUID,
        slot_key: str,
        idempotency_key: str,
        generator_max_usd: object,
        style_reference_manifest_sha256: str | None = None,
    ) -> bool:
        """Recheck the provider-facing scope at the deepest image-job boundary."""
        return self._is_authentic() and (
            self.customer_id == customer_id
            and self.product_id == product_id
            and self.source_asset_id == source_asset_id
            and self.scene_style_version_id == scene_style_version_id
            and self.slot_key == slot_key
            and self.idempotency_key == idempotency_key
            and self.authorized_usd == generator_max_usd
        )

    def authorized_usd_for(
        self, scene_style_version_id: UUID, slot_key: str
    ) -> Decimal:
        del scene_style_version_id, slot_key
        return self.authorized_usd

    def _is_authentic(self) -> bool:
        expected = _signature(
            customer_id=self.customer_id,
            product_id=self.product_id,
            source_asset_id=self.source_asset_id,
            scene_style_version_id=self.scene_style_version_id,
            slot_key=self.slot_key,
            idempotency_key=self.idempotency_key,
            authorized_usd=self.authorized_usd,
        )
        return hmac.compare_digest(self._signature, expected)


@dataclass(frozen=True)
class AuthorizedItemScope:
    scene_style_version_id: UUID
    slot_key: str
    style_reference_manifest_sha256: str
    item_authorized_usd: Decimal


@dataclass(frozen=True)
class RealPilotBatchAuthorizationCapability:
    """Process-local authority for the exact cells in one signed batch."""

    customer_id: UUID
    product_id: str
    source_asset_id: UUID
    run_idempotency_key: str
    authorization_manifest_sha256: str
    authorized_maximum_usd: Decimal
    item_scopes: tuple[AuthorizedItemScope, ...]
    _signature: bytes = field(repr=False, compare=False)

    def authorizes_image(
        self,
        *,
        customer_id: UUID,
        product_id: str,
        source_asset_id: UUID,
        scene_style_version_id: UUID,
        slot_key: str,
        idempotency_key: str,
        generator_max_usd: object,
        style_reference_manifest_sha256: str | None = None,
    ) -> bool:
        del generator_max_usd
        matching = [
            item
            for item in self.item_scopes
            if item.scene_style_version_id == scene_style_version_id
            and item.slot_key == slot_key
        ]
        return (
            self._is_authentic()
            and bool(matching)
            and (
                self.customer_id == customer_id
                and self.product_id == product_id
                and self.source_asset_id == source_asset_id
                and self.run_idempotency_key == idempotency_key
                and (
                    style_reference_manifest_sha256 is None
                    or matching[0].style_reference_manifest_sha256
                    == style_reference_manifest_sha256
                )
            )
        )

    def authorized_usd_for(
        self, scene_style_version_id: UUID, slot_key: str
    ) -> Decimal:
        for item in self.item_scopes:
            if (
                item.scene_style_version_id == scene_style_version_id
                and item.slot_key == slot_key
            ):
                return item.item_authorized_usd
        raise ValueError("real_pilot_batch_item_unavailable")

    def _is_authentic(self) -> bool:
        return hmac.compare_digest(self._signature, _batch_signature(self))


def issue_real_pilot_authorization(
    *,
    customer_id: UUID,
    product_id: str,
    source_asset_id: UUID,
    scene_style_version_id: UUID,
    slot_key: str,
    idempotency_key: str,
    authorized_usd: Decimal,
) -> RealPilotAuthorizationCapability:
    """Issue authority after the controlled route validates explicit confirmation."""
    if authorized_usd <= 0:
        raise ValueError("real_pilot_authorization_must_be_positive")
    signature = _signature(
        customer_id=customer_id,
        product_id=product_id,
        source_asset_id=source_asset_id,
        scene_style_version_id=scene_style_version_id,
        slot_key=slot_key,
        idempotency_key=idempotency_key,
        authorized_usd=authorized_usd,
    )
    return RealPilotAuthorizationCapability(
        customer_id=customer_id,
        product_id=product_id,
        source_asset_id=source_asset_id,
        scene_style_version_id=scene_style_version_id,
        slot_key=slot_key,
        idempotency_key=idempotency_key,
        authorized_usd=authorized_usd,
        _signature=signature,
    )


def issue_real_pilot_batch_authorization(
    *,
    customer_id: UUID,
    product_id: str,
    source_asset_id: UUID,
    run_idempotency_key: str,
    authorization_manifest_sha256: str,
    authorized_maximum_usd: Decimal,
    item_scopes: tuple[AuthorizedItemScope, ...],
) -> RealPilotBatchAuthorizationCapability:
    if (
        not item_scopes
        or authorized_maximum_usd <= 0
        or sum((item.item_authorized_usd for item in item_scopes), Decimal(0))
        != authorized_maximum_usd
    ):
        raise ValueError("real_pilot_batch_authorization_invalid")
    unsigned = RealPilotBatchAuthorizationCapability(
        customer_id=customer_id,
        product_id=product_id,
        source_asset_id=source_asset_id,
        run_idempotency_key=run_idempotency_key,
        authorization_manifest_sha256=authorization_manifest_sha256,
        authorized_maximum_usd=authorized_maximum_usd,
        item_scopes=item_scopes,
        _signature=b"",
    )
    return RealPilotBatchAuthorizationCapability(
        **{**unsigned.__dict__, "_signature": _batch_signature(unsigned)}
    )


def _batch_signature(capability: RealPilotBatchAuthorizationCapability) -> bytes:
    payload = {
        "customer_id": str(capability.customer_id),
        "product_id": capability.product_id,
        "source_asset_id": str(capability.source_asset_id),
        "run_idempotency_key": capability.run_idempotency_key,
        "authorization_manifest_sha256": capability.authorization_manifest_sha256,
        "authorized_maximum_usd": str(capability.authorized_maximum_usd),
        "items": [
            {
                "scene_style_version_id": str(item.scene_style_version_id),
                "slot_key": item.slot_key,
                "style_reference_manifest_sha256": (
                    item.style_reference_manifest_sha256
                ),
                "item_authorized_usd": str(item.item_authorized_usd),
            }
            for item in capability.item_scopes
        ],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(_ISSUER_SECRET, canonical, sha256).digest()


def _signature(
    *,
    customer_id: UUID,
    product_id: str,
    source_asset_id: UUID,
    scene_style_version_id: UUID,
    slot_key: str,
    idempotency_key: str,
    authorized_usd: Decimal,
) -> bytes:
    payload = "\x00".join(
        (
            str(customer_id),
            product_id,
            str(source_asset_id),
            str(scene_style_version_id),
            slot_key,
            idempotency_key,
            format(authorized_usd, "f"),
        )
    ).encode()
    return hmac.new(_ISSUER_SECRET, payload, sha256).digest()
