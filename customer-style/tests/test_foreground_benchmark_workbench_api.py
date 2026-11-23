from __future__ import annotations

import json
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw
from sqlalchemy.orm import Session, sessionmaker

from opc_style.api.dependencies import get_session
from opc_style.db import create_database_engine
from opc_style.domain.errors import DomainRuleError
from opc_style.domain.foreground_benchmark_workbench import BENCHMARK_CONSENT_VERSION
from opc_style.domain.product_foreground import (
    ExtractedForeground,
    ForegroundExtractionRequest,
    ForegroundStageReceipt,
)
from opc_style.main import create_app
from opc_style.models import Base, Customer, StyleSample
from opc_style.services.foreground_benchmark_workbench import (
    assess_proposal_disagreement,
    assess_proposal_disagreement_evidence,
)
from opc_style.services.real_photos import LocalSyntheticAssetStore

CUSTOMER_A = UUID("00000000-0000-4000-8000-0000000000a1")
CUSTOMER_B = UUID("00000000-0000-4000-8000-0000000000b2")
BASE = "/local/style-suite/api/foreground-benchmark-workbench"


def _image_bytes(image_format: str = "PNG", *, exif_secret: str | None = None) -> bytes:
    buffer = BytesIO()
    image = Image.new("RGB", (24, 16), (180, 40, 30))
    if exif_secret is None:
        image.save(buffer, format=image_format)
    else:
        exif = Image.Exif()
        exif[0x010E] = exif_secret
        image.save(buffer, format=image_format, exif=exif)
    return buffer.getvalue()


def _mask_bytes(
    *,
    size: tuple[int, int] = (24, 16),
    mode: str = "L",
    box: tuple[int, int, int, int] | None = (3, 3, 21, 13),
) -> bytes:
    image = Image.new(mode, size, 0)
    if box is not None:
        for x in range(box[0], box[2]):
            for y in range(box[1], box[3]):
                image.putpixel((x, y), 255 if mode == "L" else (255, 255, 255))
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _flat_lay_image_bytes() -> bytes:
    image = Image.new("RGB", (240, 240), (205, 205, 202))
    draw = ImageDraw.Draw(image)
    draw.ellipse((35, 85, 75, 125), fill=(190, 35, 25))
    draw.line((70, 105, 110, 70), fill=(210, 155, 25), width=6)
    draw.ellipse((135, 150, 175, 190), fill=(190, 35, 25))
    draw.line((170, 170, 210, 135), fill=(210, 155, 25), width=6)
    output = BytesIO()
    image.save(output, format="JPEG", quality=95)
    return output.getvalue()


class _FakeExtractor:
    def extract(self, request: ForegroundExtractionRequest) -> ExtractedForeground:
        mask = _mask_bytes(size=(request.width, request.height))
        with Image.open(BytesIO(mask)) as alpha:
            foreground = Image.new("RGBA", alpha.size, (50, 100, 80, 0))
            foreground.putalpha(alpha)
        output = BytesIO()
        foreground.save(output, format="PNG")
        foreground_bytes = output.getvalue()
        return ExtractedForeground(
            foreground_png=foreground_bytes,
            foreground_sha256=sha256(foreground_bytes).hexdigest(),
            mask_png=mask,
            mask_sha256=sha256(mask).hexdigest(),
            width=request.width,
            height=request.height,
            extractor_version="fake-local-v1",
        )


class _FailingExtractor:
    def extract(self, request: ForegroundExtractionRequest) -> ExtractedForeground:
        del request
        raise RuntimeError("sensitive executable path /Users/test-user/model")


class _PromptCapturingExtractor(_FakeExtractor):
    def __init__(self) -> None:
        self.request: ForegroundExtractionRequest | None = None

    def extract(self, request: ForegroundExtractionRequest) -> ExtractedForeground:
        self.request = request
        return super().extract(request)


class _PipelineExtractor:
    def __init__(self) -> None:
        self.coarse = _mask_bytes(box=(2, 2, 20, 14))

    def extract(self, request: ForegroundExtractionRequest) -> ExtractedForeground:
        refined = _mask_bytes(size=(request.width, request.height), box=(3, 3, 21, 13))
        with Image.open(BytesIO(refined)) as alpha:
            foreground = Image.new("RGBA", alpha.size, (50, 100, 80, 0))
            foreground.putalpha(alpha)
        output = BytesIO()
        foreground.save(output, format="PNG")
        foreground_bytes = output.getvalue()
        coarse_digest = sha256(self.coarse).hexdigest()
        refined_digest = sha256(refined).hexdigest()
        return ExtractedForeground(
            foreground_png=foreground_bytes,
            foreground_sha256=sha256(foreground_bytes).hexdigest(),
            mask_png=refined,
            mask_sha256=refined_digest,
            width=request.width,
            height=request.height,
            extractor_version="local-sam2-birefnet-official-v1:" + "a" * 12,
            coarse_mask_png=self.coarse,
            coarse_mask_sha256=coarse_digest,
            stage_receipts=(
                ForegroundStageReceipt(
                    role="coarse_segmenter",
                    artifact_sha256="b" * 64,
                    output_sha256=coarse_digest,
                    parameters_sha256="c" * 64,
                ),
                ForegroundStageReceipt(
                    role="alpha_refiner",
                    artifact_sha256="d" * 64,
                    output_sha256=refined_digest,
                    parameters_sha256="e" * 64,
                ),
            ),
            pipeline_manifest_sha256="a" * 64,
        )


@contextmanager
def _client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    enabled: bool = True,
) -> Generator[tuple[TestClient, Path], None, None]:
    private_root = (tmp_path / "private-workbench").resolve()
    asset_root = (tmp_path / "style-assets").resolve()
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("LOCAL_STYLE_ASSET_ROOT", str(asset_root))
    monkeypatch.setenv(
        "FOREGROUND_BENCHMARK_WORKBENCH_ENABLED", "true" if enabled else "false"
    )
    if enabled:
        monkeypatch.setenv("FOREGROUND_BENCHMARK_WORKBENCH_ROOT", str(private_root))
    else:
        monkeypatch.delenv("FOREGROUND_BENCHMARK_WORKBENCH_ROOT", raising=False)
    app = create_app()
    with TestClient(app) as client:
        yield client, private_root


def _headers(
    customer_id: UUID = CUSTOMER_A,
    *,
    confirmed: str = "true",
    version: str = BENCHMARK_CONSENT_VERSION,
) -> dict[str, str]:
    return {
        "X-Customer-Id": str(customer_id),
        "X-Consent-Confirmed": confirmed,
        "X-Consent-Version": version,
    }


def _create(client: TestClient, customer_id: UUID = CUSTOMER_A) -> dict[str, object]:
    response = client.post(
        BASE + "/datasets", headers={"X-Customer-Id": str(customer_id)}
    )
    assert response.status_code == 201, response.text
    return response.json()


def _upload(
    client: TestClient,
    dataset: dict[str, object],
    content: bytes,
    *,
    split: str = "calibration",
    headers: dict[str, str] | None = None,
) -> object:
    return client.post(
        f"{BASE}/datasets/{dataset['dataset_id']}/samples",
        params={"split": split, "expected_revision": dataset["revision"]},
        headers={**_headers(), "Content-Type": "image/png", **(headers or {})},
        content=content,
    )


def test_disabled_workbench_is_sanitized_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _client(tmp_path, monkeypatch, enabled=False) as (client, _):
        response = client.post(
            BASE + "/datasets", headers={"X-Customer-Id": str(CUSTOMER_A)}
        )
    assert response.status_code == 404
    assert response.json() == {
        "detail": {"code": "foreground_benchmark_workbench_not_found"}
    }


def test_create_recover_and_customer_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _client(tmp_path, monkeypatch) as (client, _):
        created = _create(client)
        own = client.get(
            f"{BASE}/datasets/{created['dataset_id']}",
            headers={"X-Customer-Id": str(CUSTOMER_A)},
        )
        foreign = client.get(
            f"{BASE}/datasets/{created['dataset_id']}",
            headers={"X-Customer-Id": str(CUSTOMER_B)},
        )
    assert own.status_code == 200
    assert own.json() == created
    assert foreign.status_code == 404
    assert str(CUSTOMER_A) not in own.text


@pytest.mark.parametrize(
    ("image_format", "mime"), [("PNG", "image/png"), ("JPEG", "image/jpeg")]
)
def test_upload_accepts_verified_jpeg_and_png(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    image_format: str,
    mime: str,
) -> None:
    with _client(tmp_path, monkeypatch) as (client, _):
        dataset = _create(client)
        response = _upload(client, dataset, _image_bytes(image_format))
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["revision"] == 1
    assert payload["samples"][0]["mime_type"] == mime
    assert payload["samples"][0]["state"] == "uploaded"


@pytest.mark.parametrize(
    ("headers", "code"),
    [
        ({"X-Consent-Confirmed": "false"}, "foreground_benchmark_consent_required"),
        ({"X-Consent-Confirmed": ""}, "foreground_benchmark_consent_required"),
        ({"X-Consent-Version": "old"}, "foreground_benchmark_consent_version_invalid"),
    ],
)
def test_upload_requires_current_explicit_consent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    headers: dict[str, str],
    code: str,
) -> None:
    with _client(tmp_path, monkeypatch) as (client, _):
        dataset = _create(client)
        response = _upload(client, dataset, _image_bytes(), headers=headers)
    assert response.status_code in {409, 422}
    assert response.json()["detail"]["code"] == code


def test_upload_rejects_invalid_oversized_and_decompression_bomb_but_recovers_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _client(tmp_path, monkeypatch) as (client, _):
        dataset = _create(client)
        malformed = _upload(client, dataset, b"not-an-image")
        oversized = _upload(client, dataset, b"0" * (12 * 1024 * 1024 + 1))
        monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 10)
        bomb = _upload(client, dataset, _image_bytes())
        monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 89_478_485)
        recovered = _upload(client, dataset, _image_bytes())
    assert malformed.status_code == 422
    assert oversized.status_code == 413
    assert bomb.status_code == 422
    assert recovered.status_code == 201
    assert recovered.json()["revision"] == 1


def test_upload_preserves_4284px_formal_source_but_keeps_dimension_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    accepted_buffer = BytesIO()
    Image.new("L", (4284, 4284), 0).save(accepted_buffer, format="PNG")
    rejected_buffer = BytesIO()
    Image.new("L", (4609, 1), 0).save(rejected_buffer, format="PNG")
    with _client(tmp_path, monkeypatch) as (client, _):
        dataset = _create(client)
        accepted = _upload(client, dataset, accepted_buffer.getvalue())
        rejected = _upload(client, accepted.json(), rejected_buffer.getvalue())
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["samples"][0]["width"] == 4284
    assert rejected.status_code == 413
    assert (
        rejected.json()["detail"]["code"]
        == "foreground_benchmark_image_limits_exceeded"
    )


def test_duplicate_and_cross_split_duplicate_do_not_change_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = _image_bytes()
    with _client(tmp_path, monkeypatch) as (client, _):
        dataset = _create(client)
        first = _upload(client, dataset, content)
        updated = first.json()
        duplicate = _upload(client, updated, content)
        cross_split = _upload(client, updated, content, split="acceptance")
        recovered = client.get(
            f"{BASE}/datasets/{dataset['dataset_id']}",
            headers={"X-Customer-Id": str(CUSTOMER_A)},
        )
    assert first.status_code == 201
    assert duplicate.status_code == cross_split.status_code == 409
    assert duplicate.json()["detail"]["code"] == "foreground_benchmark_source_duplicate"
    assert recovered.json()["revision"] == 1
    assert len(recovered.json()["samples"]) == 1


def test_preflight_exposes_pilot_readiness_separately_from_formal_freeze(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _client(tmp_path, monkeypatch) as (client, _):
        dataset = _create(client)
        calibration = _upload(client, dataset, _image_bytes()).json()
        acceptance_content = BytesIO()
        Image.new("RGB", (24, 16), (20, 80, 160)).save(acceptance_content, format="PNG")
        acceptance = _upload(
            client,
            calibration,
            acceptance_content.getvalue(),
            split="acceptance",
        )
        pending = acceptance.json()
        assert pending["preflight"]["pilot_ready"] is False
        assert pending["preflight"]["suitability_pending_count"] == 2
        for sample in pending["samples"]:
            response = client.patch(
                f"{BASE}/datasets/{dataset['dataset_id']}/samples/{sample['sample_id']}/suitability",
                headers={"X-Customer-Id": str(CUSTOMER_A)},
                json={
                    "expected_revision": pending["revision"],
                    "decision": "eligible",
                    "reason": None,
                    "confirmed": True,
                },
            )
            assert response.status_code == 200, response.text
            pending = response.json()
        acceptance = response

    assert acceptance.status_code == 200, acceptance.text
    preflight = acceptance.json()["preflight"]
    assert preflight["pilot_ready"] is True
    assert preflight["pilot_blockers"] == []
    assert preflight["ready_to_freeze"] is False
    assert preflight["calibration_count"] == 1
    assert preflight["acceptance_count"] == 1
    assert preflight["suitability_pending_count"] == 0
    assert preflight["excluded_count"] == 0


def test_suitability_exclusion_preserves_sample_but_removes_it_from_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _client(tmp_path, monkeypatch) as (client, _):
        dataset = _create(client)
        uploaded = _upload(client, dataset, _image_bytes()).json()
        sample = uploaded["samples"][0]
        response = client.patch(
            f"{BASE}/datasets/{dataset['dataset_id']}/samples/{sample['sample_id']}/suitability",
            headers={"X-Customer-Id": str(CUSTOMER_A)},
            json={
                "expected_revision": uploaded["revision"],
                "decision": "excluded",
                "reason": "no_unique_product_identity",
                "confirmed": True,
            },
        )
        excluded = response.json()
        undone = client.patch(
            f"{BASE}/datasets/{dataset['dataset_id']}/samples/{sample['sample_id']}/suitability",
            headers={"X-Customer-Id": str(CUSTOMER_A)},
            json={
                "expected_revision": excluded["revision"],
                "decision": "pending",
                "reason": None,
                "confirmed": True,
            },
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert len(payload["samples"]) == 1
    assert payload["samples"][0]["suitability_status"] == "excluded"
    assert payload["preflight"]["calibration_count"] == 0
    assert payload["preflight"]["excluded_count"] == 1
    assert undone.status_code == 200, undone.text
    restored = undone.json()["samples"][0]
    assert restored["suitability_status"] == "pending"
    assert restored["suitability_reason"] is None
    assert restored["suitability_reviewer_scope"] is None
    assert restored["suitability_reviewed_at"] is None
    assert restored["suitability_draft_revision"] is None


def test_review_disposition_is_durable_but_never_promotes_sample_to_gold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _client(tmp_path, monkeypatch) as (client, _):
        dataset = _create(client)
        uploaded = _upload(client, dataset, _image_bytes()).json()
        sample_id = uploaded["samples"][0]["sample_id"]
        client.app.state.product_foreground_extractor = _FakeExtractor()
        proposed = client.post(
            f"{BASE}/datasets/{dataset['dataset_id']}/samples/{sample_id}/proposal",
            params={"expected_revision": uploaded["revision"]},
            headers={"X-Customer-Id": str(CUSTOMER_A)},
        ).json()
        recorded = client.patch(
            f"{BASE}/datasets/{dataset['dataset_id']}/samples/{sample_id}/review-disposition",
            headers={"X-Customer-Id": str(CUSTOMER_A)},
            json={
                "expected_revision": proposed["revision"],
                "disposition": "needs_correction",
                "confirmed": True,
            },
        )
        recorded_payload = recorded.json()
        undone = client.patch(
            f"{BASE}/datasets/{dataset['dataset_id']}/samples/{sample_id}/review-disposition",
            headers={"X-Customer-Id": str(CUSTOMER_A)},
            json={
                "expected_revision": recorded_payload["revision"],
                "disposition": "pending",
                "confirmed": True,
            },
        )
        reloaded = client.get(
            f"{BASE}/datasets/{dataset['dataset_id']}",
            headers={"X-Customer-Id": str(CUSTOMER_A)},
        )

    assert recorded.status_code == 200, recorded.text
    assert reloaded.status_code == 200, reloaded.text
    recorded_sample = recorded_payload["samples"][0]
    assert recorded_sample["review_disposition"] == "needs_correction"
    assert recorded_sample["review_disposition_reviewer_scope"] == (
        "local-workbench-human"
    )
    assert recorded_sample["review_disposition_recorded_at"]
    assert recorded_sample["review_disposition_draft_revision"] == proposed["revision"]
    assert undone.status_code == 200, undone.text
    sample = reloaded.json()["samples"][0]
    assert sample["review_disposition"] == "pending"
    assert sample["review_disposition_reviewer_scope"] is None
    assert sample["review_disposition_recorded_at"] is None
    assert sample["review_disposition_draft_revision"] is None
    assert sample["human_reviewed"] is False
    assert sample["has_gold_mask"] is False
    assert sample["state"] == "mask_proposed"


def test_review_disposition_rejects_unconfirmed_or_maskless_samples(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _client(tmp_path, monkeypatch) as (client, _):
        dataset = _create(client)
        uploaded = _upload(client, dataset, _image_bytes()).json()
        sample_id = uploaded["samples"][0]["sample_id"]
        response = client.patch(
            f"{BASE}/datasets/{dataset['dataset_id']}/samples/{sample_id}/review-disposition",
            headers={"X-Customer-Id": str(CUSTOMER_A)},
            json={
                "expected_revision": uploaded["revision"],
                "disposition": "candidate_usable",
                "confirmed": True,
            },
        )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == (
        "foreground_benchmark_workbench_review_disposition_invalid"
    )


def test_label_schema_is_strict_and_delete_checks_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _client(tmp_path, monkeypatch) as (client, _):
        dataset = _create(client)
        uploaded = _upload(client, dataset, _image_bytes()).json()
        sample_id = uploaded["samples"][0]["sample_id"]
        invalid = client.patch(
            f"{BASE}/datasets/{dataset['dataset_id']}/samples/{sample_id}",
            headers={"X-Customer-Id": str(CUSTOMER_A)},
            json={
                "expected_revision": 1,
                "category": "bracelet",
                "difficulty": "easy",
                "scene_tags": [],
                "material_tags": [],
                "critical_component_names": ["clasp"],
                "storage_path": "/secret/path",
            },
        )
        valid = client.patch(
            f"{BASE}/datasets/{dataset['dataset_id']}/samples/{sample_id}",
            headers={"X-Customer-Id": str(CUSTOMER_A)},
            json={
                "expected_revision": 1,
                "category": "bracelet",
                "difficulty": "easy",
                "scene_tags": ["tray"],
                "material_tags": ["reflective"],
                "critical_component_names": ["clasp"],
            },
        )
        stale_delete = client.delete(
            f"{BASE}/datasets/{dataset['dataset_id']}/samples/{sample_id}",
            params={"expected_revision": 1},
            headers={"X-Customer-Id": str(CUSTOMER_A)},
        )
        deleted = client.delete(
            f"{BASE}/datasets/{dataset['dataset_id']}/samples/{sample_id}",
            params={"expected_revision": 2},
            headers={"X-Customer-Id": str(CUSTOMER_A)},
        )
    assert invalid.status_code == 422
    assert valid.status_code == 200
    assert valid.json()["samples"][0]["state"] == "labeled"
    assert stale_delete.status_code == 409
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": True, "revision": 3}


def test_label_accepts_no_component_when_product_has_no_fitting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _client(tmp_path, monkeypatch) as (client, _):
        dataset = _create(client)
        uploaded = _upload(client, dataset, _image_bytes()).json()
        sample_id = uploaded["samples"][0]["sample_id"]
        labeled = client.patch(
            f"{BASE}/datasets/{dataset['dataset_id']}/samples/{sample_id}",
            headers={"X-Customer-Id": str(CUSTOMER_A)},
            json={
                "expected_revision": 1,
                "category": "bracelet",
                "difficulty": "easy",
                "scene_tags": ["tray"],
                "material_tags": ["reflective"],
                "critical_component_names": [],
            },
        )

    assert labeled.status_code == 200, labeled.text
    assert labeled.json()["samples"][0]["state"] == "labeled"
    assert labeled.json()["samples"][0]["critical_component_names"] == []


def test_flat_lay_prompt_suggestion_is_review_only_and_customer_scoped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _client(tmp_path, monkeypatch) as (client, _):
        dataset = _create(client)
        uploaded = _upload(client, dataset, _flat_lay_image_bytes()).json()
        sample = uploaded["samples"][0]
        endpoint = (
            f"{BASE}/datasets/{dataset['dataset_id']}/samples/"
            f"{sample['sample_id']}/prompt-suggestion"
        )
        unsupported = client.get(endpoint, headers={"X-Customer-Id": str(CUSTOMER_A)})
        labeled = client.patch(
            f"{BASE}/datasets/{dataset['dataset_id']}/samples/{sample['sample_id']}",
            headers={"X-Customer-Id": str(CUSTOMER_A)},
            json={
                "expected_revision": 1,
                "category": "earring",
                "difficulty": "medium",
                "scene_tags": ["flat_lay", "plain_background"],
                "material_tags": ["reflective"],
                "critical_component_names": ["ear_hook", "pendant"],
            },
        )
        suggestion = client.get(endpoint, headers={"X-Customer-Id": str(CUSTOMER_A)})
        foreign = client.get(endpoint, headers={"X-Customer-Id": str(CUSTOMER_B)})
        unchanged = client.get(
            f"{BASE}/datasets/{dataset['dataset_id']}",
            headers={"X-Customer-Id": str(CUSTOMER_A)},
        )

    assert unsupported.status_code == 422
    assert unsupported.json()["detail"]["code"] == (
        "foreground_benchmark_prompt_suggestion_scene_unsupported"
    )
    assert labeled.status_code == 200, labeled.text
    assert suggestion.status_code == 200, suggestion.text
    payload = suggestion.json()
    assert payload["source_sha256"] == sample["source_sha256"]
    assert payload["method"] == "flat-lay-color-components-v1"
    assert payload["component_count"] == 2
    assert payload["review_required"] is True
    assert len(payload["box"]) == 4
    assert len(payload["positive_points"]) == 2
    assert payload["negative_points"] == []
    assert foreign.status_code == 404
    assert unchanged.json()["revision"] == 2
    assert unchanged.json()["samples"][0]["state"] == "labeled"


def test_flat_lay_prompt_suggestion_rejects_unvalidated_product_categories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _client(tmp_path, monkeypatch) as (client, _):
        dataset = _create(client)
        uploaded = _upload(client, dataset, _flat_lay_image_bytes()).json()
        sample = uploaded["samples"][0]
        labeled = client.patch(
            f"{BASE}/datasets/{dataset['dataset_id']}/samples/{sample['sample_id']}",
            headers={"X-Customer-Id": str(CUSTOMER_A)},
            json={
                "expected_revision": 1,
                "category": "bracelet",
                "difficulty": "medium",
                "scene_tags": ["flat_lay", "plain_background"],
                "material_tags": ["reflective"],
                "critical_component_names": [],
            },
        )
        suggestion = client.get(
            f"{BASE}/datasets/{dataset['dataset_id']}/samples/"
            f"{sample['sample_id']}/prompt-suggestion",
            headers={"X-Customer-Id": str(CUSTOMER_A)},
        )

    assert labeled.status_code == 200, labeled.text
    assert suggestion.status_code == 422
    assert suggestion.json()["detail"]["code"] == (
        "foreground_benchmark_prompt_suggestion_category_unsupported"
    )


def test_thumbnail_and_persisted_metadata_do_not_leak_filename_or_exif(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    filename_secret = "VIP-Wang-ID-card-440300-secret.jpg"
    exif_secret = "private GPS home address"
    with _client(tmp_path, monkeypatch) as (client, private_root):
        dataset = _create(client)
        uploaded = _upload(
            client,
            dataset,
            _image_bytes("JPEG", exif_secret=exif_secret),
            headers={"X-Original-Filename": filename_secret},
        )
        sample_id = uploaded.json()["samples"][0]["sample_id"]
        thumbnail = client.get(
            f"{BASE}/datasets/{dataset['dataset_id']}/samples/{sample_id}/thumbnail",
            headers={"X-Customer-Id": str(CUSTOMER_A)},
        )
        persisted_json = "\n".join(
            path.read_text(errors="ignore") for path in private_root.rglob("*.json")
        )
    public_text = json.dumps(uploaded.json(), ensure_ascii=False)
    public_text += json.dumps(dict(thumbnail.headers), ensure_ascii=False)
    public_text += caplog.text + persisted_json
    assert uploaded.status_code == 201
    assert thumbnail.status_code == 200
    assert thumbnail.headers["Cache-Control"] == "private, no-store"
    assert thumbnail.headers["Content-Type"].startswith("image/jpeg")
    assert filename_secret not in public_text
    assert exif_secret not in public_text


def test_existing_style_sample_import_requires_fresh_consent_and_owned_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private_root = (tmp_path / "private-workbench").resolve()
    asset_root = (tmp_path / "style-assets").resolve()
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("LOCAL_STYLE_ASSET_ROOT", str(asset_root))
    monkeypatch.setenv("FOREGROUND_BENCHMARK_WORKBENCH_ENABLED", "true")
    monkeypatch.setenv("FOREGROUND_BENCHMARK_WORKBENCH_ROOT", str(private_root))
    engine = create_database_engine(f"sqlite:///{tmp_path / 'style-import.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    app = create_app()

    def session_override() -> Generator[Session, None, None]:
        with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = session_override
    content = _image_bytes("JPEG")
    digest = sha256(content).hexdigest()
    sample_id = uuid4()
    store = app.state.classification_bridge_store
    assert isinstance(store, LocalSyntheticAssetStore)
    store.persist_style_sample_content(CUSTOMER_A, sample_id, content, digest)
    with sessions() as session:
        session.add(Customer(id=CUSTOMER_A, display_name="Customer A"))
        session.add(
            StyleSample(
                id=sample_id,
                customer_id=CUSTOMER_A,
                photo_id="history-private",
                authorized_at=datetime.now(UTC),
                consent_provenance="customer_style_history_opt_in",
                consent_text_version="style-consent-v1",
                content_sha256=digest,
                mime_type="image/jpeg",
                width=24,
                height=16,
                asset_store_namespace=store.store_namespace,
            )
        )
        session.commit()
    try:
        with TestClient(app) as client:
            dataset = _create(client)
            endpoint = f"{BASE}/datasets/{dataset['dataset_id']}/style-samples/import"
            payload = {
                "expected_revision": 0,
                "style_sample_id": str(sample_id),
                "split": "calibration",
            }
            missing_consent = client.post(
                endpoint,
                headers={"X-Customer-Id": str(CUSTOMER_A)},
                json=payload,
            )
            imported = client.post(endpoint, headers=_headers(), json=payload)
    finally:
        engine.dispose()
    assert missing_consent.status_code == 422
    assert imported.status_code == 201, imported.text
    assert imported.json()["samples"][0]["source_sha256"] == digest


def test_local_proposal_requires_configured_extractor_and_sanitizes_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _client(tmp_path, monkeypatch) as (client, _):
        dataset = _create(client)
        uploaded = _upload(client, dataset, _image_bytes()).json()
        sample_id = uploaded["samples"][0]["sample_id"]
        endpoint = (
            f"{BASE}/datasets/{dataset['dataset_id']}/samples/{sample_id}/proposal"
        )
        client.app.state.product_foreground_extractor = None
        unavailable = client.post(
            endpoint,
            params={"expected_revision": 1},
            headers={"X-Customer-Id": str(CUSTOMER_A)},
        )
        client.app.state.product_foreground_extractor = _FailingExtractor()
        failed = client.post(
            endpoint,
            params={"expected_revision": 1},
            headers={"X-Customer-Id": str(CUSTOMER_A)},
        )
        client.app.state.product_foreground_extractor = _FakeExtractor()
        proposed = client.post(
            endpoint,
            params={"expected_revision": 1},
            headers={"X-Customer-Id": str(CUSTOMER_A)},
        )
    assert unavailable.status_code == 409
    assert (
        unavailable.json()["detail"]["code"]
        == "foreground_benchmark_extractor_unavailable"
    )
    assert failed.status_code == 422
    assert failed.json()["detail"]["code"] == "foreground_benchmark_proposal_failed"
    assert "/Users/private" not in failed.text
    assert proposed.status_code == 200, proposed.text
    assert proposed.json()["samples"][0]["state"] == "mask_proposed"
    assert proposed.json()["samples"][0]["has_proposed_mask"] is True
    assert proposed.json()["samples"][0]["human_reviewed"] is False
    assert proposed.json()["samples"][0]["proposal_coverage_ratio"] == 0.46875
    assert proposed.json()["samples"][0]["proposal_border_contact_ratio"] == 0
    assert proposed.json()["samples"][0]["proposal_review_risk"] is None


def test_proposed_mask_content_is_private_customer_scoped_and_not_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _client(tmp_path, monkeypatch) as (client, _):
        dataset = _create(client)
        uploaded = _upload(client, dataset, _image_bytes()).json()
        sample_id = uploaded["samples"][0]["sample_id"]
        endpoint = f"{BASE}/datasets/{dataset['dataset_id']}/samples/{sample_id}/mask"
        missing = client.get(
            endpoint,
            params={"kind": "proposal"},
            headers={"X-Customer-Id": str(CUSTOMER_A)},
        )
        client.app.state.product_foreground_extractor = _FakeExtractor()
        proposed = client.post(
            f"{BASE}/datasets/{dataset['dataset_id']}/samples/{sample_id}/proposal",
            params={"expected_revision": 1},
            headers={"X-Customer-Id": str(CUSTOMER_A)},
        )
        content = client.get(
            endpoint,
            params={"kind": "proposal"},
            headers={"X-Customer-Id": str(CUSTOMER_A)},
        )
        foreign = client.get(
            endpoint,
            params={"kind": "proposal"},
            headers={"X-Customer-Id": str(CUSTOMER_B)},
        )
    assert missing.status_code == 404
    assert proposed.status_code == 200
    assert content.status_code == 200
    assert content.headers["Cache-Control"] == "private, no-store"
    assert content.headers["Content-Type"].startswith("image/png")
    assert content.content == _mask_bytes()
    assert foreign.status_code == 404


def test_proposal_passes_normalized_human_prompt_to_local_extractor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extractor = _PromptCapturingExtractor()
    with _client(tmp_path, monkeypatch) as (client, _):
        dataset = _create(client)
        uploaded = _upload(client, dataset, _image_bytes()).json()
        sample_id = uploaded["samples"][0]["sample_id"]
        client.app.state.product_foreground_extractor = extractor
        proposed = client.post(
            f"{BASE}/datasets/{dataset['dataset_id']}/samples/{sample_id}/proposal",
            params={"expected_revision": 1},
            headers={"X-Customer-Id": str(CUSTOMER_A)},
            json={
                "box": [0.4, 0.46, 0.54, 0.86],
                "positive_points": [[0.465, 0.68]],
                "negative_points": [[0.34, 0.56]],
            },
        )
    assert proposed.status_code == 200, proposed.text
    assert extractor.request is not None
    assert extractor.request.prompt_hints is not None
    assert extractor.request.prompt_hints.box == (0.4, 0.46, 0.54, 0.86)
    assert extractor.request.prompt_hints.positive_points == ((0.465, 0.68),)
    assert extractor.request.prompt_hints.negative_points == ((0.34, 0.56),)


def test_pipeline_proposal_preserves_private_coarse_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extractor = _PipelineExtractor()
    with _client(tmp_path, monkeypatch) as (client, _):
        dataset = _create(client)
        uploaded = _upload(client, dataset, _image_bytes()).json()
        sample_id = uploaded["samples"][0]["sample_id"]
        endpoint = f"{BASE}/datasets/{dataset['dataset_id']}/samples/{sample_id}"
        client.app.state.product_foreground_extractor = extractor
        proposed = client.post(
            endpoint + "/proposal",
            params={"expected_revision": 1},
            headers={"X-Customer-Id": str(CUSTOMER_A)},
        )
        coarse = client.get(
            endpoint + "/mask",
            params={"kind": "coarse"},
            headers={"X-Customer-Id": str(CUSTOMER_A)},
        )
        foreign = client.get(
            endpoint + "/mask",
            params={"kind": "coarse"},
            headers={"X-Customer-Id": str(CUSTOMER_B)},
        )
    assert proposed.status_code == 200, proposed.text
    assert proposed.json()["samples"][0]["has_coarse_mask"] is True
    assert coarse.status_code == 200
    assert coarse.content == extractor.coarse
    assert coarse.headers["Cache-Control"] == "private, no-store"
    assert foreign.status_code == 404


def test_review_audit_compares_machine_candidates_without_reviewing_sample(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extractor = _PipelineExtractor()
    refined = _mask_bytes(box=(3, 3, 21, 13))
    expected = assess_proposal_disagreement(extractor.coarse, refined)
    with _client(tmp_path, monkeypatch) as (client, _):
        dataset = _create(client)
        uploaded = _upload(client, dataset, _image_bytes()).json()
        sample_id = uploaded["samples"][0]["sample_id"]
        client.app.state.product_foreground_extractor = extractor
        proposed = client.post(
            f"{BASE}/datasets/{dataset['dataset_id']}/samples/{sample_id}/proposal",
            params={"expected_revision": uploaded["revision"]},
            headers={"X-Customer-Id": str(CUSTOMER_A)},
        )
        audited = client.get(
            f"{BASE}/datasets/{dataset['dataset_id']}/review-audit",
            headers={"X-Customer-Id": str(CUSTOMER_A)},
        )
        audited_again = client.get(
            f"{BASE}/datasets/{dataset['dataset_id']}/review-audit",
            headers={"X-Customer-Id": str(CUSTOMER_A)},
        )
        foreign = client.get(
            f"{BASE}/datasets/{dataset['dataset_id']}/review-audit",
            headers={"X-Customer-Id": str(CUSTOMER_B)},
        )
        reloaded = client.get(
            f"{BASE}/datasets/{dataset['dataset_id']}",
            headers={"X-Customer-Id": str(CUSTOMER_A)},
        )

    assert proposed.status_code == 200, proposed.text
    assert audited.status_code == 200, audited.text
    assert audited_again.json() == audited.json()
    assert audited.json() == {
        "dataset_id": dataset["dataset_id"],
        "revision": proposed.json()["revision"],
        "items": [
            {
                "sample_id": sample_id,
                "intersection_over_union": expected[0],
                "disagreement_ratio": expected[1],
                "coarse_only_ratio": expected[2],
                "refined_only_ratio": expected[3],
                "coarse_polarity": "foreground",
                "raw_coarse_coverage_ratio": 0.5625,
                "raw_coarse_border_contact_ratio": 0.0,
            }
        ],
    }
    assert foreign.status_code == 404
    assert reloaded.status_code == 200
    assert reloaded.json()["revision"] == proposed.json()["revision"]
    assert reloaded.json()["samples"][0]["human_reviewed"] is False


def test_proposal_disagreement_handles_empty_and_rejects_size_mismatch() -> None:
    empty = _mask_bytes(box=None)
    assert assess_proposal_disagreement(empty, empty) == (1.0, 0.0, 0.0, 0.0)
    with pytest.raises(DomainRuleError) as error:
        assess_proposal_disagreement(empty, _mask_bytes(size=(8, 8), box=None))
    assert getattr(error.value, "code", None) == "foreground_benchmark_proposal_failed"


def test_proposal_disagreement_reports_background_polarity_without_mutating_evidence(
) -> None:
    refined = _mask_bytes(box=(8, 5, 16, 11))
    with Image.open(BytesIO(refined)) as foreground:
        background = Image.eval(foreground, lambda value: 255 - value)
        output = BytesIO()
        background.save(output, format="PNG")
        background_bytes = output.getvalue()

    assessment = assess_proposal_disagreement_evidence(background_bytes, refined)

    assert assessment.coarse_polarity == "background_inverted_for_comparison"
    assert assessment.intersection_over_union == 1.0
    assert assessment.disagreement_ratio == 0.0
    assert assessment.raw_coarse_coverage_ratio == 0.875
    assert assessment.raw_coarse_border_contact_ratio == 1.0
    assert background_bytes != refined


def test_saved_component_mask_can_be_reopened_privately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _client(tmp_path, monkeypatch) as (client, _):
        dataset = _create(client)
        uploaded = _upload(client, dataset, _image_bytes()).json()
        sample_id = uploaded["samples"][0]["sample_id"]
        labeled = client.patch(
            f"{BASE}/datasets/{dataset['dataset_id']}/samples/{sample_id}",
            headers={"X-Customer-Id": str(CUSTOMER_A)},
            json={
                "expected_revision": uploaded["revision"],
                "category": "bracelet",
                "difficulty": "medium",
                "scene_tags": [],
                "material_tags": [],
                "critical_component_names": ["clasp"],
            },
        ).json()
        endpoint = (
            f"{BASE}/datasets/{dataset['dataset_id']}/samples/{sample_id}/gold-mask"
        )
        saved = _gold_request(
            client,
            endpoint,
            expected_revision=labeled["revision"],
            gold=_mask_bytes(box=(1, 1, 7, 7)),
            component=_mask_bytes(box=(2, 2, 4, 4)),
        )
        saved_sample = saved.json()["samples"][0]
        mask_endpoint = (
            f"{BASE}/datasets/{dataset['dataset_id']}/samples/{sample_id}/mask"
        )
        content = client.get(
            mask_endpoint,
            params={"kind": "component", "component_name": "clasp"},
            headers={"X-Customer-Id": str(CUSTOMER_A)},
        )
        foreign = client.get(
            mask_endpoint,
            params={"kind": "component", "component_name": "clasp"},
            headers={"X-Customer-Id": str(CUSTOMER_B)},
        )
    assert saved.status_code == 200
    assert saved_sample["gold_mask_coverage_ratio"] == 0.09375
    assert saved_sample["gold_mask_border_contact_ratio"] == 0
    assert saved_sample["gold_mask_soft_edge_ratio"] == 0
    assert saved_sample["gold_mask_component_count"] == 1
    assert saved_sample["gold_mask_review_risks"] == []
    assert content.status_code == 200
    assert content.content == _mask_bytes(box=(2, 2, 4, 4))
    assert content.headers["Cache-Control"] == "private, no-store"
    assert foreign.status_code == 404


def _gold_request(
    client: TestClient,
    endpoint: str,
    *,
    expected_revision: int,
    gold: bytes,
    component: bytes | None = None,
    component_name: str = "clasp",
) -> object:
    files: list[tuple[str, tuple[str, bytes, str]]] = [
        ("gold_mask", ("gold.png", gold, "image/png"))
    ]
    data: dict[str, str] = {"expected_revision": str(expected_revision)}
    if component is not None:
        files.append(("component_masks", ("component.png", component, "image/png")))
        data["component_names"] = component_name
    return client.post(
        endpoint,
        headers={"X-Customer-Id": str(CUSTOMER_A)},
        data=data,
        files=files,
    )


def test_gold_masks_are_validated_contained_and_human_review_is_digest_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _client(tmp_path, monkeypatch) as (client, private_root):
        dataset = _create(client)
        uploaded = _upload(client, dataset, _image_bytes()).json()
        sample_id = uploaded["samples"][0]["sample_id"]
        label = client.patch(
            f"{BASE}/datasets/{dataset['dataset_id']}/samples/{sample_id}",
            headers={"X-Customer-Id": str(CUSTOMER_A)},
            json={
                "expected_revision": 1,
                "category": "bracelet",
                "difficulty": "hard",
                "scene_tags": ["complex_background"],
                "material_tags": ["reflective"],
                "critical_component_names": ["clasp"],
            },
        ).json()
        assert label["revision"] == 2
        endpoint = (
            f"{BASE}/datasets/{dataset['dataset_id']}/samples/{sample_id}/gold-mask"
        )
        wrong_size = _gold_request(
            client,
            endpoint,
            expected_revision=2,
            gold=_mask_bytes(size=(10, 10), box=(2, 2, 8, 8)),
            component=_mask_bytes(box=(5, 5, 8, 8)),
        )
        wrong_mode = _gold_request(
            client,
            endpoint,
            expected_revision=2,
            gold=_mask_bytes(mode="RGB"),
            component=_mask_bytes(box=(5, 5, 8, 8)),
        )
        empty = _gold_request(
            client,
            endpoint,
            expected_revision=2,
            gold=_mask_bytes(box=None),
            component=_mask_bytes(box=(5, 5, 8, 8)),
        )
        full = _gold_request(
            client,
            endpoint,
            expected_revision=2,
            gold=_mask_bytes(box=(0, 0, 24, 16)),
            component=_mask_bytes(box=(5, 5, 8, 8)),
        )
        outside = _gold_request(
            client,
            endpoint,
            expected_revision=2,
            gold=_mask_bytes(box=(4, 4, 20, 12)),
            component=_mask_bytes(box=(3, 4, 6, 7)),
        )
        no_component = _gold_request(
            client,
            endpoint,
            expected_revision=2,
            gold=_mask_bytes(),
        )
        before_valid = list(private_root.rglob("*/masks/*.png"))
        valid = _gold_request(
            client,
            endpoint,
            expected_revision=2,
            gold=_mask_bytes(),
            component=_mask_bytes(box=(5, 5, 8, 8)),
        )
        assert valid.status_code == 200, valid.text
        body = valid.json()
        sample = body["samples"][0]
        review_endpoint = (
            f"{BASE}/datasets/{dataset['dataset_id']}/samples/{sample_id}/review"
        )
        stale = client.post(
            review_endpoint,
            headers={"X-Customer-Id": str(CUSTOMER_A)},
            json={
                "expected_revision": 3,
                "confirmed": True,
                "source_sha256": sample["source_sha256"],
                "gold_mask_sha256": "0" * 64,
                "component_mask_sha256s": sample["component_mask_sha256s"],
                "consent_text_version": BENCHMARK_CONSENT_VERSION,
            },
        )
        reviewed = client.post(
            review_endpoint,
            headers={"X-Customer-Id": str(CUSTOMER_A)},
            json={
                "expected_revision": 3,
                "confirmed": True,
                "source_sha256": sample["source_sha256"],
                "gold_mask_sha256": sample["gold_mask_sha256"],
                "component_mask_sha256s": sample["component_mask_sha256s"],
                "consent_text_version": BENCHMARK_CONSENT_VERSION,
            },
        )
        draft_file = next(
            (
                private_root / "drafts" / str(CUSTOMER_A) / str(dataset["dataset_id"])
            ).glob("draft.json")
        )
        draft_pointer = json.loads(draft_file.read_text())
        revision_file = draft_file.parent / "revisions" / draft_pointer["snapshot"]
        persisted_review = json.loads(revision_file.read_text())["samples"][0][
            "human_review"
        ]
    for response in (wrong_size, wrong_mode, empty, full):
        assert response.status_code == 422
        assert (
            response.json()["detail"]["code"]
            == "foreground_benchmark_gold_mask_invalid"
        )
    assert outside.status_code == 422
    assert (
        outside.json()["detail"]["code"]
        == "foreground_benchmark_component_outside_gold"
    )
    assert no_component.status_code == 422
    assert (
        no_component.json()["detail"]["code"]
        == "foreground_benchmark_component_required"
    )
    assert before_valid == []
    assert sample["state"] == "mask_proposed"
    assert sample["has_gold_mask"] is True
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "foreground_benchmark_review_stale"
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["samples"][0]["state"] == "human_reviewed"
    assert reviewed.json()["samples"][0]["human_reviewed"] is True
    assert persisted_review["reviewer_scope"] == f"local_customer_operator:{CUSTOMER_A}"
    assert persisted_review["reviewed_at"].endswith("+00:00")


def test_component_annotation_reopen_invalidates_old_human_signature(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _client(tmp_path, monkeypatch) as (client, _):
        dataset = _create(client)
        uploaded = _upload(client, dataset, _image_bytes()).json()
        sample_id = uploaded["samples"][0]["sample_id"]
        labeled = client.patch(
            f"{BASE}/datasets/{dataset['dataset_id']}/samples/{sample_id}",
            headers={"X-Customer-Id": str(CUSTOMER_A)},
            json={
                "expected_revision": 1,
                "category": "bracelet",
                "difficulty": "hard",
                "scene_tags": ["complex_background"],
                "material_tags": ["reflective"],
                "critical_component_names": [],
            },
        ).json()
        gold = _gold_request(
            client,
            f"{BASE}/datasets/{dataset['dataset_id']}/samples/{sample_id}/gold-mask",
            expected_revision=labeled["revision"],
            gold=_mask_bytes(),
        ).json()
        sample = gold["samples"][0]
        reviewed = client.post(
            f"{BASE}/datasets/{dataset['dataset_id']}/samples/{sample_id}/review",
            headers={"X-Customer-Id": str(CUSTOMER_A)},
            json={
                "expected_revision": gold["revision"],
                "confirmed": True,
                "source_sha256": sample["source_sha256"],
                "gold_mask_sha256": sample["gold_mask_sha256"],
                "component_mask_sha256s": {},
                "consent_text_version": BENCHMARK_CONSENT_VERSION,
            },
        ).json()
        reopened = client.patch(
            f"{BASE}/datasets/{dataset['dataset_id']}/samples/{sample_id}/component-annotation",
            headers={"X-Customer-Id": str(CUSTOMER_A)},
            json={
                "expected_revision": reviewed["revision"],
                "critical_component_names": ["clasp"],
                "confirmed": True,
            },
        )
        rejected_without_confirmation = client.patch(
            f"{BASE}/datasets/{dataset['dataset_id']}/samples/{sample_id}/component-annotation",
            headers={"X-Customer-Id": str(CUSTOMER_A)},
            json={
                "expected_revision": reopened.json()["revision"],
                "critical_component_names": ["clasp"],
                "confirmed": False,
            },
        )
        cleared = client.patch(
            f"{BASE}/datasets/{dataset['dataset_id']}/samples/{sample_id}/component-annotation",
            headers={"X-Customer-Id": str(CUSTOMER_A)},
            json={
                "expected_revision": reopened.json()["revision"],
                "critical_component_names": [],
                "confirmed": True,
            },
        )

    assert reopened.status_code == 200, reopened.text
    reopened_sample = reopened.json()["samples"][0]
    assert reopened_sample["state"] == "mask_proposed"
    assert reopened_sample["human_reviewed"] is False
    assert reopened_sample["has_gold_mask"] is True
    assert reopened_sample["critical_component_names"] == ["clasp"]
    assert reopened_sample["component_mask_sha256s"] == {}
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["samples"][0]["critical_component_names"] == []
    assert rejected_without_confirmation.status_code == 422
    assert (
        rejected_without_confirmation.json()["detail"]["code"]
        == "foreground_benchmark_component_required"
    )


def test_preflight_blocks_freeze_without_starting_a_benchmark(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _client(tmp_path, monkeypatch) as (client, _):
        dataset = _create(client)
        preflight = client.get(
            f"{BASE}/datasets/{dataset['dataset_id']}/preflight",
            headers={"X-Customer-Id": str(CUSTOMER_A)},
        )
        frozen = client.post(
            f"{BASE}/datasets/{dataset['dataset_id']}/freeze",
            headers={"X-Customer-Id": str(CUSTOMER_A)},
            json={"expected_revision": 0, "idempotency_key": "freeze-empty"},
        )
    assert preflight.status_code == 200
    assert preflight.json()["ready_to_freeze"] is False
    assert preflight.json()["calibration_count"] == 0
    assert preflight.json()["acceptance_count"] == 0
    assert frozen.status_code == 409
    assert frozen.json()["detail"]["code"] == "foreground_benchmark_workbench_not_ready"


def test_revocation_requires_confirmation_and_persists_digest_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    digest = "a" * 64
    with _client(tmp_path, monkeypatch) as (client, private_root):
        denied = client.post(
            BASE + "/revocations",
            headers={"X-Customer-Id": str(CUSTOMER_A)},
            json={"source_sha256": digest, "confirmed": False},
        )
        accepted = client.post(
            BASE + "/revocations",
            headers={"X-Customer-Id": str(CUSTOMER_A)},
            json={"source_sha256": digest, "confirmed": True},
        )
        registry = next((private_root / "revocations").glob("*.json"))
        persisted = registry.read_text()
    assert denied.status_code == 422
    assert accepted.status_code == 204
    assert json.loads(persisted) == {"digests": [digest]}
    assert str(tmp_path) not in persisted
