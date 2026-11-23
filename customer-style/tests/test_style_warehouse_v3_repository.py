from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from test_style_warehouse_v3_contract import delivery, eligibility

from opc_style.models import (
    Base,
    StyleWarehouseV3Attempt,
    StyleWarehouseV3Receipt,
)
from opc_style.repositories_style_warehouse import (
    StyleWarehouseV3Conflict,
    StyleWarehouseV3Repository,
)


def test_first_delivery_and_exact_replay_share_one_receipt(session: Session) -> None:
    repository = StyleWarehouseV3Repository(session)

    first = repository.accept_or_replay(
        delivery(), decision="accepted", rejection_code=None
    )
    session.commit()
    replay = repository.accept_or_replay(
        delivery(), decision="accepted", rejection_code=None
    )

    assert first.disposition == "accepted"
    assert replay.disposition == "duplicate"
    assert replay.decision_receipt_id == first.decision_receipt_id
    assert session.scalar(select(func.count()).select_from(StyleWarehouseV3Receipt)) == 1


def test_same_key_with_changed_digest_is_a_conflict(session: Session) -> None:
    repository = StyleWarehouseV3Repository(session)
    repository.accept_or_replay(delivery(), decision="accepted", rejection_code=None)
    session.commit()
    changed = eligibility(
        material={
            "value": "changed-synthetic-material",
            "claim_basis": "merchant_declared",
            "evidence_record_id": "55555555-5555-4555-8555-555555555555",
            "evidence_content_sha256": "d" * 64,
        }
    )

    with pytest.raises(StyleWarehouseV3Conflict, match="style_handoff_conflict"):
        repository.accept_or_replay(
            delivery(changed), decision="accepted", rejection_code=None
        )


def test_receipt_lookup_and_sanitized_attempt(session: Session) -> None:
    repository = StyleWarehouseV3Repository(session)
    first = repository.accept_or_replay(
        delivery(), decision="accepted", rejection_code=None
    )
    repository.record_attempt(
        tenant_id=delivery().payload.tenant_id,
        handoff_id=delivery().handoff_id,
        payload_sha256=delivery().payload_sha256,
        result="accepted",
        error_code=None,
    )
    session.commit()

    restored = repository.get_receipt(
        delivery().payload.tenant_id, first.decision_receipt_id
    )
    hidden = repository.get_receipt(
        eligibility(tenant_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa").tenant_id,
        first.decision_receipt_id,
    )
    attempt = session.scalar(select(StyleWarehouseV3Attempt))

    assert restored == first
    assert hidden is None
    assert attempt is not None
    assert attempt.result == "accepted"
    assert not hasattr(attempt, "request_body")
    assert not hasattr(attempt, "signing_secret")


def test_concurrent_identical_delivery_creates_one_receipt(tmp_path: Path) -> None:
    engine = create_engine(
        f"sqlite:///{tmp_path / 'concurrent.db'}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    Base.metadata.create_all(engine)

    def receive() -> tuple[str, str]:
        with Session(engine) as local_session:
            receipt = StyleWarehouseV3Repository(local_session).accept_or_replay(
                delivery(), decision="accepted", rejection_code=None
            )
            local_session.commit()
            return str(receipt.decision_receipt_id), receipt.disposition

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _: receive(), range(2)))

    with Session(engine) as local_session:
        count = local_session.scalar(
            select(func.count()).select_from(StyleWarehouseV3Receipt)
        )
    engine.dispose()

    assert count == 1
    assert len({receipt_id for receipt_id, _ in outcomes}) == 1
    assert {disposition for _, disposition in outcomes} == {"accepted", "duplicate"}
