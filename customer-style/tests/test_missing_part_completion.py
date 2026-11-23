from __future__ import annotations

import base64
from dataclasses import replace
from decimal import Decimal
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from PIL import Image, ImageDraw

from opc_style.domain.errors import DomainRuleError
from opc_style.domain.missing_part_completion import (
    MissingPartCompletionRequest,
    assess_missing_part_mask,
    compose_missing_part_candidate,
    measure_missing_part_mask,
    multiview_reference_digest,
    require_missing_part_evidence,
    validate_missing_part_request,
)
from opc_style.providers.openai_missing_part_completion import (
    OPENAI_MISSING_PART_MODEL,
    OpenAIMissingPartCompleter,
    _provider_canvas,
)
from opc_style.services.missing_part_completion import (
    MissingPartCompletionService,
    PrivateMissingPartCandidateStore,
)
from opc_style.services.missing_part_learning import (
    CompletionFeedback,
    MissingPartLearningStore,
    PreflightFeedback,
)


def _png(mode: str, size: tuple[int, int], color: object) -> bytes:
    image = Image.new(mode, size, color)
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


def _request(
    *, part_kind: str = "ear_hook", mask_box=(8, 8, 16, 16)
) -> MissingPartCompletionRequest:
    source_image = Image.new("RGB", (64, 64), (20, 30, 40))
    source_draw = ImageDraw.Draw(source_image)
    source_draw.line((0, 12, 7, 12), fill=(210, 170, 40), width=3)
    source_draw.line((17, 12, 26, 12), fill=(210, 170, 40), width=3)
    source_output = BytesIO()
    source_image.save(source_output, format="PNG", optimize=False)
    source = source_output.getvalue()
    mask = Image.new("L", (64, 64), 0)
    ImageDraw.Draw(mask).rectangle(mask_box, fill=255)
    output = BytesIO()
    mask.save(output, format="PNG", optimize=False)
    return MissingPartCompletionRequest(
        source_content=source,
        source_sha256=sha256(source).hexdigest(),
        source_mime_type="image/png",
        missing_mask_png=output.getvalue(),
        category="earring",
        part_kind=part_kind,
        material_tags=("gold", "reflective"),
        case_id="case-001",
    )


def test_validation_allows_only_small_non_identity_critical_regions() -> None:
    validated = validate_missing_part_request(_request())
    assert validated.missing_ratio == pytest.approx(81 / 4096)

    with pytest.raises(DomainRuleError, match="missing_part_identity_critical"):
        validate_missing_part_request(_request(part_kind="main_stone"))
    with pytest.raises(DomainRuleError, match="missing_part_region_too_large"):
        validate_missing_part_request(_request(mask_box=(0, 0, 63, 63)))


def test_paid_preflight_requires_visible_structure_next_to_mask() -> None:
    assessment = assess_missing_part_mask(_request())
    assert assessment.state == "anchored_to_visible_structure"

    flat_source = _png("RGB", (64, 64), (20, 30, 40))
    flat = _request()
    flat = MissingPartCompletionRequest(
        source_content=flat_source,
        source_sha256=sha256(flat_source).hexdigest(),
        source_mime_type=flat.source_mime_type,
        missing_mask_png=flat.missing_mask_png,
        category=flat.category,
        part_kind=flat.part_kind,
        material_tags=flat.material_tags,
        case_id=flat.case_id,
    )
    with pytest.raises(DomainRuleError, match="missing_part_mask_unanchored"):
        assess_missing_part_mask(flat)
    diagnosis = measure_missing_part_mask(flat)
    assert diagnosis.state == "blocked_unanchored"


def test_paid_preflight_accepts_colored_product_edge_on_neutral_background() -> None:
    source_image = Image.new("RGB", (64, 64), (120, 120, 120))
    source_draw = ImageDraw.Draw(source_image)
    source_draw.rectangle((2, 6, 7, 18), fill=(40, 180, 200))
    source_draw.rectangle((17, 6, 26, 18), fill=(40, 180, 200))
    source_output = BytesIO()
    source_image.save(source_output, format="PNG", optimize=False)
    source = source_output.getvalue()
    template = _request()
    request = MissingPartCompletionRequest(
        source_content=source,
        source_sha256=sha256(source).hexdigest(),
        source_mime_type="image/png",
        missing_mask_png=template.missing_mask_png,
        category=template.category,
        part_kind="bead_edge",
        material_tags=("turquoise",),
        case_id="colored-edge",
    )
    assessment = assess_missing_part_mask(request)
    assert assessment.context_chroma_mean >= 20
    assert assessment.context_chroma_stddev >= 8


def test_occluded_scene_requires_digest_bound_real_evidence() -> None:
    occluded = replace(_request(), scene_tags=("hand_held",))
    with pytest.raises(
        DomainRuleError, match="missing_part_occlusion_evidence_required"
    ):
        require_missing_part_evidence(occluded)

    confirmed = replace(
        occluded,
        evidence_kind="human_confirmed_mask",
        evidence_sha256=sha256(occluded.missing_mask_png).hexdigest(),
    )
    require_missing_part_evidence(confirmed)
    assert (
        validate_missing_part_request(confirmed).request_fingerprint
        != validate_missing_part_request(_request()).request_fingerprint
    )


def test_same_product_reference_must_be_real_distinct_and_human_confirmed() -> None:
    occluded = replace(_request(), scene_tags=("occluded",))
    reference = _png("RGB", (64, 64), (120, 80, 30))
    valid = replace(
        occluded,
        evidence_kind="clean_same_product_reference",
        evidence_sha256=sha256(reference).hexdigest(),
        reference_content=reference,
        reference_mime_type="image/png",
        reference_same_product_confirmed=True,
        reference_structure_confirmed=True,
    )
    require_missing_part_evidence(valid)

    with pytest.raises(DomainRuleError, match="missing_part_evidence_invalid"):
        validate_missing_part_request(
            replace(valid, reference_same_product_confirmed=False)
        )
    with pytest.raises(DomainRuleError, match="missing_part_evidence_invalid"):
        validate_missing_part_request(
            replace(valid, reference_structure_confirmed=False)
        )
    with pytest.raises(DomainRuleError, match="missing_part_evidence_digest_mismatch"):
        validate_missing_part_request(
            replace(
                valid,
                evidence_sha256=occluded.source_sha256,
                reference_content=occluded.source_content,
            )
        )


def test_existing_multiview_references_must_be_distinct_and_collectively_confirmed() -> (
    None
):
    occluded = replace(_request(), scene_tags=("occluded",))
    views = (
        _patterned_view("vertical"),
        _patterned_view("horizontal"),
    )
    valid = replace(
        occluded,
        evidence_kind="same_product_multiview_references",
        evidence_sha256=multiview_reference_digest(views),
        reference_contents=views,
        reference_mime_types=("image/png", "image/png"),
        reference_same_product_confirmed=True,
        reference_structure_confirmed=True,
    )
    require_missing_part_evidence(valid)

    with pytest.raises(DomainRuleError, match="missing_part_evidence_invalid"):
        validate_missing_part_request(
            replace(
                valid,
                reference_contents=views[:1],
                reference_mime_types=("image/png",),
                evidence_sha256=multiview_reference_digest(views[:1]),
            )
        )
    duplicate_views = (views[0], views[0])
    with pytest.raises(DomainRuleError, match="missing_part_evidence_digest_mismatch"):
        validate_missing_part_request(
            replace(
                valid,
                reference_contents=duplicate_views,
                evidence_sha256=multiview_reference_digest(duplicate_views),
            )
        )
    with pytest.raises(DomainRuleError, match="missing_part_evidence_invalid"):
        validate_missing_part_request(
            replace(valid, reference_structure_confirmed=False)
        )

    near_view_image = Image.open(BytesIO(views[0])).copy()
    near_view_image.putpixel((63, 63), (239, 239, 239))
    near_view_output = BytesIO()
    near_view_image.save(near_view_output, format="PNG", optimize=False)
    near_views = (views[0], near_view_output.getvalue())
    with pytest.raises(DomainRuleError, match="missing_part_multiview_not_diverse"):
        validate_missing_part_request(
            replace(
                valid,
                reference_contents=near_views,
                evidence_sha256=multiview_reference_digest(near_views),
            )
        )


def test_occlusion_gate_runs_before_provider_or_paid_reservation(
    tmp_path: Path,
) -> None:
    calls = 0

    class RecordingCompleter:
        model_version = "fixture-v1"

        def complete(self, request, *, learning_lessons=()):  # type: ignore[no-untyped-def]
            nonlocal calls
            calls += 1
            raise AssertionError("provider must not be called")

    candidate_root = tmp_path / "candidates"
    service = MissingPartCompletionService(
        learning_store=MissingPartLearningStore(tmp_path / "learning"),
        candidate_store=PrivateMissingPartCandidateStore(candidate_root),
        completer=RecordingCompleter(),
        model_version="fixture-v1",
        maximum_usd=Decimal("0.12"),
    )
    with pytest.raises(
        DomainRuleError, match="missing_part_occlusion_evidence_required"
    ):
        service.preview(replace(_request(), scene_tags=("hand_held",)))
    assert calls == 0
    assert not (candidate_root / "runs").exists()


def test_composition_preserves_every_pixel_outside_missing_region() -> None:
    request = _request()
    generated = _png("RGB", (64, 64), (240, 10, 20))
    candidate = compose_missing_part_candidate(
        request,
        generated_content=generated,
        model_version="fixture-model-v1",
    )
    assert candidate.publishable is False
    assert candidate.requires_human_review is True
    assert candidate.review_state == "human_review_required"
    assert candidate.automatic_approval is False
    with Image.open(BytesIO(candidate.content)) as image:
        rgb = image.convert("RGB")
        assert rgb.getpixel((0, 0)) == (20, 30, 40)
        assert rgb.getpixel((12, 12)) == (240, 10, 20)


def test_candidate_without_material_change_is_guardrail_rejected() -> None:
    request = _request()
    candidate = compose_missing_part_candidate(
        request,
        generated_content=request.source_content,
        model_version="fixture-model-v1",
    )
    assert candidate.review_state == "guardrail_rejected"
    assert candidate.review_reasons == ("candidate_has_no_meaningful_material_change",)


def test_learning_store_uses_latest_human_decision_per_case(tmp_path: Path) -> None:
    store = MissingPartLearningStore(tmp_path)
    accepted = CompletionFeedback.create(
        case_id="case-001",
        request_fingerprint="a" * 64,
        category="earring",
        part_kind="ear_hook",
        material_tags=("gold",),
        decision="accepted",
        candidate_sha256="b" * 64,
        final_sha256="b" * 64,
        model_version="fixture-v1",
        note="ear hook continuity was correct",
    )
    store.append(accepted)
    assert len(store.similar_lessons("earring", "ear_hook", ("gold",))) == 1

    rejected = CompletionFeedback.create(
        case_id="case-001",
        request_fingerprint="a" * 64,
        category="earring",
        part_kind="ear_hook",
        material_tags=("gold",),
        decision="rejected",
        candidate_sha256="c" * 64,
        final_sha256=None,
        model_version="fixture-v1",
        note="hook geometry was invented",
    )
    store.append(rejected)
    assert store.similar_lessons("earring", "ear_hook", ("gold",)) == ()
    assert MissingPartLearningStore(tmp_path).event_count == 2


def test_preflight_feedback_is_audited_without_changing_thresholds(
    tmp_path: Path,
) -> None:
    store = MissingPartLearningStore(tmp_path)
    feedback = PreflightFeedback.create(
        case_id="customer:case-001",
        request_fingerprint="a" * 64,
        category="earring",
        part_kind="ear_hook",
        material_tags=("gold",),
        system_state="blocked_unanchored",
        human_label="confirmed_structure",
        context_edge_mean=3.1,
        strong_edge_fraction=0.01,
        context_chroma_mean=2.5,
        strong_chroma_fraction=0.0,
        context_chroma_stddev=1.2,
        note="human confirmed a faint wire endpoint",
    )
    store.append_preflight(feedback)
    assert store.preflight_event_count == 1
    assert MissingPartLearningStore(tmp_path).preflight_event_count == 1
    assert store.preflight_summary() == {
        "total": 1,
        "agreed": 0,
        "false_pass": 0,
        "false_block": 1,
        "confirmed_structure": 1,
        "confirmed_background": 0,
    }

    corrected = PreflightFeedback.create(
        case_id="customer:case-001",
        request_fingerprint="a" * 64,
        category="earring",
        part_kind="ear_hook",
        material_tags=("gold",),
        system_state="blocked_unanchored",
        human_label="confirmed_background",
        context_edge_mean=3.1,
        strong_edge_fraction=0.01,
        context_chroma_mean=2.5,
        strong_chroma_fraction=0.0,
        context_chroma_stddev=1.2,
        note="reviewer corrected the same case to background",
    )
    store.append_preflight(corrected)
    assert store.preflight_summary() == {
        "total": 1,
        "agreed": 1,
        "false_pass": 0,
        "false_block": 0,
        "confirmed_structure": 0,
        "confirmed_background": 1,
    }

    false_pass = PreflightFeedback.create(
        case_id="customer:case-002",
        request_fingerprint="b" * 64,
        category="earring",
        part_kind="ear_hook",
        material_tags=("gold",),
        system_state="anchored_to_visible_structure",
        human_label="confirmed_background",
        context_edge_mean=12.4,
        strong_edge_fraction=0.2,
        context_chroma_mean=8.5,
        strong_chroma_fraction=0.1,
        context_chroma_stddev=4.2,
        note="human confirmed plain background despite an edge",
    )
    store.append_preflight(false_pass)
    queue = store.preflight_review_queue()
    assert queue["total_cases"] == 2
    assert queue["matching_cases"] == 2
    assert queue["source_preview_available"] is False
    assert queue["threshold_updated_automatically"] is False
    assert queue["paid_override_enabled"] is False
    assert queue["items"][0]["outcome"] == "false_pass"  # type: ignore[index]
    assert queue["items"][0]["case_ref"] != "customer:case-002"  # type: ignore[index]
    assert queue["groups"] == [
        {
            "category": "earring",
            "part_kind": "ear_hook",
            "confirmed_structure": 0,
            "confirmed_background": 2,
            "structure_remaining": 20,
            "background_remaining": 18,
            "ready_for_offline_comparison": False,
        }
    ]
    filtered = store.preflight_review_queue(outcome="false_pass")
    assert filtered["matching_cases"] == 1
    with pytest.raises(DomainRuleError, match="missing_part_review_filter_invalid"):
        store.preflight_review_queue(outcome="invented")


def test_openai_adapter_is_one_attempt_and_returns_non_publishable_candidate() -> None:
    request = _request()
    generated = _png("RGB", (64, 64), (200, 180, 20))
    seen: list[httpx.Request] = []

    def handler(http_request: httpx.Request) -> httpx.Response:
        seen.append(http_request)
        assert http_request.url.path == "/v1/images/edits"
        body = http_request.read()
        assert b"AI-simulated missing region" in body
        assert b"ear_hook" in body
        assert OPENAI_MISSING_PART_MODEL.encode() in body
        assert b'name="quality"' in body
        assert b"high" in body
        return httpx.Response(
            200,
            json={"data": [{"b64_json": base64.b64encode(generated).decode()}]},
        )

    completer = OpenAIMissingPartCompleter(
        api_key="test-secret",
        transport=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    candidate = completer.complete(
        request,
        learning_lessons=("accepted ear hook: preserve the open curve",),
    )
    assert len(seen) == 1
    assert candidate.publishable is False
    assert candidate.requires_human_review is True
    assert candidate.model_version == (
        "openai-gpt-image-2.5-sunburst-2026-09-08-missing-part-v1"
    )


def test_openai_adapter_sends_digest_bound_same_product_reference() -> None:
    reference = _png("RGB", (64, 64), (120, 80, 30))
    request = replace(
        _request(),
        scene_tags=("hand_held",),
        evidence_kind="clean_same_product_reference",
        evidence_sha256=sha256(reference).hexdigest(),
        reference_content=reference,
        reference_mime_type="image/png",
        reference_same_product_confirmed=True,
        reference_structure_confirmed=True,
    )
    generated = _png("RGB", (64, 64), (200, 180, 20))

    def handler(http_request: httpx.Request) -> httpx.Response:
        body = http_request.read()
        assert body.count(b'name="image[]"') == 2
        assert b"same-product-reference" in body
        assert b"human-confirmed clean photo" in body
        return httpx.Response(
            200,
            json={"data": [{"b64_json": base64.b64encode(generated).decode()}]},
        )

    completer = OpenAIMissingPartCompleter(
        api_key="test-secret",
        transport=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    candidate = completer.complete(request)
    assert candidate.requires_human_review is True


def test_openai_adapter_sends_existing_same_product_multiview_references() -> None:
    views = (
        _patterned_view("vertical"),
        _patterned_view("horizontal"),
    )
    request = replace(
        _request(),
        scene_tags=("hand_held",),
        evidence_kind="same_product_multiview_references",
        evidence_sha256=multiview_reference_digest(views),
        reference_contents=views,
        reference_mime_types=("image/png", "image/png"),
        reference_same_product_confirmed=True,
        reference_structure_confirmed=True,
    )
    generated = _png("RGB", (64, 64), (200, 180, 20))

    def handler(http_request: httpx.Request) -> httpx.Response:
        body = http_request.read()
        assert body.count(b'name="image[]"') == 3
        assert b"same-product-view-1" in body
        assert b"same-product-view-2" in body
        assert b"Triangulate only the shared visible structure" in body
        return httpx.Response(
            200,
            json={"data": [{"b64_json": base64.b64encode(generated).decode()}]},
        )

    completer = OpenAIMissingPartCompleter(
        api_key="test-secret",
        transport=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    candidate = completer.complete(request)
    assert candidate.requires_human_review is True


def test_provider_uses_same_size_png_context_and_mask() -> None:
    canvas = _provider_canvas(_request())
    with Image.open(BytesIO(canvas.source_png)) as source:
        assert source.format == "PNG"
        assert source.size == (1024, 1024)
    with Image.open(BytesIO(canvas.mask_png)) as mask:
        assert mask.format == "PNG"
        assert mask.mode == "RGBA"
        assert mask.size == (1024, 1024)
        assert mask.getchannel("A").getextrema() == (0, 255)


def test_service_requires_exact_quote_and_blocks_replay(tmp_path: Path) -> None:
    request = _request()

    class FixtureCompleter:
        model_version = "fixture-v1"

        def complete(self, request, *, learning_lessons=()):  # type: ignore[no-untyped-def]
            return compose_missing_part_candidate(
                request,
                generated_content=_png("RGB", (64, 64), (90, 100, 110)),
                model_version=self.model_version,
            )

    service = MissingPartCompletionService(
        learning_store=MissingPartLearningStore(tmp_path / "learning"),
        candidate_store=PrivateMissingPartCandidateStore(tmp_path / "candidates"),
        completer=FixtureCompleter(),
        model_version="fixture-v1",
        maximum_usd=Decimal("0.12"),
    )
    quote = service.preview(request)
    key = UUID("00000000-0000-0000-0000-000000000001")
    with pytest.raises(DomainRuleError, match="missing_part_confirmation_required"):
        service.execute(
            UUID(int=2),
            request,
            idempotency_key=key,
            confirmed_request_fingerprint=quote.request_fingerprint,
            confirmed_maximum_usd=Decimal("0.11"),
        )
    candidate = service.execute(
        UUID(int=2),
        request,
        idempotency_key=key,
        confirmed_request_fingerprint=quote.request_fingerprint,
        confirmed_maximum_usd=Decimal("0.12"),
    )
    assert candidate.publishable is False
    assert (
        service._candidate_store.review_state(
            UUID(int=2), quote.request_fingerprint, candidate.content_sha256
        )
        == "human_review_required"
    )
    with pytest.raises(DomainRuleError, match="missing_part_outcome_uncertain"):
        service.execute(
            UUID(int=2),
            request,
            idempotency_key=key,
            confirmed_request_fingerprint=quote.request_fingerprint,
            confirmed_maximum_usd=Decimal("0.12"),
        )


def test_service_reports_definitive_provider_auth_failure(tmp_path: Path) -> None:
    request = _request()

    class AuthFailureCompleter:
        model_version = "fixture-v1"

        def complete(self, request, *, learning_lessons=()):  # type: ignore[no-untyped-def]
            raise DomainRuleError("missing_part_provider_auth_failed")

    candidate_store = PrivateMissingPartCandidateStore(tmp_path / "candidates")
    service = MissingPartCompletionService(
        learning_store=MissingPartLearningStore(tmp_path / "learning"),
        candidate_store=candidate_store,
        completer=AuthFailureCompleter(),
        model_version="fixture-v1",
        maximum_usd=Decimal("0.50"),
    )
    quote = service.preview(request)
    key = UUID("00000000-0000-0000-0000-000000000009")
    customer = UUID(int=9)
    with pytest.raises(DomainRuleError, match="missing_part_provider_auth_failed"):
        service.execute(
            customer,
            request,
            idempotency_key=key,
            confirmed_request_fingerprint=quote.request_fingerprint,
            confirmed_maximum_usd=quote.maximum_usd,
        )
    run = tmp_path / "candidates" / "runs" / str(customer) / f"{key}.json"
    assert '"state": "provider_failed"' in run.read_text()
    assert "missing_part_provider_auth_failed" in run.read_text()
