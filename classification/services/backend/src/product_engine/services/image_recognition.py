import base64
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Literal, Protocol
from uuid import UUID, uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

FORBIDDEN_RECOGNITION_CLAIMS = (
    "保真",
    "真品",
    "正品",
    "产自",
    "原产于",
    "保值",
    "升值",
    "投资收益",
    "一口价",
    "固定售价",
)


class MaterialCandidate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str
    confidence_level: Literal["low", "medium", "high"]
    visible_evidence: str

    @field_validator("label", "visible_evidence")
    @classmethod
    def clean_required_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("required_text_missing")
        return cleaned


class ImageRecognitionCandidate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    recognition_id: UUID
    reused_from_recognition_id: UUID | None = None
    template_code: str
    sha256: str
    attempt: int = Field(ge=1)
    status: Literal["succeeded", "fallback_exact_match", "unavailable", "failed"]
    provider: Literal["openai_vision", "apple_vision", "local_exact_match", "none"]
    model: str | None
    material_candidates: list[MaterialCandidate] = Field(max_length=3)
    product_form_candidate: str
    visual_observations: list[str]
    selling_point_drafts: list[str]
    specification_candidates: list[str]
    price_status: Literal["pending_price_standard"] = "pending_price_standard"
    authenticity_verification: Literal["not_verified"] = "not_verified"
    origin_verification: Literal["not_verified"] = "not_verified"
    quality_verification: Literal["not_verified"] = "not_verified"
    warehouse_verification: Literal["pending_warehouse_verification"] = (
        "pending_warehouse_verification"
    )
    error_code: str | None
    created_at: datetime

    @field_validator("template_code", "product_form_candidate")
    @classmethod
    def clean_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("required_text_missing")
        return cleaned

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        normalized = value.strip().lower()
        if len(normalized) != 64 or any(
            character not in "0123456789abcdef" for character in normalized
        ):
            raise ValueError("invalid_sha256")
        return normalized

    @field_validator(
        "visual_observations",
        "selling_point_drafts",
        "specification_candidates",
    )
    @classmethod
    def clean_lines(cls, values: list[str]) -> list[str]:
        return [cleaned for value in values if (cleaned := value.strip())]

    @model_validator(mode="after")
    def validate_candidate_boundaries(self) -> "ImageRecognitionCandidate":
        all_text = " ".join(
            [
                self.product_form_candidate,
                *(candidate.label for candidate in self.material_candidates),
                *(candidate.visible_evidence for candidate in self.material_candidates),
                *self.visual_observations,
                *self.selling_point_drafts,
                *self.specification_candidates,
            ]
        )
        if any(claim in all_text for claim in FORBIDDEN_RECOGNITION_CLAIMS):
            raise ValueError("forbidden_unsupported_claim")
        if any("待测量" not in item for item in self.specification_candidates):
            raise ValueError("unverified_specification_missing_boundary")
        if self.status == "succeeded":
            if (
                self.provider not in {"openai_vision", "apple_vision"}
                or not self.model
                or not self.material_candidates
                or not self.visual_observations
                or not self.selling_point_drafts
                or self.error_code is not None
            ):
                raise ValueError("invalid_successful_recognition")
        elif not self.error_code:
            raise ValueError("recognition_error_code_missing")
        return self


class RecognitionStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = RLock()

    def _read(self) -> list[ImageRecognitionCandidate]:
        if not self.path.exists():
            return []
        return [
            ImageRecognitionCandidate.model_validate_json(line)
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def save(self, candidate: ImageRecognitionCandidate) -> ImageRecognitionCandidate:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(candidate.model_dump_json() + "\n")
            return candidate

    def current(self, template_code: str) -> ImageRecognitionCandidate | None:
        matching = [
            candidate for candidate in self._read() if candidate.template_code == template_code
        ]
        if not matching:
            return None
        return max(matching, key=lambda candidate: candidate.attempt)

    def success_for_sha256(self, sha256: str) -> ImageRecognitionCandidate | None:
        normalized = sha256.strip().lower()
        return next(
            (
                candidate
                for candidate in reversed(self._read())
                if candidate.sha256 == normalized
                and candidate.status == "succeeded"
                and candidate.provider in {"openai_vision", "apple_vision"}
            ),
            None,
        )


class VisionRecognitionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    material_candidates: list[MaterialCandidate] = Field(min_length=1, max_length=3)
    product_form_candidate: str
    visual_observations: list[str] = Field(min_length=1)
    selling_point_drafts: list[str] = Field(min_length=1)
    specification_candidates: list[str]

    @field_validator("product_form_candidate")
    @classmethod
    def clean_product_form(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("required_text_missing")
        return cleaned

    @field_validator(
        "visual_observations",
        "selling_point_drafts",
        "specification_candidates",
    )
    @classmethod
    def clean_result_lines(cls, values: list[str]) -> list[str]:
        return [cleaned for value in values if (cleaned := value.strip())]

    @model_validator(mode="after")
    def reject_unsupported_claims(self) -> "VisionRecognitionResult":
        all_text = " ".join(
            [
                self.product_form_candidate,
                *(candidate.label for candidate in self.material_candidates),
                *(candidate.visible_evidence for candidate in self.material_candidates),
                *self.visual_observations,
                *self.selling_point_drafts,
                *self.specification_candidates,
            ]
        )
        if any(claim in all_text for claim in FORBIDDEN_RECOGNITION_CLAIMS):
            raise ValueError("forbidden_unsupported_claim")
        if any("待测量" not in item for item in self.specification_candidates):
            raise ValueError("unverified_specification_missing_boundary")
        return self


class RecognitionFailure(ValueError):
    pass


class VisionProvider(Protocol):
    @property
    def provider(self) -> Literal["openai_vision", "apple_vision", "none"]: ...

    @property
    def model(self) -> str | None: ...

    async def recognize(
        self,
        content: bytes,
        mime_type: str,
    ) -> VisionRecognitionResult: ...


class UnavailableVisionProvider:
    provider: Literal["none"] = "none"
    model: None = None

    async def recognize(
        self,
        content: bytes,
        mime_type: str,
    ) -> VisionRecognitionResult:
        del content, mime_type
        raise RecognitionFailure("vision_not_configured")


VISION_PROMPT = """
你是珠宝商品图片训练助手。只根据图片可见内容输出材质候选、商品形态、可见特征和卖点草稿。
材质最多输出 3 个候选，并说明可见依据。confidence_level 只可为 low、medium 或 high。
规格若无法测量，必须使用“待测量”。
禁止输出真伪、产地、品质认证、仓验、保值、投资或固定售价结论。
所有结果仅供训练员复核。
""".strip()


class OpenAIVisionProvider:
    provider: Literal["openai_vision"] = "openai_vision"

    def __init__(
        self,
        api_key: str,
        model: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("openai_api_key_missing")
        self._api_key = api_key
        self.model = model
        self._client = client

    def _request_body(self, content: bytes, mime_type: str) -> dict[str, object]:
        image_base64 = base64.b64encode(content).decode("ascii")
        return {
            "model": self.model,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": VISION_PROMPT},
                        {
                            "type": "input_image",
                            "image_url": f"data:{mime_type};base64,{image_base64}",
                            "detail": "high",
                        },
                    ],
                }
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "jewelry_image_recognition",
                    "strict": True,
                    "schema": VisionRecognitionResult.model_json_schema(),
                }
            },
        }

    @staticmethod
    def _parse_response(payload: dict[str, object]) -> VisionRecognitionResult:
        output = payload.get("output")
        if not isinstance(output, list):
            raise RecognitionFailure("vision_invalid_response")
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "output_text"
                    and isinstance(block.get("text"), str)
                ):
                    try:
                        decoded = json.loads(block["text"])
                        return VisionRecognitionResult.model_validate(decoded)
                    except (json.JSONDecodeError, ValueError) as error:
                        raise RecognitionFailure("vision_invalid_response") from error
        raise RecognitionFailure("vision_invalid_response")

    async def _post(self, client: httpx.AsyncClient, body: dict[str, object]) -> httpx.Response:
        return await client.post(
            "https://api.openai.com/v1/responses",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=30.0,
        )

    async def recognize(
        self,
        content: bytes,
        mime_type: str,
    ) -> VisionRecognitionResult:
        body = self._request_body(content, mime_type)
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient()
        try:
            for attempt in range(2):
                try:
                    response = await self._post(client, body)
                except httpx.TimeoutException as error:
                    if attempt == 0:
                        continue
                    raise RecognitionFailure("vision_timeout") from error
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt == 0:
                        continue
                    raise RecognitionFailure("vision_temporarily_unavailable")
                if response.status_code >= 400:
                    raise RecognitionFailure("vision_request_rejected")
                try:
                    payload = response.json()
                except json.JSONDecodeError as error:
                    raise RecognitionFailure("vision_invalid_response") from error
                if not isinstance(payload, dict):
                    raise RecognitionFailure("vision_invalid_response")
                return self._parse_response(payload)
            raise RecognitionFailure("vision_temporarily_unavailable")
        finally:
            if owns_client:
                await client.aclose()


def build_default_vision_provider() -> VisionProvider:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if api_key:
        model = os.environ.get("OPENAI_VISION_MODEL", "gpt-5.6-luna").strip()
        return OpenAIVisionProvider(api_key=api_key, model=model or "gpt-5.6-luna")

    executable_value = os.environ.get("LOCAL_VISION_EXECUTABLE", "").strip()
    if executable_value:
        executable = Path(executable_value)
        if executable.is_file():
            from product_engine.services.on_device_vision import (
                OnDeviceVisionProvider,
            )

            return OnDeviceVisionProvider.from_executable(executable)

    return UnavailableVisionProvider()


class HistoricalRecognitionReference(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_template_code: str
    material_label: str
    product_form: str
    visual_observations: list[str]
    selling_points: list[str]
    specification_note: str = ""


class RecognitionService:
    def __init__(self, store: RecognitionStore, provider: VisionProvider) -> None:
        self.store = store
        self.provider = provider

    @staticmethod
    def _fallback_result(
        reference: HistoricalRecognitionReference,
    ) -> VisionRecognitionResult:
        specification_candidates: list[str] = []
        if reference.specification_note.strip():
            specification = reference.specification_note.strip()
            if "待测量" not in specification:
                specification = f"{specification}（待测量）"
            specification_candidates.append(specification)
        return VisionRecognitionResult(
            material_candidates=[
                MaterialCandidate(
                    label=reference.material_label,
                    confidence_level="high",
                    visible_evidence="历史人工确认的相同图片，仅供训练员复核",
                )
            ],
            product_form_candidate=reference.product_form,
            visual_observations=reference.visual_observations,
            selling_point_drafts=reference.selling_points,
            specification_candidates=specification_candidates,
        )

    async def recognize(
        self,
        *,
        template_code: str,
        sha256: str,
        content: bytes,
        mime_type: str,
        historical_reference: HistoricalRecognitionReference | None = None,
        force: bool = False,
    ) -> ImageRecognitionCandidate:
        current = self.store.current(template_code)
        attempt = current.attempt + 1 if current else 1

        if not force:
            reusable = self.store.success_for_sha256(sha256)
            if reusable is not None:
                return self.store.save(
                    reusable.model_copy(
                        update={
                            "recognition_id": uuid4(),
                            "reused_from_recognition_id": reusable.recognition_id,
                            "template_code": template_code,
                            "attempt": attempt,
                            "created_at": datetime.now(UTC),
                        }
                    )
                )

        try:
            result = await self.provider.recognize(content, mime_type)
        except RecognitionFailure as error:
            error_code = str(error)
            if historical_reference is not None:
                fallback = self._fallback_result(historical_reference)
                candidate = ImageRecognitionCandidate(
                    recognition_id=uuid4(),
                    template_code=template_code,
                    sha256=sha256,
                    attempt=attempt,
                    status="fallback_exact_match",
                    provider="local_exact_match",
                    model=None,
                    material_candidates=fallback.material_candidates,
                    product_form_candidate=fallback.product_form_candidate,
                    visual_observations=fallback.visual_observations,
                    selling_point_drafts=fallback.selling_point_drafts,
                    specification_candidates=fallback.specification_candidates,
                    error_code=error_code,
                    created_at=datetime.now(UTC),
                )
            else:
                candidate = ImageRecognitionCandidate(
                    recognition_id=uuid4(),
                    template_code=template_code,
                    sha256=sha256,
                    attempt=attempt,
                    status=("unavailable" if error_code == "vision_not_configured" else "failed"),
                    provider="none",
                    model=None,
                    material_candidates=[],
                    product_form_candidate="待识别",
                    visual_observations=[],
                    selling_point_drafts=[],
                    specification_candidates=[],
                    error_code=error_code,
                    created_at=datetime.now(UTC),
                )
            return self.store.save(candidate)

        return self.store.save(
            ImageRecognitionCandidate(
                recognition_id=uuid4(),
                template_code=template_code,
                sha256=sha256,
                attempt=attempt,
                status="succeeded",
                provider=self.provider.provider,
                model=self.provider.model,
                material_candidates=result.material_candidates,
                product_form_candidate=result.product_form_candidate,
                visual_observations=result.visual_observations,
                selling_point_drafts=result.selling_point_drafts,
                specification_candidates=result.specification_candidates,
                error_code=None,
                created_at=datetime.now(UTC),
            )
        )
