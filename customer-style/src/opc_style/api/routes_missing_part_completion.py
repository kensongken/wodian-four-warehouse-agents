"""Local, review-gated API for small AI-simulated jewelry repairs."""

from __future__ import annotations

import csv
import io
import json
import zipfile
from decimal import Decimal
from hashlib import sha256
from typing import Annotated, Literal, cast
from uuid import UUID

from fastapi import APIRouter, File, Form, Header, Request, UploadFile, status
from fastapi.responses import HTMLResponse, JSONResponse, Response

from opc_style.domain.errors import DomainRuleError
from opc_style.domain.missing_part_completion import (
    MissingPartCompletionRequest,
    multiview_reference_digest,
)
from opc_style.services.missing_part_calibration_review import (
    AgentConfidence,
    AgentSuggestion,
    CalibrationAssetKind,
    CalibrationDecision,
    MissingPartCalibrationReviewStore,
)
from opc_style.services.missing_part_completion import (
    MissingPartCompletionService,
    MissingPartPreflightDiagnosis,
)
from opc_style.services.missing_part_learning import (
    CompletionFeedback,
    MissingPartLearningStore,
    PreflightFeedback,
)
from opc_style.services.missing_part_reference_assets import (
    MissingPartReferenceInboxStore,
    get_gap_target_source,
    get_priority_reference_candidate,
    get_reference_asset,
    priority_reference_candidates,
    recommended_diverse_reference_pair,
    reference_gap_manifest,
    reference_pool_count,
    reference_readiness_summary,
    reference_sequence_audit,
    reference_suggestions,
    resolve_reference_assets,
)
from opc_style.services.missing_part_threshold_comparison import (
    compare_preflight_thresholds,
    observation_from_queue_item,
)
from opc_style.web.missing_part_completion_page import MISSING_PART_COMPLETION_PAGE

router = APIRouter(
    prefix="/local/style-suite/api/missing-part-completion",
    tags=["local-missing-part-completion"],
)
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_REFERENCE_BATCH_ENTRIES = 50
MAX_REFERENCE_BATCH_UNCOMPRESSED_BYTES = 100 * 1024 * 1024


def _mapping_list(value: object) -> tuple[dict[str, object], ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, dict))


def _as_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


@router.get("/page", response_class=HTMLResponse)
def missing_part_completion_page() -> HTMLResponse:
    return HTMLResponse(
        MISSING_PART_COMPLETION_PAGE,
        headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow"},
    )


@router.get("/reference-suggestions", response_model=None)
def missing_part_reference_suggestions(
    request: Request, source_sha256: str
) -> dict[str, object]:
    normalized = source_sha256.strip().lower()
    if len(normalized) != 64:
        return {
            "matched_known_source": False,
            "search_scope": "configured_audited_local_index",
            "reference_pool_count": reference_pool_count(),
            "candidates": [],
            "automatic_identity_claim": False,
        }
    candidates = reference_suggestions(normalized)
    inbox_store = _optional_reference_inbox_store(request)
    inbox_candidates = (
        inbox_store.approved_assets(normalized) if inbox_store is not None else ()
    )
    sequence_audit = reference_sequence_audit(normalized)
    recommended_asset_ids, recommended_pair_visual_distance = (
        recommended_diverse_reference_pair(normalized)
    )
    return {
        "matched_known_source": bool(candidates or inbox_candidates),
        "search_scope": "configured_audited_local_index",
        "reference_pool_count": reference_pool_count(),
        "candidates": [
            {
                "asset_id": candidate.asset_id,
                "content_sha256": candidate.content_sha256,
                "source_kind": candidate.source_kind,
                "content_url": (
                    "/local/style-suite/api/missing-part-completion/"
                    f"reference-assets/{candidate.asset_id}"
                ),
                "candidate_only": True,
                "same_product_confirmed": False,
                "missing_part_coverage_confirmed": False,
                "structure_evidence_state": candidate.structure_evidence_state,
                "eligible_for_missing_part_completion": (
                    candidate.eligible_for_missing_part_completion
                ),
                "recommended_for_viewpoint_diversity": (
                    candidate.asset_id in recommended_asset_ids
                ),
            }
            for candidate in candidates
        ]
        + [
            {
                "asset_id": candidate["asset_id"],
                "content_sha256": candidate["reference_sha256"],
                "source_kind": "human_approved_inbox_reference",
                "content_url": candidate["content_url"],
                "candidate_only": False,
                "same_product_confirmed": True,
                "missing_part_coverage_confirmed": True,
                "structure_evidence_state": "human_approved_visible_structure",
                "eligible_for_missing_part_completion": True,
                "recommended_for_viewpoint_diversity": False,
            }
            for candidate in inbox_candidates
        ],
        "recommended_asset_ids": list(recommended_asset_ids),
        "recommended_pair_visual_distance": recommended_pair_visual_distance,
        "recommendation_only": True,
        "automatic_identity_claim": False,
        "automatic_structure_claim": False,
        "structure_evidence_available": bool(inbox_candidates)
        or any(
            candidate.eligible_for_missing_part_completion for candidate in candidates
        ),
        "sequence_audit": sequence_audit,
        "recommended_action": (
            "defer_until_clean_reference"
            if candidates
            and not inbox_candidates
            and not any(
                candidate.eligible_for_missing_part_completion
                for candidate in candidates
            )
            else "review_candidates"
        ),
    }


@router.get("/reference-readiness", response_model=None)
def missing_part_reference_readiness(request: Request) -> dict[str, object]:
    payload = dict(reference_readiness_summary())
    store = _optional_reference_inbox_store(request)
    if store is None:
        return payload
    store_summary = store.summary()
    approved_targets = {
        str(record.get("target_id", ""))
        for record in _mapping_list(store_summary.get("records", []))
        if record.get("review_state") == "approved_for_completion_input"
    }
    if not approved_targets:
        return payload
    gap_manifest = reference_gap_manifest()
    gap_states = {
        str(item.get("target_id", "")): str(item.get("status", ""))
        for item in _mapping_list(gap_manifest.get("items", []))
    }
    same_occlusion_resolved = sum(
        gap_states.get(target_id) == "same_occlusion_deferred"
        for target_id in approved_targets
    )
    no_exact_resolved = sum(
        gap_states.get(target_id) == "no_exact_reference_deferred"
        for target_id in approved_targets
    )
    payload["ready_for_completion"] = _as_int(
        payload.get("ready_for_completion", 0)
    ) + len(approved_targets)
    payload["same_occlusion_deferred"] = max(
        0,
        _as_int(payload.get("same_occlusion_deferred", 0)) - same_occlusion_resolved,
    )
    payload["no_exact_reference_deferred"] = max(
        0,
        _as_int(payload.get("no_exact_reference_deferred", 0)) - no_exact_resolved,
    )
    payload["deferred_total"] = _as_int(payload.get("target_count", 0)) - _as_int(
        payload.get("ready_for_completion", 0)
    )
    payload["approved_reference_target_count"] = len(approved_targets)
    payload["automatic_generation_allowed"] = False
    return payload


def _effective_reference_gap_manifest(request: Request) -> dict[str, object]:
    """Return only unresolved targets while retaining the audited batch total."""

    payload = dict(reference_gap_manifest())
    items = _mapping_list(payload.get("items", []))
    store = _optional_reference_inbox_store(request)
    approved_targets = (
        {
            str(record.get("target_id", ""))
            for record in _mapping_list(store.summary().get("records", []))
            if record.get("review_state") == "approved_for_completion_input"
        }
        if store is not None
        else set()
    )
    unresolved = tuple(
        item for item in items if str(item.get("target_id", "")) not in approved_targets
    )
    payload["total_target_count"] = _as_int(payload.get("target_count", len(items)))
    payload["target_count"] = len(unresolved)
    payload["resolved_target_count"] = len(items) - len(unresolved)
    payload["items"] = list(unresolved)
    if items and not unresolved:
        payload["state"] = "all_real_references_approved"
    return payload


@router.get("/reference-gap-manifest", response_model=None)
def missing_part_reference_gap_manifest(request: Request) -> dict[str, object]:
    return _effective_reference_gap_manifest(request)


@router.get("/reference-gap-manifest.csv", response_class=Response)
def missing_part_reference_gap_manifest_csv(request: Request) -> Response:
    """Export the unresolved batch as an owner-friendly capture/search list."""

    manifest = _effective_reference_gap_manifest(request)
    content = _reference_gap_csv_bytes(manifest)
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": (
                'attachment; filename="missing-part-reference-gap-list.csv"'
            ),
            "Cache-Control": "no-store",
        },
    )


def _reference_gap_csv_bytes(manifest: dict[str, object]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(
        [
            "序号",
            "目标图片",
            "当前状态",
            "需要补充的真实素材",
            "不能使用的素材",
            "已检索独立图片数",
            "后续动作",
        ]
    )
    for index, item in enumerate(_mapping_list(manifest.get("items", [])), start=1):
        status_value = str(item.get("status", ""))
        rejected = item.get("rejected_evidence", [])
        rejected_values = (
            [str(value) for value in rejected] if isinstance(rejected, list) else []
        )
        writer.writerow(
            [
                index,
                str(item.get("source_filename", "")),
                (
                    "同款候选仍遮挡"
                    if status_value == "same_occlusion_deferred"
                    else "缺少确证同款"
                ),
                str(item.get("accepted_reference", "")),
                "；".join(rejected_values),
                _as_int(item.get("searched_unique_reference_count", 0)),
                str(item.get("next_action", "")),
            ]
        )
    return ("\ufeff" + output.getvalue()).encode("utf-8")


@router.get("/reference-gap-package.zip", response_class=Response, response_model=None)
def missing_part_reference_gap_package(request: Request) -> Response | JSONResponse:
    """Bundle audited target images and their instructions for handoff."""

    manifest = _effective_reference_gap_manifest(request)
    items = _mapping_list(manifest.get("items", []))
    archive_buffer = io.BytesIO()
    try:
        with zipfile.ZipFile(
            archive_buffer, mode="w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            archive.writestr("补拍找图清单.csv", _reference_gap_csv_bytes(manifest))
            archive.writestr(
                "回包命名说明.txt",
                (
                    "每张补拍图必须在文件名中保留对应的 "
                    "round137-boundary-XX 目标编号。\n"
                    "例如：round137-boundary-01__reference.jpg\n"
                    "只能放入同一件实物商品、且缺失位置清楚可见的 JPG/PNG。\n"
                    "回传后仍会进入人工并排复核，不会自动生成。\n"
                ).encode(),
            )
            for index, item in enumerate(items, start=1):
                target_id = str(item.get("target_id", ""))
                source_filename = str(item.get("source_filename", ""))
                content, _ = get_gap_target_source(target_id)
                safe_name = f"{index:02d}_{target_id}_{source_filename}"
                archive.writestr(f"待补原图/{safe_name}", content)
    except DomainRuleError as error:
        return _error(error)
    return Response(
        content=archive_buffer.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition": (
                'attachment; filename="missing-part-reference-capture-package.zip"'
            ),
            "Cache-Control": "no-store",
        },
    )


@router.get("/delivery-gate", response_model=None)
def missing_part_delivery_gate(request: Request) -> dict[str, object]:
    readiness = missing_part_reference_readiness(request)
    inbox_store = _optional_reference_inbox_store(request)
    inbox = (
        inbox_store.summary()
        if inbox_store is not None
        else {
            "reference_count": 0,
            "pending_count": 0,
            "approved_count": 0,
            "rejected_count": 0,
            "provider_calls": 0,
        }
    )
    ready = _as_int(readiness.get("ready_for_completion", 0))
    target_count = _as_int(readiness.get("target_count", 0))
    batch_ready = target_count > 0 and ready == target_count
    return {
        "state": "ready_for_controlled_generation"
        if batch_ready
        else "blocked_by_real_evidence",
        "workflow_available": True,
        "workflow_steps": [
            "gap_manifest",
            "private_reference_upload",
            "side_by_side_human_review",
            "approved_reference_binding",
            "paid_preflight_confirmation",
        ],
        "target_count": target_count,
        "ready_for_completion": ready,
        "deferred_total": _as_int(readiness.get("deferred_total", 0)),
        "inbox_reference_count": _as_int(inbox.get("reference_count", 0)),
        "pending_human_review": _as_int(inbox.get("pending_count", 0)),
        "approved_reference_count": _as_int(inbox.get("approved_count", 0)),
        "provider_calls": _as_int(inbox.get("provider_calls", 0)),
        "business_evidence_complete": batch_ready,
        "production_release_allowed": False,
        "automatic_generation_allowed": False,
        "blocking_reason": (
            None if batch_ready else "缺少同一件实物商品且能看清缺失位置的真实参考素材"
        ),
        "next_action": (
            "逐张检查补全范围与报价；仍需人工确认后才能生成"
            if batch_ready
            else "补交真实同款参考并完成人工并排复核"
        ),
        "evidence_boundary": (
            "功能链路可用不等于真实图片已经具备补全依据，也不等于生产发布验收"
        ),
    }


def _reference_inbox_store(request: Request) -> MissingPartReferenceInboxStore:
    store = getattr(request.app.state, "missing_part_reference_inbox_store", None)
    if not isinstance(store, MissingPartReferenceInboxStore):
        raise DomainRuleError("missing_part_reference_inbox_unavailable")
    return store


def _optional_reference_inbox_store(
    request: Request,
) -> MissingPartReferenceInboxStore | None:
    store = getattr(request.app.state, "missing_part_reference_inbox_store", None)
    return store if isinstance(store, MissingPartReferenceInboxStore) else None


@router.get("/reference-priority-candidates", response_model=None)
def missing_part_priority_reference_candidates(request: Request) -> dict[str, object]:
    store = _optional_reference_inbox_store(request)
    reviewed = (
        {
            (str(item.get("target_id", "")), str(item.get("reference_sha256", "")))
            for item in _mapping_list(store.summary().get("records"))
            if item.get("review_state")
            in {"approved_for_completion_input", "rejected_reference"}
        }
        if store is not None
        else set()
    )
    candidates = tuple(
        candidate
        for candidate in priority_reference_candidates()
        if (candidate.target_id, candidate.reference_sha256) not in reviewed
    )
    return {
        "state": "human_identity_review_required" if candidates else "empty",
        "candidate_count": len(candidates),
        "candidates": [
            {
                "target_id": candidate.target_id,
                "target_sha256": candidate.target_sha256,
                "source_filename": candidate.source_filename,
                "source_content_url": (
                    f"{router.prefix}/reference-inbox/{candidate.target_id}/source"
                ),
                "reference_sha256": candidate.reference_sha256,
                "reference_filename": candidate.reference_filename,
                "reference_content_url": (
                    f"{router.prefix}/reference-priority-candidates/"
                    f"{candidate.target_id}/{candidate.reference_sha256}"
                ),
                "machine_visual_triage": candidate.machine_visual_triage,
                "feature_distance": candidate.feature_distance,
                "same_physical_product_confirmed": False,
                "clean_missing_structure_confirmed": False,
                "automatic_generation_allowed": False,
                "provider_calls": 0,
            }
            for candidate in candidates
        ],
        "automatic_generation_allowed": False,
        "provider_calls": 0,
    }


@router.get(
    "/reference-priority-candidates/{target_id}/{reference_sha256}",
    response_model=None,
)
def missing_part_priority_reference_candidate_content(
    target_id: str, reference_sha256: str
) -> Response | JSONResponse:
    try:
        content, mime_type, _ = get_priority_reference_candidate(
            target_id, reference_sha256
        )
        return Response(
            content=content,
            media_type=mime_type,
            headers={"Cache-Control": "private, no-store", "X-Robots-Tag": "noindex"},
        )
    except DomainRuleError as error:
        return _error(error)


@router.post(
    "/reference-priority-candidates/{target_id}/{reference_sha256}/approve",
    response_model=None,
)
def approve_missing_part_priority_reference_candidate(
    request: Request,
    target_id: str,
    reference_sha256: str,
    identity_and_structure_confirmed: Annotated[bool, Form()] = False,
) -> dict[str, object] | JSONResponse:
    try:
        if not identity_and_structure_confirmed:
            raise DomainRuleError("missing_part_reference_confirmation_required")
        content, _, candidate = get_priority_reference_candidate(
            target_id, reference_sha256
        )
        return _reference_inbox_store(request).admit_reviewed_candidate(
            target_id=target_id,
            original_filename=candidate.reference_filename,
            content=content,
            note="优先候选并排人工确认：同一件实物且缺失位置清楚可见",
        )
    except DomainRuleError as error:
        return _error(error)


@router.post(
    "/reference-priority-candidates/{target_id}/{reference_sha256}/reject",
    response_model=None,
)
def reject_missing_part_priority_reference_candidate(
    request: Request,
    target_id: str,
    reference_sha256: str,
    not_usable_confirmed: Annotated[bool, Form()] = False,
) -> dict[str, object] | JSONResponse:
    try:
        if not not_usable_confirmed:
            raise DomainRuleError("missing_part_reference_confirmation_required")
        content, _, candidate = get_priority_reference_candidate(
            target_id, reference_sha256
        )
        return _reference_inbox_store(request).reject_reviewed_candidate(
            target_id=target_id,
            original_filename=candidate.reference_filename,
            content=content,
            note="优先候选并排人工排除：不是同一件实物或缺失位置仍不可见",
        )
    except DomainRuleError as error:
        return _error(error)


@router.get("/reference-inbox", response_model=None)
def missing_part_reference_inbox(request: Request) -> dict[str, object] | JSONResponse:
    try:
        return _reference_inbox_store(request).summary()
    except DomainRuleError as error:
        return _error(error)


@router.post("/reference-inbox/{target_id}", response_model=None)
async def submit_missing_part_reference(
    request: Request,
    target_id: str,
    reference: Annotated[UploadFile, File()],
    same_product_confirmed: Annotated[bool, Form()] = False,
    structure_visible_confirmed: Annotated[bool, Form()] = False,
) -> dict[str, object] | JSONResponse:
    try:
        content = await _read_upload(reference)
        return _reference_inbox_store(request).submit(
            target_id=target_id,
            original_filename=reference.filename or "reference",
            content=content,
            same_product_confirmed=same_product_confirmed,
            structure_visible_confirmed=structure_visible_confirmed,
        )
    except DomainRuleError as error:
        return _error(error)


@router.post("/reference-inbox-batch", response_model=None)
async def submit_missing_part_reference_batch(
    request: Request,
    reference_batch: Annotated[UploadFile, File()],
    same_product_mapping_confirmed: Annotated[bool, Form()] = False,
    structure_visible_confirmed: Annotated[bool, Form()] = False,
) -> dict[str, object] | JSONResponse:
    """Stage a named ZIP batch without approving or generating any candidate."""

    try:
        if not same_product_mapping_confirmed or not structure_visible_confirmed:
            raise DomainRuleError("missing_part_reference_confirmation_required")
        entries = _reference_batch_entries(await _read_upload(reference_batch))
        store = _reference_inbox_store(request)
        records = store.submit_batch(
            entries=entries,
            same_product_confirmed=True,
            structure_visible_confirmed=True,
        )
        return {
            "submitted_count": len(records),
            "target_count": len({str(record["target_id"]) for record in records}),
            "records": records,
            "review_state": "pending_human_visual_review",
            "automatic_generation_allowed": False,
            "provider_calls": 0,
        }
    except DomainRuleError as error:
        return _error(error)


@router.get("/reference-inbox/{target_id}/source", response_model=None)
def missing_part_reference_inbox_source(
    request: Request, target_id: str
) -> Response | JSONResponse:
    try:
        content, mime_type = _reference_inbox_store(request).source_content(target_id)
        return Response(
            content=content,
            media_type=mime_type,
            headers={"Cache-Control": "private, no-store", "X-Robots-Tag": "noindex"},
        )
    except DomainRuleError as error:
        return _error(error)


@router.get(
    "/reference-inbox/{target_id}/references/{reference_sha256}",
    response_model=None,
)
def missing_part_reference_inbox_asset(
    request: Request, target_id: str, reference_sha256: str
) -> Response | JSONResponse:
    try:
        content, mime_type = _reference_inbox_store(request).reference_content(
            target_id, reference_sha256
        )
        return Response(
            content=content,
            media_type=mime_type,
            headers={"Cache-Control": "private, no-store", "X-Robots-Tag": "noindex"},
        )
    except DomainRuleError as error:
        return _error(error)


@router.post(
    "/reference-inbox/{target_id}/references/{reference_sha256}/review",
    response_model=None,
)
def review_missing_part_reference(
    request: Request,
    target_id: str,
    reference_sha256: str,
    decision: Annotated[Literal["approved", "rejected"], Form()],
    note: Annotated[str, Form()] = "",
) -> dict[str, object] | JSONResponse:
    try:
        return _reference_inbox_store(request).decide(
            target_id=target_id,
            reference_sha256=reference_sha256,
            decision=decision,
            note=note,
        )
    except DomainRuleError as error:
        return _error(error)


@router.post("/reference-inbox-review-batch", response_model=None)
def review_missing_part_reference_batch(
    request: Request,
    decisions_json: Annotated[str, Form()],
) -> dict[str, object] | JSONResponse:
    """Save a fully selected visual-review sheet in one operation."""

    try:
        raw = json.loads(decisions_json)
        if not isinstance(raw, list) or not raw or len(raw) > 50:
            raise DomainRuleError("missing_part_reference_batch_review_invalid")
        decisions: list[tuple[str, str, Literal["approved", "rejected"], str]] = []
        for item in raw:
            if not isinstance(item, dict):
                raise DomainRuleError("missing_part_reference_batch_review_invalid")
            target_id = str(item.get("target_id", ""))
            reference_sha256 = str(item.get("reference_sha256", ""))
            decision_value = str(item.get("decision", ""))
            if decision_value not in {"approved", "rejected"}:
                raise DomainRuleError("missing_part_reference_batch_review_invalid")
            decisions.append(
                (
                    target_id,
                    reference_sha256,
                    cast(Literal["approved", "rejected"], decision_value),
                    str(item.get("note", "")),
                )
            )
        records = _reference_inbox_store(request).decide_batch(tuple(decisions))
        return {
            "reviewed_count": len(records),
            "approved_count": sum(
                record.get("review_state") == "approved_for_completion_input"
                for record in records
            ),
            "rejected_count": sum(
                record.get("review_state") == "rejected_reference" for record in records
            ),
            "records": records,
            "automatic_generation_allowed": False,
            "provider_calls": 0,
        }
    except (json.JSONDecodeError, DomainRuleError) as error:
        domain_error = (
            error
            if isinstance(error, DomainRuleError)
            else DomainRuleError("missing_part_reference_batch_review_invalid")
        )
        return _error(domain_error)


@router.get("/reference-assets/{asset_id}", response_model=None)
def missing_part_reference_asset(asset_id: str) -> Response | JSONResponse:
    try:
        content, mime_type = get_reference_asset(asset_id)
        return Response(
            content=content,
            media_type=mime_type,
            headers={"Cache-Control": "private, no-store", "X-Robots-Tag": "noindex"},
        )
    except DomainRuleError as error:
        return _error(error)


def _service(request: Request) -> MissingPartCompletionService:
    service = getattr(request.app.state, "missing_part_completion_service", None)
    if not isinstance(service, MissingPartCompletionService):
        raise DomainRuleError("missing_part_provider_disabled")
    return service


def _calibration_store(request: Request) -> MissingPartCalibrationReviewStore:
    store = getattr(request.app.state, "missing_part_calibration_review_store", None)
    if not isinstance(store, MissingPartCalibrationReviewStore):
        raise DomainRuleError("missing_part_calibration_store_unavailable")
    return store


def _reference_batch_entries(content: bytes) -> tuple[tuple[str, str, bytes], ...]:
    manifest = reference_gap_manifest()
    target_ids = {
        str(item.get("target_id", ""))
        for item in _mapping_list(manifest.get("items", []))
        if item.get("target_id")
    }
    if not target_ids:
        raise DomainRuleError("missing_part_reference_target_invalid")
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except (zipfile.BadZipFile, zipfile.LargeZipFile):
        raise DomainRuleError("missing_part_reference_batch_invalid") from None
    with archive:
        members = [member for member in archive.infolist() if not member.is_dir()]
        image_members = [
            member
            for member in members
            if member.filename.lower().endswith((".jpg", ".jpeg", ".png"))
        ]
        if not image_members or len(image_members) > MAX_REFERENCE_BATCH_ENTRIES:
            raise DomainRuleError("missing_part_reference_batch_invalid")
        if any(
            member.flag_bits & 0x1
            or member.file_size <= 0
            or member.file_size > MAX_UPLOAD_BYTES
            or member.compress_size > MAX_UPLOAD_BYTES
            for member in image_members
        ):
            raise DomainRuleError("missing_part_reference_batch_invalid")
        if (
            sum(member.file_size for member in image_members)
            > MAX_REFERENCE_BATCH_UNCOMPRESSED_BYTES
        ):
            raise DomainRuleError("missing_part_reference_batch_invalid")
        entries: list[tuple[str, str, bytes]] = []
        digests: set[str] = set()
        per_target: dict[str, int] = {}
        for member in image_members:
            basename = member.filename.replace("\\", "/").rsplit("/", 1)[-1]
            if not basename or ".." in member.filename.replace("\\", "/").split("/"):
                raise DomainRuleError("missing_part_reference_batch_invalid")
            matches = [target_id for target_id in target_ids if target_id in basename]
            if len(matches) != 1:
                raise DomainRuleError("missing_part_reference_batch_target_missing")
            target_id = matches[0]
            per_target[target_id] = per_target.get(target_id, 0) + 1
            if per_target[target_id] > 8:
                raise DomainRuleError("missing_part_reference_batch_invalid")
            extracted = archive.read(member)
            digest = sha256(extracted).hexdigest()
            if digest in digests:
                raise DomainRuleError("missing_part_reference_batch_duplicate")
            digests.add(digest)
            entries.append((target_id, basename[:255], extracted))
    return tuple(entries)


async def _read_upload(upload: UploadFile) -> bytes:
    content = await upload.read(MAX_UPLOAD_BYTES + 1)
    if not content or len(content) > MAX_UPLOAD_BYTES:
        raise DomainRuleError("missing_part_upload_invalid")
    return content


async def _completion_request(
    *,
    reference_inbox_store: MissingPartReferenceInboxStore | None = None,
    source: UploadFile,
    missing_mask: UploadFile,
    category: str,
    part_kind: str,
    material_tags: str,
    case_id: str,
    scene_tags: str = "",
    evidence_kind: str = "none",
    evidence_sha256: str = "",
    reference: UploadFile | None = None,
    reference_views: list[UploadFile] | None = None,
    reference_asset_ids: str = "",
    reference_same_product_confirmed: bool = False,
    reference_structure_confirmed: bool = False,
) -> MissingPartCompletionRequest:
    source_content = await _read_upload(source)
    mask_content = await _read_upload(missing_mask)
    normalized_evidence_kind = evidence_kind.strip()[:64] or "none"
    normalized_evidence_sha256 = evidence_sha256.strip().lower() or None
    reference_content = await _read_upload(reference) if reference is not None else None
    reference_content_mime_type = (
        reference.content_type if reference is not None else None
    )
    reference_views = reference_views or []
    uploaded_reference_contents = tuple(
        [await _read_upload(reference_view) for reference_view in reference_views]
    )
    uploaded_reference_mime_types = tuple(
        reference_view.content_type or "application/octet-stream"
        for reference_view in reference_views
    )
    selected_asset_ids = tuple(
        value.strip() for value in reference_asset_ids.split(",") if value.strip()
    )
    asset_contents: tuple[bytes, ...] = ()
    asset_mime_types: tuple[str, ...] = ()
    if selected_asset_ids:
        local_asset_ids = tuple(
            asset_id
            for asset_id in selected_asset_ids
            if not asset_id.startswith("inbox-")
        )
        inbox_asset_ids = tuple(
            asset_id for asset_id in selected_asset_ids if asset_id.startswith("inbox-")
        )
        local_contents: tuple[bytes, ...] = ()
        local_mime_types: tuple[str, ...] = ()
        inbox_contents: tuple[bytes, ...] = ()
        inbox_mime_types: tuple[str, ...] = ()
        source_digest = sha256(source_content).hexdigest()
        if local_asset_ids:
            local_contents, local_mime_types = resolve_reference_assets(
                source_digest, local_asset_ids
            )
        if inbox_asset_ids:
            if reference_inbox_store is None:
                raise DomainRuleError("missing_part_reference_inbox_unavailable")
            inbox_contents, inbox_mime_types = (
                reference_inbox_store.resolve_approved_assets(
                    source_digest, inbox_asset_ids
                )
            )
        asset_contents = (*local_contents, *inbox_contents)
        asset_mime_types = (*local_mime_types, *inbox_mime_types)
        if (
            normalized_evidence_kind == "clean_same_product_reference"
            and reference_content is None
            and len(asset_contents) == 1
        ):
            reference_content = asset_contents[0]
            reference_content_mime_type = asset_mime_types[0]
            asset_contents = ()
            asset_mime_types = ()
    reference_contents = (*uploaded_reference_contents, *asset_contents)
    reference_mime_types = (*uploaded_reference_mime_types, *asset_mime_types)
    if (
        normalized_evidence_kind == "human_confirmed_mask"
        and normalized_evidence_sha256 is None
    ):
        normalized_evidence_sha256 = sha256(mask_content).hexdigest()
    if (
        normalized_evidence_kind == "clean_same_product_reference"
        and reference_content is not None
        and normalized_evidence_sha256 is None
    ):
        normalized_evidence_sha256 = sha256(reference_content).hexdigest()
    if (
        normalized_evidence_kind == "same_product_multiview_references"
        and reference_contents
        and normalized_evidence_sha256 is None
    ):
        normalized_evidence_sha256 = multiview_reference_digest(reference_contents)
    return MissingPartCompletionRequest(
        source_content=source_content,
        source_sha256=sha256(source_content).hexdigest(),
        source_mime_type=source.content_type or "application/octet-stream",
        missing_mask_png=mask_content,
        category=category.strip()[:64],
        part_kind=part_kind.strip()[:64],
        material_tags=tuple(
            sorted(
                {
                    value.strip()[:64]
                    for value in material_tags.split(",")
                    if value.strip()
                }
            )
        ),
        case_id=case_id.strip()[:128],
        scene_tags=tuple(
            sorted(
                {value.strip()[:64] for value in scene_tags.split(",") if value.strip()}
            )
        ),
        evidence_kind=normalized_evidence_kind,
        evidence_sha256=normalized_evidence_sha256,
        reference_content=reference_content,
        reference_mime_type=reference_content_mime_type,
        reference_same_product_confirmed=reference_same_product_confirmed,
        reference_structure_confirmed=reference_structure_confirmed,
        reference_contents=reference_contents,
        reference_mime_types=reference_mime_types,
    )


def _error(error: DomainRuleError) -> JSONResponse:
    code = error.code
    status_code = (
        status.HTTP_503_SERVICE_UNAVAILABLE
        if code
        in {
            "missing_part_provider_disabled",
            "missing_part_provider_auth_failed",
            "missing_part_provider_rate_limited",
            "missing_part_provider_rejected",
            "missing_part_provider_response_invalid",
        }
        else status.HTTP_422_UNPROCESSABLE_CONTENT
        if code.startswith("missing_part_")
        else status.HTTP_409_CONFLICT
    )
    if code in {
        "missing_part_confirmation_required",
        "missing_part_outcome_uncertain",
    }:
        status_code = status.HTTP_409_CONFLICT
    return JSONResponse(status_code=status_code, content={"detail": {"code": code}})


def _diagnosis_payload(
    diagnosis: MissingPartPreflightDiagnosis,
) -> dict[str, object]:
    return {
        "request_fingerprint": diagnosis.request_fingerprint,
        "missing_ratio": diagnosis.missing_ratio,
        "state": diagnosis.state,
        "reasons": list(diagnosis.reasons),
        "context_edge_mean": diagnosis.context_edge_mean,
        "strong_edge_fraction": diagnosis.strong_edge_fraction,
        "context_chroma_mean": diagnosis.context_chroma_mean,
        "strong_chroma_fraction": diagnosis.strong_chroma_fraction,
        "context_chroma_stddev": diagnosis.context_chroma_stddev,
        "provider_called": False,
        "paid_override": False,
        "publishable": False,
    }


@router.get("/preflight/summary", response_model=None)
def missing_part_preflight_summary(
    request: Request,
) -> dict[str, object] | JSONResponse:
    learning_store = getattr(request.app.state, "missing_part_learning_store", None)
    try:
        if not isinstance(learning_store, MissingPartLearningStore):
            raise DomainRuleError("missing_part_provider_disabled")
        return {
            **learning_store.preflight_summary(),
            "threshold_updated_automatically": False,
            "paid_override_enabled": False,
        }
    except DomainRuleError as error:
        return _error(error)


@router.get("/preflight/review-queue", response_model=None)
def missing_part_preflight_review_queue(
    request: Request,
    category: str = "",
    part_kind: str = "",
    outcome: str = "all",
    limit: int = 100,
) -> dict[str, object] | JSONResponse:
    learning_store = getattr(request.app.state, "missing_part_learning_store", None)
    try:
        if not isinstance(learning_store, MissingPartLearningStore):
            raise DomainRuleError("missing_part_provider_disabled")
        return learning_store.preflight_review_queue(
            category=category.strip()[:64],
            part_kind=part_kind.strip()[:64],
            outcome=outcome.strip()[:32],
            limit=limit,
        )
    except DomainRuleError as error:
        return _error(error)


@router.get("/preflight/offline-comparison", response_model=None)
def missing_part_preflight_offline_comparison(
    request: Request,
    category: str,
    part_kind: str,
) -> dict[str, object] | JSONResponse:
    learning_store = getattr(request.app.state, "missing_part_learning_store", None)
    try:
        if not isinstance(learning_store, MissingPartLearningStore):
            raise DomainRuleError("missing_part_provider_disabled")
        queue = learning_store.preflight_review_queue(
            category=category.strip()[:64],
            part_kind=part_kind.strip()[:64],
            limit=200,
        )
        items = queue["items"]
        if not isinstance(items, list):
            raise DomainRuleError("missing_part_learning_chain_invalid")
        observations = tuple(
            observation_from_queue_item(item)
            for item in items
            if isinstance(item, dict)
        )
        return {
            "category": category.strip()[:64],
            "part_kind": part_kind.strip()[:64],
            **compare_preflight_thresholds(observations),
        }
    except (DomainRuleError, ValueError) as error:
        if isinstance(error, DomainRuleError):
            return _error(error)
        return _error(DomainRuleError(str(error)))


@router.get("/calibration/review-queue", response_model=None)
def missing_part_calibration_review_queue(
    request: Request,
) -> dict[str, object] | JSONResponse:
    try:
        return _calibration_store(request).queue()
    except DomainRuleError as error:
        return _error(error)


@router.post("/calibration/agent-reviews", response_model=None)
def record_missing_part_calibration_agent_review(
    request: Request,
    proposal_id: Annotated[str, Form()],
    suggestion: Annotated[AgentSuggestion, Form()],
    confidence: Annotated[AgentConfidence, Form()],
    reviewer_version: Annotated[str, Form()],
    reason: Annotated[str, Form()],
) -> dict[str, object] | JSONResponse:
    """Keep model advice separate from the human learning record."""

    try:
        event = _calibration_store(request).record_agent_review(
            proposal_id=proposal_id,
            suggestion=suggestion,
            confidence=confidence,
            reviewer_version=reviewer_version,
            reason=reason,
        )
        return {
            "recorded": True,
            "event_id": event["event_id"],
            "proposal_id": proposal_id,
            "suggestion": suggestion,
            "confidence": confidence,
            "provenance": "agent_visual_review",
            "training_eligible": False,
            "human_label": None,
            "threshold_updated": False,
            "publishable": False,
        }
    except DomainRuleError as error:
        return _error(error)


@router.get("/calibration/assets/{proposal_id}/{kind}", response_model=None)
def missing_part_calibration_asset(
    request: Request,
    proposal_id: str,
    kind: CalibrationAssetKind,
) -> Response | JSONResponse:
    try:
        content, media_type = _calibration_store(request).asset(proposal_id, kind)
        return Response(
            content,
            media_type=media_type,
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'",
            },
        )
    except DomainRuleError as error:
        return _error(error)


@router.post("/calibration/reviews", response_model=None)
def record_missing_part_calibration_review(
    request: Request,
    customer_id: Annotated[UUID, Header(alias="X-Customer-Id")],
    proposal_id: Annotated[str, Form()],
    decision: Annotated[CalibrationDecision, Form()],
    note: Annotated[str, Form()] = "",
) -> dict[str, object] | JSONResponse:
    learning_store = getattr(request.app.state, "missing_part_learning_store", None)
    try:
        if not isinstance(learning_store, MissingPartLearningStore):
            raise DomainRuleError("missing_part_provider_disabled")
        calibration_store = _calibration_store(request)
        proposal = calibration_store.proposal(proposal_id)
        if calibration_store.is_reviewed(proposal_id):
            raise DomainRuleError("missing_part_calibration_already_reviewed")
        training_eligible = decision in {
            "confirmed_structure",
            "confirmed_background",
        }
        learning_event_id: str | None = None
        if training_eligible:
            human_label: Literal["confirmed_structure", "confirmed_background"] = (
                "confirmed_structure"
                if decision == "confirmed_structure"
                else "confirmed_background"
            )
            feedback = PreflightFeedback.create(
                case_id=(
                    f"batch:{customer_id}:"
                    f"{sha256(proposal_id.encode()).hexdigest()[:32]}"
                ),
                request_fingerprint=str(proposal["missing_mask_sha256"]),
                category=str(proposal["category"]),
                part_kind=str(proposal["part_kind"]),
                material_tags=(),
                system_state=str(proposal["system_state"]),
                human_label=human_label,
                context_edge_mean=cast(float, proposal["context_edge_mean"]),
                strong_edge_fraction=cast(float, proposal["strong_edge_fraction"]),
                context_chroma_mean=cast(float, proposal["context_chroma_mean"]),
                strong_chroma_fraction=cast(float, proposal["strong_chroma_fraction"]),
                context_chroma_stddev=cast(float, proposal["context_chroma_stddev"]),
                note=note or f"batch review: {decision}",
            )
            learning_store.append_preflight(feedback)
            learning_event_id = feedback.event_id
        review = calibration_store.record_review(
            proposal_id=proposal_id,
            decision=decision,
            reviewer_id=customer_id,
            note=note,
        )
        return {
            "recorded": True,
            "event_id": review["event_id"],
            "proposal_id": proposal_id,
            "decision": decision,
            "training_eligible": training_eligible,
            "learning_event_id": learning_event_id,
            "provider_called": False,
            "threshold_updated": False,
            "publishable": False,
        }
    except DomainRuleError as error:
        return _error(error)


@router.post("/preflight/diagnose", response_model=None)
async def diagnose_missing_part_preflight(
    request: Request,
    source: Annotated[UploadFile, File()],
    missing_mask: Annotated[UploadFile, File()],
    category: Annotated[str, Form()],
    part_kind: Annotated[str, Form()],
    material_tags: Annotated[str, Form()] = "",
    case_id: Annotated[str, Form()] = "local-case",
    scene_tags: Annotated[str, Form()] = "",
    evidence_kind: Annotated[str, Form()] = "none",
    evidence_sha256: Annotated[str, Form()] = "",
    reference: Annotated[UploadFile | None, File()] = None,
    reference_views: Annotated[list[UploadFile] | None, File()] = None,
    reference_asset_ids: Annotated[str, Form()] = "",
    reference_same_product_confirmed: Annotated[bool, Form()] = False,
    reference_structure_confirmed: Annotated[bool, Form()] = False,
) -> dict[str, object] | JSONResponse:
    try:
        completion_request = await _completion_request(
            reference_inbox_store=_optional_reference_inbox_store(request),
            source=source,
            missing_mask=missing_mask,
            category=category,
            part_kind=part_kind,
            material_tags=material_tags,
            case_id=case_id,
            scene_tags=scene_tags,
            evidence_kind=evidence_kind,
            evidence_sha256=evidence_sha256,
            reference=reference,
            reference_views=reference_views,
            reference_asset_ids=reference_asset_ids,
            reference_same_product_confirmed=reference_same_product_confirmed,
            reference_structure_confirmed=reference_structure_confirmed,
        )
        return _diagnosis_payload(_service(request).diagnose(completion_request))
    except DomainRuleError as error:
        return _error(error)


@router.post("/preflight/feedback", response_model=None)
async def record_missing_part_preflight_feedback(
    request: Request,
    customer_id: Annotated[UUID, Header(alias="X-Customer-Id")],
    source: Annotated[UploadFile, File()],
    missing_mask: Annotated[UploadFile, File()],
    category: Annotated[str, Form()],
    part_kind: Annotated[str, Form()],
    human_label: Annotated[
        Literal["confirmed_structure", "confirmed_background"], Form()
    ],
    note: Annotated[str, Form()] = "",
    material_tags: Annotated[str, Form()] = "",
    case_id: Annotated[str, Form()] = "local-case",
    scene_tags: Annotated[str, Form()] = "",
    evidence_kind: Annotated[str, Form()] = "none",
    evidence_sha256: Annotated[str, Form()] = "",
    reference: Annotated[UploadFile | None, File()] = None,
    reference_views: Annotated[list[UploadFile] | None, File()] = None,
    reference_asset_ids: Annotated[str, Form()] = "",
    reference_same_product_confirmed: Annotated[bool, Form()] = False,
    reference_structure_confirmed: Annotated[bool, Form()] = False,
) -> dict[str, object] | JSONResponse:
    learning_store = getattr(request.app.state, "missing_part_learning_store", None)
    try:
        if not isinstance(learning_store, MissingPartLearningStore):
            raise DomainRuleError("missing_part_provider_disabled")
        completion_request = await _completion_request(
            reference_inbox_store=_optional_reference_inbox_store(request),
            source=source,
            missing_mask=missing_mask,
            category=category,
            part_kind=part_kind,
            material_tags=material_tags,
            case_id=case_id,
            scene_tags=scene_tags,
            evidence_kind=evidence_kind,
            evidence_sha256=evidence_sha256,
            reference=reference,
            reference_views=reference_views,
            reference_asset_ids=reference_asset_ids,
            reference_same_product_confirmed=reference_same_product_confirmed,
            reference_structure_confirmed=reference_structure_confirmed,
        )
        diagnosis = _service(request).diagnose(completion_request)
        feedback = PreflightFeedback.create(
            case_id=f"{customer_id}:{completion_request.case_id}",
            request_fingerprint=diagnosis.request_fingerprint,
            category=completion_request.category,
            part_kind=completion_request.part_kind,
            material_tags=completion_request.material_tags,
            system_state=diagnosis.state,
            human_label=human_label,
            context_edge_mean=diagnosis.context_edge_mean,
            strong_edge_fraction=diagnosis.strong_edge_fraction,
            context_chroma_mean=diagnosis.context_chroma_mean,
            strong_chroma_fraction=diagnosis.strong_chroma_fraction,
            context_chroma_stddev=diagnosis.context_chroma_stddev,
            note=note,
        )
        learning_store.append_preflight(feedback)
        expected_state = (
            "anchored_to_visible_structure"
            if human_label == "confirmed_structure"
            else "blocked_unanchored"
        )
        return {
            "recorded": True,
            "event_id": feedback.event_id,
            "system_state": diagnosis.state,
            "human_label": human_label,
            "system_agrees_with_human": diagnosis.state == expected_state,
            "training_state": "calibration_case_memory_ready",
            "threshold_updated": False,
            "provider_called": False,
            "paid_override": False,
        }
    except DomainRuleError as error:
        return _error(error)


@router.post("/preview", response_model=None)
async def preview_missing_part_completion(
    request: Request,
    source: Annotated[UploadFile, File()],
    missing_mask: Annotated[UploadFile, File()],
    category: Annotated[str, Form()],
    part_kind: Annotated[str, Form()],
    material_tags: Annotated[str, Form()] = "",
    case_id: Annotated[str, Form()] = "local-case",
    scene_tags: Annotated[str, Form()] = "",
    evidence_kind: Annotated[str, Form()] = "none",
    evidence_sha256: Annotated[str, Form()] = "",
    reference: Annotated[UploadFile | None, File()] = None,
    reference_views: Annotated[list[UploadFile] | None, File()] = None,
    reference_asset_ids: Annotated[str, Form()] = "",
    reference_same_product_confirmed: Annotated[bool, Form()] = False,
    reference_structure_confirmed: Annotated[bool, Form()] = False,
) -> dict[str, object] | JSONResponse:
    try:
        completion_request = await _completion_request(
            reference_inbox_store=_optional_reference_inbox_store(request),
            source=source,
            missing_mask=missing_mask,
            category=category,
            part_kind=part_kind,
            material_tags=material_tags,
            case_id=case_id,
            scene_tags=scene_tags,
            evidence_kind=evidence_kind,
            evidence_sha256=evidence_sha256,
            reference=reference,
            reference_views=reference_views,
            reference_asset_ids=reference_asset_ids,
            reference_same_product_confirmed=reference_same_product_confirmed,
            reference_structure_confirmed=reference_structure_confirmed,
        )
        quote = _service(request).preview(completion_request)
        return {
            "request_fingerprint": quote.request_fingerprint,
            "model_version": quote.model_version,
            "maximum_usd": str(quote.maximum_usd),
            "currency": quote.currency,
            "missing_ratio": quote.missing_ratio,
            "similar_lesson_count": quote.similar_lesson_count,
            "provider_enabled": quote.provider_enabled,
            "quality": quote.quality,
            "candidate_count": quote.candidate_count,
            "selection_reason": quote.selection_reason,
            "automatic_fallback": quote.automatic_fallback,
            "preflight_state": quote.preflight_state,
            "preflight_reasons": list(quote.preflight_reasons),
            "context_edge_mean": quote.context_edge_mean,
            "strong_edge_fraction": quote.strong_edge_fraction,
            "context_chroma_mean": quote.context_chroma_mean,
            "strong_chroma_fraction": quote.strong_chroma_fraction,
            "context_chroma_stddev": quote.context_chroma_stddev,
            "publishable": False,
            "requires_human_review": True,
            "provenance": "ai_simulated_missing_region",
        }
    except DomainRuleError as error:
        return _error(error)


@router.post("/execute", response_model=None)
async def execute_missing_part_completion(
    request: Request,
    customer_id: Annotated[UUID, Header(alias="X-Customer-Id")],
    source: Annotated[UploadFile, File()],
    missing_mask: Annotated[UploadFile, File()],
    category: Annotated[str, Form()],
    part_kind: Annotated[str, Form()],
    confirmed_request_fingerprint: Annotated[str, Form()],
    confirmed_maximum_usd: Annotated[Decimal, Form()],
    idempotency_key: Annotated[UUID, Form()],
    material_tags: Annotated[str, Form()] = "",
    case_id: Annotated[str, Form()] = "local-case",
    scene_tags: Annotated[str, Form()] = "",
    evidence_kind: Annotated[str, Form()] = "none",
    evidence_sha256: Annotated[str, Form()] = "",
    reference: Annotated[UploadFile | None, File()] = None,
    reference_views: Annotated[list[UploadFile] | None, File()] = None,
    reference_asset_ids: Annotated[str, Form()] = "",
    reference_same_product_confirmed: Annotated[bool, Form()] = False,
    reference_structure_confirmed: Annotated[bool, Form()] = False,
) -> dict[str, object] | JSONResponse:
    try:
        completion_request = await _completion_request(
            reference_inbox_store=_optional_reference_inbox_store(request),
            source=source,
            missing_mask=missing_mask,
            category=category,
            part_kind=part_kind,
            material_tags=material_tags,
            case_id=case_id,
            scene_tags=scene_tags,
            evidence_kind=evidence_kind,
            evidence_sha256=evidence_sha256,
            reference=reference,
            reference_views=reference_views,
            reference_asset_ids=reference_asset_ids,
            reference_same_product_confirmed=reference_same_product_confirmed,
            reference_structure_confirmed=reference_structure_confirmed,
        )
        candidate = _service(request).execute(
            customer_id,
            completion_request,
            idempotency_key=idempotency_key,
            confirmed_request_fingerprint=confirmed_request_fingerprint,
            confirmed_maximum_usd=confirmed_maximum_usd,
        )
        return {
            "request_fingerprint": candidate.request_fingerprint,
            "candidate_sha256": candidate.content_sha256,
            "model_version": candidate.model_version,
            "simulated_region_sha256": candidate.simulated_region_sha256,
            "review_state": candidate.review_state,
            "review_reasons": list(candidate.review_reasons),
            "structural_review_checklist": candidate.structural_review_checklist,
            "material_change_mean": candidate.material_change_mean,
            "automatic_approval": candidate.automatic_approval,
            "content_url": (
                "/local/style-suite/api/missing-part-completion/candidates/"
                f"{candidate.request_fingerprint}/{candidate.content_sha256}"
            ),
            "publishable": False,
            "requires_human_review": True,
            "provenance": "ai_simulated_missing_region",
        }
    except DomainRuleError as error:
        return _error(error)


@router.get("/candidates/{request_fingerprint}/{candidate_sha256}", response_model=None)
def missing_part_candidate_content(
    request: Request,
    request_fingerprint: str,
    candidate_sha256: str,
    customer_id: Annotated[UUID, Header(alias="X-Customer-Id")],
) -> Response | JSONResponse:
    store = getattr(request.app.state, "missing_part_candidate_store", None)
    try:
        if store is None:
            raise DomainRuleError("missing_part_candidate_not_found")
        content = store.read(customer_id, request_fingerprint, candidate_sha256)
        return Response(
            content,
            media_type="image/png",
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'",
            },
        )
    except DomainRuleError as error:
        return _error(error)


@router.post("/feedback", response_model=None)
async def record_missing_part_feedback(
    request: Request,
    customer_id: Annotated[UUID, Header(alias="X-Customer-Id")],
    request_fingerprint: Annotated[str, Form()],
    candidate_sha256: Annotated[str, Form()],
    category: Annotated[str, Form()],
    part_kind: Annotated[str, Form()],
    material_tags: Annotated[str, Form()],
    decision: Annotated[Literal["accepted", "edited", "rejected"], Form()],
    model_version: Annotated[str, Form()],
    note: Annotated[str, Form()],
    final_image: Annotated[UploadFile | None, File()] = None,
) -> dict[str, object] | JSONResponse:
    candidate_store = getattr(request.app.state, "missing_part_candidate_store", None)
    learning_store = getattr(request.app.state, "missing_part_learning_store", None)
    try:
        if candidate_store is None or not isinstance(
            learning_store, MissingPartLearningStore
        ):
            raise DomainRuleError("missing_part_provider_disabled")
        candidate_store.read(customer_id, request_fingerprint, candidate_sha256)
        review_state = candidate_store.review_state(
            customer_id, request_fingerprint, candidate_sha256
        )
        if decision == "accepted" and review_state == "guardrail_rejected":
            raise DomainRuleError("missing_part_candidate_guardrail_rejected")
        if decision == "edited":
            if final_image is None:
                raise DomainRuleError("missing_part_feedback_invalid")
            final_sha256 = candidate_store.save_reviewed_final(
                customer_id,
                request_fingerprint,
                candidate_sha256,
                await _read_upload(final_image),
            )
        else:
            final_sha256 = candidate_sha256 if decision == "accepted" else None
        feedback = CompletionFeedback.create(
            case_id=f"{customer_id}:{request_fingerprint}",
            request_fingerprint=request_fingerprint,
            category=category.strip()[:64],
            part_kind=part_kind.strip()[:64],
            material_tags=tuple(
                sorted(
                    value.strip() for value in material_tags.split(",") if value.strip()
                )
            ),
            decision=decision,
            candidate_sha256=candidate_sha256,
            final_sha256=final_sha256,
            model_version=model_version.strip()[:128],
            note=note,
        )
        learning_store.append(feedback)
        return {
            "recorded": True,
            "event_id": feedback.event_id,
            "decision": decision,
            "training_state": "case_memory_ready",
            "fine_tuned": False,
        }
    except DomainRuleError as error:
        return _error(error)
