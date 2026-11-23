"""Customer-scoped local synthetic generation orchestration.

This executor is deliberately single-process. A process-local per-job lock
serializes generation, publication, and database commit. Multi-process recovery
requires a future durable production queue and object store; this module fails
closed when the original process-local store instance is unavailable.
"""

import re
from _thread import RLock
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from io import BytesIO
from threading import Lock
from typing import cast
from uuid import UUID, uuid4
from weakref import WeakValueDictionary

from PIL import Image, UnidentifiedImageError
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from opc_style.domain.errors import DomainRuleError
from opc_style.domain.image_generator import (
    DETERMINISTIC_LOCAL_ADAPTER_VERSION,
    MAX_STYLE_REFERENCES,
    SYNTHETIC_CANDIDATE_COUNT,
    SYNTHETIC_TEST_GENERATION_MODE,
    DeterministicLocalImageGenerator,
    GeneratedCandidate,
    GenerationRequest,
    GenerationSource,
    ImageGenerationError,
    ImageGenerator,
)
from opc_style.domain.image_normalizer import NormalizedImage, NormalizedPayload
from opc_style.domain.output_rules import validate_product_count
from opc_style.domain.style_evidence import LOCAL_SYNTHETIC_CONSENT_PROVENANCES
from opc_style.models import (
    AuditEvent,
    ImageCandidate,
    ImageOutputJob,
    ProductAsset,
    ProductAssetLink,
    ProductIdentityCardVersion,
    RealImagePilotAuthorization,
    SceneStyle,
    StyleCard,
    StyleCardSampleLink,
    StyleCardVersion,
    StyleFieldEvidence,
    StyleSample,
    StyleTemplateSlot,
)
from opc_style.providers.openai_images import (
    OPENAI_IMAGE_ADAPTER_VERSION,
    REAL_MODEL_GENERATION_MODE,
    OpenAIImageGenerator,
)
from opc_style.repositories import ProductImageRepository
from opc_style.services.asset_store_recovery import namespace_is_accepted
from opc_style.services.product_identity import validate_confirmed_product_identity_card
from opc_style.services.product_identity_observations import (
    validate_product_identity_card_for_product,
)
from opc_style.services.real_photos import (
    AssetContentStore,
    AssetContentStoreError,
    DerivativeTarget,
)
from opc_style.services.real_pilot_authorization import (
    RealPilotAuthorizationCapability,
    RealPilotBatchAuthorizationCapability,
)
from opc_style.services.style_consent import coordinate_style_consent
from opc_style.services.style_reference_packs import StyleReferencePackService


class ImageOutputNotFoundError(Exception):
    """Conceal missing, foreign, or mismatched customer-owned identities."""


class ImageGenerationOutcomeUncertainError(Exception):
    """Preserve artifacts when a committed generation outcome cannot be verified."""


class ImageGenerationInProgressError(Exception):
    """Tell an idempotent caller that another live worker owns generation."""


class _ProviderDispatchEvidenceUncertainError(Exception):
    """Stop before publication when a paid dispatch cannot be durably recorded."""

    def __init__(self, outbound_attempts: int) -> None:
        self.outbound_attempts = outbound_attempts
        super().__init__("provider_dispatch_evidence_uncertain")


class _ImageGenerationClaimLostError(Exception):
    """Fence a stale worker without touching the current worker's artifacts."""


@dataclass(frozen=True)
class ImageSourceSelection:
    """One customer-selected product and its explicit reference asset."""

    product_id: str
    asset_id: UUID


@dataclass(frozen=True)
class ImageGenerationOutput:
    """One job plus immutable candidates and replay status."""

    job: ImageOutputJob
    candidates: tuple[ImageCandidate, ...]
    created: bool


@dataclass(frozen=True)
class _StylePins:
    scene: SceneStyle
    scene_card: StyleCard
    scene_version: StyleCardVersion
    main_version: StyleCardVersion | None


@dataclass(frozen=True)
class _AdapterSnapshot:
    adapter_version: str
    generation_mode: str
    publishable: bool = False


@dataclass(frozen=True)
class _StoreSnapshot:
    identity: str
    namespace: str


@dataclass
class _LocalLockEntry:
    lock: RLock
    users: int = 0


EXPECTED_ARTIFACT_SEQUENCES = (1, 2, 3)
_IDENTITY_CARD_REQUIREMENT_KEY = "product_identity_card_requirement"
_SEALED_IDENTITY_CARD_V1 = "sealed_v1"
_CARDLESS_NON_PILOT_V1 = "cardless_non_pilot_v1"
_LOCAL_LOCKS_GUARD = Lock()
_LOCAL_JOB_LOCKS: dict[UUID, _LocalLockEntry] = {}
_LOCAL_IDEMPOTENCY_LOCKS: dict[tuple[UUID, str], _LocalLockEntry] = {}
_LOCAL_JOB_STORES: WeakValueDictionary[UUID, AssetContentStore] = WeakValueDictionary()


class ImageJobService:
    """Generate exactly three synthetic candidates or persist a failed job with none."""

    def __init__(
        self,
        session: Session,
        content_store: AssetContentStore,
        generator: ImageGenerator,
        *,
        allow_synthetic_product_style: bool = False,
        real_pilot_authorization: (
            RealPilotAuthorizationCapability
            | RealPilotBatchAuthorizationCapability
            | None
        ) = None,
        real_pilot_scope_key: str | None = None,
        enforce_real_pilot_boundary: bool = False,
        allow_reusable_identity_card: bool = False,
    ) -> None:
        self._session = session
        self._repository = ProductImageRepository(session)
        self._content_store = content_store
        self._generator = generator
        self._allow_synthetic_product_style = allow_synthetic_product_style
        self._real_pilot_capability = real_pilot_authorization
        self._real_pilot_scope_key = real_pilot_scope_key
        self._authorized_real_pilot_usd: Decimal | None = None
        self._enforce_real_pilot_boundary = enforce_real_pilot_boundary
        self._allow_reusable_identity_card = allow_reusable_identity_card

    def create(
        self,
        customer_id: UUID,
        *,
        output_type: str,
        sources: tuple[ImageSourceSelection, ...],
        scene_style_id: UUID,
        style_card_version_id: UUID,
        output_ratio: str,
        idempotency_key: str,
        slot_key: str = "legacy",
        product_identity_card_version_id: UUID | None = None,
        product_identity_card_sha256: str | None = None,
        product_identity_card: dict[str, object] | None = None,
        wear_presentation: dict[str, object] | None = None,
        candidate_count: int = SYNTHETIC_CANDIDATE_COUNT,
    ) -> ImageGenerationOutput:
        """Create or replay one immutable, customer-owned generation request."""
        adapter = self._pin_adapter()
        if not 1 <= candidate_count <= SYNTHETIC_CANDIDATE_COUNT:
            raise DomainRuleError("generation_candidate_count_invalid")
        expected_sequences = tuple(range(1, candidate_count + 1))
        if (
            output_type == "product_style"
            and adapter.generation_mode == SYNTHETIC_TEST_GENERATION_MODE
            and not self._allow_synthetic_product_style
        ):
            raise DomainRuleError("real_image_provider_disabled")
        store = self._pin_store()
        product_ids = validate_product_count(
            output_type, (source.product_id for source in sources)
        )
        if output_type == "product_style" and len(sources) != 1:
            raise DomainRuleError("generation_source_identity_count")
        existing = self._repository.get_output_job_by_idempotency_key(
            customer_id, idempotency_key
        )
        markerless_legacy_replay = (
            existing is not None
            and existing.request_metadata.get(_IDENTITY_CARD_REQUIREMENT_KEY) is None
            and all(
                existing.request_metadata.get(key) is None
                for key in (
                    "product_identity_card_version_id",
                    "product_identity_card_sha256",
                    "product_identity_card",
                )
            )
        )
        identity_card = self._validate_requested_identity_card(
            customer_id,
            output_type=output_type,
            sources=sources,
            card_version_id=product_identity_card_version_id,
            card_sha256=product_identity_card_sha256,
            card_payload=product_identity_card,
            required=(
                adapter.generation_mode == REAL_MODEL_GENERATION_MODE
                and output_type in {"product_style", "wear"}
                and not markerless_legacy_replay
            ),
        )
        if (
            adapter.generation_mode == REAL_MODEL_GENERATION_MODE
            and self._enforce_real_pilot_boundary
        ):
            self._authorized_real_pilot_usd = self._require_real_pilot_authorization(
                customer_id,
                output_type=output_type,
                sources=sources,
                style_card_version_id=style_card_version_id,
                slot_key=slot_key,
                idempotency_key=self._real_pilot_scope_key or idempotency_key,
            )
        canonical_output_type = (
            "multi_product"
            if output_type in {"multi_product", "multi-product"}
            else output_type
        )
        if output_type != "wear" and wear_presentation is not None:
            raise DomainRuleError("wear_presentation_only_for_wear_output")
        requested_facts: dict[str, object] = {
            "sources": [
                {"product_id": source.product_id, "asset_id": str(source.asset_id)}
                for source in sources
            ],
            "scene_style_id": str(scene_style_id),
            "style_card_version_id": str(style_card_version_id),
            "output_ratio": output_ratio,
            "generation_mode": adapter.generation_mode,
            "synthetic_test_only": adapter.generation_mode
            == SYNTHETIC_TEST_GENERATION_MODE,
            "publishable": False,
            "candidate_count": candidate_count,
        }
        if identity_card is not None:
            requested_facts.update(
                product_identity_card_version_id=str(identity_card[0].id),
                product_identity_card_sha256=identity_card[0].card_sha256,
                product_identity_card=deepcopy(identity_card[1]),
            )
        if output_type == "wear":
            requested_facts["wear_presentation"] = deepcopy(
                wear_presentation
                or {
                    "position": "auto",
                    "skin_tone": "medium",
                    "gender_presentation": "neutral",
                    "age_presentation": "adult",
                    "styling": "commercial",
                }
            )
        if slot_key != "legacy":
            requested_facts["slot_key"] = slot_key
        if existing is not None:
            return self._resolve_replay(
                existing, canonical_output_type, requested_facts, adapter, store
            )
        assets = self._require_ready_assets(customer_id, sources)
        style_pins = self._require_active_style_pins(
            customer_id, scene_style_id, style_card_version_id
        )
        if self._authorized_real_pilot_usd is not None:
            self._require_customer_upload_style(customer_id, style_pins)
        style_facts = self._style_generation_facts(customer_id, style_pins, slot_key)
        if (
            adapter.generation_mode == REAL_MODEL_GENERATION_MODE
            and self._enforce_real_pilot_boundary
        ):
            self._authorized_real_pilot_usd = self._require_real_pilot_authorization(
                customer_id,
                output_type=output_type,
                sources=sources,
                style_card_version_id=style_card_version_id,
                slot_key=slot_key,
                idempotency_key=self._real_pilot_scope_key or idempotency_key,
                style_reference_manifest_sha256=cast(
                    str | None, style_facts.get("style_reference_manifest_sha256")
                ),
            )
        if (
            "slot_rules" in style_facts
            and cast(dict[str, object], style_facts["slot_rules"]).get("aspect_ratio")
            != output_ratio
        ):
            raise DomainRuleError("style_slot_ratio_mismatch")
        request_metadata = {
            **requested_facts,
            _IDENTITY_CARD_REQUIREMENT_KEY: (
                _SEALED_IDENTITY_CARD_V1
                if identity_card is not None
                else _CARDLESS_NON_PILOT_V1
            ),
            "source_product_ids": list(product_ids),
            "source_asset_ids": [str(asset.id) for asset in assets],
            "main_style_card_version_id": (
                str(style_pins.main_version.id) if style_pins.main_version else None
            ),
            **style_facts,
            "scene_style_card_id": str(style_pins.scene_card.id),
            "artifact_label": (
                "synthetic local test image; not production AI output"
                if adapter.generation_mode == SYNTHETIC_TEST_GENERATION_MODE
                else "real model local pilot image; not publishable"
            ),
        }
        if canonical_output_type in {"product_style", "wear"}:
            request_metadata["product_asset_id"] = str(assets[0].id)
        job_id = uuid4()
        cleanup_capability = self._content_store.issue_cleanup_capability(
            customer_id, job_id, expected_sequences
        )
        _remember_local_store(job_id, self._content_store)
        job = ImageOutputJob(
            id=job_id,
            customer_id=customer_id,
            output_type=canonical_output_type,
            style_card_version_id=style_pins.scene_version.id,
            scene_style_id=style_pins.scene.id,
            output_ratio=output_ratio,
            adapter_version=adapter.adapter_version,
            artifact_store_identity=store.identity,
            artifact_store_namespace=store.namespace,
            artifact_store_capability=cleanup_capability,
            expected_artifact_sequences=list(expected_sequences),
            status="queued",
            failure_reason=None,
            request_metadata=request_metadata,
            idempotency_key=idempotency_key,
        )
        created_job_id: UUID | None = None
        try:
            self._session.add(job)
            self._session.flush()
            created_job_id = job.id
            if self._authorized_real_pilot_usd is not None:
                self._session.add(
                    RealImagePilotAuthorization(
                        customer_id=customer_id,
                        output_job_id=job.id,
                        budget_scope=f"real_image_pilot:{job.id}",
                        authorized_usd=self._authorized_real_pilot_usd,
                    )
                )
            for position, asset in enumerate(assets, start=1):
                self._session.add(
                    ProductAssetLink(
                        customer_id=customer_id,
                        product_asset_id=asset.id,
                        output_job_id=job.id,
                        position=position,
                    )
                )
            self._record_audit(
                customer_id,
                "image_output.generation_requested",
                {
                    "job_id": str(job.id),
                    "output_type": canonical_output_type,
                    "source_product_ids": list(product_ids),
                    "source_asset_ids": [str(asset.id) for asset in assets],
                    "adapter_version": adapter.adapter_version,
                    "generation_mode": adapter.generation_mode,
                    "style_card_version_id": str(style_pins.scene_version.id),
                    "main_style_card_version_id": request_metadata[
                        "main_style_card_version_id"
                    ],
                    "scene_style_id": str(style_pins.scene.id),
                    "style_reference_manifest_sha256": request_metadata.get(
                        "style_reference_manifest_sha256"
                    ),
                    "style_reference_selection_version": request_metadata.get(
                        "style_reference_selection_version"
                    ),
                    "style_reference_selected_count": request_metadata.get(
                        "style_reference_selected_count"
                    ),
                },
            )
            self._session.commit()
        except Exception:
            self._session.rollback()
            winner = self._reload_job_by_idempotency_key(customer_id, idempotency_key)
            if winner is None:
                _forget_local_store(job.id, self._content_store)
                raise
            if winner.id != job.id:
                _forget_local_store(job.id, self._content_store)
            attached = self._repository.get_output_job_by_idempotency_key(
                customer_id, idempotency_key
            )
            if attached is None:
                raise ImageGenerationOutcomeUncertainError
            return self._resolve_replay(
                attached,
                canonical_output_type,
                requested_facts,
                adapter,
                store,
                created=winner.id == created_job_id,
            )
        return self._execute_or_replay(job, adapter, store, created=True)

    def _validate_requested_identity_card(
        self,
        customer_id: UUID,
        *,
        output_type: str,
        sources: tuple[ImageSourceSelection, ...],
        card_version_id: UUID | None,
        card_sha256: str | None,
        card_payload: dict[str, object] | None,
        required: bool,
    ) -> tuple[ProductIdentityCardVersion, dict[str, object]] | None:
        facts = (card_version_id, card_sha256, card_payload)
        if all(value is None for value in facts):
            if required:
                raise DomainRuleError("confirmed_product_identity_card_required")
            return None
        if any(value is None for value in facts) or (
            output_type == "product_style" and len(sources) != 1
        ):
            raise DomainRuleError("product_identity_card_scope_mismatch")
        assert card_version_id is not None
        assert card_sha256 is not None
        assert card_payload is not None
        if output_type != "product_style":
            raise DomainRuleError("product_identity_card_scope_mismatch")
        if self._allow_reusable_identity_card:
            card, canonical_payload = validate_product_identity_card_for_product(
                self._session,
                self._content_store,
                customer_id,
                sources[0].product_id,
                card_version_id,
            )
        else:
            card, canonical_payload = validate_confirmed_product_identity_card(
                self._session,
                self._content_store,
                customer_id,
                sources[0].product_id,
                sources[0].asset_id,
                card_version_id,
            )
        if card.card_sha256 != card_sha256 or canonical_payload != card_payload:
            raise DomainRuleError("product_identity_card_scope_mismatch")
        return card, canonical_payload

    def _require_real_pilot_authorization(
        self,
        customer_id: UUID,
        *,
        output_type: str,
        sources: tuple[ImageSourceSelection, ...],
        style_card_version_id: UUID,
        slot_key: str,
        idempotency_key: str,
        style_reference_manifest_sha256: str | None = None,
    ) -> Decimal:
        authorization = self._real_pilot_capability
        if (
            output_type != "product_style"
            or len(sources) != 1
            or authorization is None
            or not authorization.authorizes_image(
                customer_id=customer_id,
                product_id=sources[0].product_id,
                source_asset_id=sources[0].asset_id,
                scene_style_version_id=style_card_version_id,
                slot_key=slot_key,
                idempotency_key=idempotency_key,
                generator_max_usd=getattr(self._generator, "max_run_usd", None),
                style_reference_manifest_sha256=style_reference_manifest_sha256,
            )
        ):
            raise DomainRuleError("real_image_pilot_authorization_required")
        return authorization.authorized_usd_for(style_card_version_id, slot_key)

    def _require_customer_upload_style(
        self, customer_id: UUID, pins: _StylePins
    ) -> None:
        if (
            pins.scene.style_origin != "customer_upload"
            or pins.scene_version.content.get("style_origin") != "customer_upload"
        ):
            raise DomainRuleError("real_pilot_customer_upload_style_required")
        rows = self._all_style_reference_rows(customer_id, pins.scene_version.id)
        if any(
            row.consent_provenance in LOCAL_SYNTHETIC_CONSENT_PROVENANCES
            for row in rows
        ):
            raise DomainRuleError("real_pilot_customer_upload_style_required")

    def get(self, customer_id: UUID, job_id: UUID) -> ImageGenerationOutput:
        """Return scene or multi-product history only to its owning customer."""
        job = self._repository.get_output_job(customer_id, job_id)
        if job is None or job.output_type not in {
            "scene",
            "multi_product",
            "product_style",
            "wear",
        }:
            raise ImageOutputNotFoundError
        candidates = tuple(self._repository.list_candidates(customer_id, job.id))
        if job.status == "machine_check":
            self._verify_machine_check(job, candidates, self._pin_store())
        return ImageGenerationOutput(job=job, candidates=candidates, created=False)

    def retry_failed(self, customer_id: UUID, job_id: UUID) -> ImageGenerationOutput:
        """Retry one failed sealed suite child without changing its identity facts."""
        adapter = self._pin_adapter()
        store = self._pin_store()
        job = self._repository.get_output_job(customer_id, job_id)
        if (
            job is None
            or job.output_type != "product_style"
            or job.status != "generation_failed"
            or job.request_metadata.get("slot_key", "legacy") == "legacy"
        ):
            raise DomainRuleError("style_suite_child_not_retryable")
        with _local_job_lock(job.id):
            current = self._reload_attached_job(customer_id, job.id)
            if current.status != "generation_failed":
                return self._resolve_replay_locked(
                    current,
                    current.output_type,
                    _requested_facts_from_job(current),
                    adapter,
                    store,
                    created=False,
                )
            if not self._store_metadata_matches(current, store):
                raise ImageGenerationOutcomeUncertainError
            request = self._generation_request_for_dispatch(current, adapter)
            _remember_local_store(current.id, self._content_store)
            current.status = "generating"
            current.failure_reason = None
            current.generation_claim_token = str(uuid4())
            current.generation_lease_expires_at = datetime.now(UTC) + timedelta(
                minutes=5
            )
            self._session.commit()
            self._reserve_real_pilot_dispatch(current, adapter)
            return self._generate_all(
                customer_id,
                current,
                request,
                adapter,
                store,
                created=False,
            )

    def regenerate_from_job(
        self, customer_id: UUID, job_id: UUID, *, idempotency_key: str
    ) -> ImageGenerationOutput:
        """Repeat sealed source/style selections without accepting caller overrides."""
        job = self._repository.get_output_job(customer_id, job_id)
        if job is None:
            raise ImageOutputNotFoundError
        if (
            job.status != "customer_rejected"
            or job.output_type
            not in {"scene", "multi_product", "product_style", "wear"}
            or job.scene_style_id is None
            or job.style_card_version_id is None
        ):
            raise DomainRuleError("review_job_cannot_regenerate")
        links = self._repository.list_asset_links(customer_id, job.id)
        sources: list[ImageSourceSelection] = []
        for link in links:
            asset = self._repository.get_asset(customer_id, link.product_asset_id)
            if asset is None:
                raise ImageOutputNotFoundError
            sources.append(ImageSourceSelection(asset.product_id, asset.id))
        if (
            not sources
            or job.request_metadata.get("sources")
            != [
                {"product_id": source.product_id, "asset_id": str(source.asset_id)}
                for source in sources
            ]
            or job.request_metadata.get("source_asset_ids")
            != [str(source.asset_id) for source in sources]
            or job.request_metadata.get("source_product_ids")
            != [source.product_id for source in sources]
        ):
            raise DomainRuleError("review_job_source_seal_mismatch")
        # Already generated results retain their historical pins even when a
        # scene is later disabled; create() validates and replays the same key.
        if (
            self._repository.get_output_job_by_idempotency_key(
                customer_id, idempotency_key
            )
            is None
        ):
            pins = self._require_active_style_pins(
                customer_id, job.scene_style_id, job.style_card_version_id
            )
            if job.request_metadata.get("main_style_card_version_id") != (
                str(pins.main_version.id) if pins.main_version else None
            ) or job.request_metadata.get("scene_style_card_id") != str(
                pins.scene_card.id
            ):
                raise DomainRuleError("review_job_style_seal_mismatch")
        return self.create(
            customer_id,
            output_type=job.output_type,
            sources=tuple(sources),
            scene_style_id=job.scene_style_id,
            style_card_version_id=job.style_card_version_id,
            output_ratio=job.output_ratio,
            idempotency_key=idempotency_key,
            slot_key=cast(str, job.request_metadata.get("slot_key", "legacy")),
            candidate_count=int(
                job.request_metadata.get("candidate_count", SYNTHETIC_CANDIDATE_COUNT)
            ),
        )

    def _generate_all(
        self,
        customer_id: UUID,
        job: ImageOutputJob,
        request: GenerationRequest,
        adapter: _AdapterSnapshot,
        store: _StoreSnapshot,
        *,
        created: bool,
    ) -> ImageGenerationOutput:
        job_id = job.id
        claim_token = job.generation_claim_token
        if not claim_token:
            raise ImageGenerationOutcomeUncertainError
        try:
            self._assert_claim_owned(customer_id, job_id, claim_token)
            with coordinate_style_consent(customer_id):
                # Refresh after claim/commit and keep withdrawal ordered through the
                # provider response ledger plus the post-call consent check.
                self._verify_style_reference_seal(job)
                generated = self._generator.generate(request)
                self._record_provider_dispatch(
                    customer_id, job, generated, adapter, claim_token
                )
                self._verify_style_reference_seal(job)
            self._assert_claim_owned(customer_id, job_id, claim_token)
            self._assert_adapter_unchanged(adapter)
            self._validate_generated_batch(
                generated, adapter, expected_count=request.candidate_count
            )
            published = self._publish_all(
                customer_id, job, generated, adapter, claim_token
            )
            self._assert_adapter_unchanged(adapter)
            self._assert_claim_owned(customer_id, job_id, claim_token)
            candidates = self._persist_candidates(
                customer_id, job, published, adapter, claim_token
            )
            completed_job = self._reload_attached_job(customer_id, job_id)
            return ImageGenerationOutput(
                job=completed_job, candidates=candidates, created=created
            )
        except _ImageGenerationClaimLostError as error:
            self._session.rollback()
            raise ImageGenerationInProgressError from error
        except Exception as error:
            committed = self._reload_committed_success(customer_id, job_id, adapter)
            if committed is not None:
                if self._verify_committed_artifacts(committed):
                    return ImageGenerationOutput(
                        job=committed.job,
                        candidates=committed.candidates,
                        created=created,
                    )
                _forget_local_store(job_id, self._content_store)
                raise ImageGenerationOutcomeUncertainError from error
            try:
                return self._fail_job(
                    customer_id,
                    job,
                    claim_token,
                    _sanitized_failure_reason(error),
                    adapter,
                    store,
                    outbound_attempts=_outbound_attempts(error),
                    created=created,
                )
            except _ImageGenerationClaimLostError as claim_error:
                self._session.rollback()
                raise ImageGenerationInProgressError from claim_error

    def _publish_all(
        self,
        customer_id: UUID,
        job: ImageOutputJob,
        generated: list[GeneratedCandidate],
        adapter: _AdapterSnapshot,
        claim_token: str,
    ) -> list[tuple[DerivativeTarget, NormalizedImage]]:
        published: list[tuple[DerivativeTarget, NormalizedImage]] = []
        for sequence_number, candidate in enumerate(generated, start=1):
            self._assert_claim_owned(customer_id, job.id, claim_token)
            target = self._content_store.reserve_derivative(
                customer_id, job.id, sequence_number
            )
            self._assert_claim_owned(customer_id, job.id, claim_token)
            metadata = dict(candidate.metadata)
            metadata.update(
                {
                    "adapter_version": job.adapter_version,
                    "generation_mode": job.request_metadata["generation_mode"],
                    "synthetic_test_only": adapter.generation_mode
                    == SYNTHETIC_TEST_GENERATION_MODE,
                    "publishable": False,
                    "output_job_id": str(job.id),
                    "source_product_ids": job.request_metadata["source_product_ids"],
                    "source_asset_ids": job.request_metadata["source_asset_ids"],
                    "style_card_version_id": job.request_metadata[
                        "style_card_version_id"
                    ],
                    "main_style_card_version_id": job.request_metadata[
                        "main_style_card_version_id"
                    ],
                    "scene_style_id": job.request_metadata["scene_style_id"],
                    "scene_style_card_id": job.request_metadata["scene_style_card_id"],
                }
            )
            payload = NormalizedPayload(
                content=candidate.content,
                content_sha256=candidate.content_sha256,
                mime_type=candidate.mime_type,
                width=candidate.width,
                height=candidate.height,
                metadata=metadata,
            )

            self._assert_claim_owned(customer_id, job.id, claim_token)
            result = self._content_store.create_derivative(target, payload)
            self._assert_claim_owned(customer_id, job.id, claim_token)
            published.append((target, result))
        for target, result in published:
            self._assert_claim_owned(customer_id, job.id, claim_token)
            verified = self._content_store.verify_derivative(
                customer_id,
                job.id,
                target.sequence_number,
                result.content_sha256,
            )
            self._assert_claim_owned(customer_id, job.id, claim_token)
            if not verified:
                raise AssetContentStoreError("asset_derivative_attestation_failed")
        return published

    def _persist_candidates(
        self,
        customer_id: UUID,
        job: ImageOutputJob,
        published: list[tuple[DerivativeTarget, NormalizedImage]],
        adapter: _AdapterSnapshot,
        claim_token: str,
    ) -> tuple[ImageCandidate, ...]:
        candidates: list[ImageCandidate] = []
        for target, result in published:
            metadata = dict(result.metadata)
            metadata["output_job_id"] = str(job.id)
            candidate = ImageCandidate(
                customer_id=customer_id,
                output_job_id=job.id,
                sequence_number=target.sequence_number,
                identity=(
                    "synthetic_style_image_candidate"
                    if adapter.generation_mode == SYNTHETIC_TEST_GENERATION_MODE
                    else "real_style_image_candidate"
                ),
                artifact_path=str(result.output_path),
                content_sha256=result.content_sha256,
                mime_type=result.mime_type,
                width=result.width,
                height=result.height,
                adapter_version=adapter.adapter_version,
                artifact_metadata=metadata,
            )
            candidates.append(candidate)
            self._session.add(candidate)
        transitioned = self._session.execute(
            update(ImageOutputJob)
            .where(
                ImageOutputJob.customer_id == customer_id,
                ImageOutputJob.id == job.id,
                ImageOutputJob.status == "generating",
                ImageOutputJob.generation_claim_token == claim_token,
            )
            .values(
                status="machine_check",
                failure_reason=None,
                generation_claim_token=None,
                generation_lease_expires_at=None,
                updated_at=datetime.now(UTC),
            )
            .execution_options(synchronize_session=False)
        )
        if transitioned.rowcount != 1:  # type: ignore[attr-defined]
            self._session.rollback()
            raise _ImageGenerationClaimLostError
        self._record_audit(
            customer_id,
            "image_output.generation_completed",
            {
                "job_id": str(job.id),
                "candidate_count": len(candidates),
                "adapter_version": adapter.adapter_version,
                "generation_mode": adapter.generation_mode,
            },
        )
        try:
            self._session.commit()
        except Exception as error:
            self._session.rollback()
            committed = self._reload_committed_success(customer_id, job.id, adapter)
            if committed is None:
                raise ImageGenerationOutcomeUncertainError from error
            if not self._verify_committed_artifacts(committed):
                _forget_local_store(job.id, self._content_store)
                raise ImageGenerationOutcomeUncertainError from error
            job.status = committed.job.status
            job.generation_claim_token = committed.job.generation_claim_token
            return committed.candidates
        job.status = "machine_check"
        job.generation_claim_token = None
        return tuple(candidates)

    def _fail_job(
        self,
        customer_id: UUID,
        job: ImageOutputJob,
        claim_token: str,
        reason: str,
        adapter: _AdapterSnapshot,
        store: _StoreSnapshot,
        *,
        outbound_attempts: int,
        created: bool,
    ) -> ImageGenerationOutput:
        self._session.rollback()
        self._assert_claim_owned(customer_id, job.id, claim_token)
        cleanup_state = self._cleanup_scope_state(job, store)
        cleanup_errors: tuple[str, ...] = ()
        if cleanup_state == "owned":
            self._assert_claim_owned(customer_id, job.id, claim_token)
            cleanup_errors = _safe_scoped_cleanup(
                self._content_store,
                customer_id,
                job.id,
                cast(str, job.artifact_store_capability),
            )
            self._assert_claim_owned(customer_id, job.id, claim_token)
            cleanup_state = self._cleanup_scope_state(job, store)
        if cleanup_state == "abandoned":
            status = "cleanup_abandoned"
            persisted_reason = _cleanup_abandoned_reason(
                reason,
                cleanup_errors[0]
                if cleanup_errors
                else "asset_store_cleanup_abandoned",
            )
        elif cleanup_state != "cleaned":
            status = "cleanup_pending"
            persisted_reason = _cleanup_pending_reason(
                reason,
                cleanup_errors or (_cleanup_state_reason(cleanup_state),),
            )
        else:
            status = "generation_failed"
            persisted_reason = reason
        transitioned = self._session.execute(
            update(ImageOutputJob)
            .where(
                ImageOutputJob.customer_id == customer_id,
                ImageOutputJob.id == job.id,
                ImageOutputJob.status == "generating",
                ImageOutputJob.generation_claim_token == claim_token,
            )
            .values(
                status=status,
                failure_reason=persisted_reason,
                generation_claim_token=None,
                generation_lease_expires_at=None,
                updated_at=datetime.now(UTC),
            )
            .execution_options(synchronize_session=False)
        )
        if transitioned.rowcount != 1:  # type: ignore[attr-defined]
            self._session.rollback()
            raise _ImageGenerationClaimLostError
        self._record_audit(
            customer_id,
            f"image_output.{status}",
            {
                "job_id": str(job.id),
                "reason": persisted_reason,
                "cleanup_errors": list(cleanup_errors),
                "adapter_version": adapter.adapter_version,
                "generation_mode": adapter.generation_mode,
                "outbound_attempts": outbound_attempts,
            },
        )
        try:
            self._session.commit()
        except Exception as error:
            self._session.rollback()
            persisted = self._reload_job(customer_id, job.id)
            if persisted is None or persisted.status != status:
                raise ImageGenerationOutcomeUncertainError from error
            self._release_store_if_settled(persisted)
            return ImageGenerationOutput(job=persisted, candidates=(), created=created)
        try:
            failed_job = self._reload_attached_job(customer_id, job.id)
            return ImageGenerationOutput(job=failed_job, candidates=(), created=created)
        finally:
            if status != "cleanup_pending":
                _forget_local_store(job.id, self._content_store)

    def _resolve_replay(
        self,
        job: ImageOutputJob,
        output_type: str,
        requested_facts: dict[str, object],
        adapter: _AdapterSnapshot,
        store: _StoreSnapshot,
        *,
        created: bool = False,
    ) -> ImageGenerationOutput:
        with _local_job_lock(job.id):
            try:
                current = self._reload_attached_job(job.customer_id, job.id)
                result = self._resolve_replay_locked(
                    current,
                    output_type,
                    requested_facts,
                    adapter,
                    store,
                    created=created,
                )
                self._release_store_if_settled(result.job)
                return result
            except _ImageGenerationClaimLostError as error:
                self._session.rollback()
                raise ImageGenerationInProgressError from error

    def _resolve_replay_locked(
        self,
        job: ImageOutputJob,
        output_type: str,
        requested_facts: dict[str, object],
        adapter: _AdapterSnapshot,
        store: _StoreSnapshot,
        *,
        created: bool,
    ) -> ImageGenerationOutput:
        stored_facts = job.request_metadata
        if (
            job.output_type != output_type
            or job.adapter_version != adapter.adapter_version
            or stored_facts.get("slot_key", "legacy")
            != requested_facts.get("slot_key", "legacy")
            or any(
                stored_facts.get(key) != value for key, value in requested_facts.items()
            )
        ):
            raise DomainRuleError("image_output_idempotency_conflict")
        if job.status == "cleanup_pending":
            return self._retry_cleanup(job, adapter, store)
        if job.status in {"queued", "generating"}:
            return self._execute_or_replay(job, adapter, store, created=created)
        candidates = tuple(self._repository.list_candidates(job.customer_id, job.id))
        if job.status == "machine_check":
            self._verify_machine_check(job, candidates, store)
        return ImageGenerationOutput(
            job=job,
            candidates=candidates,
            created=created,
        )

    def _execute_or_replay(
        self,
        job: ImageOutputJob,
        adapter: _AdapterSnapshot,
        store: _StoreSnapshot,
        *,
        created: bool,
    ) -> ImageGenerationOutput:
        with _local_job_lock(job.id):
            current = self._reload_attached_job(job.customer_id, job.id)
            if current.status not in {"queued", "generating"}:
                result = self._resolve_replay_locked(
                    current,
                    current.output_type,
                    _requested_facts_from_job(current),
                    adapter,
                    store,
                    created=created,
                )
                self._release_store_if_settled(result.job)
                return result
            if not self._store_scope_matches(current, store):
                raise ImageGenerationOutcomeUncertainError
            request = self._generation_request_for_dispatch(current, adapter)
            claimed = self._claim_generation(current)
            if claimed is None:
                raise ImageGenerationInProgressError
            self._reserve_real_pilot_dispatch(claimed, adapter)
            result = self._generate_all(
                claimed.customer_id,
                claimed,
                request,
                adapter,
                store,
                created=created,
            )
            self._release_store_if_settled(result.job)
            return result

    def _generation_sources_from_job(
        self, job: ImageOutputJob
    ) -> tuple[GenerationSource, ...]:
        if getattr(self._generator, "requires_source_content", False) is not True:
            return ()
        product_ids = tuple(cast(list[str], job.request_metadata["source_product_ids"]))
        asset_ids = tuple(
            UUID(str(value))
            for value in cast(list[str], job.request_metadata["source_asset_ids"])
        )
        if len(product_ids) != len(asset_ids):
            raise ImageGenerationError("generation_source_seal_mismatch")
        links = self._repository.list_asset_links(job.customer_id, job.id)
        if [link.product_asset_id for link in links] != list(asset_ids):
            raise ImageGenerationError("generation_source_seal_mismatch")
        sources: list[GenerationSource] = []
        for product_id, asset_id in zip(product_ids, asset_ids, strict=True):
            asset = self._repository.get_asset(job.customer_id, asset_id)
            if asset is None or asset.product_id != product_id:
                raise ImageGenerationError("generation_source_seal_mismatch")
            content = self._content_store.read_original(
                job.customer_id, asset.id, asset.content_sha256
            )
            sources.append(
                GenerationSource(
                    asset_id=asset.id,
                    product_id=asset.product_id,
                    content=content,
                    content_sha256=asset.content_sha256,
                    mime_type=asset.mime_type,
                )
            )
        source_facts = [
            {"product_id": source.product_id, "asset_id": str(source.asset_id)}
            for source in sources
        ]
        if job.request_metadata.get("sources") != source_facts or (
            job.output_type in {"product_style", "wear"}
            and job.request_metadata.get("product_asset_id") != str(asset_ids[0])
        ):
            raise ImageGenerationError("generation_source_seal_mismatch")
        sources.extend(self._style_reference_sources(job))
        return tuple(sources)

    def _generation_request_from_sealed_job(
        self, job: ImageOutputJob
    ) -> GenerationRequest:
        """Rebuild and verify every persisted source/style input before mutation."""
        if job.scene_style_id is None or job.style_card_version_id is None:
            raise DomainRuleError("image_generation_request_seal_mismatch")
        slot_key = job.request_metadata.get("slot_key", "legacy")
        if not isinstance(slot_key, str):
            raise DomainRuleError("image_generation_request_seal_mismatch")
        pins = self._require_active_style_pins(
            job.customer_id, job.scene_style_id, job.style_card_version_id
        )
        expected_facts: dict[str, object] = {
            "scene_style_id": str(pins.scene.id),
            "style_card_version_id": str(pins.scene_version.id),
            "main_style_card_version_id": (
                str(pins.main_version.id) if pins.main_version else None
            ),
            "scene_style_card_id": str(pins.scene_card.id),
            "output_ratio": job.output_ratio,
            **self._style_generation_facts(
                job.customer_id,
                pins,
                slot_key,
                sealed_pack="style_reference_pack_id" in job.request_metadata,
            ),
        }
        if any(
            job.request_metadata.get(key) != value
            for key, value in expected_facts.items()
        ):
            raise DomainRuleError("image_generation_request_seal_mismatch")
        return _generation_request_from_job(job, self._generation_sources_from_job(job))

    def _generation_request_for_dispatch(
        self, job: ImageOutputJob, adapter: _AdapterSnapshot
    ) -> GenerationRequest:
        """Apply the common deepest paid gate before any dispatch mutation."""
        if adapter.generation_mode != REAL_MODEL_GENERATION_MODE:
            return self._generation_request_from_sealed_job(job)
        if job.output_type in {"product_style", "wear"}:
            self._validate_dispatch_identity_card(job)
        authorization = self._real_pilot_authorization(job)
        if self._recorded_outbound_attempts(job) > 0 or (
            authorization is not None
            and (
                authorization.dispatch_state != "reserved"
                or authorization.outbound_attempts != 0
            )
        ):
            raise ImageGenerationOutcomeUncertainError
        request = self._generation_request_from_sealed_job(job)
        _require_structured_style_rules(request)
        if not self._enforce_real_pilot_boundary:
            return request
        if (
            job.adapter_version != adapter.adapter_version
            or job.request_metadata.get("generation_mode") != REAL_MODEL_GENERATION_MODE
            or job.request_metadata.get("synthetic_test_only") is not False
            or job.request_metadata.get("publishable") is not False
        ):
            raise DomainRuleError("real_image_pilot_authorization_required")
        if (
            authorization is None
            or len(request.product_ids) != 1
            or len(request.asset_ids) != 1
            or job.scene_style_id is None
            or job.style_card_version_id is None
        ):
            raise DomainRuleError("real_image_pilot_authorization_required")
        authorized_usd = self._require_real_pilot_authorization(
            job.customer_id,
            output_type=job.output_type,
            sources=(
                ImageSourceSelection(request.product_ids[0], request.asset_ids[0]),
            ),
            style_card_version_id=job.style_card_version_id,
            slot_key=request.slot_key,
            idempotency_key=self._real_pilot_scope_key or job.idempotency_key,
            style_reference_manifest_sha256=request.style_reference_manifest_sha256,
        )
        pins = self._require_active_style_pins(
            job.customer_id, job.scene_style_id, job.style_card_version_id
        )
        self._require_customer_upload_style(job.customer_id, pins)
        if (
            authorization.authorized_usd != authorized_usd
            or authorization.budget_scope != f"real_image_pilot:{job.id}"
        ):
            raise DomainRuleError("real_image_pilot_authorization_required")
        self._authorized_real_pilot_usd = authorized_usd
        return request

    def _validate_dispatch_identity_card(self, job: ImageOutputJob) -> None:
        """Require current-job seals while retaining markerless legacy recovery."""
        marker = job.request_metadata.get(_IDENTITY_CARD_REQUIREMENT_KEY)
        raw_version = job.request_metadata.get("product_identity_card_version_id")
        raw_sha256 = job.request_metadata.get("product_identity_card_sha256")
        raw_payload = job.request_metadata.get("product_identity_card")
        facts = (raw_version, raw_sha256, raw_payload)
        if marker is None and all(value is None for value in facts):
            return
        if marker not in {None, _SEALED_IDENTITY_CARD_V1}:
            raise DomainRuleError("confirmed_product_identity_card_required")
        if any(value is None for value in facts):
            raise DomainRuleError("product_identity_card_scope_mismatch")
        product_ids = job.request_metadata.get("source_product_ids")
        asset_ids = job.request_metadata.get("source_asset_ids")
        if (
            not isinstance(product_ids, list)
            or len(product_ids) != 1
            or not isinstance(product_ids[0], str)
            or not isinstance(asset_ids, list)
            or len(asset_ids) != 1
            or not isinstance(raw_sha256, str)
            or not isinstance(raw_payload, dict)
        ):
            raise DomainRuleError("product_identity_card_scope_mismatch")
        try:
            card_version_id = UUID(str(raw_version))
            asset_id = UUID(str(asset_ids[0]))
        except (TypeError, ValueError) as error:
            raise DomainRuleError("product_identity_card_scope_mismatch") from error
        self._validate_requested_identity_card(
            job.customer_id,
            output_type=job.output_type,
            sources=(ImageSourceSelection(product_ids[0], asset_id),),
            card_version_id=card_version_id,
            card_sha256=raw_sha256,
            card_payload=cast(dict[str, object], raw_payload),
            required=True,
        )

    def _style_generation_facts(
        self,
        customer_id: UUID,
        pins: _StylePins,
        slot_key: str,
        *,
        sealed_pack: bool | None = None,
    ) -> dict[str, object]:
        version = pins.scene_version
        historical = version.content.get("historical_style") is True
        observations = deepcopy(pins.main_version.content if pins.main_version else {})
        observations.update(deepcopy(version.content.get("overrides", {})))
        facts: dict[str, object] = {"style_observations": observations}
        if not historical:
            if slot_key != "legacy":
                raise DomainRuleError("style_slot_unavailable")
            return facts
        if version.content.get("stability") != "customer_confirmed":
            raise DomainRuleError("style_reference_unconfirmed")
        slot = self._session.scalar(
            select(StyleTemplateSlot).where(
                StyleTemplateSlot.customer_id == customer_id,
                StyleTemplateSlot.scene_style_id == pins.scene.id,
                StyleTemplateSlot.style_card_version_id == version.id,
                StyleTemplateSlot.slot_key == slot_key,
                StyleTemplateSlot.active.is_(True),
            )
        )
        if slot is None:
            raise DomainRuleError("style_slot_unavailable")
        snapshot = {
            "id": str(slot.id),
            "customer_id": str(customer_id),
            "scene_style_id": str(pins.scene.id),
            "style_card_version_id": str(version.id),
            "slot_key": slot.slot_key,
            "label": slot.label,
            "rules": slot.rules,
            "active": slot.active,
            "position": slot.position,
        }
        if snapshot not in version.content.get("slots", []):
            raise DomainRuleError("style_slot_seal_mismatch")
        all_rows = self._all_style_reference_rows(customer_id, version.id)
        selection_facts: dict[str, object]
        if sealed_pack is None:
            sealed_pack = (
                getattr(self._generator, "generation_mode", None)
                == REAL_MODEL_GENERATION_MODE
            )
        if sealed_pack:
            pack = StyleReferencePackService(self._session).get_or_create(
                customer_id, version.id
            )
            rows: list[StyleSample] = []
            for reference in pack.references:
                row = self._session.scalar(
                    select(StyleSample).where(
                        StyleSample.customer_id == customer_id,
                        StyleSample.id == reference.sample_id,
                    )
                )
                if row is None:
                    raise DomainRuleError("style_reference_pack_unavailable")
                rows.append(row)
            selection_facts = {
                "style_reference_selection": "sealed_style_reference_pack_v1",
                "style_reference_selection_version": pack.selection_version,
                "style_reference_pack_id": str(pack.id),
                "style_reference_manifest_sha256": pack.manifest_sha256,
            }
        else:
            rows, selection = self._confirmed_style_reference_rows(
                customer_id, version, all_rows
            )
            selection_facts = {
                "style_reference_selection": selection,
            }
        reference_facts = [self._reference_facts(row) for row in rows]
        facts.update(
            {
                "slot_key": slot_key,
                "slot_rules": deepcopy(slot.rules),
                "slot_snapshot": deepcopy(snapshot),
                "style_snapshot": deepcopy(version.content),
                "style_reference_max_count": MAX_STYLE_REFERENCES,
                "style_reference_total_count": len(all_rows),
                "style_reference_selected_count": len(rows),
                "style_references": reference_facts,
                "style_sample_ids": [fact["sample_id"] for fact in reference_facts],
                "style_sample_sha256": [
                    fact["content_sha256"] for fact in reference_facts
                ],
            }
        )
        facts.update(selection_facts)
        return facts

    @staticmethod
    def _reference_facts(row: StyleSample) -> dict[str, object]:
        return {
            "sample_id": str(row.id),
            "content_sha256": row.content_sha256,
            "mime_type": row.mime_type,
            "asset_store_namespace": row.asset_store_namespace,
            "consent_text_version": row.consent_text_version,
        }

    def _all_style_reference_rows(
        self, customer_id: UUID, version_id: UUID
    ) -> list[StyleSample]:
        links = list(
            self._session.scalars(
                select(StyleCardSampleLink)
                .where(
                    StyleCardSampleLink.customer_id == customer_id,
                    StyleCardSampleLink.style_card_version_id == version_id,
                )
                .order_by(StyleCardSampleLink.style_sample_id)
            )
        )
        if not links:
            raise DomainRuleError("style_reference_permission_unavailable")
        rows = []
        for link in links:
            row = self._session.scalar(
                select(StyleSample)
                .where(
                    StyleSample.customer_id == customer_id,
                    StyleSample.id == link.style_sample_id,
                )
                .execution_options(populate_existing=True)
            )
            if (
                row is None
                or row.withdrawn_at is not None
                or not row.has_complete_content_seal
            ):
                raise DomainRuleError("style_reference_permission_unavailable")
            rows.append(row)
        return rows

    @staticmethod
    def _select_style_reference_rows(rows: list[StyleSample]) -> list[StyleSample]:
        if len(rows) <= MAX_STYLE_REFERENCES:
            return rows
        chronological = sorted(rows, key=lambda row: (row.created_at, str(row.id)))
        denominator = MAX_STYLE_REFERENCES - 1
        last_index = len(chronological) - 1
        return [
            chronological[(position * last_index + denominator // 2) // denominator]
            for position in range(MAX_STYLE_REFERENCES)
        ]

    def _confirmed_style_reference_rows(
        self,
        customer_id: UUID,
        version: StyleCardVersion,
        all_rows: list[StyleSample],
    ) -> tuple[list[StyleSample], str]:
        """Prefer the customer-confirmed evidence subset used by real analysis."""
        evidence_ids = set(
            self._session.scalars(
                select(StyleFieldEvidence.style_sample_id).where(
                    StyleFieldEvidence.customer_id == customer_id,
                    StyleFieldEvidence.style_card_version_id == version.id,
                )
            )
        )
        evidence_rows = [row for row in all_rows if row.id in evidence_ids]
        if evidence_rows:
            selected = self._select_style_reference_rows(evidence_rows)
            return selected, (
                "confirmed_field_evidence_timeline_v1"
                if len(evidence_rows) > MAX_STYLE_REFERENCES
                else "confirmed_field_evidence_v1"
            )
        selected = self._select_style_reference_rows(all_rows)
        return selected, (
            "timeline_evenly_spaced_v1"
            if len(all_rows) > MAX_STYLE_REFERENCES
            else "all_evidence_sample_id_ascending"
        )

    def _verify_style_reference_seal(self, job: ImageOutputJob) -> list[StyleSample]:
        if "style_references" not in job.request_metadata:
            return []
        version_id = UUID(str(job.request_metadata["style_card_version_id"]))
        version = self._session.get(StyleCardVersion, version_id)
        if version is None or version.customer_id != job.customer_id:
            raise DomainRuleError("style_reference_seal_mismatch")
        raw_pack_id = job.request_metadata.get("style_reference_pack_id")
        if raw_pack_id is not None:
            try:
                pack_id = UUID(str(raw_pack_id))
            except (TypeError, ValueError) as error:
                raise DomainRuleError("style_reference_seal_mismatch") from error
            try:
                pack = StyleReferencePackService(self._session).get(
                    job.customer_id, pack_id
                )
            except DomainRuleError as error:
                raise DomainRuleError(
                    "style_reference_permission_unavailable"
                ) from error
            if (
                pack.scene_style_version_id != version_id
                or pack.manifest_sha256
                != job.request_metadata.get("style_reference_manifest_sha256")
                or job.request_metadata.get("style_reference_selection")
                != "sealed_style_reference_pack_v1"
            ):
                raise DomainRuleError("style_reference_seal_mismatch")
            rows = []
            for reference in pack.references:
                row = self._session.scalar(
                    select(StyleSample).where(
                        StyleSample.customer_id == job.customer_id,
                        StyleSample.id == reference.sample_id,
                    )
                )
                if row is None:
                    raise DomainRuleError("style_reference_seal_mismatch")
                rows.append(row)
            reference_facts = [self._reference_facts(row) for row in rows]
            if (
                reference_facts != job.request_metadata["style_references"]
                or job.request_metadata.get("style_reference_selected_count")
                != len(rows)
                or job.request_metadata.get("style_sample_ids")
                != [fact["sample_id"] for fact in reference_facts]
                or job.request_metadata.get("style_sample_sha256")
                != [fact["content_sha256"] for fact in reference_facts]
            ):
                raise DomainRuleError("style_reference_seal_mismatch")
            return rows
        all_rows = self._all_style_reference_rows(job.customer_id, version_id)
        selection = job.request_metadata.get(
            "style_reference_selection", "all_evidence_sample_id_ascending"
        )
        if selection in {
            "confirmed_field_evidence_timeline_v1",
            "confirmed_field_evidence_v1",
        }:
            rows, expected_selection = self._confirmed_style_reference_rows(
                job.customer_id,
                version,
                all_rows,
            )
            if selection != expected_selection:
                raise DomainRuleError("style_reference_seal_mismatch")
        elif selection == "timeline_evenly_spaced_v1":
            rows = self._select_style_reference_rows(all_rows)
        elif (
            selection == "all_evidence_sample_id_ascending"
            and len(all_rows) <= MAX_STYLE_REFERENCES
        ):
            rows = all_rows
        else:
            raise DomainRuleError("style_reference_seal_mismatch")
        reference_facts = [self._reference_facts(row) for row in rows]
        if reference_facts != job.request_metadata["style_references"]:
            raise DomainRuleError("style_reference_seal_mismatch")
        expected_ids = [fact["sample_id"] for fact in reference_facts]
        expected_hashes = [fact["content_sha256"] for fact in reference_facts]
        if (
            (
                "style_sample_ids" in job.request_metadata
                and job.request_metadata["style_sample_ids"] != expected_ids
            )
            or (
                "style_sample_sha256" in job.request_metadata
                and job.request_metadata["style_sample_sha256"] != expected_hashes
            )
            or (
                "style_reference_total_count" in job.request_metadata
                and job.request_metadata["style_reference_total_count"] != len(all_rows)
            )
            or (
                "style_reference_selected_count" in job.request_metadata
                and job.request_metadata["style_reference_selected_count"] != len(rows)
            )
            or (
                job.request_metadata.get("style_reference_manifest_sha256")
                != version.content.get("reference_manifest_sha256")
            )
        ):
            raise DomainRuleError("style_reference_seal_mismatch")
        return rows

    def _style_reference_sources(self, job: ImageOutputJob) -> list[GenerationSource]:
        sources = []
        for row in self._verify_style_reference_seal(job):
            digest, mime = row.content_sha256, row.mime_type
            if (
                not digest
                or not mime
                or row.asset_store_namespace is None
                or not namespace_is_accepted(
                    self._session,
                    job.customer_id,
                    row.asset_store_namespace,
                    self._content_store.store_namespace,
                )
            ):
                raise DomainRuleError("style_reference_store_mismatch")
            try:
                if not self._content_store.verify_style_sample_content(
                    job.customer_id, row.id, digest
                ):
                    raise DomainRuleError("style_reference_content_unavailable")
                content = self._content_store.read_style_sample_content(
                    job.customer_id, row.id, digest
                )
            except AssetContentStoreError as error:
                raise DomainRuleError("style_reference_content_unavailable") from error
            if sha256(content).hexdigest() != digest:
                raise DomainRuleError("style_reference_content_mismatch")
            sources.append(
                GenerationSource(
                    row.id, None, content, digest, mime, role="style_reference"
                )
            )
        return sources

    def _claim_generation(self, job: ImageOutputJob) -> ImageOutputJob | None:
        token = str(uuid4())
        claimed_at = datetime.now(UTC)
        for attempt in range(2):
            result = self._session.execute(
                update(ImageOutputJob)
                .where(
                    ImageOutputJob.customer_id == job.customer_id,
                    ImageOutputJob.id == job.id,
                    ImageOutputJob.status.in_(("queued", "generating")),
                )
                .values(
                    status="generating",
                    failure_reason=None,
                    generation_claim_token=token,
                    generation_lease_expires_at=None,
                    updated_at=claimed_at,
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:  # type: ignore[attr-defined]
                self._session.rollback()
                return None
            try:
                self._session.commit()
            except Exception as error:
                self._session.rollback()
                persisted = self._reload_job(job.customer_id, job.id)
                if (
                    persisted is not None
                    and persisted.status == "generating"
                    and persisted.generation_claim_token == token
                ):
                    return self._reload_attached_job(job.customer_id, job.id)
                if (
                    attempt == 0
                    and persisted is not None
                    and persisted.status == "queued"
                ):
                    continue
                raise ImageGenerationOutcomeUncertainError from error
            return self._reload_attached_job(job.customer_id, job.id)
        raise ImageGenerationOutcomeUncertainError

    def _assert_claim_owned(
        self,
        customer_id: UUID,
        job_id: UUID,
        claim_token: str,
        *,
        status: str = "generating",
    ) -> None:
        persisted = self._reload_job(customer_id, job_id)
        if (
            persisted is None
            or persisted.status != status
            or persisted.generation_claim_token != claim_token
        ):
            raise _ImageGenerationClaimLostError

    def _require_ready_assets(
        self, customer_id: UUID, sources: tuple[ImageSourceSelection, ...]
    ) -> tuple[ProductAsset, ...]:
        assets: list[ProductAsset] = []
        for source in sources:
            asset = self._repository.get_asset(customer_id, source.asset_id)
            if asset is None or asset.product_id != source.product_id:
                raise ImageOutputNotFoundError
            if not asset.confirmed:
                raise DomainRuleError("organizer_confirmation_missing")
            permission = self._repository.get_latest_asset_permission(
                customer_id, asset.id
            )
            if permission is None:
                raise DomainRuleError("transform_permission_unknown")
            if not permission.transform_permission:
                raise DomainRuleError("transform_permission_denied")
            assets.append(asset)
        return tuple(assets)

    def _require_active_style_pins(
        self,
        customer_id: UUID,
        scene_style_id: UUID,
        style_card_version_id: UUID,
    ) -> _StylePins:
        scene = self._session.scalar(
            select(SceneStyle).where(
                SceneStyle.customer_id == customer_id,
                SceneStyle.id == scene_style_id,
            )
        )
        if scene is None or scene.style_card_id is None:
            raise ImageOutputNotFoundError
        scene_card = self._session.scalar(
            select(StyleCard).where(
                StyleCard.customer_id == customer_id,
                StyleCard.id == scene.style_card_id,
                StyleCard.kind == "scene",
            )
        )
        if scene_card is None:
            raise ImageOutputNotFoundError
        scene_version = self._session.scalar(
            select(StyleCardVersion).where(
                StyleCardVersion.customer_id == customer_id,
                StyleCardVersion.id == style_card_version_id,
                StyleCardVersion.style_card_id == scene_card.id,
            )
        )
        if scene_version is None:
            raise ImageOutputNotFoundError
        raw_main_version_id = scene_version.content.get("main_style_card_version_id")
        if (
            raw_main_version_id is None
            and scene_version.content.get("historical_style") is True
        ):
            if not scene.active or not scene_card.active:
                raise DomainRuleError("style_or_scene_inactive")
            return _StylePins(scene, scene_card, scene_version, None)
        try:
            main_version_id = UUID(cast(str, raw_main_version_id))
        except (TypeError, ValueError, AttributeError) as error:
            raise ImageOutputNotFoundError from error
        main_version = self._session.scalar(
            select(StyleCardVersion)
            .join(StyleCard, StyleCard.id == StyleCardVersion.style_card_id)
            .where(
                StyleCardVersion.customer_id == customer_id,
                StyleCardVersion.id == main_version_id,
                StyleCard.customer_id == customer_id,
                StyleCard.kind == "main",
            )
        )
        if main_version is None:
            raise ImageOutputNotFoundError
        main_card = self._session.scalar(
            select(StyleCard).where(
                StyleCard.customer_id == customer_id,
                StyleCard.id == main_version.style_card_id,
                StyleCard.kind == "main",
            )
        )
        if main_card is None:
            raise ImageOutputNotFoundError
        if not scene.active or not scene_card.active or not main_card.active:
            raise DomainRuleError("style_or_scene_inactive")
        return _StylePins(
            scene=scene,
            scene_card=scene_card,
            scene_version=scene_version,
            main_version=main_version,
        )

    def _validate_generated_batch(
        self,
        generated: list[GeneratedCandidate],
        adapter: _AdapterSnapshot,
        *,
        expected_count: int,
    ) -> None:
        if len(generated) != expected_count:
            raise ImageGenerationError("generator_must_return_exactly_three_candidates")
        for sequence_number, candidate in enumerate(generated, start=1):
            try:
                with Image.open(BytesIO(candidate.content)) as decoded:
                    decoded.verify()
                with Image.open(BytesIO(candidate.content)) as decoded:
                    actual_format = decoded.format
                    actual_size = decoded.size
            except (OSError, UnidentifiedImageError, ValueError) as error:
                raise ImageGenerationError("invalid_generated_candidate") from error
            if (
                not candidate.content
                or sha256(candidate.content).hexdigest() != candidate.content_sha256
                or candidate.mime_type != "image/jpeg"
                or candidate.width <= 0
                or candidate.height <= 0
                or actual_format != "JPEG"
                or actual_size != (candidate.width, candidate.height)
                or candidate.metadata.get("adapter_version") != adapter.adapter_version
                or candidate.metadata.get("generation_mode") != adapter.generation_mode
                or candidate.metadata.get("synthetic_test_only")
                is not (adapter.generation_mode == SYNTHETIC_TEST_GENERATION_MODE)
                or candidate.metadata.get("publishable") is not False
                or candidate.metadata.get("sequence_number") != sequence_number
            ):
                raise ImageGenerationError("invalid_generated_candidate")

    def _pin_adapter(self) -> _AdapterSnapshot:
        trusted: dict[type[object], tuple[str, str]] = {
            DeterministicLocalImageGenerator: (
                DETERMINISTIC_LOCAL_ADAPTER_VERSION,
                SYNTHETIC_TEST_GENERATION_MODE,
            ),
            OpenAIImageGenerator: (
                OPENAI_IMAGE_ADAPTER_VERSION,
                REAL_MODEL_GENERATION_MODE,
            ),
        }
        expected = trusted.get(type(self._generator))
        if expected is None:
            raise DomainRuleError("unsupported_image_generator")
        adapter_version = getattr(self._generator, "adapter_version", None)
        generation_mode = getattr(self._generator, "generation_mode", None)
        publishable = getattr(self._generator, "publishable", None)
        if (
            adapter_version != expected[0]
            or generation_mode != expected[1]
            or publishable is not False
        ):
            raise DomainRuleError("unsupported_image_generator")
        return _AdapterSnapshot(
            adapter_version=expected[0],
            generation_mode=expected[1],
            publishable=False,
        )

    def _pin_store(self) -> _StoreSnapshot:
        identity = getattr(self._content_store, "store_identity", None)
        namespace = getattr(self._content_store, "store_namespace", None)
        if (
            not isinstance(identity, str)
            or not identity
            or len(identity) > 100
            or not isinstance(namespace, str)
            or not namespace
            or len(namespace) > 64
            or not callable(
                getattr(self._content_store, "issue_cleanup_capability", None)
            )
            or not callable(
                getattr(self._content_store, "verify_cleanup_capability", None)
            )
            or not callable(
                getattr(self._content_store, "cleanup_scoped_derivatives", None)
            )
        ):
            raise DomainRuleError("unsupported_asset_content_store")
        return _StoreSnapshot(identity=identity, namespace=namespace)

    def _assert_adapter_unchanged(self, adapter: _AdapterSnapshot) -> None:
        if (
            getattr(self._generator, "adapter_version", None) != adapter.adapter_version
            or getattr(self._generator, "generation_mode", None)
            != adapter.generation_mode
            or getattr(self._generator, "publishable", None) is not adapter.publishable
        ):
            raise ImageGenerationError("image_generator_identity_changed")

    def _reload_committed_success(
        self, customer_id: UUID, job_id: UUID, adapter: _AdapterSnapshot
    ) -> ImageGenerationOutput | None:
        bind = self._session.get_bind()
        try:
            with Session(bind=bind, expire_on_commit=False) as verification_session:
                repository = ProductImageRepository(verification_session)
                persisted_job = repository.get_output_job(customer_id, job_id)
                if (
                    persisted_job is None
                    or persisted_job.status != "machine_check"
                    or persisted_job.failure_reason is not None
                    or persisted_job.adapter_version != adapter.adapter_version
                ):
                    return None
                candidates = tuple(repository.list_candidates(customer_id, job_id))
                if (
                    len(candidates) != len(_expected_artifact_sequences(persisted_job))
                    or tuple(item.sequence_number for item in candidates)
                    != _expected_artifact_sequences(persisted_job)
                    or any(
                        item.adapter_version != adapter.adapter_version
                        for item in candidates
                    )
                ):
                    raise ImageGenerationOutcomeUncertainError
                verification_session.expunge_all()
                return ImageGenerationOutput(
                    job=persisted_job, candidates=candidates, created=True
                )
        except ImageGenerationOutcomeUncertainError:
            raise
        except Exception as error:
            raise ImageGenerationOutcomeUncertainError from error

    def _verify_committed_artifacts(self, result: ImageGenerationOutput) -> bool:
        try:
            return all(
                self._content_store.verify_derivative(
                    result.job.customer_id,
                    result.job.id,
                    candidate.sequence_number,
                    candidate.content_sha256,
                )
                for candidate in result.candidates
            )
        except Exception:  # noqa: BLE001 - uncertain means never delete
            return False

    def _verify_machine_check(
        self,
        job: ImageOutputJob,
        candidates: tuple[ImageCandidate, ...],
        store: _StoreSnapshot,
    ) -> None:
        if (
            not self._store_scope_matches(job, store)
            or len(candidates) != len(_expected_artifact_sequences(job))
            or tuple(item.sequence_number for item in candidates)
            != _expected_artifact_sequences(job)
            or any(item.adapter_version != job.adapter_version for item in candidates)
        ):
            raise ImageGenerationOutcomeUncertainError
        result = ImageGenerationOutput(job=job, candidates=candidates, created=False)
        if not self._verify_committed_artifacts(result):
            raise ImageGenerationOutcomeUncertainError

    def _store_scope_matches(self, job: ImageOutputJob, store: _StoreSnapshot) -> bool:
        return _local_store_matches(
            job.id, self._content_store
        ) and self._store_metadata_matches(job, store)

    def _store_metadata_matches(
        self, job: ImageOutputJob, store: _StoreSnapshot
    ) -> bool:
        return (
            job.artifact_store_identity == store.identity
            and job.artifact_store_namespace == store.namespace
            and job.expected_artifact_sequences
            == list(_expected_artifact_sequences(job))
        )

    def _cleanup_scope_state(self, job: ImageOutputJob, store: _StoreSnapshot) -> str:
        capability = job.artifact_store_capability
        if (
            not self._store_scope_matches(job, store)
            or not isinstance(capability, str)
            or not capability
        ):
            return "scope_mismatch"
        try:
            if not self._content_store.verify_cleanup_capability(
                job.customer_id,
                job.id,
                _expected_artifact_sequences(job),
                capability,
            ):
                return "unavailable"
            return self._content_store.cleanup_scope_state(
                job.customer_id,
                job.id,
                _expected_artifact_sequences(job),
                capability,
            )
        except Exception:  # noqa: BLE001 - state remains explicitly unresolved
            return "unavailable"

    def _retry_cleanup(
        self,
        job: ImageOutputJob,
        adapter: _AdapterSnapshot,
        store: _StoreSnapshot,
    ) -> ImageGenerationOutput:
        claim_token = self._claim_cleanup(job)
        if claim_token is None:
            raise ImageGenerationInProgressError
        original_reason = _original_failure_reason(job.failure_reason)
        before_state = self._cleanup_scope_state(job, store)
        if before_state == "abandoned":
            return self._abandon_cleanup(
                job,
                adapter,
                claim_token,
                original_reason,
                _cleanup_state_reason(before_state),
            )
        if before_state not in {"owned", "cleaned"}:
            return self._keep_cleanup_pending(
                job,
                adapter,
                claim_token,
                original_reason,
                _cleanup_state_reason(before_state),
            )
        if before_state == "cleaned":
            return self._complete_cleanup(job, adapter, claim_token, original_reason)
        self._assert_claim_owned(
            job.customer_id, job.id, claim_token, status="cleanup_pending"
        )
        cleanup_errors = _safe_scoped_cleanup(
            self._content_store,
            job.customer_id,
            job.id,
            cast(str, job.artifact_store_capability),
        )
        self._assert_claim_owned(
            job.customer_id, job.id, claim_token, status="cleanup_pending"
        )
        after_state = self._cleanup_scope_state(job, store)
        if after_state == "abandoned":
            reason = (
                cleanup_errors[0]
                if cleanup_errors
                else _cleanup_state_reason(after_state)
            )
            return self._abandon_cleanup(
                job, adapter, claim_token, original_reason, reason
            )
        if after_state != "cleaned":
            return self._keep_cleanup_pending(
                job,
                adapter,
                claim_token,
                original_reason,
                cleanup_errors[0]
                if cleanup_errors
                else _cleanup_state_reason(after_state),
            )
        return self._complete_cleanup(job, adapter, claim_token, original_reason)

    def _claim_cleanup(self, job: ImageOutputJob) -> str | None:
        token = str(uuid4())
        now = datetime.now(UTC)
        result = self._session.execute(
            update(ImageOutputJob)
            .where(
                ImageOutputJob.customer_id == job.customer_id,
                ImageOutputJob.id == job.id,
                ImageOutputJob.status == "cleanup_pending",
            )
            .values(
                generation_claim_token=token,
                generation_lease_expires_at=None,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:  # type: ignore[attr-defined]
            self._session.rollback()
            return None
        try:
            self._session.commit()
        except Exception as error:
            self._session.rollback()
            persisted = self._reload_job(job.customer_id, job.id)
            if (
                persisted is not None
                and persisted.status == "cleanup_pending"
                and persisted.generation_claim_token == token
            ):
                return token
            raise ImageGenerationOutcomeUncertainError from error
        self._session.expire_all()
        return token

    def _keep_cleanup_pending(
        self,
        job: ImageOutputJob,
        adapter: _AdapterSnapshot,
        claim_token: str,
        original_reason: str,
        unresolved_reason: str,
    ) -> ImageGenerationOutput:
        persisted_reason = _cleanup_pending_reason(
            original_reason,
            (_safe_code(unresolved_reason, "asset_store_cleanup_unverified"),),
        )
        transitioned = self._session.execute(
            update(ImageOutputJob)
            .where(
                ImageOutputJob.customer_id == job.customer_id,
                ImageOutputJob.id == job.id,
                ImageOutputJob.status == "cleanup_pending",
                ImageOutputJob.generation_claim_token == claim_token,
            )
            .values(
                failure_reason=persisted_reason,
                generation_claim_token=None,
                generation_lease_expires_at=None,
                updated_at=datetime.now(UTC),
            )
            .execution_options(synchronize_session=False)
        )
        if transitioned.rowcount != 1:  # type: ignore[attr-defined]
            self._session.rollback()
            raise ImageGenerationInProgressError
        self._record_audit(
            job.customer_id,
            "image_output.cleanup_retry_failed",
            {
                "job_id": str(job.id),
                "reason": persisted_reason,
                "cleanup_errors": [unresolved_reason],
                "adapter_version": adapter.adapter_version,
                "generation_mode": adapter.generation_mode,
            },
        )
        try:
            self._session.commit()
        except Exception as error:
            self._session.rollback()
            persisted = self._reload_job(job.customer_id, job.id)
            if (
                persisted is None
                or persisted.status != "cleanup_pending"
                or persisted.generation_claim_token is not None
                or persisted.failure_reason != persisted_reason
            ):
                raise ImageGenerationOutcomeUncertainError from error
            return ImageGenerationOutput(job=persisted, candidates=(), created=False)
        persisted = self._reload_attached_job(job.customer_id, job.id)
        return ImageGenerationOutput(job=persisted, candidates=(), created=False)

    def _complete_cleanup(
        self,
        job: ImageOutputJob,
        adapter: _AdapterSnapshot,
        claim_token: str,
        original_reason: str,
    ) -> ImageGenerationOutput:
        transitioned = self._session.execute(
            update(ImageOutputJob)
            .where(
                ImageOutputJob.customer_id == job.customer_id,
                ImageOutputJob.id == job.id,
                ImageOutputJob.status == "cleanup_pending",
                ImageOutputJob.generation_claim_token == claim_token,
            )
            .values(
                status="generation_failed",
                failure_reason=original_reason,
                generation_claim_token=None,
                generation_lease_expires_at=None,
                updated_at=datetime.now(UTC),
            )
            .execution_options(synchronize_session=False)
        )
        if transitioned.rowcount != 1:  # type: ignore[attr-defined]
            self._session.rollback()
            raise ImageGenerationInProgressError
        common_payload: dict[str, object] = {
            "job_id": str(job.id),
            "reason": original_reason,
            "cleanup_errors": [],
            "adapter_version": adapter.adapter_version,
            "generation_mode": adapter.generation_mode,
        }
        self._record_audit(
            job.customer_id, "image_output.cleanup_completed", common_payload
        )
        self._record_audit(
            job.customer_id, "image_output.generation_failed", common_payload
        )
        self._commit_cleanup_outcome(job, "generation_failed")
        try:
            failed_job = self._reload_attached_job(job.customer_id, job.id)
            return ImageGenerationOutput(job=failed_job, candidates=(), created=False)
        finally:
            _forget_local_store(job.id, self._content_store)

    def _abandon_cleanup(
        self,
        job: ImageOutputJob,
        adapter: _AdapterSnapshot,
        claim_token: str,
        original_reason: str,
        abandonment_reason: str,
    ) -> ImageGenerationOutput:
        persisted_reason = _cleanup_abandoned_reason(
            original_reason, abandonment_reason
        )
        transitioned = self._session.execute(
            update(ImageOutputJob)
            .where(
                ImageOutputJob.customer_id == job.customer_id,
                ImageOutputJob.id == job.id,
                ImageOutputJob.status == "cleanup_pending",
                ImageOutputJob.generation_claim_token == claim_token,
            )
            .values(
                status="cleanup_abandoned",
                failure_reason=persisted_reason,
                generation_claim_token=None,
                generation_lease_expires_at=None,
                updated_at=datetime.now(UTC),
            )
            .execution_options(synchronize_session=False)
        )
        if transitioned.rowcount != 1:  # type: ignore[attr-defined]
            self._session.rollback()
            raise ImageGenerationInProgressError
        self._record_audit(
            job.customer_id,
            "image_output.cleanup_abandoned",
            {
                "job_id": str(job.id),
                "reason": persisted_reason,
                "cleanup_errors": [abandonment_reason],
                "adapter_version": adapter.adapter_version,
                "generation_mode": adapter.generation_mode,
            },
        )
        self._commit_cleanup_outcome(job, "cleanup_abandoned")
        try:
            abandoned_job = self._reload_attached_job(job.customer_id, job.id)
            return ImageGenerationOutput(
                job=abandoned_job, candidates=(), created=False
            )
        finally:
            _forget_local_store(job.id, self._content_store)

    def _commit_cleanup_outcome(
        self, job: ImageOutputJob, expected_status: str
    ) -> None:
        try:
            self._session.commit()
        except Exception as error:
            self._session.rollback()
            persisted = self._reload_job(job.customer_id, job.id)
            if persisted is None or persisted.status != expected_status:
                raise ImageGenerationOutcomeUncertainError from error

    def _reload_job_by_idempotency_key(
        self, customer_id: UUID, idempotency_key: str
    ) -> ImageOutputJob | None:
        bind = self._session.get_bind()
        with Session(bind=bind, expire_on_commit=False) as verification_session:
            job = ProductImageRepository(
                verification_session
            ).get_output_job_by_idempotency_key(customer_id, idempotency_key)
            if job is not None:
                verification_session.expunge(job)
            return job

    def _reload_job(self, customer_id: UUID, job_id: UUID) -> ImageOutputJob | None:
        bind = self._session.get_bind()
        with Session(bind=bind, expire_on_commit=False) as verification_session:
            job = ProductImageRepository(verification_session).get_output_job(
                customer_id, job_id
            )
            if job is not None:
                verification_session.expunge(job)
            return job

    def _reload_attached_job(self, customer_id: UUID, job_id: UUID) -> ImageOutputJob:
        self._session.expire_all()
        job = self._repository.get_output_job(customer_id, job_id)
        if job is None:
            raise ImageOutputNotFoundError
        return job

    def _release_store_if_settled(self, job: ImageOutputJob) -> None:
        if job.status in {
            "generation_failed",
            "cleanup_abandoned",
            "customer_rejected",
        }:
            _forget_local_store(job.id, self._content_store)

    def _record_audit(
        self, customer_id: UUID, event_type: str, payload: dict[str, object]
    ) -> None:
        self._session.add(
            AuditEvent(
                customer_id=customer_id,
                event_type=event_type,
                payload=payload,
                occurred_at=datetime.now(UTC),
            )
        )

    def _record_provider_dispatch(
        self,
        customer_id: UUID,
        job: ImageOutputJob,
        generated: list[GeneratedCandidate],
        adapter: _AdapterSnapshot,
        claim_token: str,
    ) -> None:
        """Commit paid dispatch evidence before a fallible local publication step."""
        if (
            adapter.generation_mode != REAL_MODEL_GENERATION_MODE
            or self._authorized_real_pilot_usd is None
        ):
            return
        outbound_attempts = _generated_outbound_attempts(generated)
        self._assert_claim_owned(customer_id, job.id, claim_token)
        authorization = self._real_pilot_authorization(job)
        if (
            authorization is None
            or authorization.dispatch_state != "dispatch_uncertain"
        ):
            raise ImageGenerationOutcomeUncertainError
        authorization.dispatch_state = "provider_responded"
        authorization.outbound_attempts = outbound_attempts
        self._record_audit(
            customer_id,
            "image_output.provider_dispatch_succeeded",
            {
                "job_id": str(job.id),
                "adapter_version": adapter.adapter_version,
                "generation_mode": adapter.generation_mode,
                "outbound_attempts": outbound_attempts,
            },
        )
        try:
            self._session.commit()
        except Exception as error:
            self._session.rollback()
            if self._recorded_outbound_attempts(job) >= outbound_attempts:
                return
            raise _ProviderDispatchEvidenceUncertainError(outbound_attempts) from error

    def _reserve_real_pilot_dispatch(
        self, job: ImageOutputJob, adapter: _AdapterSnapshot
    ) -> None:
        if (
            adapter.generation_mode != REAL_MODEL_GENERATION_MODE
            or self._authorized_real_pilot_usd is None
        ):
            return
        authorization = self._real_pilot_authorization(job)
        if authorization is None or authorization.dispatch_state != "reserved":
            raise ImageGenerationOutcomeUncertainError
        authorization.dispatch_state = "dispatch_uncertain"
        self._record_audit(
            job.customer_id,
            "image_output.provider_dispatch_uncertain",
            {
                "job_id": str(job.id),
                "adapter_version": adapter.adapter_version,
                "generation_mode": adapter.generation_mode,
                "authorized_usd": str(authorization.authorized_usd),
                "outbound_attempts": 0,
                "usage_evidence": "authorization_reservation_not_billing_receipt",
            },
        )
        try:
            self._session.commit()
        except Exception as error:
            self._session.rollback()
            raise ImageGenerationOutcomeUncertainError from error

    def _real_pilot_authorization(
        self, job: ImageOutputJob
    ) -> RealImagePilotAuthorization | None:
        return self._session.scalar(
            select(RealImagePilotAuthorization).where(
                RealImagePilotAuthorization.customer_id == job.customer_id,
                RealImagePilotAuthorization.output_job_id == job.id,
            )
        )

    def _recorded_outbound_attempts(self, job: ImageOutputJob) -> int:
        attempts = 0
        for event in self._session.scalars(
            select(AuditEvent).where(AuditEvent.customer_id == job.customer_id)
        ):
            if event.payload.get("job_id") != str(job.id):
                continue
            value = event.payload.get("outbound_attempts", 0)
            if isinstance(value, int) and value >= 0:
                attempts = max(attempts, value)
        return attempts


def _require_structured_style_rules(request: GenerationRequest) -> None:
    """Fail before paid dispatch when photography rules are incomplete."""

    def string_values(value: object) -> tuple[str, ...]:
        if not isinstance(value, (list, tuple)):
            return ()
        return tuple(item for item in value if isinstance(item, str) and item.strip())

    observations = request.style_observations
    rules = request.slot_rules
    if (
        any(
            not string_values(observations.get(field))
            for field in ("palette", "lighting", "composition", "backgrounds")
        )
        or any(
            not isinstance(rules.get(field), str) or not str(rules[field]).strip()
            for field in ("aspect_ratio", "shot_distance", "camera_angle")
        )
        or not string_values(rules.get("composition"))
    ):
        raise DomainRuleError("structured_style_rules_required")


def _sanitized_failure_reason(error: Exception) -> str:
    if isinstance(
        error, (ImageGenerationError, AssetContentStoreError, DomainRuleError)
    ):
        return _safe_code(error.code, "image_generation_failed")
    return "image_generation_failed"


def _outbound_attempts(error: Exception) -> int:
    attempts = getattr(error, "outbound_attempts", 0)
    return attempts if isinstance(attempts, int) and attempts >= 0 else 0


def _generated_outbound_attempts(generated: list[GeneratedCandidate]) -> int:
    attempts = [candidate.metadata.get("outbound_attempts") for candidate in generated]
    if (
        not attempts
        or any(not isinstance(value, int) or value <= 0 for value in attempts)
        or len(set(attempts)) != 1
    ):
        raise ImageGenerationError("provider_dispatch_evidence_invalid")
    return cast(int, attempts[0])


@contextmanager
def _local_job_lock(job_id: UUID) -> Iterator[None]:
    with _LOCAL_LOCKS_GUARD:
        entry = _LOCAL_JOB_LOCKS.get(job_id)
        if entry is None:
            entry = _LocalLockEntry(lock=RLock())
            _LOCAL_JOB_LOCKS[job_id] = entry
        entry.users += 1
    try:
        with entry.lock:
            yield
    finally:
        with _LOCAL_LOCKS_GUARD:
            entry.users -= 1
            if entry.users == 0 and _LOCAL_JOB_LOCKS.get(job_id) is entry:
                del _LOCAL_JOB_LOCKS[job_id]


def _remember_local_store(job_id: UUID, content_store: AssetContentStore) -> None:
    with _LOCAL_LOCKS_GUARD:
        _LOCAL_JOB_STORES[job_id] = content_store


@contextmanager
def _local_idempotency_lock(customer_id: UUID, key: str) -> Iterator[None]:
    """Serialize one customer/key in the documented single-process executor."""
    lock_key = (customer_id, key)
    with _LOCAL_LOCKS_GUARD:
        entry = _LOCAL_IDEMPOTENCY_LOCKS.get(lock_key)
        if entry is None:
            entry = _LocalLockEntry(lock=RLock())
            _LOCAL_IDEMPOTENCY_LOCKS[lock_key] = entry
        entry.users += 1
    try:
        with entry.lock:
            yield
    finally:
        with _LOCAL_LOCKS_GUARD:
            entry.users -= 1
            if entry.users == 0 and _LOCAL_IDEMPOTENCY_LOCKS.get(lock_key) is entry:
                del _LOCAL_IDEMPOTENCY_LOCKS[lock_key]


def _local_store_matches(job_id: UUID, content_store: AssetContentStore) -> bool:
    with _LOCAL_LOCKS_GUARD:
        return _LOCAL_JOB_STORES.get(job_id) is content_store


def _forget_local_store(job_id: UUID, content_store: AssetContentStore) -> None:
    with _LOCAL_LOCKS_GUARD:
        if _LOCAL_JOB_STORES.get(job_id) is content_store:
            _LOCAL_JOB_STORES.pop(job_id, None)


def _safe_scoped_cleanup(
    content_store: AssetContentStore,
    customer_id: UUID,
    job_id: UUID,
    capability: str,
) -> tuple[str, ...]:
    try:
        reported = content_store.cleanup_scoped_derivatives(
            customer_id, job_id, capability
        )
    except Exception:  # noqa: BLE001 - cleanup failure is recorded, not leaked
        return ("asset_store_cleanup_failed",)
    return tuple(_safe_code(code, "asset_store_cleanup_failed") for code in reported)


def _cleanup_pending_reason(reason: str, cleanup_errors: tuple[str, ...]) -> str:
    codes = ",".join(cleanup_errors)[:180]
    suffix = f";cleanup_pending:{codes}"
    return f"{reason[: 255 - len(suffix)]}{suffix}"


def _cleanup_abandoned_reason(reason: str, abandonment_reason: str) -> str:
    safe_reason = _safe_code(abandonment_reason, "asset_store_cleanup_abandoned")
    suffix = f";cleanup_abandoned:{safe_reason}"
    return f"{reason[: 255 - len(suffix)]}{suffix}"


def _cleanup_state_reason(state: str) -> str:
    return {
        "scope_mismatch": "content_store_scope_mismatch",
        "unknown": "asset_store_cleanup_unknown_job",
        "unavailable": "asset_store_cleanup_unavailable",
        "abandoned": "asset_store_cleanup_abandoned",
    }.get(state, "asset_store_cleanup_unverified")


def _original_failure_reason(reason: str | None) -> str:
    if reason is None:
        return "image_generation_failed"
    return reason.partition(";cleanup_pending:")[0] or "image_generation_failed"


def _requested_facts_from_job(job: ImageOutputJob) -> dict[str, object]:
    keys = (
        "sources",
        "scene_style_id",
        "style_card_version_id",
        "output_ratio",
        "generation_mode",
        "synthetic_test_only",
        "publishable",
    )
    facts = {key: job.request_metadata[key] for key in keys}
    if "slot_key" in job.request_metadata:
        facts["slot_key"] = job.request_metadata["slot_key"]
    for key in (
        "product_identity_card_version_id",
        "product_identity_card_sha256",
        "product_identity_card",
        "wear_presentation",
    ):
        if key in job.request_metadata:
            facts[key] = job.request_metadata[key]
    return facts


def _generation_request_from_job(
    job: ImageOutputJob, sources: tuple[GenerationSource, ...] = ()
) -> GenerationRequest:
    metadata = job.request_metadata
    return GenerationRequest(
        output_type=job.output_type,
        product_ids=tuple(cast(list[str], metadata["source_product_ids"])),
        asset_ids=tuple(
            UUID(str(value)) for value in cast(list[str], metadata["source_asset_ids"])
        ),
        style_card_version_id=UUID(str(metadata["style_card_version_id"])),
        main_style_card_version_id=(
            UUID(str(metadata["main_style_card_version_id"]))
            if metadata["main_style_card_version_id"]
            else None
        ),
        scene_style_id=UUID(str(metadata["scene_style_id"])),
        output_ratio=job.output_ratio,
        sources=sources,
        slot_key=cast(str, metadata.get("slot_key", "legacy")),
        slot_rules=deepcopy(cast(dict[str, object], metadata.get("slot_rules", {}))),
        style_observations=deepcopy(
            cast(dict[str, object], metadata.get("style_observations", {}))
        ),
        product_identity_card_version_id=(
            UUID(str(metadata["product_identity_card_version_id"]))
            if metadata.get("product_identity_card_version_id")
            else None
        ),
        product_identity_card_sha256=cast(
            str | None, metadata.get("product_identity_card_sha256")
        ),
        product_identity_card=deepcopy(
            cast(dict[str, object] | None, metadata.get("product_identity_card"))
        ),
        wear_presentation=deepcopy(
            cast(dict[str, object] | None, metadata.get("wear_presentation"))
        ),
        style_reference_manifest_sha256=cast(
            str | None, metadata.get("style_reference_manifest_sha256")
        ),
        candidate_count=int(metadata.get("candidate_count", SYNTHETIC_CANDIDATE_COUNT)),
    )


def _expected_artifact_sequences(job: ImageOutputJob) -> tuple[int, ...]:
    raw = job.expected_artifact_sequences
    if (
        not isinstance(raw, list)
        or not raw
        or any(not isinstance(value, int) for value in raw)
    ):
        return EXPECTED_ARTIFACT_SEQUENCES
    return tuple(raw)


def _safe_code(value: str, fallback: str) -> str:
    return value if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,254}", value) else fallback
