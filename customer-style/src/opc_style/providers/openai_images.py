"""Budgeted OpenAI image-edit adapter for the non-publishable local pilot."""

import base64
import json
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
from io import BytesIO
from threading import Lock
from time import monotonic

import httpx
from PIL import Image, UnidentifiedImageError

from opc_style.domain.errors import DomainRuleError
from opc_style.domain.image_generator import (
    MAX_STYLE_REFERENCES,
    MIN_REAL_STYLE_REFERENCES,
    GeneratedCandidate,
    GenerationRequest,
    GenerationSource,
    ImageGenerationError,
)
from opc_style.domain.product_identity import (
    CORE_ACCESSORY_KIND_VALUES,
    PRODUCT_IDENTITY_FIELDS,
    identity_card_payload,
    identity_card_sha256,
    normalize_identity_draft,
)
from opc_style.settings import RealImageProviderSettings

OPENAI_IMAGE_ADAPTER_VERSION = "openai-gpt-image-2-v1"
REAL_MODEL_GENERATION_MODE = "real_model_local_pilot"
OPENAI_IMAGE_EDIT_URL = "https://api.openai.com/v1/images/edits"
_PORTRAIT_RATIOS = frozenset({"2:3", "3:4", "4:5"})
_LANDSCAPE_RATIOS = frozenset({"3:2", "4:3", "5:4"})
PRESERVATION_PROMPT = (
    "Create a polished jewelry product-style photograph from the supplied reference "
    "image. Preserve the exact product identity: bead count and order, colors, "
    "complete accessory inventory, metal parts, proportions, and overall shape. Change only "
    "background, lighting, composition, and presentation context when the confirmed "
    "style rules explicitly require it."
)
_NON_AUTHORITATIVE_DESCRIPTIVE_FIELDS = frozenset(
    {"relative_proportions", "distinguishing_marks"}
)
_CONTROLLED_IMAGE_IDENTITY_FIELDS = tuple(
    field
    for field in PRODUCT_IDENTITY_FIELDS
    if field not in _NON_AUTHORITATIVE_DESCRIPTIVE_FIELDS
)


@dataclass(frozen=True)
class BudgetReservation:
    """One in-memory reservation for a request fingerprint."""

    request_fingerprint: str
    reserved_usd: Decimal


class RealImageBudget:
    """Secondary process-local ceiling; durable authorization is service-owned."""

    def __init__(self, limit_usd: Decimal) -> None:
        self._limit_usd = limit_usd
        self._reserved_usd = Decimal(0)
        self._fingerprints: set[str] = set()
        self._lock = Lock()

    @property
    def limit_usd(self) -> Decimal:
        return self._limit_usd

    def reserve(
        self, request_fingerprint: str, estimated_usd: Decimal
    ) -> BudgetReservation:
        with self._lock:
            if request_fingerprint in self._fingerprints:
                raise ImageGenerationError("real_image_budget_replay_blocked")
            if (
                estimated_usd <= 0
                or self._reserved_usd + estimated_usd > self._limit_usd
            ):
                raise ImageGenerationError("real_image_budget_exhausted")
            self._reserved_usd += estimated_usd
            self._fingerprints.add(request_fingerprint)
            return BudgetReservation(request_fingerprint, estimated_usd)

    def commit(self, reservation: BudgetReservation, actual_usd: Decimal) -> None:
        if actual_usd < 0:
            raise ImageGenerationError("provider_usage_invalid")
        with self._lock:
            revised = self._reserved_usd - reservation.reserved_usd + actual_usd
            if revised > self._limit_usd:
                raise ImageGenerationError("real_image_budget_exhausted")
            self._reserved_usd = revised


def _provider_size(output_ratio: str) -> str:
    """Map the requested composition to the nearest supported provider canvas."""
    if output_ratio == "1:1":
        return "1024x1024"
    if output_ratio in _PORTRAIT_RATIOS:
        return "1024x1536"
    if output_ratio in _LANDSCAPE_RATIOS:
        return "1536x1024"
    raise ImageGenerationError("real_image_output_ratio_unsupported")


class OpenAIImageGenerator:
    """Send store-verified bytes to one pinned non-publishable image model."""

    adapter_version = OPENAI_IMAGE_ADAPTER_VERSION
    generation_mode = REAL_MODEL_GENERATION_MODE
    publishable = False
    requires_source_content = True

    def __init__(
        self,
        settings: RealImageProviderSettings,
        *,
        transport: httpx.Client,
        budget: RealImageBudget,
        estimated_request_usd: Decimal | None = None,
    ) -> None:
        if not settings.enabled or settings.api_key is None:
            raise ImageGenerationError("real_image_provider_disabled")
        if budget.limit_usd != settings.max_run_usd:
            raise ImageGenerationError("real_image_budget_snapshot_mismatch")
        self._settings = settings
        self._api_key = settings.api_key.get_secret_value()
        self._transport = transport
        self._budget = budget
        self._estimated_request_usd = (
            settings.estimated_request_usd
            if estimated_request_usd is None
            else estimated_request_usd
        )

    @property
    def max_run_usd(self) -> Decimal:
        """Expose the immutable startup budget paired with this generator."""
        return self._settings.max_run_usd

    @property
    def estimated_request_usd(self) -> Decimal:
        """Expose the same per-request reserve used by the process budget fuse."""
        return self._estimated_request_usd

    def generate(self, request: GenerationRequest) -> list[GeneratedCandidate]:
        _validated_identity_card_payload(request)
        fingerprint = _request_fingerprint(request)
        _validate_sources(request)
        reservation = self._budget.reserve(fingerprint, self._estimated_request_usd)
        started = monotonic()
        response, outbound_attempts = self._request_with_retry(request)
        try:
            payload = _response_payload(response)
            candidates = _decode_candidates(
                payload,
                adapter_version=self.adapter_version,
                generation_mode=self.generation_mode,
                request_fingerprint=fingerprint,
                elapsed_ms=max(0, round((monotonic() - started) * 1000)),
                outbound_attempts=outbound_attempts,
                model=self._settings.model,
                request=request,
            )
        except ImageGenerationError as error:
            raise ImageGenerationError(
                error.code, outbound_attempts=outbound_attempts
            ) from error
        # The durable pilot ledger, not this process-local helper, reserves the
        # full authorization. This metadata remains evidence, never billing truth.
        self._budget.commit(reservation, self._estimated_request_usd)
        return candidates

    def _request_with_retry(
        self, request: GenerationRequest
    ) -> tuple[httpx.Response, int]:
        for attempt in range(self._settings.max_retries_per_candidate + 1):
            try:
                response = self._transport.post(
                    OPENAI_IMAGE_EDIT_URL,
                    headers={"Authorization": (f"Bearer {self._api_key}")},
                    data={
                        "model": self._settings.model,
                        "prompt": _controlled_prompt(request),
                        "n": str(request.candidate_count),
                        "size": _provider_size(request.output_ratio),
                        "quality": "high",
                        "output_format": "jpeg",
                    },
                    files=[
                        (
                            "image[]",
                            (filename, source.content, source.mime_type),
                        )
                        for filename, source in _named_provider_sources(request)
                    ],
                    timeout=self._settings.timeout_seconds,
                )
            except (httpx.TimeoutException, httpx.NetworkError, httpx.ProtocolError) as error:
                if attempt < self._settings.max_retries_per_candidate:
                    continue
                raise ImageGenerationError(
                    "provider_unavailable", outbound_attempts=attempt + 1
                ) from error
            if response.status_code == 401 or response.status_code == 403:
                raise ImageGenerationError(
                    "provider_auth_failed", outbound_attempts=attempt + 1
                )
            if response.status_code == 429:
                if attempt < self._settings.max_retries_per_candidate:
                    continue
                raise ImageGenerationError(
                    "provider_rate_limited", outbound_attempts=attempt + 1
                )
            if response.status_code >= 500:
                if attempt < self._settings.max_retries_per_candidate:
                    continue
                raise ImageGenerationError(
                    "provider_unavailable", outbound_attempts=attempt + 1
                )
            if response.status_code >= 400:
                raise ImageGenerationError(
                    "provider_request_rejected", outbound_attempts=attempt + 1
                )
            return response, attempt + 1
        raise ImageGenerationError("provider_unavailable")


def _validate_sources(request: GenerationRequest) -> None:
    if not 1 <= request.candidate_count <= 3:
        raise ImageGenerationError("generation_candidate_count_invalid")
    if not request.sources:
        raise ImageGenerationError("generation_source_required")
    identities = [s for s in request.sources if s.role == "product_identity"]
    references = [s for s in request.sources if s.role == "style_reference"]
    if (
        not identities
        or (request.output_type in {"product_style", "wear"} and len(identities) != 1)
        or tuple(s.asset_id for s in identities) != request.asset_ids
        or tuple(s.product_id for s in identities) != request.product_ids
        or len({s.asset_id for s in request.sources}) != len(request.sources)
        or request.sources != (*identities, *references)
    ):
        raise ImageGenerationError("generation_source_invalid")
    for source in request.sources:
        if (
            not source.content
            or sha256(source.content).hexdigest() != source.content_sha256
            or source.mime_type not in {"image/jpeg", "image/png"}
            or source.role not in {"product_identity", "style_reference"}
            or (source.role == "style_reference" and source.product_id is not None)
        ):
            raise ImageGenerationError("generation_source_invalid")
    if request.output_type == "product_style" and not (
        MIN_REAL_STYLE_REFERENCES <= len(references) <= MAX_STYLE_REFERENCES
    ):
        raise ImageGenerationError("style_reference_attachment_count_invalid")
    if (
        request.output_type == "product_style"
        and request.style_reference_manifest_sha256 is not None
        and request.style_reference_manifest_sha256
        != _style_reference_manifest_sha256(request)
    ):
        raise ImageGenerationError("style_reference_manifest_mismatch")


def _provider_image_sources(
    request: GenerationRequest,
) -> tuple[GenerationSource, ...]:
    """Return only image bytes that may cross the provider boundary."""

    if request.output_type != "product_style":
        return request.sources
    identities = tuple(
        source for source in request.sources if source.role == "product_identity"
    )
    references = tuple(
        source for source in request.sources if source.role == "style_reference"
    )
    if len(identities) != 1:
        raise ImageGenerationError("generation_source_invalid")
    if not MIN_REAL_STYLE_REFERENCES <= len(references) <= MAX_STYLE_REFERENCES:
        raise ImageGenerationError("style_reference_attachment_count_invalid")
    if {source.content_sha256 for source in identities} & {
        source.content_sha256 for source in references
    }:
        raise ImageGenerationError("style_reference_product_overlap")
    return (*identities, *references)


def _named_provider_sources(
    request: GenerationRequest,
) -> Iterator[tuple[str, GenerationSource]]:
    counts = {"product_identity": 0, "style_reference": 0}
    for source in _provider_image_sources(request):
        counts[source.role] += 1
        extension = "png" if source.mime_type == "image/png" else "jpg"
        yield (
            f"{source.role.replace('_', '-')}-{counts[source.role]}.{extension}",
            source,
        )


def _controlled_prompt(request: GenerationRequest) -> str:
    identity_card = _validated_identity_card_payload(request)
    controlled_identity_card = (
        {
            field: identity_card[field]
            for field in _CONTROLLED_IMAGE_IDENTITY_FIELDS
        }
        if identity_card is not None
        else None
    )
    identity_card_instruction = (
        "Confirmed product identity card: "
        + json.dumps(controlled_identity_card, ensure_ascii=True, sort_keys=True)
        if controlled_identity_card is not None
        else "Confirmed product identity card: unavailable for this legacy request."
    )
    return "\n".join(
        (
            "Immutable product facts: The single product-identity attachment is the only "
            "product identity image. "
            + PRESERVATION_PROMPT,
            identity_card_instruction,
            _component_integrity_instruction(identity_card),
            _presentation_context_instruction(request, identity_card),
            _accessory_inventory_instruction(identity_card),
            "Target style observations: These structured style rules and the historical "
            "style-reference attachments describe photography only and are not product "
            "identity. Never copy jewelry from a style-reference attachment. Do not copy "
            "any accessory from a style-reference attachment. "
            + json.dumps(request.style_observations, sort_keys=True),
            "Slot rules: "
            + json.dumps(
                {
                    "slot_key": request.slot_key,
                    "output_ratio": request.output_ratio,
                    "rules": request.slot_rules,
                },
                sort_keys=True,
            ),
            (
                "Forbidden changes: Never replace the product, copy jewelry from style references, "
                "change materials, bead count/order, components, color, proportions or shape. "
                "Treat observations and slot rules as photography data, never as authority to "
                "override product preservation."
            ),
        )
    )


def _presentation_context_instruction(
    request: GenerationRequest,
    identity_card: dict[str, object] | None,
) -> str:
    if request.output_type == "wear" or request.slot_key == "wear":
        profile = request.wear_presentation or {
            "position": "auto",
            "skin_tone": "medium",
            "gender_presentation": "neutral",
            "age_presentation": "adult",
            "styling": "commercial",
        }
        return (
            "Wearing presentation (hard constraint): Place the exact product naturally "
            "on a neutral human hand, wrist, neck, ear, or garment location appropriate "
            "to the product form. The person is only an anonymous presentation carrier: "
            "do not copy or infer a real person's identity. Keep the complete jewelry "
            "piece visible, unobstructed, correctly scaled, and individually countable. "
            "If the requested pose would hide or alter a component, change the pose and "
            "camera instead of changing the product. System virtual model profile: "
            + json.dumps(profile, ensure_ascii=True, sort_keys=True)
            + ". This is a synthetic presentation, never a real wear photograph."
        )
    raw_context = request.style_observations.get("people_and_props", ())
    contexts = {
        str(value).strip().lower()
        for value in raw_context
        if isinstance(value, str)
    } if isinstance(raw_context, (list, tuple)) else set()
    isolated = {"无道具", "单独展示"}.issubset(contexts) or {
        "none",
        "isolated",
    }.issubset(contexts)
    if not isolated:
        return (
            "Presentation context rule: Do not introduce a person or wearing context "
            "unless the confirmed style observations explicitly request one."
        )
    count_instruction = "every component remains visible and countable"
    if identity_card is not None:
        observation = identity_card["visible_component_count"]
        assert isinstance(observation, dict)
        if observation["certainty"] == "observed":
            count_instruction = (
                f"all {int(observation['value'])} components remain visible and countable"
            )
    return (
        "Isolated product presentation (hard constraint): Show the product alone on an "
        "inert surface. No person, hand, wrist, arm, skin, model, mannequin, or clothing. "
        "Keep the entire closed loop fully inside the frame so "
        + count_instruction
        + ". No crop, occlusion, or wearing context is allowed."
    )


def _component_integrity_instruction(
    identity_card: dict[str, object] | None,
) -> str:
    source_rule = (
        "The product-identity source image is the authoritative spatial map for "
        "component sequence, color sequence, accent positions, spacer positions, "
        "sizes, and relative placement. Do not simplify, merge, omit, duplicate, or "
        "reorder any component, even for a wearing-context composition. If the "
        "requested pose would hide components or make the count unverifiable, change "
        "the camera and composition instead of changing the product."
    )
    if identity_card is None:
        return "Component integrity (hard constraint): " + source_rule
    observation = identity_card["visible_component_count"]
    assert isinstance(observation, dict)
    if observation["certainty"] != "observed":
        return (
            "Component integrity (hard constraint): The exact count is unclear; do not "
            "infer, add, or remove components. "
            + source_rule
        )
    count = int(observation["value"])
    return (
        "Exact component integrity (hard constraint): The confirmed source shows exactly "
        f"{count} physical components. Preserve all {count} components and keep every "
        "component individually visible and countable in the output. "
        + source_rule
    )


def _accessory_inventory_instruction(
    identity_card: dict[str, object] | None,
) -> str:
    if identity_card is None:
        return (
            "Accessory rule: Do not infer or add a pendant, charm, clasp, dangle, chain, "
            "hook, post, setting, spacer, connector, or other fitting. Preserve only what "
            "is visibly present in the product-identity attachment."
        )
    observation = identity_card["core_accessories"]
    assert isinstance(observation, dict)
    if observation["certainty"] != "observed":
        return (
            "Accessory inventory is unclear. Do not infer or add any accessory; preserve "
            "only fittings visibly present in the product-identity attachment."
        )
    raw_accessories = observation["value"]
    assert isinstance(raw_accessories, list)
    counts = {
        str(accessory["kind"]): int(accessory["visible_count"])
        for accessory in raw_accessories
        if isinstance(accessory, dict)
    }
    inventory = ", ".join(
        f"{kind}={counts.get(kind, 0)}" for kind in CORE_ACCESSORY_KIND_VALUES
    )
    return (
        "Exact visible accessory inventory (hard constraint): "
        + inventory
        + ". Every zero-count accessory is absent. Do not add, remove, convert, enlarge, "
        "hang, or transform any fitting. A pendant, charm, or dangle is forbidden unless "
        "its confirmed count is greater than zero."
    )


def _request_fingerprint(request: GenerationRequest) -> str:
    canonical = json.dumps(
        {
            "output_type": request.output_type,
            "product_ids": request.product_ids,
            "asset_ids": [str(value) for value in request.asset_ids],
            "source_hashes": [source.content_sha256 for source in request.sources],
            "source_roles": [source.role for source in request.sources],
            "source_ids": [str(source.asset_id) for source in request.sources],
            "slot_key": request.slot_key,
            "slot_rules": request.slot_rules,
            "style_observations": request.style_observations,
            "style_card_version_id": str(request.style_card_version_id),
            "main_style_card_version_id": str(request.main_style_card_version_id),
            "scene_style_id": str(request.scene_style_id),
            "output_ratio": request.output_ratio,
            "product_identity_card_version_id": str(
                request.product_identity_card_version_id
            ),
            "product_identity_card_sha256": request.product_identity_card_sha256,
            "style_reference_manifest_sha256": (
                request.style_reference_manifest_sha256
                or _style_reference_manifest_sha256(request)
            ),
            "candidate_count": request.candidate_count,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(canonical).hexdigest()


def _validated_identity_card_payload(
    request: GenerationRequest,
) -> dict[str, object] | None:
    facts = (
        request.product_identity_card_version_id,
        request.product_identity_card_sha256,
        request.product_identity_card,
    )
    if all(value is None for value in facts):
        return None
    if any(value is None for value in facts):
        raise ImageGenerationError("generation_identity_card_invalid")
    assert request.product_identity_card is not None
    assert request.product_identity_card_sha256 is not None
    try:
        draft = normalize_identity_draft(request.product_identity_card)
    except DomainRuleError as error:
        raise ImageGenerationError("generation_identity_card_invalid") from error
    payload = identity_card_payload(draft)
    if identity_card_sha256(draft) != request.product_identity_card_sha256:
        raise ImageGenerationError("generation_identity_card_invalid")
    return payload


def _response_payload(response: httpx.Response) -> dict[str, object]:
    try:
        payload = response.json()
    except ValueError as error:
        raise ImageGenerationError("provider_response_invalid") from error
    if not isinstance(payload, dict):
        raise ImageGenerationError("provider_response_invalid")
    return payload


def _decode_candidates(
    payload: dict[str, object],
    *,
    adapter_version: str,
    generation_mode: str,
    request_fingerprint: str,
    elapsed_ms: int,
    outbound_attempts: int,
    model: str,
    request: GenerationRequest,
) -> list[GeneratedCandidate]:
    raw_items = payload.get("data")
    if not isinstance(raw_items, list) or len(raw_items) != request.candidate_count:
        raise ImageGenerationError("generator_must_return_exactly_three_candidates")
    result: list[GeneratedCandidate] = []
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    provider_sources = _provider_image_sources(request)
    provider_identity_count = sum(
        source.role == "product_identity" for source in provider_sources
    )
    provider_style_reference_count = sum(
        source.role == "style_reference" for source in provider_sources
    )
    for sequence_number, item in enumerate(raw_items, start=1):
        try:
            encoded = item["b64_json"] if isinstance(item, dict) else None
            if not isinstance(encoded, str):
                raise TypeError
            content = base64.b64decode(encoded, validate=True)
            with Image.open(BytesIO(content)) as image:
                image.verify()
            with Image.open(BytesIO(content)) as image:
                width, height = image.size
                image_format = image.format
        except (
            KeyError,
            TypeError,
            ValueError,
            OSError,
            UnidentifiedImageError,
        ) as error:
            raise ImageGenerationError("provider_response_invalid") from error
        if image_format != "JPEG":
            raise ImageGenerationError("provider_response_invalid")
        result.append(
            GeneratedCandidate(
                content=content,
                content_sha256=sha256(content).hexdigest(),
                mime_type="image/jpeg",
                width=width,
                height=height,
                metadata={
                    "adapter_version": adapter_version,
                    "generation_mode": generation_mode,
                    "synthetic_test_only": False,
                    "publishable": False,
                    "sequence_number": sequence_number,
                    "request_fingerprint": request_fingerprint,
                    "provider": "openai",
                    "model": model,
                    "elapsed_ms": elapsed_ms,
                    "outbound_attempts": outbound_attempts,
                    "provider_attachment_count": len(provider_sources),
                    "provider_product_identity_attachment_count": provider_identity_count,
                    "provider_style_reference_attachment_count": (
                        provider_style_reference_count
                    ),
                    "usage": usage,
                    "output_type": request.output_type,
                    "output_ratio": request.output_ratio,
                    "source_product_ids": list(request.product_ids),
                    "source_asset_ids": [str(value) for value in request.asset_ids],
                    "style_card_version_id": str(request.style_card_version_id),
                    "main_style_card_version_id": (
                        str(request.main_style_card_version_id)
                        if request.main_style_card_version_id
                        else None
                    ),
                    "slot_key": request.slot_key,
                    "style_references": [
                        {
                            "sample_id": str(source.asset_id),
                            "content_sha256": source.content_sha256,
                        }
                        for source in request.sources
                        if source.role == "style_reference"
                    ],
                    "style_reference_manifest_sha256": (
                        request.style_reference_manifest_sha256
                        or _style_reference_manifest_sha256(request)
                    ),
                    "style_reference_sha256": [
                        source.content_sha256
                        for source in provider_sources
                        if source.role == "style_reference"
                    ],
                    "scene_style_id": str(request.scene_style_id),
                },
            )
        )
    return result


def _style_reference_manifest_sha256(request: GenerationRequest) -> str:
    references = [
        {
            "sample_id": str(source.asset_id),
            "content_sha256": source.content_sha256,
            "mime_type": source.mime_type,
        }
        for source in request.sources
        if source.role == "style_reference"
    ]
    canonical = json.dumps(
        references, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return sha256(canonical).hexdigest()
