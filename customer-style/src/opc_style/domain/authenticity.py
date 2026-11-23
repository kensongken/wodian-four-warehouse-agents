"""Deterministic local-only authenticity rules for synthetic fixtures."""

from collections.abc import Iterable
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from typing import Literal, Protocol, cast
from uuid import UUID

from PIL import Image, UnidentifiedImageError

from opc_style.domain.errors import DomainRuleError
from opc_style.domain.product_identity import (
    ProductIdentityDraft,
    identity_card_payload,
)
from opc_style.models import ImageCandidate, ImageOutputJob

DETERMINISTIC_LOCAL_AUTHENTICITY_CHECKER_VERSION = (
    "deterministic-local-authenticity-rules-v1"
)
SYNTHETIC_LOCAL_AUTHENTICITY_MODE = "synthetic_local_rule_based"
REQUIRED_AUTHENTICITY_CHECKS = (
    "subject_match",
    "component_count",
    "color_shift",
    "artifact_scan",
    "dimensions",
)
CURRENT_AUTHENTICITY_ATTEMPT = 1
EXPECTED_AUTHENTICITY_CANDIDATE_COUNT = 3
MIN_FIDELITY_CANDIDATE_COUNT = 1
MAX_FIDELITY_CANDIDATE_COUNT = 3
ALLOWED_FIDELITY_FLAGS = frozenset(
    {
        "subject_incomplete",
        "component_count_changed",
        "primary_color_changed",
        "core_hardware_changed",
        "product_form_changed",
        "product_category_changed",
        "component_shape_changed",
        "component_arrangement_changed",
        "core_accessory_changed",
        "provider_check_unavailable",
        "minor_color_drift",
        "minor_detail_uncertain",
        "background_style_drift",
    }
)
FidelityState = Literal["green", "yellow", "red", "unknown"]
YELLOW_FIDELITY_FLAGS = frozenset(
    {"minor_color_drift", "minor_detail_uncertain", "background_style_drift"}
)
FIDELITY_COMPARISON_FIELDS = frozenset(
    {
        "category",
        "primary_colors",
        "visible_surface_appearance",
        "component_shapes",
        "visible_component_count",
        "component_arrangement",
        "core_accessories",
        "overall_form",
        "subject_completeness",
    }
)
FIDELITY_COMPARISON_VERDICTS = frozenset(
    {"match", "contradiction", "unclear"}
)


def validate_fidelity_decision(
    state: FidelityState, flags: Iterable[str]
) -> tuple[str, ...]:
    """Normalize the bounded fidelity vocabulary and reject contradictions."""
    normalized = tuple(sorted(set(flags)))
    if any(flag not in ALLOWED_FIDELITY_FLAGS for flag in normalized):
        raise ValueError("fidelity_flag_invalid")
    if state == "green" and normalized:
        raise ValueError("green_fidelity_cannot_have_flags")
    if state == "red" and not normalized:
        raise ValueError("red_fidelity_requires_flag")
    if state == "yellow" and (
        not normalized or not set(normalized) <= YELLOW_FIDELITY_FLAGS
    ):
        raise ValueError("yellow_fidelity_requires_minor_flag")
    if state == "unknown" and normalized != ("provider_check_unavailable",):
        raise ValueError("unknown_fidelity_requires_provider_flag")
    return normalized


@dataclass(frozen=True)
class AuthenticityRuleResult:
    """One stable local rule outcome; it is not a visual-AI judgment."""

    check_type: str
    passed: bool
    reason: str
    details: dict[str, object]


@dataclass(frozen=True)
class AuthenticityResult:
    """Complete required rule set for one immutable candidate."""

    candidate_id: UUID
    checker_version: str
    checks: tuple[AuthenticityRuleResult, ...]


class AuthenticityChecker(Protocol):
    """Replaceable checker boundary used only after storage attestation."""

    checker_version: str

    def check(
        self, job: ImageOutputJob, candidate: ImageCandidate
    ) -> AuthenticityResult:
        """Return all required rules for one candidate."""
        ...


@dataclass(frozen=True)
class FidelityAssessment:
    """Provider-neutral visual comparison result requiring human disposition."""

    state: FidelityState
    flags: tuple[str, ...]
    assessor_version: str
    candidate_id: UUID | None = None
    summary_zh: str = ""
    comparisons: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "flags", validate_fidelity_decision(self.state, self.flags)
        )
        if not self.assessor_version or not self.summary_zh.strip():
            raise ValueError("fidelity_assessment_invalid")
        comparison_fields = {field for field, _verdict in self.comparisons}
        if (
            len(comparison_fields) != len(self.comparisons)
            or comparison_fields != FIDELITY_COMPARISON_FIELDS
            or any(
                verdict not in FIDELITY_COMPARISON_VERDICTS
                for _field, verdict in self.comparisons
            )
        ):
            raise ValueError("fidelity_comparisons_invalid")


@dataclass(frozen=True)
class FidelityCandidateInput:
    """One attested candidate image supplied to the fidelity boundary."""

    candidate_id: UUID
    content: bytes
    content_sha256: str
    mime_type: Literal["image/jpeg", "image/png"]

    def __post_init__(self) -> None:
        if (
            not self.content
            or sha256(self.content).hexdigest() != self.content_sha256
            or self.mime_type not in {"image/jpeg", "image/png"}
        ):
            raise ValueError("fidelity_candidate_invalid")


@dataclass(frozen=True)
class FidelityBatchRequest:
    """One product identity compared with one bounded candidate batch."""

    identity_card_version_id: UUID
    identity_card: ProductIdentityDraft
    product_content: bytes
    product_sha256: str
    product_mime_type: Literal["image/jpeg", "image/png"]
    candidates: tuple[FidelityCandidateInput, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.identity_card_version_id, UUID):
            raise TypeError("fidelity_identity_card_invalid")
        try:
            identity_card_payload(self.identity_card)
        except DomainRuleError as error:
            raise ValueError("fidelity_identity_card_invalid") from error
        if (
            not MIN_FIDELITY_CANDIDATE_COUNT
            <= len(self.candidates)
            <= MAX_FIDELITY_CANDIDATE_COUNT
        ):
            raise ValueError("fidelity_batch_candidate_count_invalid")
        if len({candidate.candidate_id for candidate in self.candidates}) != len(
            self.candidates
        ):
            raise ValueError("fidelity_batch_candidate_ids_must_be_unique")
        if (
            not self.product_content
            or sha256(self.product_content).hexdigest() != self.product_sha256
            or self.product_mime_type not in {"image/jpeg", "image/png"}
        ):
            raise ValueError("fidelity_product_invalid")


@dataclass(frozen=True)
class FidelityBatchAssessment:
    """Ordered, bounded results for one immutable candidate batch."""

    assessments: tuple[FidelityAssessment, ...]
    assessor_version: str

    def __post_init__(self) -> None:
        candidate_ids = tuple(item.candidate_id for item in self.assessments)
        if (
            not MIN_FIDELITY_CANDIDATE_COUNT
            <= len(self.assessments)
            <= MAX_FIDELITY_CANDIDATE_COUNT
            or any(candidate_id is None for candidate_id in candidate_ids)
            or len(set(candidate_ids)) != len(candidate_ids)
            or not self.assessor_version
        ):
            raise ValueError("fidelity_batch_assessment_invalid")


class FidelityAssessor(Protocol):
    """Replaceable product-vs-candidate assessor with no publication authority."""

    assessor_version: str

    def assess(
        self,
        *,
        product_content: bytes,
        product_sha256: str,
        candidate_content: bytes,
        candidate_sha256: str,
    ) -> FidelityAssessment:
        """Return bounded red/yellow/green evidence; never choose a final image."""
        ...


class FidelityBatchAssessor(Protocol):
    """Replaceable batch comparison boundary with no publication authority."""

    assessor_version: str

    def assess_batch(self, request: FidelityBatchRequest) -> FidelityBatchAssessment:
        """Compare the product with one to three candidates in one request."""
        ...


class _PersistedCheck(Protocol):
    check_type: str
    passed: bool
    checker_version: str
    attempt: int


def current_authenticity_outcome(
    checks: Iterable[_PersistedCheck],
) -> Literal["passed", "failed"] | None:
    """Accept only one complete set from the fixed checker and attempt."""
    materialized = tuple(checks)
    by_type = {check.check_type: check for check in materialized}
    if (
        len(materialized) != len(REQUIRED_AUTHENTICITY_CHECKS)
        or set(by_type) != set(REQUIRED_AUTHENTICITY_CHECKS)
        or any(
            check.checker_version != DETERMINISTIC_LOCAL_AUTHENTICITY_CHECKER_VERSION
            or check.attempt != CURRENT_AUTHENTICITY_ATTEMPT
            for check in materialized
        )
    ):
        return None
    return "failed" if any(not check.passed for check in materialized) else "passed"


def derive_candidate_state(job_status: str, checks: Iterable[_PersistedCheck]) -> str:
    """Derive state from the fixed check set under the job-level batch gate."""
    if job_status == "authenticity_failed":
        return "authenticity_failed"
    if job_status != "awaiting_customer_review":
        return "pending_authenticity_check"
    outcome = current_authenticity_outcome(checks)
    if outcome == "passed":
        return "awaiting_customer_review"
    if outcome == "failed":
        return "authenticity_failed"
    return "pending_authenticity_check"


def derive_candidate_batch_state(
    job_status: str, candidate_checks: Iterable[Iterable[_PersistedCheck]]
) -> str:
    """Derive one fail-closed state for the complete three-candidate batch."""
    if job_status == "authenticity_failed":
        return "authenticity_failed"
    if job_status != "awaiting_customer_review":
        return "pending_authenticity_check"
    outcomes = tuple(
        current_authenticity_outcome(checks) for checks in candidate_checks
    )
    if len(outcomes) != EXPECTED_AUTHENTICITY_CANDIDATE_COUNT or any(
        outcome is None for outcome in outcomes
    ):
        return "pending_authenticity_check"
    if any(outcome == "failed" for outcome in outcomes):
        return "authenticity_failed"
    return "awaiting_customer_review"


class DeterministicLocalAuthenticityChecker:
    """Evaluate bytes and explicit synthetic fixture facts without network or AI."""

    checker_version = DETERMINISTIC_LOCAL_AUTHENTICITY_CHECKER_VERSION

    def check(
        self, job: ImageOutputJob, candidate: ImageCandidate
    ) -> AuthenticityResult:
        """Read one locally attested path and apply deterministic rules."""
        metadata = candidate.artifact_metadata
        fixture_value = metadata.get("local_authenticity_fixture")
        fixture = (
            cast(dict[str, object], fixture_value)
            if isinstance(fixture_value, dict)
            else {}
        )
        content: bytes | None
        read_error = False
        try:
            content = Path(candidate.artifact_path).read_bytes()
        except OSError:
            content = None
            read_error = True

        actual_hash = sha256(content).hexdigest() if content is not None else None
        image_format: str | None = None
        actual_size: tuple[int, int] | None = None
        decode_error = False
        if content is not None:
            try:
                with Image.open(BytesIO(content)) as image:
                    image.load()
                    image_format = image.format
                    actual_size = image.size
            except (UnidentifiedImageError, OSError, ValueError):
                decode_error = True

        expected_products, expected_assets = _expected_sources(job)
        candidate_products = _string_list(metadata.get("source_product_ids"))
        candidate_assets = _string_list(metadata.get("source_asset_ids"))
        subject_passed = (
            metadata.get("output_job_id") == str(job.id)
            and metadata.get("sequence_number") == candidate.sequence_number
            and metadata.get("adapter_version") == job.adapter_version
            and candidate.output_job_id == job.id
            and candidate.adapter_version == job.adapter_version
            and candidate_products == expected_products
            and candidate_assets == expected_assets
            and bool(expected_products)
            and bool(expected_assets)
            and fixture.get("subject_complete") is True
        )

        component_passed = all(
            fixture.get(name) is True
            for name in (
                "component_count_preserved",
                "product_shape_preserved",
                "material_preserved",
            )
        )
        color_passed = fixture.get("color_shift_within_fixture_tolerance") is True
        unexpected = fixture.get("unexpected_artifacts")
        identity_locked_png = (
            candidate.identity == "identity_locked_style_candidate"
            and image_format == "PNG"
            and candidate.mime_type == "image/png"
            and metadata.get("identity_lock_code") == "identity_lock_passed"
            and metadata.get("publishable") is False
        )
        legacy_jpeg = image_format == "JPEG" and candidate.mime_type == "image/jpeg"
        artifact_passed = (
            not read_error
            and not decode_error
            and (identity_locked_png or legacy_jpeg)
            and actual_hash == candidate.content_sha256
            and unexpected == []
        )
        dimensions_passed = (
            actual_size == (candidate.width, candidate.height)
            and candidate.width > 0
            and candidate.height > 0
        )

        common = {
            "check_mode": SYNTHETIC_LOCAL_AUTHENTICITY_MODE,
            "visual_ai": False,
            "synthetic_local_rules_only": True,
        }
        if read_error:
            artifact_reason = "artifact_read_failed"
        elif decode_error:
            artifact_reason = "artifact_not_decodable_jpeg"
        elif not (identity_locked_png or legacy_jpeg):
            artifact_reason = "artifact_format_or_identity_lock_invalid"
        elif actual_hash != candidate.content_sha256:
            artifact_reason = "artifact_hash_mismatch"
        elif unexpected != []:
            artifact_reason = "unexpected_artifact_fixture_marker"
        else:
            artifact_reason = "jpeg_hash_and_artifact_fixture_passed"
        checks = (
            AuthenticityRuleResult(
                "subject_match",
                subject_passed,
                (
                    "synthetic_subject_and_provenance_match"
                    if subject_passed
                    else "subject_or_provenance_mismatch"
                ),
                {**common, "rules": ["subject_complete", "source_trace_match"]},
            ),
            AuthenticityRuleResult(
                "component_count",
                component_passed,
                (
                    "synthetic_shape_material_components_preserved"
                    if component_passed
                    else "shape_material_or_component_fixture_failed"
                ),
                {
                    **common,
                    "rules": [
                        "component_count_preserved",
                        "product_shape_preserved",
                        "material_preserved",
                    ],
                },
            ),
            AuthenticityRuleResult(
                "color_shift",
                color_passed,
                (
                    "synthetic_color_fixture_within_tolerance"
                    if color_passed
                    else "color_shift_fixture_failed"
                ),
                {**common, "rules": ["color_shift_within_fixture_tolerance"]},
            ),
            AuthenticityRuleResult(
                "artifact_scan",
                artifact_passed,
                artifact_reason,
                {
                    **common,
                    "rules": ["jpeg_decode", "sha256", "unexpected_artifacts"],
                },
            ),
            AuthenticityRuleResult(
                "dimensions",
                dimensions_passed,
                (
                    "decoded_dimensions_match_candidate"
                    if dimensions_passed
                    else "decoded_dimensions_mismatch"
                ),
                {**common, "rules": ["positive_dimensions", "decoded_size_match"]},
            ),
        )
        return AuthenticityResult(
            candidate_id=candidate.id,
            checker_version=self.checker_version,
            checks=checks,
        )


def _expected_sources(job: ImageOutputJob) -> tuple[list[str], list[str]]:
    metadata = job.request_metadata
    products = _string_list(metadata.get("source_product_ids"))
    assets = _string_list(metadata.get("source_asset_ids"))
    if not products and isinstance(metadata.get("product_id"), str):
        products = [str(metadata["product_id"])]
    if not assets and metadata.get("asset_id") is not None:
        assets = [str(metadata["asset_id"])]
    return products, assets


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        return []
    return cast(list[str], value)
