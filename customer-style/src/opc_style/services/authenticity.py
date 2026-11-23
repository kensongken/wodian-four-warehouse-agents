"""Customer-scoped, append-only authenticity-gate orchestration."""

import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from marshal import dumps as marshal_dumps
from types import MethodType
from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from opc_style.domain.authenticity import (
    DETERMINISTIC_LOCAL_AUTHENTICITY_CHECKER_VERSION,
    REQUIRED_AUTHENTICITY_CHECKS,
    SYNTHETIC_LOCAL_AUTHENTICITY_MODE,
    AuthenticityChecker,
    AuthenticityResult,
    AuthenticityRuleResult,
    DeterministicLocalAuthenticityChecker,
    current_authenticity_outcome,
    derive_candidate_batch_state,
)
from opc_style.domain.image_generator import DETERMINISTIC_LOCAL_ADAPTER_VERSION
from opc_style.models import (
    AuditEvent,
    AuthenticityCheck,
    AuthenticityCompletion,
    ImageCandidate,
    ImageOutputJob,
    ProductAsset,
    ProductSubjectSeal,
    SceneStyle,
    StyleCard,
    StyleCardVersion,
)
from opc_style.repositories import ProductImageRepository
from opc_style.services.image_jobs import (
    EXPECTED_ARTIFACT_SEQUENCES,
    _forget_local_store,
    _local_job_lock,
    _local_store_matches,
)
from opc_style.services.real_photos import AssetContentStore

_OFFICIAL_LOCAL_CHECK_IMPLEMENTATION = DeterministicLocalAuthenticityChecker.check
_OFFICIAL_LOCAL_CHECK_CODE = _OFFICIAL_LOCAL_CHECK_IMPLEMENTATION.__code__
_OFFICIAL_LOCAL_CHECK_CODE_FINGERPRINT = sha256(
    marshal_dumps(_OFFICIAL_LOCAL_CHECK_CODE)
).hexdigest()
_OFFICIAL_LOCAL_CHECKER_VERSION_ATTRIBUTE = (
    DeterministicLocalAuthenticityChecker.__dict__["checker_version"]
)


class AuthenticityJobNotFoundError(Exception):
    """Conceal a missing or foreign customer-owned job."""


class AuthenticityConflictError(Exception):
    """Reject a caller-visible state or history conflict."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class AuthenticityOutcomeUncertainError(Exception):
    """Fail closed when exact local artifact proof is unavailable."""


@dataclass(frozen=True)
class AuthenticityCandidateOutput:
    """One candidate with event-derived state and immutable checks."""

    candidate: ImageCandidate
    state: str
    checks: tuple[AuthenticityCheck, ...]


@dataclass(frozen=True)
class AuthenticityGateOutput:
    """One complete job-level gate result."""

    job: ImageOutputJob
    candidates: tuple[AuthenticityCandidateOutput, ...]
    checker_version: str
    created: bool


class AuthenticityService:
    """Run one local rule set atomically across exactly three candidates."""

    def __init__(
        self,
        session: Session,
        content_store: AssetContentStore,
        checker: AuthenticityChecker,
    ) -> None:
        self._session = session
        self._repository = ProductImageRepository(session)
        self._content_store = content_store
        self._checker = checker

    def run(self, customer_id: UUID, job_id: UUID) -> AuthenticityGateOutput:
        """Append all checks once, or replay the one durable terminal result."""
        with _local_job_lock(job_id):
            job = self._repository.get_output_job(customer_id, job_id)
            if job is None:
                raise AuthenticityJobNotFoundError
            if not _is_official_local_checker(self._checker):
                raise AuthenticityConflictError("unsupported_authenticity_checker")
            if job.status in {"awaiting_customer_review", "authenticity_failed"}:
                result = self._load_replay(customer_id, job, created=False)
                if job.status == "authenticity_failed":
                    _forget_local_store(job.id, self._content_store)
                return result
            if job.status != "machine_check":
                raise AuthenticityConflictError("authenticity_job_not_machine_check")

            candidates = tuple(self._repository.list_candidates(customer_id, job.id))
            if (
                len(candidates) != 3
                or tuple(item.sequence_number for item in candidates)
                != EXPECTED_ARTIFACT_SEQUENCES
            ):
                raise AuthenticityConflictError(
                    "authenticity_requires_exactly_three_candidates"
                )
            if any(
                self._repository.list_authenticity_checks(customer_id, candidate.id)
                for candidate in candidates
            ):
                raise AuthenticityConflictError("authenticity_check_history_conflict")
            self._verify_exact_store_scope(job)
            provenance_valid = self._database_provenance_matches(
                customer_id, job, candidates
            )

            persisted_by_candidate: dict[UUID, tuple[AuthenticityCheck, ...]] = {}
            any_failed = False
            for candidate in candidates:
                self._verify_artifact_immediately_before_read(job, candidate)
                check_result = self._safe_check(job, candidate)
                if not provenance_valid:
                    check_result = _fail_subject_provenance(check_result)
                rows = tuple(
                    AuthenticityCheck(
                        customer_id=customer_id,
                        image_candidate_id=candidate.id,
                        check_type=check.check_type,
                        attempt=1,
                        passed=check.passed,
                        reason=check.reason,
                        checker_version=check_result.checker_version,
                        details=check.details,
                        created_at=datetime.now(UTC),
                    )
                    for check in check_result.checks
                )
                self._session.add_all(rows)
                persisted_by_candidate[candidate.id] = rows
                any_failed = any_failed or any(not row.passed for row in rows)

            new_status = (
                "authenticity_failed" if any_failed else "awaiting_customer_review"
            )
            failure_reason = "authenticity_check_failed" if any_failed else None
            transitioned = self._repository.transition_output_job(
                customer_id,
                job.id,
                new_status,
                failure_reason=failure_reason,
            )
            if transitioned is None:
                raise AuthenticityJobNotFoundError
            # Flush the job transition and all 15 immutable check rows before the
            # completion seal is created. They still commit atomically below.
            self._session.flush()
            completed_at = datetime.now(UTC)
            batch_digest = _authenticity_batch_digest(
                transitioned,
                candidates,
                tuple(persisted_by_candidate[candidate.id] for candidate in candidates),
                result_status=new_status,
            )
            completion = AuthenticityCompletion(
                customer_id=customer_id,
                output_job_id=job.id,
                checker_version=self._checker.checker_version,
                check_mode=SYNTHETIC_LOCAL_AUTHENTICITY_MODE,
                candidate_batch_digest=batch_digest,
                previous_status="machine_check",
                result_status=new_status,
                created_at=completed_at,
            )
            self._session.add(completion)
            self._session.flush()
            self._session.add(
                AuditEvent(
                    customer_id=customer_id,
                    event_type="image_output.authenticity_completed",
                    payload={
                        "job_id": str(job.id),
                        "checker_version": self._checker.checker_version,
                        "check_mode": SYNTHETIC_LOCAL_AUTHENTICITY_MODE,
                        "visual_ai": False,
                        "candidate_count": len(candidates),
                        "previous_status": "machine_check",
                        "result_status": new_status,
                        "completion_id": str(completion.id),
                        "candidate_batch_digest": batch_digest,
                    },
                    occurred_at=completed_at,
                )
            )
            try:
                self._session.commit()
            except Exception as error:
                self._session.rollback()
                persisted_job = self._repository.get_output_job(customer_id, job.id)
                if persisted_job is None or persisted_job.status not in {
                    "awaiting_customer_review",
                    "authenticity_failed",
                }:
                    raise AuthenticityOutcomeUncertainError from error
                replay = self._load_replay(customer_id, persisted_job, created=False)
                if persisted_job.status == "authenticity_failed":
                    _forget_local_store(job.id, self._content_store)
                return replay

            batch_state = derive_candidate_batch_state(
                new_status,
                tuple(persisted_by_candidate[candidate.id] for candidate in candidates),
            )
            output = AuthenticityGateOutput(
                job=transitioned,
                candidates=tuple(
                    AuthenticityCandidateOutput(
                        candidate=candidate,
                        state=batch_state,
                        checks=persisted_by_candidate[candidate.id],
                    )
                    for candidate in candidates
                ),
                checker_version=self._checker.checker_version,
                created=True,
            )
            if transitioned.status == "authenticity_failed":
                _forget_local_store(job.id, self._content_store)
            return output

    def verify_candidate_for_customer_review(
        self,
        customer_id: UUID,
        job: ImageOutputJob,
        candidate: ImageCandidate,
    ) -> None:
        """Re-attest exact local scope, provenance and bytes before human review."""
        candidates = tuple(self._repository.list_candidates(customer_id, job.id))
        if (
            len(candidates) != 3
            or tuple(item.sequence_number for item in candidates)
            != EXPECTED_ARTIFACT_SEQUENCES
            or candidate.id not in {item.id for item in candidates}
            or not self._database_provenance_matches(customer_id, job, candidates)
        ):
            raise AuthenticityOutcomeUncertainError
        self._verify_exact_store_scope(job)
        self._verify_artifact_immediately_before_read(job, candidate)

    def has_trusted_pass_completion(
        self,
        customer_id: UUID,
        job: ImageOutputJob,
        candidates: tuple[ImageCandidate, ...],
    ) -> bool:
        """Require the immutable Task 5 seal and its matching audit provenance."""
        return self._completion_matches(
            customer_id,
            job,
            candidates,
            expected_status="awaiting_customer_review",
        )

    def _safe_check(
        self, job: ImageOutputJob, candidate: ImageCandidate
    ) -> AuthenticityResult:
        try:
            result = self._checker.check(job, candidate)
            _validate_result(result, candidate.id, self._checker.checker_version)
            return result
        except Exception:  # noqa: BLE001 - checker errors become explicit failures
            return _failed_result(candidate.id, "checker_exception")

    def _verify_exact_store_scope(self, job: ImageOutputJob) -> None:
        try:
            matches = (
                _local_store_matches(job.id, self._content_store)
                and job.artifact_store_identity == self._content_store.store_identity
                and job.artifact_store_namespace == self._content_store.store_namespace
                and job.expected_artifact_sequences == list(EXPECTED_ARTIFACT_SEQUENCES)
            )
        except Exception:  # noqa: BLE001 - storage proof is fail-closed
            matches = False
        if not matches:
            raise AuthenticityOutcomeUncertainError

    def _verify_artifact_immediately_before_read(
        self, job: ImageOutputJob, candidate: ImageCandidate
    ) -> None:
        try:
            verified = self._content_store.verify_derivative(
                job.customer_id,
                job.id,
                candidate.sequence_number,
                candidate.content_sha256,
            )
        except Exception:  # noqa: BLE001 - storage proof is fail-closed
            verified = False
        if not verified:
            raise AuthenticityOutcomeUncertainError

    def _database_provenance_matches(
        self,
        customer_id: UUID,
        job: ImageOutputJob,
        candidates: tuple[ImageCandidate, ...],
    ) -> bool:
        metadata = job.request_metadata
        if job.output_type == "real_photo":
            scalar_product = metadata.get("product_id")
            scalar_asset = metadata.get("asset_id")
            if (
                not isinstance(scalar_product, str)
                or not scalar_product
                or _uuid_or_none(scalar_asset) is None
            ):
                return False
            expected_products = [scalar_product]
            expected_assets = [str(scalar_asset)]
            listed_products = metadata.get("source_product_ids")
            listed_assets = metadata.get("source_asset_ids")
            if (
                listed_products is not None
                and _string_list(listed_products) != expected_products
            ) or (
                listed_assets is not None
                and _string_list(listed_assets) != expected_assets
            ):
                return False
        else:
            expected_products = _string_list(metadata.get("source_product_ids"))
            expected_assets = _string_list(metadata.get("source_asset_ids"))
            source_count = len(expected_products)
            valid_count = (
                source_count == 1
                if job.output_type in {"scene", "product_style", "wear"}
                else 2 <= source_count <= 5
            )
            if (
                job.output_type
                not in {"scene", "multi_product", "product_style", "wear"}
                or not valid_count
                or len(expected_assets) != source_count
                or len(set(expected_products)) != source_count
                or len(set(expected_assets)) != source_count
                or metadata.get("sources")
                != [
                    {"product_id": product_id, "asset_id": asset_id}
                    for product_id, asset_id in zip(
                        expected_products, expected_assets, strict=True
                    )
                ]
            ):
                return False
        links = self._repository.list_asset_links(customer_id, job.id)
        if [str(link.product_asset_id) for link in links] != expected_assets:
            return False
        maybe_assets = [
            self._repository.get_asset(customer_id, link.product_asset_id)
            for link in links
        ]
        if any(asset is None for asset in maybe_assets):
            return False
        assets = [asset for asset in maybe_assets if asset is not None]
        if [asset.product_id for asset in assets] != expected_products:
            return False
        base_candidate_trace_matches = all(
            candidate.customer_id == customer_id
            and candidate.output_job_id == job.id
            and candidate.adapter_version == job.adapter_version
            and candidate.artifact_metadata.get("output_job_id") == str(job.id)
            and candidate.artifact_metadata.get("sequence_number")
            == candidate.sequence_number
            and candidate.artifact_metadata.get("adapter_version")
            == job.adapter_version
            and candidate.artifact_metadata.get("output_type") == job.output_type
            and candidate.artifact_metadata.get("output_ratio") == job.output_ratio
            and _string_list(candidate.artifact_metadata.get("source_product_ids"))
            == expected_products
            and _string_list(candidate.artifact_metadata.get("source_asset_ids"))
            == expected_assets
            for candidate in candidates
        )
        if not base_candidate_trace_matches:
            return False
        if job.output_type in {"scene", "multi_product", "wear"}:
            return self._scene_style_provenance_matches(customer_id, job, candidates)
        if job.output_type == "product_style":
            return self._identity_lock_provenance_matches(
                customer_id, job, candidates, assets
            )
        if job.output_type == "real_photo":
            return self._real_photo_provenance_matches(job, candidates)
        return False

    def _identity_lock_provenance_matches(
        self,
        customer_id: UUID,
        job: ImageOutputJob,
        candidates: tuple[ImageCandidate, ...],
        assets: list[ProductAsset],
    ) -> bool:
        if (
            job.adapter_version != "identity-locked-style-candidate-v1"
            or job.subject_seal_id is None
            or job.style_card_version_id is None
            or job.scene_style_id is None
            or len(assets) != 1
        ):
            return False
        seal = self._session.scalar(
            select(ProductSubjectSeal).where(
                ProductSubjectSeal.customer_id == customer_id,
                ProductSubjectSeal.id == job.subject_seal_id,
                ProductSubjectSeal.product_asset_id == assets[0].id,
                ProductSubjectSeal.product_id == assets[0].product_id,
                ProductSubjectSeal.publishable.is_(False),
            )
        )
        scene = self._session.scalar(
            select(SceneStyle).where(
                SceneStyle.customer_id == customer_id,
                SceneStyle.id == job.scene_style_id,
                SceneStyle.active.is_(True),
            )
        )
        version = self._session.scalar(
            select(StyleCardVersion).where(
                StyleCardVersion.customer_id == customer_id,
                StyleCardVersion.id == job.style_card_version_id,
            )
        )
        return (
            seal is not None
            and scene is not None
            and version is not None
            and scene.style_card_id == version.style_card_id
            and all(
                candidate.identity == "identity_locked_style_candidate"
                and candidate.mime_type == "image/png"
                and candidate.artifact_metadata.get("identity_lock_code")
                == "identity_lock_passed"
                and candidate.artifact_metadata.get("subject_rgba_sha256")
                == seal.normalized_subject_sha256
                and candidate.artifact_metadata.get("core_mask_sha256")
                == seal.mask_sha256
                for candidate in candidates
            )
        )

    def _scene_style_provenance_matches(
        self,
        customer_id: UUID,
        job: ImageOutputJob,
        candidates: tuple[ImageCandidate, ...],
    ) -> bool:
        if (
            job.adapter_version != DETERMINISTIC_LOCAL_ADAPTER_VERSION
            or job.style_card_version_id is None
            or job.scene_style_id is None
        ):
            return False
        scene = self._session.scalar(
            select(SceneStyle).where(
                SceneStyle.customer_id == customer_id,
                SceneStyle.id == job.scene_style_id,
                SceneStyle.active.is_(True),
            )
        )
        if scene is None or scene.style_card_id is None:
            return False
        scene_card = self._session.scalar(
            select(StyleCard).where(
                StyleCard.customer_id == customer_id,
                StyleCard.id == scene.style_card_id,
                StyleCard.kind == "scene",
                StyleCard.active.is_(True),
            )
        )
        if scene_card is None:
            return False
        scene_version = self._session.scalar(
            select(StyleCardVersion).where(
                StyleCardVersion.customer_id == customer_id,
                StyleCardVersion.id == job.style_card_version_id,
                StyleCardVersion.style_card_id == scene_card.id,
            )
        )
        if scene_version is None:
            return False
        main_version_id = _uuid_or_none(
            scene_version.content.get("main_style_card_version_id")
        )
        if main_version_id is None:
            return False
        main_version = self._session.scalar(
            select(StyleCardVersion).where(
                StyleCardVersion.customer_id == customer_id,
                StyleCardVersion.id == main_version_id,
            )
        )
        if main_version is None:
            return False
        main_card = self._session.scalar(
            select(StyleCard).where(
                StyleCard.customer_id == customer_id,
                StyleCard.id == main_version.style_card_id,
                StyleCard.kind == "main",
                StyleCard.active.is_(True),
            )
        )
        if main_card is None:
            return False
        metadata = job.request_metadata
        if (
            _uuid_or_none(metadata.get("style_card_version_id")) != scene_version.id
            or _uuid_or_none(metadata.get("main_style_card_version_id"))
            != main_version.id
            or _uuid_or_none(metadata.get("scene_style_id")) != scene.id
            or _uuid_or_none(metadata.get("scene_style_card_id")) != scene_card.id
            or metadata.get("generation_mode") != "synthetic_test_only"
            or metadata.get("synthetic_test_only") is not True
            or metadata.get("publishable") is not False
        ):
            return False
        return all(
            _uuid_or_none(candidate.artifact_metadata.get("style_card_version_id"))
            == scene_version.id
            and _uuid_or_none(
                candidate.artifact_metadata.get("main_style_card_version_id")
            )
            == main_version.id
            and _uuid_or_none(candidate.artifact_metadata.get("scene_style_id"))
            == scene.id
            and _uuid_or_none(candidate.artifact_metadata.get("scene_style_card_id"))
            == scene_card.id
            and candidate.identity == "synthetic_style_image_candidate"
            and candidate.artifact_metadata.get("generation_mode")
            == "synthetic_test_only"
            and candidate.artifact_metadata.get("synthetic_test_only") is True
            and candidate.artifact_metadata.get("publishable") is False
            for candidate in candidates
        )

    @staticmethod
    def _real_photo_provenance_matches(
        job: ImageOutputJob, candidates: tuple[ImageCandidate, ...]
    ) -> bool:
        metadata = job.request_metadata
        return (
            job.adapter_version == "pillow-catalog-v1"
            and job.style_card_version_id is None
            and job.scene_style_id is None
            and job.output_ratio == "source"
            and metadata.get("profile") == "catalog-v1"
            and metadata.get("generation_mode") == "real_photo_normalization"
            and metadata.get("is_ai_generated") is False
            and not _contains_style_or_scene_metadata(metadata)
            and all(
                candidate.identity == "normalized_real_photo_derivative"
                and candidate.artifact_metadata.get("profile") == "catalog-v1"
                and candidate.artifact_metadata.get("generation_mode")
                == "real_photo_normalization"
                and candidate.artifact_metadata.get("is_ai_generated") is False
                and not _contains_style_or_scene_metadata(candidate.artifact_metadata)
                for candidate in candidates
            )
        )

    def _load_replay(
        self, customer_id: UUID, job: ImageOutputJob, *, created: bool
    ) -> AuthenticityGateOutput:
        candidates = tuple(self._repository.list_candidates(customer_id, job.id))
        if (
            len(candidates) != 3
            or tuple(item.sequence_number for item in candidates)
            != EXPECTED_ARTIFACT_SEQUENCES
        ):
            raise AuthenticityOutcomeUncertainError
        persisted: list[tuple[ImageCandidate, tuple[AuthenticityCheck, ...]]] = []
        any_failed = False
        for candidate in candidates:
            checks = tuple(
                self._repository.list_authenticity_checks(customer_id, candidate.id)
            )
            outcome = current_authenticity_outcome(checks)
            if outcome is None:
                raise AuthenticityOutcomeUncertainError
            any_failed = any_failed or outcome == "failed"
            persisted.append((candidate, checks))
        expected_status = (
            "authenticity_failed" if any_failed else "awaiting_customer_review"
        )
        if job.status != expected_status:
            raise AuthenticityOutcomeUncertainError
        if not self._completion_matches(
            customer_id, job, candidates, expected_status=expected_status
        ):
            raise AuthenticityOutcomeUncertainError
        batch_state = derive_candidate_batch_state(
            job.status, tuple(checks for _, checks in persisted)
        )
        return AuthenticityGateOutput(
            job=job,
            candidates=tuple(
                AuthenticityCandidateOutput(
                    candidate=candidate, state=batch_state, checks=checks
                )
                for candidate, checks in persisted
            ),
            checker_version=DETERMINISTIC_LOCAL_AUTHENTICITY_CHECKER_VERSION,
            created=created,
        )

    def _completion_matches(
        self,
        customer_id: UUID,
        job: ImageOutputJob,
        candidates: tuple[ImageCandidate, ...],
        *,
        expected_status: str,
    ) -> bool:
        completion = self._repository.get_authenticity_completion(customer_id, job.id)
        if (
            completion is None
            or job.status != expected_status
            or completion.result_status != expected_status
            or completion.checker_version
            != DETERMINISTIC_LOCAL_AUTHENTICITY_CHECKER_VERSION
            or completion.check_mode != SYNTHETIC_LOCAL_AUTHENTICITY_MODE
            or completion.previous_status != "machine_check"
        ):
            return False
        check_batches = tuple(
            tuple(self._repository.list_authenticity_checks(customer_id, candidate.id))
            for candidate in candidates
        )
        if any(
            current_authenticity_outcome(checks) is None for checks in check_batches
        ):
            return False
        expected_digest = _authenticity_batch_digest(
            job,
            candidates,
            check_batches,
            result_status=expected_status,
        )
        if completion.candidate_batch_digest != expected_digest:
            return False
        expected_payload = {
            "job_id": str(job.id),
            "checker_version": DETERMINISTIC_LOCAL_AUTHENTICITY_CHECKER_VERSION,
            "check_mode": SYNTHETIC_LOCAL_AUTHENTICITY_MODE,
            "visual_ai": False,
            "candidate_count": len(candidates),
            "previous_status": "machine_check",
            "result_status": expected_status,
            "completion_id": str(completion.id),
            "candidate_batch_digest": expected_digest,
        }
        matching_events = [
            event
            for event in self._repository.list_authenticity_completion_events(
                customer_id
            )
            if event.payload == expected_payload
        ]
        return len(matching_events) == 1


def _authenticity_batch_digest(
    job: ImageOutputJob,
    candidates: tuple[ImageCandidate, ...],
    check_batches: tuple[tuple[AuthenticityCheck, ...], ...],
    *,
    result_status: str,
) -> str:
    """Seal immutable candidate identities and every fixed check result."""
    payload = {
        "customer_id": str(job.customer_id),
        "job_id": str(job.id),
        "checker_version": DETERMINISTIC_LOCAL_AUTHENTICITY_CHECKER_VERSION,
        "check_mode": SYNTHETIC_LOCAL_AUTHENTICITY_MODE,
        "previous_status": "machine_check",
        "result_status": result_status,
        "candidates": [
            {
                "id": str(candidate.id),
                "sequence_number": candidate.sequence_number,
                "identity": candidate.identity,
                "content_sha256": candidate.content_sha256,
                "adapter_version": candidate.adapter_version,
                "checks": [
                    {
                        "check_type": check.check_type,
                        "attempt": check.attempt,
                        "passed": check.passed,
                        "reason": check.reason,
                        "checker_version": check.checker_version,
                        "details": check.details,
                    }
                    for check in sorted(checks, key=lambda item: item.check_type)
                ],
            }
            for candidate, checks in zip(candidates, check_batches, strict=True)
        ],
    }
    encoded = json.dumps(
        payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _validate_result(
    result: AuthenticityResult, candidate_id: UUID, checker_version: str
) -> None:
    if (
        result.candidate_id != candidate_id
        or result.checker_version != checker_version
        or tuple(check.check_type for check in result.checks)
        != REQUIRED_AUTHENTICITY_CHECKS
    ):
        raise ValueError("invalid_authenticity_checker_result")


def _is_official_local_checker(checker: AuthenticityChecker) -> bool:
    """Bind trust to the exact built-in type and unmodified check method."""
    bound_check = getattr(checker, "check", None)
    return (
        type(checker) is DeterministicLocalAuthenticityChecker
        and "check" not in checker.__dict__
        and "checker_version" not in checker.__dict__
        and type(bound_check) is MethodType
        and bound_check.__self__ is checker
        and bound_check.__func__ is _OFFICIAL_LOCAL_CHECK_IMPLEMENTATION
        and DeterministicLocalAuthenticityChecker.__dict__.get("check")
        is _OFFICIAL_LOCAL_CHECK_IMPLEMENTATION
        and bound_check.__func__.__code__ is _OFFICIAL_LOCAL_CHECK_CODE
        and sha256(marshal_dumps(bound_check.__func__.__code__)).hexdigest()
        == _OFFICIAL_LOCAL_CHECK_CODE_FINGERPRINT
        and DeterministicLocalAuthenticityChecker.__dict__.get("checker_version")
        is _OFFICIAL_LOCAL_CHECKER_VERSION_ATTRIBUTE
        and checker.checker_version is _OFFICIAL_LOCAL_CHECKER_VERSION_ATTRIBUTE
        and checker.checker_version == DETERMINISTIC_LOCAL_AUTHENTICITY_CHECKER_VERSION
    )


def _failed_result(candidate_id: UUID, reason: str) -> AuthenticityResult:
    details: dict[str, object] = {
        "check_mode": SYNTHETIC_LOCAL_AUTHENTICITY_MODE,
        "visual_ai": False,
        "synthetic_local_rules_only": True,
    }
    return AuthenticityResult(
        candidate_id=candidate_id,
        checker_version=DETERMINISTIC_LOCAL_AUTHENTICITY_CHECKER_VERSION,
        checks=tuple(
            AuthenticityRuleResult(check_type, False, reason, details)
            for check_type in REQUIRED_AUTHENTICITY_CHECKS
        ),
    )


def _fail_subject_provenance(result: AuthenticityResult) -> AuthenticityResult:
    return replace(
        result,
        checks=tuple(
            replace(
                check,
                passed=False,
                reason="database_source_provenance_mismatch",
            )
            if check.check_type == "subject_match"
            else check
            for check in result.checks
        ),
    )


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        return []
    return cast(list[str], value)


def _uuid_or_none(value: object) -> UUID | None:
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _contains_style_or_scene_metadata(metadata: dict[str, object]) -> bool:
    return any(
        {"style", "scene"}.intersection(key.lower().split("_")) for key in metadata
    )
