from __future__ import annotations

import asyncio
import json
import zipfile
from decimal import Decimal
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

from fastapi import FastAPI, UploadFile
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from opc_style.api.routes_missing_part_completion import _completion_request, router
from opc_style.domain.missing_part_completion import compose_missing_part_candidate
from opc_style.services.missing_part_completion import (
    MissingPartCompletionService,
    PrivateMissingPartCandidateStore,
)
from opc_style.services.missing_part_learning import MissingPartLearningStore
from opc_style.services.missing_part_reference_assets import (
    MissingPartReferenceInboxStore,
    local_reference_assets,
)

CUSTOMER = UUID("00000000-0000-4000-8000-0000000000a1")


def _png(mode: str, color: object) -> bytes:
    image = Image.new(mode, (64, 64), color)
    output = BytesIO()
    image.save(output, format="PNG", optimize=False)
    return output.getvalue()


def _mask() -> bytes:
    image = Image.new("L", (64, 64), 0)
    ImageDraw.Draw(image).rectangle((8, 8, 16, 16), fill=255)
    output = BytesIO()
    image.save(output, format="PNG", optimize=False)
    return output.getvalue()


def _source() -> bytes:
    image = Image.new("RGB", (64, 64), (20, 30, 40))
    draw = ImageDraw.Draw(image)
    draw.line((0, 12, 7, 12), fill=(210, 170, 40), width=3)
    draw.line((17, 12, 26, 12), fill=(210, 170, 40), width=3)
    output = BytesIO()
    image.save(output, format="PNG", optimize=False)
    return output.getvalue()


def _patterned_view(direction: str) -> bytes:
    image = Image.new("RGB", (64, 64), (240, 240, 240))
    draw = ImageDraw.Draw(image)
    if direction == "vertical":
        draw.rectangle((8, 4, 24, 59), fill=(20, 20, 20))
    else:
        draw.rectangle((4, 8, 59, 24), fill=(20, 20, 20))
    output = BytesIO()
    image.save(output, format="PNG", optimize=False)
    return output.getvalue()


def _reference_batch_zip(files: dict[str, bytes]) -> bytes:
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return output.getvalue()


class _Completer:
    model_version = "fixture-missing-part-v1"

    def complete(self, request, *, learning_lessons=()):  # type: ignore[no-untyped-def]
        return compose_missing_part_candidate(
            request,
            generated_content=_png("RGB", (220, 180, 30)),
            model_version=self.model_version,
        )


def test_reference_batch_stages_named_images_without_generation(tmp_path: Path) -> None:
    app = FastAPI()
    store = MissingPartReferenceInboxStore((tmp_path / "reference-inbox").resolve())
    app.state.missing_part_reference_inbox_store = store
    app.include_router(router)
    client = TestClient(app)
    batch = _reference_batch_zip(
        {
            "returned/round137-boundary-01__reference.png": _png("RGB", (10, 80, 120)),
            "returned/round137-boundary-02__reference.png": _png("RGB", (120, 80, 10)),
        }
    )
    missing_confirmation = client.post(
        "/local/style-suite/api/missing-part-completion/reference-inbox-batch",
        files={"reference_batch": ("returned.zip", batch, "application/zip")},
    )
    assert missing_confirmation.status_code == 422
    assert missing_confirmation.json()["detail"]["code"] == (
        "missing_part_reference_confirmation_required"
    )
    staged = client.post(
        "/local/style-suite/api/missing-part-completion/reference-inbox-batch",
        files={"reference_batch": ("returned.zip", batch, "application/zip")},
        data={
            "same_product_mapping_confirmed": "true",
            "structure_visible_confirmed": "true",
        },
    )
    assert staged.status_code == 200
    assert staged.json()["submitted_count"] == 2
    assert staged.json()["target_count"] == 2
    assert staged.json()["review_state"] == "pending_human_visual_review"
    assert staged.json()["automatic_generation_allowed"] is False
    assert staged.json()["provider_calls"] == 0
    summary = client.get(
        "/local/style-suite/api/missing-part-completion/reference-inbox"
    ).json()
    assert summary["reference_count"] == 2
    assert summary["pending_count"] == 2
    assert summary["approved_count"] == 0
    assert summary["provider_calls"] == 0
    records = staged.json()["records"]
    invalid_review = [
        {
            "target_id": records[0]["target_id"],
            "reference_sha256": records[0]["reference_sha256"],
            "decision": "approved",
            "note": "first looks correct",
        },
        {
            "target_id": records[1]["target_id"],
            "reference_sha256": "0" * 64,
            "decision": "rejected",
            "note": "invalid record",
        },
    ]
    rejected_batch_review = client.post(
        "/local/style-suite/api/missing-part-completion/reference-inbox-review-batch",
        data={"decisions_json": json.dumps(invalid_review)},
    )
    assert rejected_batch_review.status_code == 422
    assert store.summary()["pending_count"] == 2
    valid_review = [
        {
            "target_id": records[0]["target_id"],
            "reference_sha256": records[0]["reference_sha256"],
            "decision": "approved",
            "note": "same product and visible structure",
        },
        {
            "target_id": records[1]["target_id"],
            "reference_sha256": records[1]["reference_sha256"],
            "decision": "rejected",
            "note": "not the same physical item",
        },
    ]
    saved_batch_review = client.post(
        "/local/style-suite/api/missing-part-completion/reference-inbox-review-batch",
        data={"decisions_json": json.dumps(valid_review)},
    )
    assert saved_batch_review.status_code == 200
    assert saved_batch_review.json()["reviewed_count"] == 2
    assert saved_batch_review.json()["approved_count"] == 1
    assert saved_batch_review.json()["rejected_count"] == 1
    assert saved_batch_review.json()["provider_calls"] == 0
    reviewed_summary = store.summary()
    assert reviewed_summary["pending_count"] == 0
    assert reviewed_summary["approved_count"] == 1
    assert reviewed_summary["rejected_count"] == 1


def test_reference_batch_rejects_missing_target_and_duplicate_bytes(
    tmp_path: Path,
) -> None:
    app = FastAPI()
    store = MissingPartReferenceInboxStore((tmp_path / "reference-inbox").resolve())
    app.state.missing_part_reference_inbox_store = store
    app.include_router(router)
    client = TestClient(app)
    data = {
        "same_product_mapping_confirmed": "true",
        "structure_visible_confirmed": "true",
    }
    missing_target = _reference_batch_zip(
        {"returned/unknown-reference.png": _png("RGB", (10, 20, 30))}
    )
    response = client.post(
        "/local/style-suite/api/missing-part-completion/reference-inbox-batch",
        files={
            "reference_batch": ("missing-target.zip", missing_target, "application/zip")
        },
        data=data,
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == (
        "missing_part_reference_batch_target_missing"
    )
    partially_invalid = _reference_batch_zip(
        {
            "round137-boundary-01__reference.png": _png("RGB", (70, 80, 90)),
            "round137-boundary-02__reference.png": b"not-an-image",
        }
    )
    response = client.post(
        "/local/style-suite/api/missing-part-completion/reference-inbox-batch",
        files={
            "reference_batch": (
                "partially-invalid.zip",
                partially_invalid,
                "application/zip",
            )
        },
        data=data,
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "missing_part_reference_invalid"
    assert store.summary()["reference_count"] == 0
    repeated = _png("RGB", (40, 50, 60))
    duplicate = _reference_batch_zip(
        {
            "round137-boundary-01__reference.png": repeated,
            "round137-boundary-02__reference.png": repeated,
        }
    )
    response = client.post(
        "/local/style-suite/api/missing-part-completion/reference-inbox-batch",
        files={"reference_batch": ("duplicate.zip", duplicate, "application/zip")},
        data=data,
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "missing_part_reference_batch_duplicate"
    assert store.summary()["reference_count"] == 0


def test_preview_execute_content_feedback_learning_loop(
    tmp_path: Path, monkeypatch
) -> None:
    learning = MissingPartLearningStore(tmp_path / "learning")
    candidates = PrivateMissingPartCandidateStore(tmp_path / "candidates")
    service = MissingPartCompletionService(
        learning_store=learning,
        candidate_store=candidates,
        completer=_Completer(),
        model_version=_Completer.model_version,
        maximum_usd=Decimal("0.12"),
    )
    app = FastAPI()
    app.state.missing_part_completion_service = service
    app.state.missing_part_learning_store = learning
    app.state.missing_part_candidate_store = candidates
    reference_inbox_store = MissingPartReferenceInboxStore(
        (tmp_path / "reference-inbox").resolve()
    )
    app.state.missing_part_reference_inbox_store = reference_inbox_store
    priority_content_fixture = _png("RGB", (35, 70, 105))
    priority_digest = sha256(priority_content_fixture).hexdigest()
    priority_candidate_fixture = SimpleNamespace(
        target_id="round137-boundary-12",
        target_sha256=(
            "f731d99bfd7f794f0090b6319f0cede5089b2486def84e82ca9057210d640d06"
        ),
        source_filename="r137-12-f731d99bfd7f.jpg",
        reference_sha256=priority_digest,
        reference_filename="private-priority-reference.png",
        machine_visual_triage=(
            "plausible_same_design_requires_owner_identity_confirmation"
        ),
        feature_distance=0.5253679,
    )
    monkeypatch.setattr(
        "opc_style.api.routes_missing_part_completion.priority_reference_candidates",
        lambda: (priority_candidate_fixture,),
    )
    monkeypatch.setattr(
        "opc_style.api.routes_missing_part_completion.get_priority_reference_candidate",
        lambda target_id, reference_sha256: (
            priority_content_fixture,
            "image/png",
            priority_candidate_fixture,
        ),
    )
    app.include_router(router)
    client = TestClient(app)
    page = client.get("/local/style-suite/api/missing-part-completion/page")
    assert page.status_code == 200
    assert "珠宝缺失部件补全" in page.text
    assert "人工确认前不可发布" in page.text
    assert "确实是商品断口" in page.text
    assert "确实是背景" in page.text
    assert "误放行" in page.text
    assert "不会触发付费" in page.text
    assert "批量复核队列" in page.text
    assert "没有保留原图或涂抹图" in page.text
    assert "上传 1 张同商品干净参考图" in page.text
    assert "复用现有素材：选择 2–8 张同商品多角度图" in page.text
    assert "正在本机已索引素材中查找候选" in page.text
    assert "整批真实依据" in page.text
    assert "reference_same_product_confirmed" in page.text
    assert "只需优先核对" in page.text
    assert "reference-priority-candidates" in page.text
    assert ".join('\\n')" in page.text

    no_match = client.get(
        "/local/style-suite/api/missing-part-completion/reference-suggestions",
        params={"source_sha256": "0" * 64},
    )
    assert no_match.status_code == 200
    assert no_match.json()["matched_known_source"] is False

    readiness = client.get(
        "/local/style-suite/api/missing-part-completion/reference-readiness"
    )
    assert readiness.status_code == 200
    assert readiness.json() == {
        "state": "deferred_pending_clean_reference",
        "target_count": 10,
        "ready_for_completion": 0,
        "same_occlusion_deferred": 2,
        "no_exact_reference_deferred": 8,
        "deferred_total": 10,
        "automatic_generation_allowed": False,
        "provider_calls": 0,
        "expanded_search_target_count": 10,
        "expanded_unique_references_reviewed": 3241,
        "expanded_clean_exact_reference_found": False,
        "expanded_priority_human_review_count": 1,
    }

    gap_manifest = client.get(
        "/local/style-suite/api/missing-part-completion/reference-gap-manifest"
    )
    assert gap_manifest.status_code == 200
    manifest_payload = gap_manifest.json()
    assert manifest_payload["target_count"] == 10
    assert manifest_payload["automatic_generation_allowed"] is False
    assert manifest_payload["provider_calls"] == 0
    assert len(manifest_payload["items"]) == 10
    assert {item["status"] for item in manifest_payload["items"]} == {
        "same_occlusion_deferred",
        "no_exact_reference_deferred",
    }
    gap_csv = client.get(
        "/local/style-suite/api/missing-part-completion/reference-gap-manifest.csv"
    )
    assert gap_csv.status_code == 200
    assert gap_csv.headers["content-type"].startswith("text/csv")
    assert gap_csv.headers["content-disposition"] == (
        'attachment; filename="missing-part-reference-gap-list.csv"'
    )
    assert gap_csv.content.startswith(b"\xef\xbb\xbf")
    csv_text = gap_csv.content.decode("utf-8-sig")
    assert "目标图片,当前状态" in csv_text
    assert "r137-02-4f450f73e3eb.jpg,同款候选仍遮挡" in csv_text
    assert csv_text.count("\n") == 11
    gap_package = client.get(
        "/local/style-suite/api/missing-part-completion/reference-gap-package.zip"
    )
    assert gap_package.status_code == 200
    assert gap_package.headers["content-type"] == "application/zip"
    assert gap_package.headers["content-disposition"] == (
        'attachment; filename="missing-part-reference-capture-package.zip"'
    )
    with zipfile.ZipFile(BytesIO(gap_package.content)) as archive:
        names = archive.namelist()
        assert "补拍找图清单.csv" in names
        source_names = [name for name in names if name.startswith("待补原图/")]
        assert len(source_names) == 10
        assert all(archive.read(name) for name in source_names)
    delivery_gate = client.get(
        "/local/style-suite/api/missing-part-completion/delivery-gate"
    )
    assert delivery_gate.status_code == 200
    gate_payload = delivery_gate.json()
    assert gate_payload["state"] == "blocked_by_real_evidence"
    assert gate_payload["workflow_available"] is True
    assert gate_payload["target_count"] == 10
    assert gate_payload["ready_for_completion"] == 0
    assert gate_payload["deferred_total"] == 10
    assert gate_payload["business_evidence_complete"] is False
    assert gate_payload["production_release_allowed"] is False
    assert gate_payload["automatic_generation_allowed"] is False
    assert gate_payload["provider_calls"] == 0

    priority = client.get(
        "/local/style-suite/api/missing-part-completion/reference-priority-candidates"
    )
    assert priority.status_code == 200
    priority_payload = priority.json()
    assert priority_payload["candidate_count"] == 1
    priority_candidate = priority_payload["candidates"][0]
    assert priority_candidate["target_id"] == "round137-boundary-12"
    assert priority_candidate["same_physical_product_confirmed"] is False
    assert priority_candidate["clean_missing_structure_confirmed"] is False
    priority_content = client.get(priority_candidate["reference_content_url"])
    assert priority_content.status_code == 200
    missing_priority_confirmation = client.post(
        priority_candidate["reference_content_url"] + "/approve"
    )
    assert missing_priority_confirmation.status_code == 422
    assert missing_priority_confirmation.json()["detail"]["code"] == (
        "missing_part_reference_confirmation_required"
    )
    approved_priority = client.post(
        priority_candidate["reference_content_url"] + "/approve",
        data={"identity_and_structure_confirmed": "true"},
    )
    assert approved_priority.status_code == 200
    assert approved_priority.json()["review_state"] == ("approved_for_completion_input")
    assert approved_priority.json()["review_method"] == (
        "priority_candidate_side_by_side"
    )
    assert approved_priority.json()["provider_calls"] == 0
    priority_after_review = client.get(
        "/local/style-suite/api/missing-part-completion/reference-priority-candidates"
    )
    assert priority_after_review.status_code == 200
    assert priority_after_review.json()["candidate_count"] == 0

    inbox_before = client.get(
        "/local/style-suite/api/missing-part-completion/reference-inbox"
    )
    assert inbox_before.status_code == 200
    assert inbox_before.json()["reference_count"] == 1
    assert inbox_before.json()["approved_count"] == 1
    staged = client.post(
        "/local/style-suite/api/missing-part-completion/reference-inbox/round137-boundary-01",
        files={
            "reference": ("clean-angle.png", _png("RGB", (90, 70, 30)), "image/png")
        },
        data={
            "same_product_confirmed": "true",
            "structure_visible_confirmed": "true",
        },
    )
    assert staged.status_code == 200
    assert staged.json()["review_state"] == "pending_human_visual_review"
    assert staged.json()["automatic_generation_allowed"] is False
    assert staged.json()["provider_calls"] == 0
    target_id = "round137-boundary-01"
    reference_sha256 = staged.json()["reference_sha256"]
    source_asset = client.get(
        f"/local/style-suite/api/missing-part-completion/reference-inbox/{target_id}/source"
    )
    assert source_asset.status_code == 200
    assert source_asset.headers["content-type"] == "image/jpeg"
    reference_asset = client.get(
        "/local/style-suite/api/missing-part-completion/reference-inbox/"
        f"{target_id}/references/{reference_sha256}"
    )
    assert reference_asset.status_code == 200
    assert reference_asset.content == _png("RGB", (90, 70, 30))
    reviewed = client.post(
        "/local/style-suite/api/missing-part-completion/reference-inbox/"
        f"{target_id}/references/{reference_sha256}/review",
        data={"decision": "approved", "note": "同一商品且缺失位置清楚"},
    )
    assert reviewed.status_code == 200
    assert reviewed.json()["review_state"] == "approved_for_completion_input"
    assert reviewed.json()["automatic_generation_allowed"] is False
    inbox_after = client.get(
        "/local/style-suite/api/missing-part-completion/reference-inbox"
    ).json()
    assert inbox_after["approved_count"] == 2
    assert inbox_after["pending_count"] == 0
    approved_suggestions = client.get(
        "/local/style-suite/api/missing-part-completion/reference-suggestions",
        params={
            "source_sha256": "d9e11fc705f1db5d651d48c2434838d46e0c77743c192bd4780236c67f807364"
        },
    )
    assert approved_suggestions.status_code == 200
    approved_payload = approved_suggestions.json()
    assert approved_payload["matched_known_source"] is True
    assert approved_payload["structure_evidence_available"] is True
    assert len(approved_payload["candidates"]) == 1
    assert approved_payload["candidates"][0]["source_kind"] == (
        "human_approved_inbox_reference"
    )
    assert (
        approved_payload["candidates"][0]["eligible_for_missing_part_completion"]
        is True
    )
    readiness_after_approval = client.get(
        "/local/style-suite/api/missing-part-completion/reference-readiness"
    ).json()
    assert readiness_after_approval["ready_for_completion"] == 2
    assert readiness_after_approval["no_exact_reference_deferred"] == 6
    assert readiness_after_approval["deferred_total"] == 8
    assert readiness_after_approval["approved_reference_target_count"] == 2
    assert readiness_after_approval["automatic_generation_allowed"] is False
    gap_after_approval = client.get(
        "/local/style-suite/api/missing-part-completion/reference-gap-manifest"
    )
    assert gap_after_approval.status_code == 200
    assert gap_after_approval.json()["total_target_count"] == 10
    assert gap_after_approval.json()["target_count"] == 8
    assert gap_after_approval.json()["resolved_target_count"] == 2
    assert all(
        item["target_id"] != "round137-boundary-01"
        for item in gap_after_approval.json()["items"]
    )
    request_with_approved_reference = asyncio.run(
        _completion_request(
            reference_inbox_store=reference_inbox_store,
            source=UploadFile(
                filename="source.jpg",
                file=BytesIO(source_asset.content),
                headers={"content-type": "image/jpeg"},
            ),
            missing_mask=UploadFile(
                filename="mask.png",
                file=BytesIO(_mask()),
                headers={"content-type": "image/png"},
            ),
            category="bracelet",
            part_kind="bead_edge",
            material_tags="stone",
            case_id="approved-inbox-reference",
            scene_tags="hand_held",
            evidence_kind="clean_same_product_reference",
            reference_asset_ids=approved_payload["candidates"][0]["asset_id"],
            reference_same_product_confirmed=True,
            reference_structure_confirmed=True,
        )
    )
    assert request_with_approved_reference.reference_content == reference_asset.content
    assert request_with_approved_reference.reference_mime_type == "image/png"

    stale_external_target_sha256 = (
        "4f450f73e3ebfea089c21dd72fc7a75d6ca1089f707e95559fb7ff53ddeac8e8"
    )
    assert local_reference_assets() == ()
    suggestions = client.get(
        "/local/style-suite/api/missing-part-completion/reference-suggestions",
        params={"source_sha256": stale_external_target_sha256},
    )
    assert suggestions.status_code == 200
    payload = suggestions.json()
    assert payload["matched_known_source"] is False
    assert payload["candidates"] == []
    assert payload["recommended_asset_ids"] == []
    assert payload["recommended_pair_visual_distance"] is None
    assert payload["recommendation_only"] is True
    assert payload["automatic_identity_claim"] is False
    assert payload["automatic_structure_claim"] is False
    assert payload["structure_evidence_available"] is False
    assert payload["recommended_action"] == "review_candidates"
    assert payload["sequence_audit"]["reviewed_frame_count"] == 60
    assert payload["sequence_audit"]["missing_part_structure_revealed"] is False
    source = _source()
    form = {
        "category": "earring",
        "part_kind": "ear_hook",
        "material_tags": "gold,reflective",
        "case_id": "api-case-1",
    }
    files = {
        "source": ("source.png", source, "image/png"),
        "missing_mask": ("mask.png", _mask(), "image/png"),
    }
    preview = client.post(
        "/local/style-suite/api/missing-part-completion/preview",
        data=form,
        files=files,
    )
    assert preview.status_code == 200
    quote = preview.json()
    assert quote["provider_enabled"] is True
    assert quote["publishable"] is False
    assert quote["similar_lesson_count"] == 0
    assert quote["quality"] == "high"
    assert quote["candidate_count"] == 1
    assert quote["automatic_fallback"] is False
    assert quote["preflight_state"] == "anchored_to_visible_structure"
    assert quote["context_edge_mean"] >= 8

    execution = client.post(
        "/local/style-suite/api/missing-part-completion/execute",
        headers={"X-Customer-Id": str(CUSTOMER)},
        data={
            **form,
            "confirmed_request_fingerprint": quote["request_fingerprint"],
            "confirmed_maximum_usd": quote["maximum_usd"],
            "idempotency_key": "00000000-0000-4000-8000-000000000001",
        },
        files=files,
    )
    assert execution.status_code == 200
    result = execution.json()
    assert result["requires_human_review"] is True
    assert result["review_state"] == "human_review_required"
    assert result["automatic_approval"] is False
    assert "耳钩" in result["structural_review_checklist"]
    candidate = client.get(
        result["content_url"], headers={"X-Customer-Id": str(CUSTOMER)}
    )
    assert candidate.status_code == 200
    assert candidate.headers["cache-control"] == "no-store"

    feedback = client.post(
        "/local/style-suite/api/missing-part-completion/feedback",
        headers={"X-Customer-Id": str(CUSTOMER)},
        data={
            "request_fingerprint": result["request_fingerprint"],
            "candidate_sha256": result["candidate_sha256"],
            "category": "earring",
            "part_kind": "ear_hook",
            "material_tags": "gold,reflective",
            "decision": "accepted",
            "model_version": result["model_version"],
            "note": "ear hook curve matches the visible original",
        },
    )
    assert feedback.status_code == 200
    assert feedback.json()["fine_tuned"] is False

    edited = client.post(
        "/local/style-suite/api/missing-part-completion/feedback",
        headers={"X-Customer-Id": str(CUSTOMER)},
        data={
            "request_fingerprint": result["request_fingerprint"],
            "candidate_sha256": result["candidate_sha256"],
            "category": "earring",
            "part_kind": "ear_hook",
            "material_tags": "gold,reflective",
            "decision": "edited",
            "model_version": result["model_version"],
            "note": "human narrowed the hook curve",
        },
        files={"final_image": ("edited.png", candidate.content, "image/png")},
    )
    assert edited.status_code == 200
    assert edited.json()["training_state"] == "case_memory_ready"

    learned_preview = client.post(
        "/local/style-suite/api/missing-part-completion/preview",
        data={**form, "case_id": "api-case-2"},
        files=files,
    )
    assert learned_preview.status_code == 200
    assert learned_preview.json()["similar_lesson_count"] == 1


def test_preview_blocks_background_only_mask_before_provider_call(
    tmp_path: Path,
) -> None:
    learning = MissingPartLearningStore(tmp_path / "learning")
    service = MissingPartCompletionService(
        learning_store=learning,
        candidate_store=PrivateMissingPartCandidateStore(tmp_path / "candidates"),
        completer=_Completer(),
        model_version=_Completer.model_version,
        maximum_usd=Decimal("0.12"),
    )
    app = FastAPI()
    app.state.missing_part_completion_service = service
    app.state.missing_part_learning_store = learning
    app.include_router(router)
    client = TestClient(app)
    response = client.post(
        "/local/style-suite/api/missing-part-completion/preview",
        data={"category": "earring", "part_kind": "ear_hook"},
        files={
            "source": ("source.png", _png("RGB", (20, 30, 40)), "image/png"),
            "missing_mask": ("mask.png", _mask(), "image/png"),
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "missing_part_mask_unanchored"

    diagnosis = client.post(
        "/local/style-suite/api/missing-part-completion/preflight/diagnose",
        data={"category": "earring", "part_kind": "ear_hook"},
        files={
            "source": ("source.png", _png("RGB", (20, 30, 40)), "image/png"),
            "missing_mask": ("mask.png", _mask(), "image/png"),
        },
    )
    assert diagnosis.status_code == 200
    assert diagnosis.json()["state"] == "blocked_unanchored"
    assert diagnosis.json()["provider_called"] is False

    feedback = client.post(
        "/local/style-suite/api/missing-part-completion/preflight/feedback",
        headers={"X-Customer-Id": str(CUSTOMER)},
        data={
            "category": "earring",
            "part_kind": "ear_hook",
            "human_label": "confirmed_background",
            "note": "mask is on plain background",
        },
        files={
            "source": ("source.png", _png("RGB", (20, 30, 40)), "image/png"),
            "missing_mask": ("mask.png", _mask(), "image/png"),
        },
    )
    assert feedback.status_code == 200
    assert feedback.json()["system_agrees_with_human"] is True
    assert feedback.json()["threshold_updated"] is False
    assert learning.preflight_event_count == 1

    summary = client.get(
        "/local/style-suite/api/missing-part-completion/preflight/summary"
    )
    assert summary.status_code == 200
    assert summary.json() == {
        "total": 1,
        "agreed": 1,
        "false_pass": 0,
        "false_block": 0,
        "confirmed_structure": 0,
        "confirmed_background": 1,
        "threshold_updated_automatically": False,
        "paid_override_enabled": False,
    }

    queue = client.get(
        "/local/style-suite/api/missing-part-completion/preflight/review-queue",
        params={"category": "earring", "part_kind": "ear_hook", "outcome": "agreed"},
    )
    assert queue.status_code == 200
    assert queue.json()["total_cases"] == 1
    assert queue.json()["matching_cases"] == 1
    assert queue.json()["items"][0]["outcome"] == "agreed"
    assert queue.json()["items"][0]["case_ref"] != "api-case-1"
    assert queue.json()["groups"][0]["background_remaining"] == 19
    assert queue.json()["source_preview_available"] is False

    invalid_queue = client.get(
        "/local/style-suite/api/missing-part-completion/preflight/review-queue",
        params={"outcome": "invented"},
    )
    assert invalid_queue.status_code == 422
    assert invalid_queue.json()["detail"]["code"] == (
        "missing_part_review_filter_invalid"
    )


def test_occluded_preview_requires_and_accepts_confirmed_reference(
    tmp_path: Path,
) -> None:
    learning = MissingPartLearningStore(tmp_path / "learning")
    service = MissingPartCompletionService(
        learning_store=learning,
        candidate_store=PrivateMissingPartCandidateStore(tmp_path / "candidates"),
        completer=_Completer(),
        model_version=_Completer.model_version,
        maximum_usd=Decimal("0.12"),
    )
    app = FastAPI()
    app.state.missing_part_completion_service = service
    app.state.missing_part_learning_store = learning
    app.include_router(router)
    client = TestClient(app)
    form = {
        "category": "earring",
        "part_kind": "ear_hook",
        "scene_tags": "hand_held",
        "evidence_kind": "clean_same_product_reference",
        "reference_same_product_confirmed": "true",
        "reference_structure_confirmed": "true",
    }
    base_files = {
        "source": ("source.png", _source(), "image/png"),
        "missing_mask": ("mask.png", _mask(), "image/png"),
    }
    blocked = client.post(
        "/local/style-suite/api/missing-part-completion/preview",
        data=form,
        files=base_files,
    )
    assert blocked.status_code == 422
    assert blocked.json()["detail"]["code"] == "missing_part_evidence_invalid"

    accepted = client.post(
        "/local/style-suite/api/missing-part-completion/preview",
        data=form,
        files={
            **base_files,
            "reference": (
                "same-product.png",
                _png("RGB", (120, 80, 30)),
                "image/png",
            ),
        },
    )
    assert accepted.status_code == 200
    assert accepted.json()["provider_enabled"] is True


def test_occluded_preview_accepts_two_existing_same_product_views(
    tmp_path: Path,
) -> None:
    learning = MissingPartLearningStore(tmp_path / "learning")
    service = MissingPartCompletionService(
        learning_store=learning,
        candidate_store=PrivateMissingPartCandidateStore(tmp_path / "candidates"),
        completer=_Completer(),
        model_version=_Completer.model_version,
        maximum_usd=Decimal("0.12"),
    )
    app = FastAPI()
    app.state.missing_part_completion_service = service
    app.state.missing_part_learning_store = learning
    app.include_router(router)
    client = TestClient(app)
    response = client.post(
        "/local/style-suite/api/missing-part-completion/preview",
        data={
            "category": "earring",
            "part_kind": "ear_hook",
            "scene_tags": "hand_held",
            "evidence_kind": "same_product_multiview_references",
            "reference_same_product_confirmed": "true",
            "reference_structure_confirmed": "true",
        },
        files=[
            ("source", ("source.png", _source(), "image/png")),
            ("missing_mask", ("mask.png", _mask(), "image/png")),
            (
                "reference_views",
                ("view-1.png", _patterned_view("vertical"), "image/png"),
            ),
            (
                "reference_views",
                ("view-2.png", _patterned_view("horizontal"), "image/png"),
            ),
        ],
    )
    assert response.status_code == 200
    assert response.json()["provider_enabled"] is True
