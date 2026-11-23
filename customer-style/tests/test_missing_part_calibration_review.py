from __future__ import annotations

from io import BytesIO
from pathlib import Path
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from opc_style.api.routes_missing_part_completion import router
from opc_style.domain.errors import DomainRuleError
from opc_style.services.missing_part_calibration_batch import build_calibration_batch
from opc_style.services.missing_part_calibration_review import (
    MissingPartCalibrationReviewStore,
    calibration_review_triage,
)
from opc_style.services.missing_part_learning import MissingPartLearningStore

CUSTOMER = UUID("00000000-0000-4000-8000-0000000000b2")


def _batch(tmp_path: Path) -> Path:
    source_dir = tmp_path / "sources"
    foreground_root = tmp_path / "foreground"
    output_dir = tmp_path / "batch"
    source_dir.mkdir()
    (foreground_root / "bracelet-001").mkdir(parents=True)
    source = Image.new("RGB", (256, 256), (225, 225, 225))
    ImageDraw.Draw(source).ellipse((60, 60, 196, 196), outline=(180, 30, 30), width=18)
    source.save(source_dir / "bracelet-001.jpg", quality=95)
    foreground = Image.new("L", source.size, 0)
    ImageDraw.Draw(foreground).ellipse((60, 60, 196, 196), outline=255, width=18)
    foreground.save(foreground_root / "bracelet-001" / "mask.png")
    build_calibration_batch(
        source_dir=source_dir,
        foreground_root=foreground_root,
        output_dir=output_dir,
        category="bracelet",
        part_kind="bead_edge",
    )
    return output_dir


def test_review_store_keeps_confirmed_and_invalid_decisions_separate(
    tmp_path: Path,
) -> None:
    store = MissingPartCalibrationReviewStore(tmp_path / "review")
    imported = store.import_batch(_batch(tmp_path))
    assert imported["total"] == 2
    assert imported["pending"] == 2
    assert imported["priority_counts"]
    first, second = imported["items"]
    assert isinstance(first, dict) and isinstance(second, dict)
    source, mime = store.asset(str(first["proposal_id"]), "source")
    assert source
    assert mime == "image/jpeg"
    zoom, zoom_mime = store.asset(str(first["proposal_id"]), "zoom")
    assert zoom
    assert zoom_mime == "image/jpeg"
    with Image.open(BytesIO(zoom)) as image:
        assert max(image.size) <= 1200

    store.record_review(
        proposal_id=str(first["proposal_id"]),
        decision="confirmed_structure",
        reviewer_id=CUSTOMER,
        note="visible bead edge",
    )
    store.record_review(
        proposal_id=str(second["proposal_id"]),
        decision="invalid_position",
        reviewer_id=CUSTOMER,
        note="red area is too far away",
    )
    reviewed = store.queue()
    assert reviewed["pending"] == 0
    assert reviewed["reviewed"] == 2
    assert reviewed["training_eligible"] == 1
    assert reviewed["invalid_position"] == 1
    with pytest.raises(DomainRuleError, match="already_reviewed"):
        store.record_review(
            proposal_id=str(first["proposal_id"]),
            decision="confirmed_background",
            reviewer_id=CUSTOMER,
            note="attempted overwrite",
        )


def test_triage_prioritizes_near_threshold_without_claiming_human_truth() -> None:
    triage = calibration_review_triage(
        {
            "proposal_kind": "structure_edge",
            "system_state": "anchored_to_visible_structure",
            "context_edge_mean": 8.08,
            "strong_edge_fraction": 0.0404,
            "context_chroma_mean": 0.0,
            "strong_chroma_fraction": 0.0,
            "context_chroma_stddev": 0.0,
        }
    )
    assert triage["review_priority"] == "critical_threshold"
    assert triage["provisional_suggestion"] == "confirmed_structure"
    assert triage["suggestion_training_eligible"] is False


def test_agent_review_is_traceable_but_never_becomes_training_truth(
    tmp_path: Path,
) -> None:
    store = MissingPartCalibrationReviewStore(tmp_path / "review")
    imported = store.import_batch(_batch(tmp_path))
    item = imported["items"][0]
    assert isinstance(item, dict)

    event = store.record_agent_review(
        proposal_id=str(item["proposal_id"]),
        suggestion="confirmed_structure",
        confidence="medium",
        reviewer_version="gpt-5.6-sol-visual-review-v1",
        reason="The marked area touches a continuous visible bead edge.",
    )
    assert event["training_eligible"] is False
    assert event["provenance"] == "agent_visual_review"

    queue = store.queue()
    reviewed_item = next(
        value for value in queue["items"] if value["proposal_id"] == item["proposal_id"]
    )
    assert queue["agent_reviewed"] == 1
    assert queue["reviewed"] == 0
    assert queue["training_eligible"] == 0
    assert reviewed_item["human_label"] is None
    assert reviewed_item["agent_suggestion"] == "confirmed_structure"
    assert reviewed_item["agent_training_eligible"] is False
    with pytest.raises(DomainRuleError, match="already_recorded"):
        store.record_agent_review(
            proposal_id=str(item["proposal_id"]),
            suggestion="uncertain",
            confidence="low",
            reviewer_version="gpt-5.6-sol-visual-review-v1",
            reason="Attempted overwrite.",
        )


def test_review_api_copies_only_confirmed_labels_to_learning_store(
    tmp_path: Path,
) -> None:
    review = MissingPartCalibrationReviewStore(tmp_path / "review")
    queue = review.import_batch(_batch(tmp_path))
    items = queue["items"]
    assert isinstance(items, list)
    first, second = items
    assert isinstance(first, dict) and isinstance(second, dict)
    learning = MissingPartLearningStore(tmp_path / "learning")
    app = FastAPI()
    app.state.missing_part_calibration_review_store = review
    app.state.missing_part_learning_store = learning
    app.include_router(router)
    client = TestClient(app)

    page = client.get("/local/style-suite/api/missing-part-completion/page")
    assert page.status_code == 200
    assert "逐张审核候选" in page.text
    assert "确认断口" in page.text
    assert "确认背景" in page.text
    assert "位置不合格" in page.text
    assert "上一张" in page.text
    assert "下一张" in page.text
    assert "只看临界样本" in page.text
    assert "全部待审核" in page.text
    assert "新旧规则离线对比" in page.text
    assert "不计入训练，不算人工确认" in page.text

    agent_review = client.post(
        "/local/style-suite/api/missing-part-completion/calibration/agent-reviews",
        data={
            "proposal_id": first["proposal_id"],
            "suggestion": "confirmed_structure",
            "confidence": "medium",
            "reviewer_version": "gpt-5.6-sol-visual-review-v1",
            "reason": "Marked region touches the visible bead edge.",
        },
    )
    assert agent_review.status_code == 200
    assert agent_review.json()["training_eligible"] is False
    assert agent_review.json()["human_label"] is None
    assert learning.preflight_event_count == 0

    asset = client.get(
        "/local/style-suite/api/missing-part-completion/"
        f"calibration/assets/{first['proposal_id']}/overlay"
    )
    assert asset.status_code == 200
    assert asset.headers["cache-control"] == "no-store"

    confirmed = client.post(
        "/local/style-suite/api/missing-part-completion/calibration/reviews",
        headers={"X-Customer-Id": str(CUSTOMER)},
        data={
            "proposal_id": first["proposal_id"],
            "decision": "confirmed_structure",
            "note": "visible bead edge",
        },
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["training_eligible"] is True
    assert learning.preflight_event_count == 1

    duplicate = client.post(
        "/local/style-suite/api/missing-part-completion/calibration/reviews",
        headers={"X-Customer-Id": str(CUSTOMER)},
        data={
            "proposal_id": first["proposal_id"],
            "decision": "confirmed_background",
        },
    )
    assert duplicate.status_code == 422
    assert duplicate.json()["detail"]["code"] == (
        "missing_part_calibration_already_reviewed"
    )
    assert learning.preflight_event_count == 1

    invalid = client.post(
        "/local/style-suite/api/missing-part-completion/calibration/reviews",
        headers={"X-Customer-Id": str(CUSTOMER)},
        data={
            "proposal_id": second["proposal_id"],
            "decision": "invalid_position",
            "note": "proposal misses the object",
        },
    )
    assert invalid.status_code == 200
    assert invalid.json()["training_eligible"] is False
    assert invalid.json()["learning_event_id"] is None
    assert learning.preflight_event_count == 1

    final_queue = client.get(
        "/local/style-suite/api/missing-part-completion/calibration/review-queue"
    )
    assert final_queue.status_code == 200
    assert final_queue.json()["pending"] == 0
    assert final_queue.json()["provider_call_count"] == 0
    assert final_queue.json()["threshold_updated_automatically"] is False

    comparison = client.get(
        "/local/style-suite/api/missing-part-completion/preflight/offline-comparison",
        params={"category": "bracelet", "part_kind": "bead_edge"},
    )
    assert comparison.status_code == 200
    assert comparison.json()["ready_for_offline_comparison"] is False
    assert comparison.json()["live_thresholds_changed"] is False
