from datetime import timedelta

from sqlalchemy.orm import Session
from test_style_warehouse_v3_contract import NOW, delivery, eligibility

from opc_style.repositories_style_warehouse import StyleWarehouseV3Repository
from opc_style.services.style_warehouse_v3 import StyleWarehouseV3Service


def service(session: Session) -> StyleWarehouseV3Service:
    return StyleWarehouseV3Service(
        StyleWarehouseV3Repository(session), now=lambda: NOW
    )


def test_accepts_complete_local_only_eligibility_without_generation(
    session: Session,
) -> None:
    receipt = service(session).receive(delivery())

    assert receipt.decision == "accepted"
    assert receipt.rejection_code is None
    assert receipt.generation_job_id is None


def test_expired_authorization_is_a_stable_rejection(session: Session) -> None:
    value = eligibility(
        transform_authorization_status="expired",
        transform_authorization_expires_at=NOW - timedelta(seconds=1),
    )
    first = service(session).receive(delivery(value))
    replay = service(session).receive(delivery(value))

    assert first.decision == "rejected"
    assert first.rejection_code == "style_transform_authorization_expired"
    assert replay.decision_receipt_id == first.decision_receipt_id
    assert replay.disposition == "duplicate"


def test_revoked_authorization_is_rejected(session: Session) -> None:
    value = eligibility(
        transform_authorization_status="revoked",
        transform_authorization_revoked_at=NOW - timedelta(seconds=1),
    )

    receipt = service(session).receive(delivery(value))

    assert receipt.rejection_code == "style_transform_authorization_revoked"


def test_authorization_image_set_mismatch_is_rejected(session: Session) -> None:
    value = eligibility(transform_authorization_image_set_sha256="e" * 64)

    receipt = service(session).receive(delivery(value))

    assert receipt.rejection_code == "style_image_set_mismatch"


def test_missing_authorization_is_rejected(session: Session) -> None:
    value = eligibility(
        transform_authorization_id=None,
        transform_authorization_status="none",
        transform_authorization_scope=None,
        transform_authorization_image_set_sha256=None,
        transform_authorization_expires_at=None,
    )

    receipt = service(session).receive(delivery(value))

    assert receipt.rejection_code == "style_transform_authorization_required"
