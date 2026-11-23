"""Shared local run projection and signed candidate-content access."""

from __future__ import annotations

import base64
import hmac
from hashlib import sha256
from typing import Literal, cast
from uuid import UUID

from fastapi import HTTPException, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from opc_style.domain.image_generator import (
    DETERMINISTIC_LOCAL_ADAPTER_VERSION,
    SYNTHETIC_TEST_GENERATION_MODE,
)
from opc_style.domain.product_identity import (
    ProductIdentityDraft,
    identity_card_sha256,
    normalize_identity_draft,
)
from opc_style.domain.style_reference_pack import style_reference_access_token
from opc_style.models import (
    AuditEvent,
    ImageCandidate,
    ImageOutputJob,
    ImageReview,
    ProductAsset,
    ProductIdentityCardVersion,
    StyleSuiteCandidateFidelity,
    StyleSuiteRun,
    StyleSuiteRunItem,
)
from opc_style.providers.openai_images import REAL_MODEL_GENERATION_MODE
from opc_style.schemas import (
    LocalStyleSuiteBatchCell,
    LocalStyleSuiteBatchRow,
    LocalStyleSuiteBatchRunResponse,
    LocalStyleSuiteReviewCard,
    LocalStyleSuiteRunResponse,
    StyleSuiteExecutionResponse,
)
from opc_style.services.real_photos import AssetContentStore, AssetContentStoreError
from opc_style.services.style_reference_packs import StyleReferencePackService
from opc_style.services.style_suites import StyleSuiteService

_IDENTITY_CATEGORY_ZH = {
    "bracelet": "手链",
    "necklace": "项链",
    "ring": "戒指",
    "earrings": "耳饰",
    "pendant": "吊坠",
    "anklet": "脚链",
    "brooch": "胸针",
    "other": "其他品类",
}
_IDENTITY_COLOR_ZH = {
    "black": "黑色",
    "white": "白色",
    "gray": "灰色",
    "red": "红色",
    "orange": "橙色",
    "yellow": "黄色",
    "green": "绿色",
    "blue": "蓝色",
    "purple": "紫色",
    "pink": "粉色",
    "brown": "棕色",
    "gold_tone": "金色调",
    "silver_tone": "银色调",
    "multicolor": "多色",
}
_IDENTITY_SHAPE_ZH = {
    "round_bead": "圆珠",
    "square_bead": "方珠",
    "oval": "椭圆",
    "teardrop": "水滴形",
    "rectangular": "矩形",
    "irregular": "异形",
    "chain_link": "链节",
    "cabochon": "弧面",
    "faceted": "切面",
    "other": "其他形状",
}


def _identity_summary_zh(card: ProductIdentityDraft) -> str:
    """Summarize only controlled visible facts for the browser run header."""
    parts: list[str] = []
    if card.category.certainty == "observed":
        parts.append(_IDENTITY_CATEGORY_ZH[str(card.category.value)])
    if card.primary_colors.certainty == "observed":
        colors = cast(tuple[str, ...], card.primary_colors.value)
        parts.append("、".join(_IDENTITY_COLOR_ZH[value] for value in colors))
    if card.component_shapes.certainty == "observed":
        shapes = cast(tuple[str, ...], card.component_shapes.value)
        parts.append("、".join(_IDENTITY_SHAPE_ZH[value] for value in shapes))
    if card.visible_component_count.certainty == "observed":
        parts.append(f"可见组件 {card.visible_component_count.value} 个")
    return " · ".join(parts) or "已确认，原图该部分看不清"


def project_local_run(
    session: Session,
    *,
    customer_id: UUID,
    run_id: UUID,
    preview_secret: bytes,
    candidate_url_prefix: str,
) -> LocalStyleSuiteRunResponse:
    """Load one customer-owned run and project it for the local browser."""
    run = StyleSuiteService(session).get(customer_id, run_id)
    return build_review_projection(
        session,
        run=run,
        preview_secret=preview_secret,
        candidate_url_prefix=candidate_url_prefix,
    )


def project_local_batch_run(
    session: Session,
    *,
    customer_id: UUID,
    run_id: UUID,
    preview_secret: bytes,
    candidate_url_prefix: str,
) -> LocalStyleSuiteBatchRunResponse:
    """Project a persisted batch as style rows and slot cells after refresh."""
    execution = StyleSuiteService(session).get(customer_id, run_id)
    review = build_review_projection(
        session,
        run=execution,
        preview_secret=preview_secret,
        candidate_url_prefix=candidate_url_prefix,
    )
    cards_by_job: dict[UUID, list[LocalStyleSuiteReviewCard]] = {}
    for card in review.review_cards:
        cards_by_job.setdefault(card.job_id, []).append(card)
    source_token = source_access_token(
        preview_secret, customer_id, execution.id, execution.source_asset_id
    )
    source_url = (
        f"{candidate_url_prefix.rstrip('/')}/{execution.id}/source/content"
        f"?access={source_token}"
    )
    rows: dict[UUID, dict[str, object]] = {}
    persisted_items = list(
        session.scalars(
            select(StyleSuiteRunItem)
            .where(
                StyleSuiteRunItem.customer_id == customer_id,
                StyleSuiteRunItem.run_id == run_id,
            )
            .order_by(StyleSuiteRunItem.created_at, StyleSuiteRunItem.id)
        )
    )
    for item in persisted_items:
        if (
            item.style_reference_pack_id is None
            or item.style_reference_manifest_sha256 is None
        ):
            raise HTTPException(status_code=404)
        pack = StyleReferencePackService(session).get(
            customer_id, item.style_reference_pack_id
        )
        thumbnail_urls = tuple(
            "/local/style-suite/api/reference-packs/"
            f"{pack.id}/samples/{reference.sample_id}/content?access="
            + style_reference_access_token(
                preview_secret, customer_id, pack.id, reference.sample_id
            )
            for reference in pack.references
        )
        row = rows.setdefault(
            item.scene_style_version_id,
            {
                "scene_style_version_id": item.scene_style_version_id,
                "style_name": item.style_name,
                "style_version": item.style_version,
                "style_reference_manifest_sha256": (
                    item.style_reference_manifest_sha256
                ),
                "style_reference_thumbnail_urls": thumbnail_urls,
                "slots": [],
            },
        )
        candidates = tuple(
            cards_by_job.get(item.output_job_id, [])
            if item.output_job_id is not None
            else []
        )
        output_job = (
            session.get(ImageOutputJob, item.output_job_id)
            if item.output_job_id is not None
            else None
        )
        render_mode: Literal[
            "generative_candidate_only", "pixel_preserving_isolated"
        ] = (
            "pixel_preserving_isolated"
            if item.state == "pixel_background_planned"
            or (
                output_job is not None
                and output_job.request_metadata.get("generation_mode")
                == "pixel_preserving_isolated"
            )
            else "generative_candidate_only"
        )
        cast(list[LocalStyleSuiteBatchCell], row["slots"]).append(
            LocalStyleSuiteBatchCell(
                item_id=item.id,
                slot_key=item.slot_key,
                state=item.state,
                render_mode=render_mode,
                failure_reason=item.failure_reason,
                source_preview_url=source_url,
                style_reference_thumbnail_urls=thumbnail_urls,
                provider_product_attachment_count=(
                    0 if render_mode == "pixel_preserving_isolated" else 1
                ),
                provider_style_reference_attachment_count=len(pack.references),
                candidates=candidates,
            )
        )
    matrix = tuple(
        LocalStyleSuiteBatchRow(
            scene_style_version_id=version_id,
            style_name=cast(str, row["style_name"]),
            style_version=cast(int, row["style_version"]),
            style_reference_manifest_sha256=cast(
                str, row["style_reference_manifest_sha256"]
            ),
            style_reference_thumbnail_urls=cast(
                tuple[str, ...], row["style_reference_thumbnail_urls"]
            ),
            slots=tuple(cast(list[LocalStyleSuiteBatchCell], row["slots"])),
        )
        for version_id, row in rows.items()
    )
    return LocalStyleSuiteBatchRunResponse(
        id=execution.id,
        state=execution.state,
        matrix=matrix,
        candidate_count=sum(
            len(cell.candidates) for row in matrix for cell in row.slots
        ),
    )


def build_review_projection(
    session: Session,
    *,
    run: StyleSuiteExecutionResponse,
    preview_secret: bytes,
    candidate_url_prefix: str,
) -> LocalStyleSuiteRunResponse:
    """Build the complete review-card view without obtaining a runtime implicitly."""
    customer_id = run.customer_id
    created_at = session.scalar(
        select(StyleSuiteRun.created_at).where(
            StyleSuiteRun.customer_id == customer_id,
            StyleSuiteRun.id == run.id,
        )
    )
    if created_at is None:
        raise HTTPException(status_code=404)
    source_token = source_access_token(
        preview_secret, customer_id, run.id, run.source_asset_id
    )
    source_preview_url = (
        f"{candidate_url_prefix.rstrip('/')}/{run.id}/source/content"
        f"?access={source_token}"
    )
    cards: list[LocalStyleSuiteReviewCard] = []
    generation_adapter = DETERMINISTIC_LOCAL_ADAPTER_VERSION
    generation_mode: Literal[
        "synthetic_local_deterministic",
        "real_model_local_pilot",
        "pixel_preserving_isolated",
    ] = "synthetic_local_deterministic"
    outbound_attempts = 0
    identity_card_version: int | None = None
    identity_card_summary_zh: str | None = None
    if run.product_identity_card_version_id is not None:
        card_row = session.scalar(
            select(ProductIdentityCardVersion).where(
                ProductIdentityCardVersion.id == run.product_identity_card_version_id,
                ProductIdentityCardVersion.customer_id == customer_id,
                ProductIdentityCardVersion.product_id == run.product_id,
            )
        )
        if card_row is None or card_row.card_sha256 != run.product_identity_card_sha256:
            raise HTTPException(status_code=404)
        identity_card = normalize_identity_draft(card_row.card_payload)
        if identity_card_sha256(identity_card) != card_row.card_sha256:
            raise HTTPException(status_code=404)
        identity_card_version = card_row.version
        identity_card_summary_zh = _identity_summary_zh(identity_card)
    audit_events = list(
        session.scalars(select(AuditEvent).where(AuditEvent.customer_id == customer_id))
    )
    fidelities = {
        row.image_candidate_id: row
        for row in session.scalars(
            select(StyleSuiteCandidateFidelity).where(
                StyleSuiteCandidateFidelity.customer_id == customer_id
            )
        )
    }
    reviews = {
        row.image_candidate_id: row
        for row in session.scalars(
            select(ImageReview).where(ImageReview.customer_id == customer_id)
        )
    }
    for item in run.items:
        if item.output_job_id is None:
            continue
        job = session.get(ImageOutputJob, item.output_job_id)
        if job is not None:
            generation_adapter = job.adapter_version
            stored_mode = job.request_metadata.get("generation_mode")
            if stored_mode == REAL_MODEL_GENERATION_MODE:
                generation_mode = "real_model_local_pilot"
            elif stored_mode == SYNTHETIC_TEST_GENERATION_MODE:
                generation_mode = "synthetic_local_deterministic"
            elif stored_mode == "pixel_preserving_isolated":
                generation_mode = "pixel_preserving_isolated"
            else:
                raise HTTPException(status_code=404)
            for event in audit_events:
                if event.payload.get("job_id") != str(job.id):
                    continue
                attempts = event.payload.get("outbound_attempts", 0)
                if isinstance(attempts, int) and attempts >= 0:
                    outbound_attempts = max(outbound_attempts, attempts)
        candidates = session.scalars(
            select(ImageCandidate)
            .where(
                ImageCandidate.customer_id == customer_id,
                ImageCandidate.output_job_id == item.output_job_id,
            )
            .order_by(ImageCandidate.sequence_number)
        )
        for candidate in candidates:
            attempts = candidate.artifact_metadata.get("outbound_attempts", 0)
            if isinstance(attempts, int) and attempts >= 0:
                outbound_attempts = max(outbound_attempts, attempts)
            fidelity = fidelities.get(candidate.id)
            review = reviews.get(candidate.id)
            selected = item.selected_candidate_id == candidate.id
            preview_token = candidate_access_token(
                preview_secret,
                customer_id,
                run.id,
                candidate.id,
            )
            cards.append(
                LocalStyleSuiteReviewCard(
                    job_id=item.output_job_id,
                    candidate_id=candidate.id,
                    preview_url=(
                        f"{candidate_url_prefix.rstrip('/')}/{run.id}/candidates/"
                        f"{candidate.id}/content?access={preview_token}"
                    ),
                    source_preview_url=source_preview_url,
                    style_name=item.style_name,
                    slot_key=item.slot_key,
                    sequence_number=candidate.sequence_number,
                    candidate_state=(
                        "selected"
                        if selected
                        else "rejected"
                        if review
                        else "awaiting_review"
                    ),
                    fidelity_state=cast(
                        Literal["green", "yellow", "red", "unknown"] | None,
                        fidelity.fidelity_state if fidelity else None,
                    ),
                    fidelity_flags=tuple(fidelity.fidelity_flags) if fidelity else (),
                    fidelity_summary_zh=fidelity.summary_zh if fidelity else None,
                    selected=selected,
                )
            )
    return LocalStyleSuiteRunResponse(
        run_id=run.id,
        created_at=created_at,
        workspaces=("historical_styles", "generate_suite"),
        state=run.state,
        progress={
            "successful": run.successful_child_count,
            "failed": run.failed_child_count,
            "needs_reconciliation": run.uncertain_child_count,
            "pending": run.pending_child_count,
        },
        review_cards=tuple(cards),
        generation_adapter=generation_adapter,
        generation_mode=generation_mode,
        outbound_attempts=outbound_attempts,
        identity_card_version=identity_card_version,
        identity_card_summary_zh=identity_card_summary_zh,
    )


def candidate_access_token(
    secret: bytes,
    customer_id: UUID,
    run_id: UUID,
    candidate_id: UUID,
) -> str:
    """Sign an exact customer/run/candidate lineage tuple."""
    payload = customer_id.bytes
    signature = hmac.new(
        secret, payload + run_id.bytes + candidate_id.bytes, sha256
    ).digest()
    return base64.urlsafe_b64encode(payload + signature).decode().rstrip("=")


def source_access_token(
    secret: bytes,
    customer_id: UUID,
    run_id: UUID,
    source_asset_id: UUID,
) -> str:
    """Sign the original source bound to one customer-owned suite run."""
    payload = customer_id.bytes
    signature = hmac.new(
        secret, b"source" + payload + run_id.bytes + source_asset_id.bytes, sha256
    ).digest()
    return base64.urlsafe_b64encode(payload + signature).decode().rstrip("=")


def resolve_source_customer(
    secret: bytes,
    run_id: UUID,
    source_asset_id: UUID,
    token: str,
) -> UUID | None:
    """Resolve a customer only when the signed source lineage matches exactly."""
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        customer_id = UUID(bytes=raw[:16])
    except (ValueError, TypeError):
        return None
    expected = source_access_token(secret, customer_id, run_id, source_asset_id)
    return customer_id if hmac.compare_digest(token, expected) else None


def read_source_response(
    session: Session,
    store: AssetContentStore,
    *,
    secret: bytes,
    run_id: UUID,
    access: str,
    header_customer_id: UUID | None,
) -> Response:
    """Read the sealed product identity image for an exact suite lineage."""
    run = session.scalar(select(StyleSuiteRun).where(StyleSuiteRun.id == run_id))
    if run is None:
        raise HTTPException(status_code=404)
    customer_id = resolve_source_customer(secret, run_id, run.source_asset_id, access)
    if (
        customer_id is None
        or customer_id != run.customer_id
        or (header_customer_id is not None and header_customer_id != customer_id)
    ):
        raise HTTPException(status_code=404)
    asset = session.scalar(
        select(ProductAsset).where(
            ProductAsset.id == run.source_asset_id,
            ProductAsset.customer_id == customer_id,
            ProductAsset.content_sha256 == run.source_content_sha256,
        )
    )
    if asset is None:
        raise HTTPException(status_code=404)
    try:
        content = store.read_original(customer_id, asset.id, asset.content_sha256)
    except AssetContentStoreError as error:
        raise HTTPException(status_code=404) from error
    return Response(
        content=content,
        media_type=asset.mime_type,
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


def resolve_candidate_customer(
    secret: bytes,
    run_id: UUID,
    candidate_id: UUID,
    token: str,
) -> UUID | None:
    """Resolve a customer only when the candidate token matches exactly."""
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        customer_id = UUID(bytes=raw[:16])
    except (ValueError, TypeError):
        return None
    expected = candidate_access_token(secret, customer_id, run_id, candidate_id)
    return customer_id if hmac.compare_digest(token, expected) else None


def read_candidate_response(
    session: Session,
    store: AssetContentStore,
    *,
    secret: bytes,
    run_id: UUID,
    candidate_id: UUID,
    access: str,
    header_customer_id: UUID | None,
    propagate_store_error: bool = False,
) -> Response:
    """Read only a signed candidate in the caller-owned run lineage."""
    customer_id = resolve_candidate_customer(secret, run_id, candidate_id, access)
    if customer_id is None or (
        header_customer_id is not None and header_customer_id != customer_id
    ):
        raise HTTPException(status_code=404)
    lineage = session.execute(
        select(ImageCandidate, StyleSuiteRunItem)
        .join(
            StyleSuiteRunItem,
            StyleSuiteRunItem.output_job_id == ImageCandidate.output_job_id,
        )
        .where(
            ImageCandidate.id == candidate_id,
            ImageCandidate.customer_id == customer_id,
            StyleSuiteRunItem.customer_id == customer_id,
            StyleSuiteRunItem.run_id == run_id,
        )
    ).one_or_none()
    if lineage is None:
        raise HTTPException(status_code=404)
    candidate, item = lineage
    assert item.output_job_id is not None
    try:
        if (
            candidate.identity == "pixel_preserving_composition"
            and candidate.artifact_path
            == f"private:pixel-composition:{candidate.id}"
        ):
            content = store.read_pixel_composition(
                customer_id, candidate.id, candidate.content_sha256
            )
        else:
            content = store.read_derivative(
                customer_id,
                item.output_job_id,
                candidate.sequence_number,
                candidate.content_sha256,
            )
    except AssetContentStoreError as error:
        if propagate_store_error:
            raise
        raise HTTPException(status_code=404) from error
    headers = {
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    }
    if candidate.artifact_metadata.get("synthetic_test_only") is True:
        headers["X-Synthetic-Artifact"] = "true"
    return Response(content=content, media_type=candidate.mime_type, headers=headers)
