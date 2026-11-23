"""Expand confirmed historical suites without provider or persistence side effects."""

import json
from copy import deepcopy
from decimal import Decimal
from hashlib import sha256
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from opc_style.domain.errors import DomainRuleError
from opc_style.domain.image_generator import ImageGenerator
from opc_style.domain.real_style_eligibility import assess_real_style
from opc_style.domain.style_reference_pack import style_reference_access_token
from opc_style.models import (
    ImageCandidate,
    ProductAsset,
    ProductForegroundVersion,
    ProductIdentityCardVersion,
    SceneStyle,
    StyleCard,
    StyleCardSampleLink,
    StyleCardVersion,
    StyleFieldEvidence,
    StyleReferencePack,
    StyleSample,
    StyleSuiteCompletion,
    StyleSuiteRun,
    StyleSuiteRunItem,
    StyleTemplateSlot,
)
from opc_style.providers.openai_images import REAL_MODEL_GENERATION_MODE
from opc_style.schemas import (
    StyleSuiteBatchCreateRequest,
    StyleSuiteBatchPreviewItem,
    StyleSuiteBatchPreviewRequest,
    StyleSuiteBatchPreviewResponse,
    StyleSuiteBatchStyleSelection,
    StyleSuiteExecutionItemResponse,
    StyleSuiteExecutionResponse,
    StyleSuitePilotScope,
    StyleSuitePlanItem,
    StyleSuitePreviewResponse,
)
from opc_style.services.assets import ProductAssetService
from opc_style.services.image_jobs import (
    ImageGenerationInProgressError,
    ImageGenerationOutcomeUncertainError,
    ImageJobService,
    ImageSourceSelection,
)
from opc_style.services.product_identity_observations import (
    ValidatedProductIdentityEvidence,
    validate_product_identity_card_for_product,
    validate_product_identity_generation_evidence,
)
from opc_style.services.real_photos import AssetContentStore
from opc_style.services.real_pilot_authorization import (
    AuthorizedItemScope,
    RealPilotAuthorizationCapability,
    RealPilotBatchAuthorizationCapability,
    issue_real_pilot_batch_authorization,
)
from opc_style.services.style_reference_packs import StyleReferencePackService
from opc_style.services.style_suite_batch_scope import SignedBatchScope
from opc_style.settings import StyleSuitePlanningSettings


def _validate_reusable_identity_card(
    session: Session,
    content_store: AssetContentStore,
    customer_id: UUID,
    product_id: str,
    card_version_id: UUID,
) -> tuple[ProductIdentityCardVersion, dict[str, object]]:
    try:
        return validate_product_identity_card_for_product(
            session, content_store, customer_id, product_id, card_version_id
        )
    except DomainRuleError as error:
        raise DomainRuleError("product_identity_card_scope_mismatch") from error

StyleSuitePlan = StyleSuitePreviewResponse


class StyleSuiteService:
    """Plan the complete current matrix; execution must revalidate authority."""

    def __init__(
        self,
        session: Session,
        *,
        settings: StyleSuitePlanningSettings | None = None,
        content_store: AssetContentStore | None = None,
        generator: ImageGenerator | None = None,
        allow_synthetic_product_style: bool = False,
        real_pilot_authorization: (
            RealPilotAuthorizationCapability
            | RealPilotBatchAuthorizationCapability
            | None
        ) = None,
        enforce_real_pilot_boundary: bool = False,
        required_style_origin: Literal["customer_upload", "local_demo"] | None = None,
    ) -> None:
        self._session = session
        self._settings = (
            settings if settings is not None else StyleSuitePlanningSettings()
        )
        self._content_store = content_store
        self._generator = generator
        self._allow_synthetic_product_style = allow_synthetic_product_style
        self._real_pilot_authorization = real_pilot_authorization
        self._enforce_real_pilot_boundary = enforce_real_pilot_boundary
        self._required_style_origin = required_style_origin

    def preview_batch(
        self,
        customer_id: UUID,
        request: StyleSuiteBatchPreviewRequest,
        *,
        secret: bytes,
    ) -> StyleSuiteBatchPreviewResponse:
        """Quote an exact first-round matrix without writing or dispatching."""
        if self._content_store is None:
            raise DomainRuleError("style_suite_source_unavailable")
        with self._session.no_autoflush:
            evidence = self._require_identity_evidence(
                customer_id,
                request.product_id,
                request.source_asset_id,
                request.product_identity_card_version_id,
                request.product_identity_observation_run_id,
                request.product_identity_observation_review_id,
            )
            card, _ = _validate_reusable_identity_card(
                self._session,
                self._content_store,
                customer_id,
                request.product_id,
                request.product_identity_card_version_id,
            )
            source = self._session.scalar(
                select(ProductAsset).where(
                    ProductAsset.customer_id == customer_id,
                    ProductAsset.id == request.source_asset_id,
                    ProductAsset.product_id == request.product_id,
                    ProductAsset.confirmed.is_(True),
                    ProductAsset.identity == "original_real_photo",
                )
            )
            if source is None:
                raise DomainRuleError("style_suite_source_unavailable")
            if len(request.styles) > 3:
                raise DomainRuleError("style_suite_batch_scope_exceeded")
            foreground = self._session.scalar(
                select(ProductForegroundVersion)
                .where(
                    ProductForegroundVersion.customer_id == customer_id,
                    ProductForegroundVersion.product_asset_id
                    == request.source_asset_id,
                    ProductForegroundVersion.product_id == request.product_id,
                    ProductForegroundVersion.source_content_sha256
                    == source.content_sha256,
                    ProductForegroundVersion.state == "confirmed",
                )
                .order_by(ProductForegroundVersion.version.desc())
                .limit(1)
            )
            items: list[StyleSuiteBatchPreviewItem] = []
            seen_versions: set[UUID] = set()
            for selection in request.styles:
                if (
                    selection.scene_style_version_id in seen_versions
                    or len(selection.slot_keys) > 2
                    or len(set(selection.slot_keys)) != len(selection.slot_keys)
                ):
                    raise DomainRuleError("style_suite_batch_scope_exceeded")
                seen_versions.add(selection.scene_style_version_id)
                version = self._session.scalar(
                    select(StyleCardVersion).where(
                        StyleCardVersion.customer_id == customer_id,
                        StyleCardVersion.id == selection.scene_style_version_id,
                    )
                )
                if (
                    version is None
                    or version.content.get("historical_style") is not True
                    or version.content.get("stability") != "customer_confirmed"
                    or version.content.get("style_origin") != "customer_upload"
                ):
                    raise DomainRuleError("style_suite_batch_style_unavailable")
                latest_id = self._session.scalar(
                    select(StyleCardVersion.id)
                    .where(
                        StyleCardVersion.customer_id == customer_id,
                        StyleCardVersion.style_card_id == version.style_card_id,
                    )
                    .order_by(StyleCardVersion.version.desc())
                    .limit(1)
                )
                scene = self._session.scalar(
                    select(SceneStyle).where(
                        SceneStyle.customer_id == customer_id,
                        SceneStyle.style_card_id == version.style_card_id,
                        SceneStyle.active.is_(True),
                    )
                )
                if scene is None or latest_id != version.id:
                    raise DomainRuleError("style_suite_batch_style_unavailable")
                pack_row = self._session.scalar(
                    select(StyleReferencePack)
                    .where(
                        StyleReferencePack.customer_id == customer_id,
                        StyleReferencePack.scene_style_version_id == version.id,
                        StyleReferencePack.active.is_(True),
                    )
                    .order_by(StyleReferencePack.created_at.desc())
                    .limit(1)
                )
                if pack_row is None:
                    raise DomainRuleError("style_reference_pack_unavailable")
                pack = StyleReferencePackService(self._session).get(
                    customer_id, pack_row.id
                )
                sealed_slots = version.content.get("slots", [])
                for slot_key in selection.slot_keys:
                    slot = self._session.scalar(
                        select(StyleTemplateSlot).where(
                            StyleTemplateSlot.customer_id == customer_id,
                            StyleTemplateSlot.scene_style_id == scene.id,
                            StyleTemplateSlot.style_card_version_id == version.id,
                            StyleTemplateSlot.slot_key == slot_key,
                            StyleTemplateSlot.active.is_(True),
                        )
                    )
                    if slot is None or not any(
                        row.get("id") == str(slot.id)
                        and row.get("slot_key") == slot_key
                        and row.get("active") is True
                        for row in sealed_slots
                        if isinstance(row, dict)
                    ):
                        raise DomainRuleError("style_suite_batch_slot_unavailable")
                    eligible, blocking_code = self._pixel_preserving_eligibility(
                        foreground=foreground,
                        identity_payload=card.card_payload,
                        slot_key=slot_key,
                        slot_rules=slot.rules,
                    )
                    items.append(
                        StyleSuiteBatchPreviewItem(
                            scene_style_version_id=version.id,
                            style_name=scene.name,
                            slot_key=slot_key,
                            style_reference_pack_id=pack.id,
                            style_reference_manifest_sha256=pack.manifest_sha256,
                            style_reference_count=len(pack.references),
                            style_reference_thumbnail_urls=tuple(
                                "/local/style-suite/api/reference-packs/"
                                f"{pack.id}/samples/{reference.sample_id}/content?access="
                                + style_reference_access_token(
                                    secret,
                                    customer_id,
                                    pack.id,
                                    reference.sample_id,
                                )
                                for reference in pack.references
                            ),
                            item_estimated_usd=self._settings.candidate_estimate_usd,
                            render_mode=(
                                "pixel_preserving_isolated"
                                if eligible
                                else "generative_candidate_only"
                            ),
                            foreground_version_id=(
                                foreground.id
                                if eligible and foreground is not None
                                else None
                            ),
                            pixel_preserving_eligible=eligible,
                            blocking_code=blocking_code,
                            provider_image_count=(
                                len(pack.references)
                                if eligible
                                else len(pack.references) + 1
                            ),
                            background_candidate_count=1 if eligible else 0,
                        )
                    )
            if not items or len(items) > 6:
                raise DomainRuleError("style_suite_batch_scope_exceeded")
            maximum = self._settings.candidate_estimate_usd * len(items)
            if maximum > 3:
                raise DomainRuleError("style_suite_batch_cost_exceeded")
            payload: dict[str, object] = {
                "customer_id": str(customer_id),
                "product_id": request.product_id,
                "source_asset_id": str(request.source_asset_id),
                "source_content_sha256": source.content_sha256,
                "product_identity_card_version_id": str(card.id),
                "product_identity_card_sha256": card.card_sha256,
                "product_identity_observation_run_id": str(
                    evidence.observation_run_id
                ),
                "product_identity_observation_result_sha256": (
                    evidence.observation_result_sha256
                ),
                "product_identity_observation_review_id": (
                    str(evidence.review_id) if evidence.review_id is not None else None
                ),
                "candidate_count": len(items),
                "maximum_estimated_cost_usd": str(maximum),
                "currency": "USD",
                "publishable": False,
                "items": [item.model_dump(mode="json") for item in items],
            }
            signed = SignedBatchScope.issue(secret, payload)
            return StyleSuiteBatchPreviewResponse(
                preview_token=signed.token,
                candidate_count=len(items),
                maximum_estimated_cost_usd=maximum,
                items=tuple(items),
            )

    def _require_identity_evidence(
        self,
        customer_id: UUID,
        product_id: str,
        source_asset_id: UUID,
        card_version_id: UUID,
        observation_run_id: UUID | None,
        review_id: UUID | None,
    ) -> ValidatedProductIdentityEvidence:
        if observation_run_id is None:
            raise DomainRuleError("product_identity_observation_required")
        if self._content_store is None:
            raise DomainRuleError("product_identity_observation_evidence_invalid")
        return validate_product_identity_generation_evidence(
            self._session,
            self._content_store,
            customer_id,
            product_id,
            source_asset_id,
            card_version_id,
            observation_run_id,
            review_id,
        )

    def _pixel_preserving_eligibility(
        self,
        *,
        foreground: ProductForegroundVersion | None,
        identity_payload: dict[str, object],
        slot_key: str,
        slot_rules: dict[str, object],
    ) -> tuple[bool, str | None]:
        """Choose the safe mode explicitly; never silently weaken the gate."""
        camera_angle = slot_rules.get("camera_angle")
        if slot_key == "wear" or camera_angle not in {None, "front", "overhead"}:
            return False, "pixel_preserving_ineligible"
        if foreground is None:
            return False, "pixel_preserving_mask_required"
        assert self._content_store is not None
        if not (
            self._content_store.verify_product_foreground(
                foreground.customer_id, foreground.id, foreground.foreground_sha256
            )
            and self._content_store.verify_product_mask(
                foreground.customer_id, foreground.id, foreground.mask_sha256
            )
        ):
            return False, "pixel_preserving_mask_required"
        blocked_visibility = {"cropped", "partially_occluded", "back_side_hidden"}
        raw_visibility = identity_payload.get("visibility_limits")
        visibility_values: set[str] = set()
        if isinstance(raw_visibility, dict):
            raw_values = raw_visibility.get("value")
            if isinstance(raw_values, list):
                visibility_values = {
                    str(value) for value in raw_values if isinstance(value, str)
                }
        if blocked_visibility & visibility_values or blocked_visibility & set(
            foreground.quality_flags
        ):
            return False, "pixel_preserving_ineligible"
        return True, None

    def create_batch(
        self,
        customer_id: UUID,
        request: StyleSuiteBatchCreateRequest,
        *,
        secret: bytes,
    ) -> StyleSuiteExecutionResponse:
        """Persist and run exactly the signed first-round cells once."""
        if self._content_store is None or self._generator is None:
            raise DomainRuleError("style_suite_generation_unavailable")
        signed = SignedBatchScope.verify(secret, request.preview_token)
        payload = signed.payload
        if payload.get("customer_id") != str(customer_id):
            raise DomainRuleError("style_suite_preview_expired")
        try:
            raw_items = payload["items"]
            if not isinstance(raw_items, list):
                raise TypeError
            if any(
                isinstance(raw, dict)
                and raw.get("blocking_code") == "pixel_preserving_mask_required"
                for raw in raw_items
            ):
                raise DomainRuleError("pixel_preserving_mask_required")
            grouped: dict[UUID, list[str]] = {}
            for raw in raw_items:
                if not isinstance(raw, dict):
                    raise TypeError
                version_id = UUID(str(raw["scene_style_version_id"]))
                grouped.setdefault(version_id, []).append(str(raw["slot_key"]))
            preview_request = StyleSuiteBatchPreviewRequest(
                product_id=str(payload["product_id"]),
                source_asset_id=UUID(str(payload["source_asset_id"])),
                product_identity_card_version_id=UUID(
                    str(payload["product_identity_card_version_id"])
                ),
                product_identity_observation_run_id=UUID(
                    str(payload["product_identity_observation_run_id"])
                ),
                product_identity_observation_review_id=(
                    UUID(str(payload["product_identity_observation_review_id"]))
                    if payload.get("product_identity_observation_review_id") is not None
                    else None
                ),
                styles=tuple(
                    StyleSuiteBatchStyleSelection(
                        scene_style_version_id=version_id,
                        slot_keys=tuple(slot_keys),
                    )
                    for version_id, slot_keys in grouped.items()
                ),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise DomainRuleError("style_suite_preview_expired") from error
        current = self.preview_batch(customer_id, preview_request, secret=secret)
        current_payload = SignedBatchScope.verify(secret, current.preview_token).payload
        if current_payload != payload or request.confirmed_maximum_usd != Decimal(
            str(payload.get("maximum_estimated_cost_usd"))
        ):
            raise DomainRuleError("style_suite_preview_expired")
        generator_ceiling = getattr(self._generator, "max_run_usd", None)
        if (
            isinstance(generator_ceiling, Decimal)
            and request.confirmed_maximum_usd > generator_ceiling
        ):
            raise DomainRuleError("style_suite_batch_cost_exceeded")
        preview_sha = _canonical_sha256(payload)
        authorization_sha = _canonical_sha256(
            {
                "preview_payload_sha256": preview_sha,
                "idempotency_key": request.idempotency_key,
                "confirmed_maximum_usd": str(request.confirmed_maximum_usd),
            }
        )
        run = self._session.scalar(
            select(StyleSuiteRun).where(
                StyleSuiteRun.customer_id == customer_id,
                StyleSuiteRun.idempotency_key == request.idempotency_key,
            )
        )
        if run is None:
            run = StyleSuiteRun(
                id=uuid4(),
                customer_id=customer_id,
                product_id=preview_request.product_id,
                source_asset_id=preview_request.source_asset_id,
                source_content_sha256=str(payload["source_content_sha256"]),
                product_identity_card_version_id=(
                    preview_request.product_identity_card_version_id
                ),
                product_identity_card_sha256=str(
                    payload["product_identity_card_sha256"]
                ),
                product_identity_observation_run_id=(
                    preview_request.product_identity_observation_run_id
                ),
                product_identity_observation_result_sha256=str(
                    payload["product_identity_observation_result_sha256"]
                ),
                product_identity_observation_review_id=(
                    preview_request.product_identity_observation_review_id
                ),
                style_count=len(grouped),
                slot_count=len(raw_items),
                child_job_count=len(raw_items),
                candidate_count=len(raw_items),
                candidate_estimate_usd=self._settings.candidate_estimate_usd,
                maximum_estimated_cost_usd=request.confirmed_maximum_usd,
                cost_basis="local_planning_estimate",
                currency="USD",
                real_generation_enabled=True,
                preview_payload_sha256=preview_sha,
                confirmed_maximum_usd=request.confirmed_maximum_usd,
                candidate_count_per_item=1,
                authorization_manifest_sha256=authorization_sha,
                pilot_scope={"batch_authorization_manifest_sha256": authorization_sha},
                idempotency_key=request.idempotency_key,
                state="planned",
            )
            self._session.add(run)
            self._session.flush()
            for raw in raw_items:
                version_id = UUID(str(raw["scene_style_version_id"]))
                slot_key = str(raw["slot_key"])
                version = self._session.get(StyleCardVersion, version_id)
                if version is None or version.customer_id != customer_id:
                    raise DomainRuleError("style_suite_preview_expired")
                scene = self._session.scalar(
                    select(SceneStyle).where(
                        SceneStyle.customer_id == customer_id,
                        SceneStyle.style_card_id == version.style_card_id,
                    )
                )
                slot = self._session.scalar(
                    select(StyleTemplateSlot).where(
                        StyleTemplateSlot.customer_id == customer_id,
                        StyleTemplateSlot.style_card_version_id == version_id,
                        StyleTemplateSlot.slot_key == slot_key,
                    )
                )
                if scene is None or slot is None:
                    raise DomainRuleError("style_suite_preview_expired")
                self._session.add(
                    StyleSuiteRunItem(
                        customer_id=customer_id,
                        run_id=run.id,
                        scene_style_id=scene.id,
                        scene_style_version_id=version.id,
                        style_name=str(raw["style_name"]),
                        style_version=version.version,
                        slot_id=slot.id,
                        slot_key=slot.slot_key,
                        style_snapshot=deepcopy(version.content),
                        slot_snapshot={
                            "id": str(slot.id),
                            "customer_id": str(customer_id),
                            "scene_style_id": str(scene.id),
                            "style_card_version_id": str(version.id),
                            "slot_key": slot.slot_key,
                            "label": slot.label,
                            "rules": deepcopy(slot.rules),
                            "active": slot.active,
                            "position": slot.position,
                        },
                        style_reference_pack_id=UUID(
                            str(raw["style_reference_pack_id"])
                        ),
                        style_reference_manifest_sha256=str(
                            raw["style_reference_manifest_sha256"]
                        ),
                        item_authorized_usd=Decimal(str(raw["item_estimated_usd"])),
                        state=(
                            "pixel_background_planned"
                            if raw.get("render_mode") == "pixel_preserving_isolated"
                            else "planned"
                        ),
                    )
                )
            self._session.commit()
        elif any(
            (
                run.preview_payload_sha256 != preview_sha,
                run.authorization_manifest_sha256 != authorization_sha,
                run.confirmed_maximum_usd != request.confirmed_maximum_usd,
                run.candidate_count_per_item != 1,
            )
        ):
            raise DomainRuleError("style_suite_idempotency_conflict")
        run_items = self._items(run)
        self._real_pilot_authorization = issue_real_pilot_batch_authorization(
            customer_id=customer_id,
            product_id=run.product_id,
            source_asset_id=run.source_asset_id,
            run_idempotency_key=run.idempotency_key,
            authorization_manifest_sha256=authorization_sha,
            authorized_maximum_usd=request.confirmed_maximum_usd,
            item_scopes=tuple(
                AuthorizedItemScope(
                    scene_style_version_id=item.scene_style_version_id,
                    slot_key=item.slot_key,
                    style_reference_manifest_sha256=str(
                        item.style_reference_manifest_sha256
                    ),
                    item_authorized_usd=Decimal(item.item_authorized_usd or 0),
                )
                for item in run_items
            ),
        )
        for item in run_items:
            if item.state in {"succeeded", "approved", "uncertain"}:
                continue
            if item.state == "pixel_background_planned":
                continue
            self._execute_item(run, item)
        return self._response(run)

    def create(
        self,
        customer_id: UUID,
        product_id: str,
        source_asset_id: UUID,
        *,
        idempotency_key: str,
        pilot_scope: StyleSuitePilotScope | None = None,
        product_identity_card_version_id: UUID | None = None,
        product_identity_observation_run_id: UUID | None = None,
        product_identity_observation_review_id: UUID | None = None,
    ) -> StyleSuiteExecutionResponse:
        """Persist one immutable matrix and execute only unfinished children."""
        if not idempotency_key or len(idempotency_key) > 255:
            raise DomainRuleError("style_suite_idempotency_key_invalid")
        if (
            self._requires_real_pilot_capability()
            and self._real_pilot_authorization is None
        ):
            raise DomainRuleError("real_image_pilot_authorization_required")
        identity_card: tuple[ProductIdentityCardVersion, dict[str, object]] | None = (
            None
        )
        if product_identity_card_version_id is not None:
            if self._content_store is None:
                raise DomainRuleError("product_identity_card_scope_mismatch")
            identity_card = _validate_reusable_identity_card(
                self._session,
                self._content_store,
                customer_id,
                product_id,
                product_identity_card_version_id,
            )
        elif self._requires_real_pilot_capability():
            raise DomainRuleError("confirmed_product_identity_card_required")
        identity_evidence: ValidatedProductIdentityEvidence | None = None
        if (
            self._requires_real_pilot_capability()
            or product_identity_observation_run_id is not None
        ):
            assert product_identity_card_version_id is not None
            identity_evidence = self._require_identity_evidence(
                customer_id,
                product_id,
                source_asset_id,
                product_identity_card_version_id,
                product_identity_observation_run_id,
                product_identity_observation_review_id,
            )
        if self._requires_real_pilot_capability():
            if pilot_scope is None:
                raise DomainRuleError("real_image_pilot_authorization_required")
            assert self._real_pilot_authorization is not None
            if not isinstance(
                self._real_pilot_authorization, RealPilotAuthorizationCapability
            ) or not self._real_pilot_authorization.authorizes_suite(
                customer_id=customer_id,
                product_id=product_id,
                source_asset_id=source_asset_id,
                scene_style_version_id=pilot_scope.scene_style_version_id,
                slot_key=pilot_scope.slot_key,
                idempotency_key=idempotency_key,
                generator_max_usd=getattr(self._generator, "max_run_usd", None),
            ):
                raise DomainRuleError("real_image_pilot_authorization_required")
        run = self._session.scalar(
            select(StyleSuiteRun).where(
                StyleSuiteRun.customer_id == customer_id,
                StyleSuiteRun.idempotency_key == idempotency_key,
            )
        )
        if run is None:
            plan = self.preview(
                customer_id,
                product_id,
                source_asset_id,
                pilot_scope=pilot_scope,
            )
            run = StyleSuiteRun(
                id=uuid4(),
                **plan.model_dump(exclude={"items"}),
                idempotency_key=idempotency_key,
                pilot_scope=self._scope_snapshot(pilot_scope),
                product_identity_card_version_id=(
                    identity_card[0].id if identity_card is not None else None
                ),
                product_identity_card_sha256=(
                    identity_card[0].card_sha256 if identity_card is not None else None
                ),
                product_identity_observation_run_id=(
                    identity_evidence.observation_run_id
                    if identity_evidence is not None
                    else None
                ),
                product_identity_observation_result_sha256=(
                    identity_evidence.observation_result_sha256
                    if identity_evidence is not None
                    else None
                ),
                product_identity_observation_review_id=(
                    identity_evidence.review_id
                    if identity_evidence is not None
                    else None
                ),
            )
            self._session.add(run)
            for planned in plan.items:
                self._session.add(
                    StyleSuiteRunItem(
                        customer_id=customer_id,
                        run_id=run.id,
                        **planned.model_dump(),
                    )
                )
            try:
                self._session.commit()
            except IntegrityError:
                self._session.rollback()
                run = self._session.scalar(
                    select(StyleSuiteRun).where(
                        StyleSuiteRun.customer_id == customer_id,
                        StyleSuiteRun.idempotency_key == idempotency_key,
                    )
                )
                if run is None:
                    raise
        if run.product_id != product_id or run.source_asset_id != source_asset_id:
            raise DomainRuleError("style_suite_idempotency_conflict")
        if run.product_identity_card_version_id != product_identity_card_version_id or (
            identity_card is not None
            and run.product_identity_card_sha256 != identity_card[0].card_sha256
        ):
            raise DomainRuleError("style_suite_idempotency_conflict")
        if self._requires_real_pilot_capability() and (
            run.product_identity_observation_run_id
            != product_identity_observation_run_id
            or run.product_identity_observation_review_id
            != product_identity_observation_review_id
            or identity_evidence is None
            or run.product_identity_observation_result_sha256
            != identity_evidence.observation_result_sha256
        ):
            raise DomainRuleError("style_suite_idempotency_conflict")
        self._require_matching_replay_scope(run, pilot_scope)
        if self._real_pilot_authorization is not None and (
            pilot_scope is None or len(self._items(run)) != 1
        ):
            raise DomainRuleError("real_image_pilot_authorization_required")
        if not run.real_generation_enabled:
            return self._response(run)
        if self._content_store is None or self._generator is None:
            raise DomainRuleError("style_suite_generation_unavailable")
        for item in self._items(run):
            if item.state in {"succeeded", "approved", "uncertain"}:
                continue
            self._execute_item(run, item)
        return self._response(run)

    @staticmethod
    def _scope_snapshot(
        pilot_scope: StyleSuitePilotScope | None,
    ) -> dict[str, str] | None:
        if pilot_scope is None:
            return None
        return pilot_scope.model_dump(mode="json")

    def _require_matching_replay_scope(
        self,
        run: StyleSuiteRun,
        pilot_scope: StyleSuitePilotScope | None,
    ) -> None:
        if run.pilot_scope != self._scope_snapshot(pilot_scope):
            raise DomainRuleError("style_suite_idempotency_conflict")
        if pilot_scope is None:
            return
        items = self._items(run)
        if len(items) != 1 or (
            items[0].scene_style_version_id != pilot_scope.scene_style_version_id
            or items[0].slot_key != pilot_scope.slot_key
        ):
            raise DomainRuleError("style_suite_idempotency_conflict")

    def get(self, customer_id: UUID, run_id: UUID) -> StyleSuiteExecutionResponse:
        run = self._session.scalar(
            select(StyleSuiteRun).where(
                StyleSuiteRun.customer_id == customer_id, StyleSuiteRun.id == run_id
            )
        )
        if run is None:
            raise DomainRuleError("style_suite_run_not_found")
        return self._response(run)

    def _execute_item(self, run: StyleSuiteRun, item: StyleSuiteRunItem) -> None:
        assert self._content_store is not None and self._generator is not None
        identity_card: tuple[ProductIdentityCardVersion, dict[str, object]] | None = (
            None
        )
        if run.product_identity_card_version_id is not None:
            identity_card = _validate_reusable_identity_card(
                self._session,
                self._content_store,
                run.customer_id,
                run.product_id,
                run.product_identity_card_version_id,
            )
            if identity_card[0].card_sha256 != run.product_identity_card_sha256:
                raise DomainRuleError("product_identity_card_scope_mismatch")
        if run.product_identity_observation_run_id is not None:
            if run.product_identity_card_version_id is None:
                raise DomainRuleError("product_identity_observation_evidence_invalid")
            evidence = self._require_identity_evidence(
                run.customer_id,
                run.product_id,
                run.source_asset_id,
                run.product_identity_card_version_id,
                run.product_identity_observation_run_id,
                run.product_identity_observation_review_id,
            )
            if (
                evidence.observation_result_sha256
                != run.product_identity_observation_result_sha256
            ):
                raise DomainRuleError("product_identity_observation_evidence_invalid")
        jobs = ImageJobService(
            self._session,
            self._content_store,
            self._generator,
            allow_synthetic_product_style=self._allow_synthetic_product_style,
            real_pilot_authorization=self._real_pilot_authorization,
            real_pilot_scope_key=run.idempotency_key,
            enforce_real_pilot_boundary=self._enforce_real_pilot_boundary,
            allow_reusable_identity_card=(
                run.product_identity_observation_run_id is not None
            ),
        )
        key = (
            f"suite:{run.id}:{item.scene_style_version_id}:"
            f"{item.slot_key}:{run.source_content_sha256}"
        )
        if item.style_reference_manifest_sha256 is not None:
            key = f"{key}:{item.style_reference_manifest_sha256}"
        was_failed_retry = item.output_job_id is not None and item.state == "failed"
        try:
            if was_failed_retry:
                assert item.output_job_id is not None
                output = jobs.retry_failed(run.customer_id, item.output_job_id)
            else:
                output = jobs.create(
                    run.customer_id,
                    output_type="product_style",
                    sources=(
                        ImageSourceSelection(run.product_id, run.source_asset_id),
                    ),
                    scene_style_id=item.scene_style_id,
                    style_card_version_id=item.scene_style_version_id,
                    output_ratio=str(item.slot_snapshot["rules"]["aspect_ratio"]),
                    idempotency_key=key,
                    slot_key=item.slot_key,
                    product_identity_card_version_id=(
                        identity_card[0].id if identity_card is not None else None
                    ),
                    product_identity_card_sha256=(
                        identity_card[0].card_sha256
                        if identity_card is not None
                        else None
                    ),
                    product_identity_card=(
                        identity_card[1] if identity_card is not None else None
                    ),
                    candidate_count=run.candidate_count_per_item or 3,
                )
            item.output_job_id = output.job.id
            if output.job.status in {"generation_failed", "cleanup_abandoned"}:
                item.state = "failed"
                item.failure_reason = output.job.failure_reason
            elif output.job.status in {"queued", "generating", "cleanup_pending"}:
                item.state = "uncertain"
                item.failure_reason = output.job.failure_reason
            else:
                item.state = "succeeded"
                item.failure_reason = None
            self._session.commit()
            self._persist_aggregate_state(run)
        except (ImageGenerationInProgressError, ImageGenerationOutcomeUncertainError):
            self._session.rollback()
            attached = self._session.get(StyleSuiteRunItem, item.id)
            if attached is not None:
                attached.state = "uncertain"
                attached.failure_reason = "image_generation_outcome_uncertain"
                self._session.commit()
                self._persist_aggregate_state(run)
        except DomainRuleError as error:
            self._session.rollback()
            attached = self._session.get(StyleSuiteRunItem, item.id)
            if attached is not None:
                attached.state = "failed"
                attached.failure_reason = error.code
                self._session.commit()
                self._persist_aggregate_state(run)

    def _requires_real_pilot_capability(self) -> bool:
        return (
            self._enforce_real_pilot_boundary
            and self._generator is not None
            and getattr(self._generator, "generation_mode", None)
            == REAL_MODEL_GENERATION_MODE
        )

    def _persist_aggregate_state(self, run: StyleSuiteRun) -> None:
        if run.state == "completed":
            return
        states = [item.state for item in self._items(run)]
        succeeded = states.count("succeeded") + states.count("approved")
        failed = states.count("failed")
        uncertain = states.count("uncertain")
        run.state = (
            "awaiting_review"
            if succeeded == len(states)
            else "needs_reconciliation"
            if uncertain
            else "partial_failure"
            if succeeded and failed
            else "failed"
            if failed == len(states)
            else "running"
        )
        self._session.commit()

    def _items(self, run: StyleSuiteRun) -> list[StyleSuiteRunItem]:
        return list(
            self._session.scalars(
                select(StyleSuiteRunItem)
                .where(
                    StyleSuiteRunItem.customer_id == run.customer_id,
                    StyleSuiteRunItem.run_id == run.id,
                )
                .order_by(
                    StyleSuiteRunItem.style_name,
                    StyleSuiteRunItem.scene_style_version_id,
                    StyleSuiteRunItem.slot_key,
                )
            )
        )

    def _response(self, run: StyleSuiteRun) -> StyleSuiteExecutionResponse:
        items = self._items(run)
        candidate_counts: dict[UUID, int] = {
            item.id: (
                0
                if item.output_job_id is None
                else int(
                    self._session.scalar(
                        select(func.count(ImageCandidate.id)).where(
                            ImageCandidate.customer_id == run.customer_id,
                            ImageCandidate.output_job_id == item.output_job_id,
                        )
                    )
                    or 0
                )
            )
            for item in items
        }
        succeeded = sum(item.state in {"succeeded", "approved"} for item in items)
        failed = sum(item.state == "failed" for item in items)
        uncertain = sum(item.state == "uncertain" for item in items)
        pending = len(items) - succeeded - failed - uncertain
        has_completion = (
            self._session.scalar(
                select(func.count(StyleSuiteCompletion.id)).where(
                    StyleSuiteCompletion.customer_id == run.customer_id,
                    StyleSuiteCompletion.run_id == run.id,
                )
            )
            == 1
        )
        state = (
            "completed"
            if run.state == "completed" and has_completion
            else "awaiting_review"
            if succeeded == len(items)
            else "needs_reconciliation"
            if uncertain
            else "partial_failure"
            if succeeded and failed
            else "failed"
            if failed == len(items)
            else "running"
            if succeeded or pending < len(items)
            else "planned"
        )
        return StyleSuiteExecutionResponse(
            id=run.id,
            customer_id=run.customer_id,
            product_id=run.product_id,
            source_asset_id=run.source_asset_id,
            source_content_sha256=run.source_content_sha256,
            product_identity_card_version_id=run.product_identity_card_version_id,
            product_identity_card_sha256=run.product_identity_card_sha256,
            product_identity_observation_run_id=(
                run.product_identity_observation_run_id
            ),
            product_identity_observation_result_sha256=(
                run.product_identity_observation_result_sha256
            ),
            product_identity_observation_review_id=(
                run.product_identity_observation_review_id
            ),
            idempotency_key=run.idempotency_key,
            state=state,
            style_count=run.style_count,
            slot_count=run.slot_count,
            child_job_count=len(items),
            candidate_count=sum(candidate_counts.values()),
            successful_child_count=succeeded,
            failed_child_count=failed,
            uncertain_child_count=uncertain,
            pending_child_count=pending,
            items=tuple(
                StyleSuiteExecutionItemResponse(
                    **{
                        field: getattr(item, field)
                        for field in (
                            "id",
                            "scene_style_id",
                            "scene_style_version_id",
                            "style_name",
                            "style_version",
                            "slot_id",
                            "slot_key",
                            "style_snapshot",
                            "slot_snapshot",
                            "state",
                            "output_job_id",
                            "failure_reason",
                            "fidelity_state",
                            "selected_candidate_id",
                        )
                    },
                    fidelity_flags=tuple(item.fidelity_flags or ()),
                    candidate_count=candidate_counts.get(item.id, 0),
                )
                for item in items
            ),
        )

    def preview(
        self,
        customer_id: UUID,
        product_id: str,
        source_asset_id: UUID,
        *,
        pilot_scope: StyleSuitePilotScope | None = None,
    ) -> StyleSuitePlan:
        # Reading a plan must not accidentally flush unrelated pending work.
        with self._session.no_autoflush:
            return self._preview(
                customer_id,
                product_id,
                source_asset_id,
                pilot_scope=pilot_scope,
            )

    def _preview(
        self,
        customer_id: UUID,
        product_id: str,
        source_asset_id: UUID,
        *,
        pilot_scope: StyleSuitePilotScope | None = None,
    ) -> StyleSuitePlan:
        source = self._session.scalar(
            select(ProductAsset).where(
                ProductAsset.customer_id == customer_id,
                ProductAsset.product_id == product_id,
                ProductAsset.id == source_asset_id,
            )
        )
        if source is None or source.identity != "original_real_photo":
            raise DomainRuleError("style_suite_source_unavailable")
        if (
            not ProductAssetService(self._session)
            .readiness(customer_id, source)
            .generation_ready
        ):
            raise DomainRuleError("style_suite_source_not_ready")
        scenes = self._session.scalars(
            select(SceneStyle)
            .join(
                StyleCard,
                StyleCard.id == SceneStyle.style_card_id,
            )
            .where(
                SceneStyle.customer_id == customer_id,
                SceneStyle.active.is_(True),
                StyleCard.customer_id == customer_id,
                StyleCard.active.is_(True),
                StyleCard.kind == "scene",
            )
            .order_by(SceneStyle.name, SceneStyle.id)
        )
        items: list[StyleSuitePlanItem] = []
        for scene in scenes:
            version = self._session.scalar(
                select(StyleCardVersion)
                .where(
                    StyleCardVersion.customer_id == customer_id,
                    StyleCardVersion.style_card_id == scene.style_card_id,
                )
                .order_by(StyleCardVersion.version.desc())
                .limit(1)
            )
            if version is None or version.content.get("historical_style") is not True:
                continue
            if (
                pilot_scope is not None
                and version.id != pilot_scope.scene_style_version_id
            ):
                continue
            version_origin = version.content.get("style_origin")
            if self._required_style_origin is not None:
                if (
                    scene.style_origin != self._required_style_origin
                    or version_origin != self._required_style_origin
                ):
                    continue
            elif version_origin != scene.style_origin:
                raise DomainRuleError("style_suite_style_unconfirmed")
            # Edits retain the original customer-confirmed historical marker.
            if version.content.get("stability") != "customer_confirmed":
                raise DomainRuleError("style_suite_style_unconfirmed")
            samples = self._require_evidence(
                customer_id,
                version.id,
                required_style_origin=self._required_style_origin,
            )
            slots = list(
                self._session.scalars(
                    select(StyleTemplateSlot)
                    .where(
                        StyleTemplateSlot.customer_id == customer_id,
                        StyleTemplateSlot.scene_style_id == scene.id,
                        StyleTemplateSlot.style_card_version_id == version.id,
                    )
                    .order_by(StyleTemplateSlot.slot_key)
                )
            )
            sealed = version.content.get("slots", [])
            if not slots or len(slots) != len(sealed):
                raise DomainRuleError("style_suite_slots_unavailable")
            if (
                self._enforce_real_pilot_boundary
                and self._required_style_origin == "customer_upload"
            ):
                evidence = list(
                    self._session.scalars(
                        select(StyleFieldEvidence).where(
                            StyleFieldEvidence.customer_id == customer_id,
                            StyleFieldEvidence.style_card_version_id == version.id,
                        )
                    )
                )
                eligibility = assess_real_style(
                    version.content, evidence, len(samples), slots
                )
                if not eligibility.eligible:
                    if pilot_scope is None:
                        continue
                    raise DomainRuleError(eligibility.reason_codes[0])
            for slot in slots:
                snapshot = next(
                    (item for item in sealed if item.get("id") == str(slot.id)), None
                )
                if snapshot is None or snapshot != {
                    "id": str(slot.id),
                    "customer_id": str(customer_id),
                    "scene_style_id": str(scene.id),
                    "style_card_version_id": str(version.id),
                    "slot_key": slot.slot_key,
                    "label": slot.label,
                    "rules": slot.rules,
                    "active": slot.active,
                    "position": slot.position,
                }:
                    raise DomainRuleError("style_suite_slots_unavailable")
                if slot.active:
                    items.append(
                        StyleSuitePlanItem(
                            scene_style_id=scene.id,
                            scene_style_version_id=version.id,
                            style_name=scene.name,
                            style_version=version.version,
                            slot_id=slot.id,
                            slot_key=slot.slot_key,
                            style_snapshot=deepcopy(version.content),
                            slot_snapshot=deepcopy(snapshot),
                        )
                    )
        if pilot_scope is not None:
            items = [
                item
                for item in items
                if item.scene_style_version_id == pilot_scope.scene_style_version_id
                and item.slot_key == pilot_scope.slot_key
            ]
            if len(items) != 1:
                raise DomainRuleError("style_suite_pilot_scope_unavailable")
        elif not items:
            raise DomainRuleError("style_suite_no_active_styles")
        count = len(items)
        return StyleSuitePlan(
            customer_id=customer_id,
            product_id=product_id,
            source_asset_id=source.id,
            source_content_sha256=source.content_sha256,
            style_count=len({item.scene_style_id for item in items}),
            slot_count=count,
            child_job_count=count,
            candidate_count=count * 3,
            candidate_estimate_usd=self._settings.candidate_estimate_usd,
            maximum_estimated_cost_usd=self._settings.candidate_estimate_usd
            * count
            * 3,
            real_generation_enabled=self._settings.real_generation_enabled,
            items=tuple(items),
        )

    def _require_evidence(
        self,
        customer_id: UUID,
        version_id: UUID,
        *,
        required_style_origin: Literal["customer_upload", "local_demo"] | None = None,
    ) -> list[StyleSample]:
        links = list(
            self._session.scalars(
                select(StyleCardSampleLink).where(
                    StyleCardSampleLink.customer_id == customer_id,
                    StyleCardSampleLink.style_card_version_id == version_id,
                )
            )
        )
        if not links:
            raise DomainRuleError("style_suite_style_permission_unavailable")
        samples: list[StyleSample] = []
        for link in links:
            sample = self._session.scalar(
                select(StyleSample)
                .where(
                    StyleSample.customer_id == customer_id,
                    StyleSample.id == link.style_sample_id,
                )
                .execution_options(populate_existing=True)
            )
            if (
                sample is None
                or sample.withdrawn_at is not None
                or not sample.has_complete_content_seal
                or (
                    required_style_origin == "customer_upload"
                    and sample.consent_provenance == "local_synthetic_acceptance"
                )
                or (
                    required_style_origin == "local_demo"
                    and sample.consent_provenance != "local_synthetic_acceptance"
                )
            ):
                raise DomainRuleError("style_suite_style_permission_unavailable")
            samples.append(sample)
        return samples


def _canonical_sha256(payload: object) -> str:
    return sha256(
        json.dumps(
            payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
