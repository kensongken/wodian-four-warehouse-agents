from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from product_engine.services.image_recognition import (
    ImageRecognitionCandidate,
    MaterialCandidate,
    OpenAIVisionProvider,
    RecognitionFailure,
    RecognitionService,
    RecognitionStore,
    UnavailableVisionProvider,
    build_default_vision_provider,
)
from product_engine.services.on_device_vision import (
    LocalVisionClassification,
    LocalVisionObservation,
    OnDeviceVisionProvider,
)


def candidate(
    *,
    template_code: str = "LOCAL-GREEN",
    sha256: str = "a" * 64,
    status: str = "succeeded",
    material: str = "绿松",
    provider: str = "openai_vision",
) -> ImageRecognitionCandidate:
    return ImageRecognitionCandidate(
        recognition_id=uuid4(),
        template_code=template_code,
        sha256=sha256,
        attempt=1,
        status=status,
        provider=provider if status == "succeeded" else "none",
        model=(
            "apple-vision-vnclassify-v1"
            if status == "succeeded" and provider == "apple_vision"
            else "gpt-5.6-luna"
            if status == "succeeded"
            else None
        ),
        material_candidates=[
            MaterialCandidate(
                label=material,
                confidence_level="medium",
                visible_evidence="绿色调珠体，仅作材质候选",
            )
        ]
        if status == "succeeded"
        else [],
        product_form_candidate="手串" if status == "succeeded" else "待识别",
        visual_observations=["绿色调明显"] if status == "succeeded" else [],
        selling_point_drafts=["近景实拍展示珠体轮廓"] if status == "succeeded" else [],
        specification_candidates=["珠径待测量"] if status == "succeeded" else [],
        error_code=None if status == "succeeded" else "vision_not_configured",
        created_at=datetime.now(UTC),
    )


def test_store_appends_and_returns_latest_candidate(tmp_path) -> None:
    path = tmp_path / "image-recognitions.jsonl"
    store = RecognitionStore(path)
    first = candidate()
    second = first.model_copy(
        update={
            "recognition_id": uuid4(),
            "attempt": 2,
            "material_candidates": [
                MaterialCandidate(
                    label="绿松石",
                    confidence_level="high",
                    visible_evidence="绿色珠体与深色纹理，仅供复核",
                )
            ],
            "created_at": datetime.now(UTC),
        }
    )

    store.save(first)
    store.save(second)

    current = RecognitionStore(path).current("LOCAL-GREEN")
    assert current is not None
    assert current.recognition_id == second.recognition_id
    assert current.attempt == 2
    assert len(path.read_text(encoding="utf-8").splitlines()) == 2


def test_store_reuses_only_successful_online_candidate_for_same_sha256(tmp_path) -> None:
    store = RecognitionStore(tmp_path / "image-recognitions.jsonl")
    store.save(candidate(template_code="LOCAL-FAILED", status="unavailable"))
    successful = candidate(template_code="LOCAL-SUCCESS")
    store.save(successful)

    match = store.success_for_sha256("a" * 64)

    assert match is not None
    assert match.recognition_id == successful.recognition_id
    assert match.provider == "openai_vision"


def test_candidate_rejects_unsupported_authenticity_and_fixed_price_claims() -> None:
    payload = candidate().model_dump()
    payload["selling_point_drafts"] = ["保证正品并且固定售价1200元"]

    with pytest.raises(ValidationError):
        ImageRecognitionCandidate.model_validate(payload)


def test_candidate_keeps_verification_and_price_boundaries_fixed() -> None:
    result = candidate()

    assert result.price_status == "pending_price_standard"
    assert result.authenticity_verification == "not_verified"
    assert result.origin_verification == "not_verified"
    assert result.quality_verification == "not_verified"
    assert result.warehouse_verification == "pending_warehouse_verification"


@pytest.mark.asyncio
async def test_openai_provider_sends_image_and_parses_structured_result() -> None:
    captured_request: httpx.Request | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_request
        captured_request = request
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": (
                                    '{"material_candidates":[{"label":"绿松",'
                                    '"confidence_level":"medium",'
                                    '"visible_evidence":"绿色珠体，仅供复核"}],'
                                    '"product_form_candidate":"手串",'
                                    '"visual_observations":["绿色调明显"],'
                                    '"selling_point_drafts":["近景展示珠体轮廓"],'
                                    '"specification_candidates":["珠径待测量"]}'
                                ),
                            }
                        ],
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAIVisionProvider(
            api_key="test-secret",
            model="gpt-5.6-luna",
            client=client,
        )
        result = await provider.recognize(b"\xff\xd8\xffgreen", "image/jpeg")

    assert captured_request is not None
    assert captured_request.url == "https://api.openai.com/v1/responses"
    assert captured_request.headers["authorization"] == "Bearer test-secret"
    request_body = captured_request.read().decode("utf-8")
    assert "data:image/jpeg;base64,/9j/Z3JlZW4=" in request_body
    assert '"type":"json_schema"' in request_body
    assert result.material_candidates[0].label == "绿松"
    assert result.product_form_candidate == "手串"


@pytest.mark.asyncio
async def test_openai_provider_retries_one_retryable_server_error() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(500, json={"error": {"message": "temporary"}})
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": (
                                    '{"material_candidates":[{"label":"绿松",'
                                    '"confidence_level":"low",'
                                    '"visible_evidence":"仅根据绿色调推测"}],'
                                    '"product_form_candidate":"手串",'
                                    '"visual_observations":["珠体排列成环"],'
                                    '"selling_point_drafts":["环形排列适合上手展示"],'
                                    '"specification_candidates":[]}'
                                ),
                            }
                        ],
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAIVisionProvider("test-secret", "gpt-5.6-luna", client)
        result = await provider.recognize(b"\xff\xd8\xffgreen", "image/jpeg")

    assert calls == 2
    assert result.product_form_candidate == "手串"


@pytest.mark.asyncio
async def test_unavailable_provider_returns_stable_error_without_network() -> None:
    with pytest.raises(RecognitionFailure, match="vision_not_configured"):
        await UnavailableVisionProvider().recognize(b"\xff\xd8\xffgreen", "image/jpeg")


def test_candidate_accepts_successful_on_device_source() -> None:
    result = candidate(provider="apple_vision", material="绿色宝石或矿物类候选")

    assert result.provider == "apple_vision"
    assert result.model == "apple-vision-vnclassify-v1"
    assert result.authenticity_verification == "not_verified"


@pytest.mark.asyncio
async def test_service_reuses_successful_on_device_candidate_for_same_sha256(
    tmp_path,
) -> None:
    calls = 0

    async def local_runner(_: bytes, __: str) -> LocalVisionObservation:
        nonlocal calls
        calls += 1
        return LocalVisionObservation(
            classifications=[LocalVisionClassification(identifier="bracelet", confidence=0.82)],
            dominant_color="green",
            brightness="medium",
            image_width=800,
            image_height=800,
        )

    service = RecognitionService(
        RecognitionStore(tmp_path / "recognitions.jsonl"),
        OnDeviceVisionProvider(runner=local_runner),
    )
    first = await service.recognize(
        template_code="LOCAL-FIRST",
        sha256="b" * 64,
        content=b"same-image",
        mime_type="image/jpeg",
    )
    second = await service.recognize(
        template_code="LOCAL-SECOND",
        sha256="b" * 64,
        content=b"same-image",
        mime_type="image/jpeg",
    )

    assert calls == 1
    assert first.provider == "apple_vision"
    assert second.provider == "apple_vision"
    assert second.reused_from_recognition_id == first.recognition_id


def test_default_provider_prefers_local_executable_without_online_key(
    tmp_path,
    monkeypatch,
) -> None:
    executable = tmp_path / "local-vision"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("LOCAL_VISION_EXECUTABLE", str(executable))

    provider = build_default_vision_provider()

    assert isinstance(provider, OnDeviceVisionProvider)
    assert provider.provider == "apple_vision"


def test_default_provider_preserves_explicit_online_precedence(tmp_path, monkeypatch) -> None:
    executable = tmp_path / "local-vision"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setenv("OPENAI_API_KEY", "controlled-key")
    monkeypatch.setenv("LOCAL_VISION_EXECUTABLE", str(executable))

    provider = build_default_vision_provider()

    assert isinstance(provider, OpenAIVisionProvider)
    assert provider.provider == "openai_vision"
