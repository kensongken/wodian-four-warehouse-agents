"""Quoted execution boundary and private storage for missing-part candidates."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from uuid import UUID

from PIL import Image, UnidentifiedImageError

from opc_style.domain.errors import DomainRuleError
from opc_style.domain.missing_part_completion import (
    MissingPartCandidate,
    MissingPartCompleter,
    MissingPartCompletionRequest,
    assess_missing_part_mask,
    measure_missing_part_mask,
    require_missing_part_evidence,
    validate_missing_part_request,
)
from opc_style.services.missing_part_learning import MissingPartLearningStore


@dataclass(frozen=True)
class MissingPartCompletionQuote:
    request_fingerprint: str
    model_version: str
    maximum_usd: Decimal
    currency: str
    missing_ratio: float
    similar_lesson_count: int
    provider_enabled: bool
    quality: str
    candidate_count: int
    selection_reason: str
    automatic_fallback: bool
    preflight_state: str
    preflight_reasons: tuple[str, ...]
    context_edge_mean: float
    strong_edge_fraction: float
    context_chroma_mean: float
    strong_chroma_fraction: float
    context_chroma_stddev: float
    publishable: bool = False
    requires_human_review: bool = True


@dataclass(frozen=True)
class MissingPartPreflightDiagnosis:
    request_fingerprint: str
    missing_ratio: float
    state: str
    reasons: tuple[str, ...]
    context_edge_mean: float
    strong_edge_fraction: float
    context_chroma_mean: float
    strong_chroma_fraction: float
    context_chroma_stddev: float
    provider_called: bool = False
    paid_override: bool = False


class PrivateMissingPartCandidateStore:
    """Keep model guesses separate from benchmark gold masks."""

    def __init__(self, root: Path) -> None:
        if not root.is_absolute() or root.is_symlink():
            raise DomainRuleError("missing_part_candidate_store_unsafe")
        root.mkdir(parents=True, mode=0o700, exist_ok=True)
        root.chmod(0o700)
        root_stat = root.lstat()
        if (
            not stat.S_ISDIR(root_stat.st_mode)
            or root_stat.st_uid != os.getuid()
            or stat.S_IMODE(root_stat.st_mode) & 0o077
        ):
            raise DomainRuleError("missing_part_candidate_store_unsafe")
        self._root = root.resolve(strict=True)

    def reserve(
        self,
        customer_id: UUID,
        idempotency_key: UUID,
        request_fingerprint: str,
    ) -> None:
        run_dir = self._private_dir(self._root / "runs" / str(customer_id))
        path = run_dir / f"{idempotency_key}.json"
        record = {
            "request_fingerprint": request_fingerprint,
            "state": "dispatch_uncertain",
        }
        try:
            descriptor = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
            )
        except FileExistsError:
            raise DomainRuleError("missing_part_outcome_uncertain") from None
        try:
            os.write(descriptor, json.dumps(record, sort_keys=True).encode("utf-8"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def complete(
        self,
        customer_id: UUID,
        idempotency_key: UUID,
        candidate: MissingPartCandidate,
    ) -> None:
        candidate_dir = self._private_dir(
            self._root / "candidates" / str(customer_id) / candidate.request_fingerprint
        )
        candidate_path = candidate_dir / f"{candidate.content_sha256}.png"
        self._atomic_write(candidate_path, candidate.content)
        self._atomic_write(
            candidate_dir / f"{candidate.content_sha256}.json",
            json.dumps(
                {
                    "review_state": candidate.review_state,
                    "review_reasons": list(candidate.review_reasons),
                    "automatic_approval": False,
                },
                sort_keys=True,
            ).encode("utf-8"),
        )
        run_path = self._root / "runs" / str(customer_id) / f"{idempotency_key}.json"
        record = {
            "request_fingerprint": candidate.request_fingerprint,
            "state": "provider_responded",
            "candidate_sha256": candidate.content_sha256,
            "model_version": candidate.model_version,
            "publishable": False,
            "requires_human_review": True,
            "review_state": candidate.review_state,
        }
        self._atomic_write(run_path, json.dumps(record, sort_keys=True).encode("utf-8"))

    def fail(
        self,
        customer_id: UUID,
        idempotency_key: UUID,
        request_fingerprint: str,
        error_code: str,
    ) -> None:
        """Persist a definitive provider refusal without treating it as a candidate."""

        run_path = self._root / "runs" / str(customer_id) / f"{idempotency_key}.json"
        record = {
            "request_fingerprint": request_fingerprint,
            "state": "provider_failed",
            "error_code": error_code,
        }
        self._atomic_write(run_path, json.dumps(record, sort_keys=True).encode("utf-8"))

    def read(
        self,
        customer_id: UUID,
        request_fingerprint: str,
        candidate_sha256: str,
    ) -> bytes:
        if not _is_digest(request_fingerprint) or not _is_digest(candidate_sha256):
            raise DomainRuleError("missing_part_candidate_not_found")
        path = (
            self._root
            / "candidates"
            / str(customer_id)
            / request_fingerprint
            / f"{candidate_sha256}.png"
        )
        if path.is_symlink() or not path.is_file():
            raise DomainRuleError("missing_part_candidate_not_found")
        content = path.read_bytes()
        if sha256(content).hexdigest() != candidate_sha256:
            raise DomainRuleError("missing_part_candidate_digest_mismatch")
        return content

    def review_state(
        self,
        customer_id: UUID,
        request_fingerprint: str,
        candidate_sha256: str,
    ) -> str:
        self.read(customer_id, request_fingerprint, candidate_sha256)
        path = (
            self._root
            / "candidates"
            / str(customer_id)
            / request_fingerprint
            / f"{candidate_sha256}.json"
        )
        if path.is_symlink():
            raise DomainRuleError("missing_part_candidate_not_found")
        if not path.is_file():
            # Candidates created before guardrail metadata existed were already
            # human-review-only. Preserve that safe, reviewable workflow.
            return "human_review_required"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            state = payload["review_state"]
        except (OSError, ValueError, KeyError, TypeError):
            raise DomainRuleError("missing_part_candidate_not_found") from None
        if not isinstance(state, str) or state not in {
            "human_review_required",
            "guardrail_rejected",
        }:
            raise DomainRuleError("missing_part_candidate_not_found")
        return state

    def save_reviewed_final(
        self,
        customer_id: UUID,
        request_fingerprint: str,
        candidate_sha256: str,
        content: bytes,
    ) -> str:
        candidate = self.read(customer_id, request_fingerprint, candidate_sha256)
        try:
            with Image.open(BytesIO(candidate)) as opened:
                candidate_size = opened.size
            with Image.open(BytesIO(content)) as opened:
                opened.load()
                if (
                    opened.format not in {"PNG", "JPEG"}
                    or opened.size != candidate_size
                ):
                    raise DomainRuleError("missing_part_feedback_invalid")
        except DomainRuleError:
            raise
        except (
            Image.DecompressionBombError,
            UnidentifiedImageError,
            OSError,
            ValueError,
        ):
            raise DomainRuleError("missing_part_feedback_invalid") from None
        digest = sha256(content).hexdigest()
        final_dir = self._private_dir(
            self._root / "reviewed-finals" / str(customer_id) / request_fingerprint
        )
        self._atomic_write(final_dir / f"{digest}.bin", content)
        return digest

    def _private_dir(self, path: Path) -> Path:
        current = self._root
        for part in path.relative_to(self._root).parts:
            current = current / part
            if current.exists() and current.is_symlink():
                raise DomainRuleError("missing_part_candidate_store_unsafe")
            current.mkdir(mode=0o700, exist_ok=True)
            current.chmod(0o700)
        return path

    @staticmethod
    def _atomic_write(path: Path, content: bytes) -> None:
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        try:
            os.write(descriptor, content)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)


class MissingPartCompletionService:
    """Preview first; execute one paid attempt only with an exact confirmation."""

    def __init__(
        self,
        *,
        learning_store: MissingPartLearningStore,
        candidate_store: PrivateMissingPartCandidateStore,
        completer: MissingPartCompleter | None,
        model_version: str,
        maximum_usd: Decimal,
        quality: str = "high",
        selection_reason: str = "precision-first masked edit",
    ) -> None:
        self._learning_store = learning_store
        self._candidate_store = candidate_store
        self._completer = completer
        self._model_version = model_version
        self._maximum_usd = maximum_usd
        self._quality = quality
        self._selection_reason = selection_reason

    def preview(
        self, request: MissingPartCompletionRequest
    ) -> MissingPartCompletionQuote:
        validated = validate_missing_part_request(request)
        require_missing_part_evidence(request)
        assessment = assess_missing_part_mask(request)
        lessons = self._lessons(request)
        return MissingPartCompletionQuote(
            request_fingerprint=validated.request_fingerprint,
            model_version=self._model_version,
            maximum_usd=self._maximum_usd,
            currency="USD",
            missing_ratio=validated.missing_ratio,
            similar_lesson_count=len(lessons),
            provider_enabled=self._completer is not None and self._maximum_usd > 0,
            quality=self._quality,
            candidate_count=1,
            selection_reason=self._selection_reason,
            automatic_fallback=False,
            preflight_state=assessment.state,
            preflight_reasons=assessment.reasons,
            context_edge_mean=assessment.context_edge_mean,
            strong_edge_fraction=assessment.strong_edge_fraction,
            context_chroma_mean=assessment.context_chroma_mean,
            strong_chroma_fraction=assessment.strong_chroma_fraction,
            context_chroma_stddev=assessment.context_chroma_stddev,
        )

    def diagnose(
        self, request: MissingPartCompletionRequest
    ) -> MissingPartPreflightDiagnosis:
        validated = validate_missing_part_request(request)
        assessment = measure_missing_part_mask(request)
        return MissingPartPreflightDiagnosis(
            request_fingerprint=validated.request_fingerprint,
            missing_ratio=validated.missing_ratio,
            state=assessment.state,
            reasons=assessment.reasons,
            context_edge_mean=assessment.context_edge_mean,
            strong_edge_fraction=assessment.strong_edge_fraction,
            context_chroma_mean=assessment.context_chroma_mean,
            strong_chroma_fraction=assessment.strong_chroma_fraction,
            context_chroma_stddev=assessment.context_chroma_stddev,
        )

    def execute(
        self,
        customer_id: UUID,
        request: MissingPartCompletionRequest,
        *,
        idempotency_key: UUID,
        confirmed_request_fingerprint: str,
        confirmed_maximum_usd: Decimal,
    ) -> MissingPartCandidate:
        quote = self.preview(request)
        if self._completer is None or not quote.provider_enabled:
            raise DomainRuleError("missing_part_provider_disabled")
        if (
            confirmed_request_fingerprint != quote.request_fingerprint
            or confirmed_maximum_usd != quote.maximum_usd
        ):
            raise DomainRuleError("missing_part_confirmation_required")
        self._candidate_store.reserve(
            customer_id, idempotency_key, quote.request_fingerprint
        )
        try:
            candidate = self._completer.complete(
                request, learning_lessons=self._lessons(request)
            )
        except DomainRuleError as error:
            if error.code in {
                "missing_part_provider_auth_failed",
                "missing_part_provider_rate_limited",
                "missing_part_provider_rejected",
                "missing_part_provider_response_invalid",
            }:
                self._candidate_store.fail(
                    customer_id,
                    idempotency_key,
                    quote.request_fingerprint,
                    error.code,
                )
                raise
            raise DomainRuleError("missing_part_outcome_uncertain") from error
        except Exception as error:
            raise DomainRuleError("missing_part_outcome_uncertain") from error
        if (
            candidate.request_fingerprint != quote.request_fingerprint
            or candidate.publishable
            or not candidate.requires_human_review
        ):
            raise DomainRuleError("missing_part_provider_response_invalid")
        self._candidate_store.complete(customer_id, idempotency_key, candidate)
        return candidate

    def _lessons(self, request: MissingPartCompletionRequest) -> tuple[str, ...]:
        return self._learning_store.similar_lessons(
            request.category, request.part_kind, request.material_tags
        )


def _is_digest(value: str) -> bool:
    return len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )
