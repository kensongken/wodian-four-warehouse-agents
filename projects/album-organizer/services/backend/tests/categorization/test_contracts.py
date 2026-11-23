from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import pytest
from pydantic import ValidationError

from photo_organizer.categorization.contracts import ConfirmedSnapshotBatch
from photo_organizer.domain.limits import MAX_TASK_ACCEPTED_ASSETS

FIXTURE_PATH = (
    Path(__file__).resolve().parents[4]
    / "contracts/fixtures/first-10-sku-categorization-events.json"
)


def _payload() -> dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def test_first_ten_sku_fixture_parses_at_stage_2_boundary() -> None:
    batch = ConfirmedSnapshotBatch.model_validate(_payload())

    assert len(batch.events) == 10
    assert len({event.album_product_id for event in batch.events}) == 10
    assert all(event.mall_product_id is None for event in batch.events)
    assert all(event.bead_id is None for event in batch.events)
    assert all(
        any(image.role == "original" for image in event.images)
        for event in batch.events
    )


def test_confirmed_snapshot_batch_accepts_ten_thousand_events() -> None:
    """A 3,000-event batch limit would reject a task that is valid at the shared cap."""
    source_event = ConfirmedSnapshotBatch.model_validate(_payload()).events[0]
    events = tuple(
        source_event.model_copy(
            update={
                "event_id": uuid5(NAMESPACE_URL, f"capacity-event-{index}"),
                "album_product_id": f"capacity-product-{index}",
            }
        )
        for index in range(MAX_TASK_ACCEPTED_ASSETS)
    )

    batch = ConfirmedSnapshotBatch(
        schema_version="album-product-categorization/1.0",
        batch_id=uuid5(NAMESPACE_URL, "capacity-batch"),
        generated_at=source_event.occurred_at,
        data_classification="synthetic_no_customer_data",
        events=events,
    )

    assert len(batch.events) == MAX_TASK_ACCEPTED_ASSETS


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload["events"][0]["human_confirmation"].update(
            status="pending"
        ),
        lambda payload: payload["events"][0]["images"][0].update(
            storage_ref="evidence/synthetic-opc-001/../escape.jpg"
        ),
        lambda payload: payload["events"][0].update(bead_id="bead-auto-created"),
        lambda payload: payload["events"][0].update(
            mall_product_id="mall-auto-created"
        ),
        lambda payload: payload["events"][0]["categorization_input"].update(
            level_1_candidate="真品南红"
        ),
        lambda payload: payload["events"][0].update(
            images=[
                image
                for image in payload["events"][0]["images"]
                if image["role"] != "original"
            ]
        ),
    ],
    ids=[
        "not_confirmed",
        "storage_traversal",
        "automatic_bead_id",
        "automatic_mall_product_id",
        "category_outside_locked_vocabulary",
        "missing_original",
    ],
)
def test_unsafe_snapshot_is_rejected_before_engine_call(mutate: Any) -> None:
    payload = copy.deepcopy(_payload())
    mutate(payload)

    with pytest.raises(ValidationError):
        ConfirmedSnapshotBatch.model_validate(payload)
