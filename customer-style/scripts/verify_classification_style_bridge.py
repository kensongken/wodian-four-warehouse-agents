"""Run the deterministic, local-only classification-to-style acceptance."""

# Security-critical: the existing guards must be imported before application code.
# ruff: noqa: I001

from __future__ import annotations

import os
import sys
import json as _bootstrap_json
from pathlib import Path

os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.dont_write_bytecode = True

CONSUMER_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CONSUMER_ROOT / "src"))

_PREFLIGHT_ENV = "_CLASSIFICATION_STYLE_BRIDGE_PREFLIGHT_V1"
_preflight_payload = os.environ.pop(_PREFLIGHT_ENV, None)
if __name__ == "__main__" and _preflight_payload is None:
    import argparse as _bootstrap_argparse

    from classification_style_bridge_preflight import (
        build_preflight as _build_preflight,
        emit_bootstrap_failure as _emit_bootstrap_failure,
    )

    _bootstrap_parser = _bootstrap_argparse.ArgumentParser(add_help=False)
    _bootstrap_parser.add_argument("--producer-root", type=Path, required=True)
    _bootstrap_parser.add_argument("--consumer-head", required=True)
    _bootstrap_parser.add_argument("--producer-head", required=True)
    _bootstrap_parser.add_argument("--output", type=Path)
    _bootstrap_args, _ = _bootstrap_parser.parse_known_args()
    try:
        _bootstrap_evidence = _build_preflight(
            consumer_root=CONSUMER_ROOT,
            producer_root=_bootstrap_args.producer_root.resolve(strict=True),
            consumer_head=_bootstrap_args.consumer_head,
            producer_head=_bootstrap_args.producer_head,
        )
    except Exception as _bootstrap_error:
        raise SystemExit(
            _emit_bootstrap_failure(
                error=_bootstrap_error,
                output=_bootstrap_args.output,
                schema_version="classification-style-bridge-acceptance-v1",
            )
        ) from _bootstrap_error
    _bootstrap_environment = os.environ.copy()
    _bootstrap_environment[_PREFLIGHT_ENV] = _bootstrap_json.dumps(
        _bootstrap_evidence, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )
    os.execve(
        sys.executable,
        [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]],
        _bootstrap_environment,
    )

PREFLIGHT_EVIDENCE = (
    _bootstrap_json.loads(_preflight_payload)
    if _preflight_payload is not None
    else None
)

# Reuse the existing fail-closed guards.  Importing this module installs them
# before the producer package or any acceptance fixture is loaded.
from verify_image_output_demo import (
    FILE_SIDE_EFFECT_GUARD,
    OUTBOUND_GUARD,
    _emit,
)

import argparse
import json
import re
import secrets
import shutil
import sqlite3
import tempfile
import traceback
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from io import BytesIO
from typing import Any, cast
from unittest.mock import patch
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from opc_style.contracts.classification_handoff import (
    ClassificationStyleHandoffV1,
    canonical_handoff_bytes,
    parse_classification_handoff_v1,
)
from opc_style.db import create_database_engine
from opc_style.domain.image_generator import (
    DeterministicLocalImageGenerator,
    ImageGenerationError,
)
from opc_style.main import create_app
from opc_style.models import (
    ApprovedImageAssociation,
    AuditEvent,
    Base,
    Customer,
    ImageCandidate,
    ImageOutputJob,
    ImageReview,
    ImageReviewRound,
    ProductAsset,
    SceneStyle,
    StyleCard,
    StyleCardVersion,
)
from opc_style.services.classification_bridge import _customer_id
from opc_style.services.local_media_stage import LocalMediaStage
from opc_style.settings import LocalBridgeSettings

ACCEPTANCE_SCHEMA = "classification-style-bridge-acceptance-v1"
GOLDEN_HANDOFF_SHA256 = (
    "95c62a43872a4befd1728a041c1ffb434ec0c9ae473c7d0898b9949e590ef201"
)
FUTURE_CLEANUP_TIME = datetime(2036, 1, 1, tzinfo=UTC)
FORBIDDEN_HANDOFF_FIELDS = {
    "access_token",
    "api_key",
    "credential",
    "credentials",
    "file_path",
    "password",
    "path",
    "secret",
    "source_path",
    "token",
    "url",
}


@dataclass(frozen=True)
class ProducerRuntime:
    create_local_app: Callable[..., FastAPI]
    style_bridge_client: type[Any]
    style_bridge_configuration_error: type[Exception]
    unavailable_vision_provider: type[Any]
    handoff_model: type[Any]
    canonical_handoff_bytes: Callable[[Any], bytes]


@dataclass(frozen=True)
class FixturePhoto:
    filename: str
    size: tuple[int, int]
    color: tuple[int, int, int]
    expected_automatic: str
    expected_confirmed: str
    correction: str | None = None


class RecordingASGITransport(httpx.ASGITransport):
    """Record only boundary facts while forwarding to the real in-process app."""

    def __init__(self, *, app: FastAPI, nonce: str) -> None:
        super().__init__(app=app, client=("127.0.0.1", 51000))
        self.forwarded_app = app
        self.expected_nonce = nonce
        self.nonce_headers: list[str | None] = []
        self.handoff_bodies: list[bytes] = []
        self.media_count = 0
        self.request_counts: dict[str, int] = {}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        request_path = request.url.path
        handoff_prefix = "/v1/local-test/classification-bridge/handoffs/"
        if request_path.startswith(handoff_prefix):
            request_path = f"{handoff_prefix}{{handoff_id}}"
        request_path = re.sub(
            r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}",
            "{id}",
            request_path,
        )
        request_key = f"{request.method} {request_path}"
        self.request_counts[request_key] = self.request_counts.get(request_key, 0) + 1
        self.nonce_headers.append(
            request.headers.get("X-Local-Classification-Bridge-Nonce")
        )
        if request.url.path.endswith("/handoffs") and request.method == "POST":
            self.handoff_bodies.append(body)
        if request.url.path.endswith("/media") and request.method == "POST":
            self.media_count += 1
        replay = httpx.Request(
            request.method,
            request.url,
            headers=request.headers,
            content=body,
            extensions=request.extensions,
        )
        return await super().handle_async_request(replay)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--producer-root", type=Path, required=True)
    parser.add_argument("--consumer-head", required=True)
    parser.add_argument("--producer-head", required=True)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def _git_directory(root: Path) -> Path:
    marker = root / ".git"
    if marker.is_dir():
        return marker.resolve()
    content = marker.read_text(encoding="utf-8").strip()
    prefix = "gitdir: "
    if not content.startswith(prefix):
        raise ValueError("git_directory_invalid")
    value = Path(content.removeprefix(prefix))
    return (root / value).resolve() if not value.is_absolute() else value.resolve()


def _git_common_directory(git_directory: Path) -> Path:
    marker = git_directory / "commondir"
    if not marker.is_file():
        return git_directory
    value = Path(marker.read_text(encoding="utf-8").strip())
    return (git_directory / value).resolve()


def _read_ref(common: Path, ref: str) -> str:
    loose = common / ref
    if loose.is_file():
        return loose.read_text(encoding="ascii").strip()
    packed = common / "packed-refs"
    if packed.is_file():
        for line in packed.read_text(encoding="ascii").splitlines():
            if not line or line.startswith(("#", "^")):
                continue
            digest, candidate = line.split(" ", 1)
            if candidate == ref:
                return digest
    raise ValueError("git_ref_not_found")


def _head(root: Path) -> str:
    git_directory = _git_directory(root)
    value = (git_directory / "HEAD").read_text(encoding="ascii").strip()
    if not value.startswith("ref: "):
        return value
    return _read_ref(_git_common_directory(git_directory), value.removeprefix("ref: "))


def _validate_repository_pins(
    producer_root: Path, *, consumer_head: str, producer_head: str
) -> None:
    if _head(CONSUMER_ROOT) != consumer_head:
        raise ValueError("consumer_head_mismatch")
    if _head(producer_root) != producer_head:
        raise ValueError("producer_head_mismatch")


def _copy_and_load_producer(producer_root: Path, temp_root: Path) -> ProducerRuntime:
    source = producer_root / "services" / "backend" / "src" / "product_engine"
    if not source.is_dir():
        raise ValueError("producer_source_missing")
    runtime_root = temp_root / "producer-runtime"
    destination = runtime_root / "services" / "backend" / "src" / "product_engine"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )
    sys.path.insert(0, str(destination.parent))
    with patch.dict(
        os.environ,
        {"OPENAI_API_KEY": "", "LOCAL_VISION_EXECUTABLE": ""},
        clear=False,
    ):
        from product_engine.api.local_app import create_local_app
        from product_engine.contracts.style_test_bridge import (
            ClassificationStyleHandoffV1 as ProducerHandoff,
        )
        from product_engine.contracts.style_test_bridge import (
            canonical_handoff_bytes as producer_canonical_handoff_bytes,
        )
        from product_engine.integrations.style_test_bridge_client import (
            StyleBridgeClient,
            StyleBridgeConfigurationError,
        )
        from product_engine.services.image_recognition import (
            UnavailableVisionProvider,
        )

    return ProducerRuntime(
        create_local_app=create_local_app,
        style_bridge_client=StyleBridgeClient,
        style_bridge_configuration_error=StyleBridgeConfigurationError,
        unavailable_vision_provider=UnavailableVisionProvider,
        handoff_model=ProducerHandoff,
        canonical_handoff_bytes=producer_canonical_handoff_bytes,
    )


def _expect(response: Any, status: int, label: str) -> dict[str, Any]:
    if response.status_code != status:
        raise AssertionError(
            f"{label}: expected HTTP {status}, got {response.status_code}: "
            f"{response.text}"
        )
    body = response.json()
    if not isinstance(body, dict):
        raise TypeError(f"{label}: response must be an object")
    return cast(dict[str, Any], body)


def _png_bytes(size: tuple[int, int], color: tuple[int, int, int]) -> bytes:
    output = BytesIO()
    Image.new("RGB", size, color).save(output, format="PNG")
    return output.getvalue()


def _golden_payload() -> dict[str, object]:
    return {
        "schema_version": "classification-style-handoff-v1",
        "handoff_id": "11111111-1111-4111-8111-111111111111",
        "customer_test_id": "test-customer-001",
        "product_id": "test-product-001",
        "product_category": "turquoise",
        "product_form": "bracelet",
        "classification_version": "classification-v7",
        "confirmation_version": 3,
        "main_photo_id": "21111111-1111-4111-8111-111111111111",
        "transform_permission": True,
        "photos": [
            {
                "photo_id": "21111111-1111-4111-8111-111111111111",
                "content_sha256": "a" * 64,
                "mime_type": "image/jpeg",
                "width": 1200,
                "height": 900,
                "automatic_category": "real_photo",
                "confidence": "high",
                "human_corrected": False,
                "confirmed_category": "real_photo",
                "media_receipt_id": "31111111-1111-4111-8111-111111111111",
            },
            {
                "photo_id": "22222222-2222-4222-8222-222222222222",
                "content_sha256": "b" * 64,
                "mime_type": "image/png",
                "width": 1080,
                "height": 1440,
                "automatic_category": "wear_photo",
                "confidence": "medium",
                "human_corrected": True,
                "confirmed_category": "wear_photo",
                "media_receipt_id": "32222222-2222-4222-8222-222222222222",
            },
            {
                "photo_id": "23333333-3333-4333-8333-333333333333",
                "content_sha256": "c" * 64,
                "mime_type": "image/heic",
                "width": 1600,
                "height": 1200,
                "automatic_category": "scene_photo",
                "confidence": "low",
                "human_corrected": False,
                "confirmed_category": "scene_photo",
                "media_receipt_id": "33333333-3333-4333-8333-333333333333",
            },
            {
                "photo_id": "24444444-4444-4444-8444-444444444444",
                "content_sha256": "d" * 64,
                "mime_type": "image/jpeg",
                "width": 2048,
                "height": 1536,
                "automatic_category": "multi_product_photo",
                "confidence": "high",
                "human_corrected": False,
                "confirmed_category": "multi_product_photo",
                "media_receipt_id": "34444444-4444-4444-8444-444444444444",
            },
        ],
        "idempotency_key": "style-test:test-product-001:v3",
        "desensitized_test_only": True,
        "source": "classification_local_page",
        "publishable": False,
        "production_eligible": False,
    }


def _verify_contract_pin(producer: ProducerRuntime) -> None:
    golden_bytes = json.dumps(
        _golden_payload(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    producer_handoff = producer.handoff_model.model_validate_json(golden_bytes)
    consumer_handoff = parse_classification_handoff_v1(golden_bytes)
    if producer.canonical_handoff_bytes(producer_handoff) != golden_bytes:
        raise AssertionError("producer_contract_canonical_drift")
    if canonical_handoff_bytes(consumer_handoff) != golden_bytes:
        raise AssertionError("consumer_contract_canonical_drift")
    if sha256(golden_bytes).hexdigest() != GOLDEN_HANDOFF_SHA256:
        raise AssertionError("handoff_golden_digest_drift")


def _catalog() -> dict[str, dict[str, str]]:
    def entry(
        product_id: str,
        *,
        customer: str = "test-customer-local",
        category: str = "turquoise",
        form: str = "bracelet",
    ) -> dict[str, str]:
        return {
            "customer_test_id": customer,
            "product_id": product_id,
            "product_category": category,
            "product_form": form,
            "display_label": product_id,
        }

    return {
        "test-product-scene": entry("test-product-scene"),
        "test-product-multi": entry(
            "test-product-multi", category="nanhong", form="necklace"
        ),
        "test-product-wear": entry(
            "test-product-wear", category="jadeite", form="ring"
        ),
        "test-product-no-pin": entry(
            "test-product-no-pin", customer="test-customer-no-pin"
        ),
        "test-product-negative": entry("test-product-negative"),
        "test-product-authenticity-failure": entry("test-product-authenticity-failure"),
        "test-product-multiple-scenes": entry("test-product-multiple-scenes"),
        "test-product-generation-failure": entry("test-product-generation-failure"),
    }


def _seed_local_style_pin(factory: sessionmaker[Session]) -> None:
    customer_id = _customer_id("test-customer-local")
    with factory() as session:
        session.add(
            Customer(id=customer_id, display_name="Synthetic bridge acceptance")
        )
        main = StyleCard(customer_id=customer_id, kind="main", active=True)
        session.add(main)
        session.flush()
        main_version = StyleCardVersion(
            customer_id=customer_id,
            style_card_id=main.id,
            version=1,
            content={"tone": ["synthetic-local-test"]},
        )
        session.add(main_version)
        scene_card = StyleCard(customer_id=customer_id, kind="scene", active=True)
        session.add(scene_card)
        session.flush()
        scene_version = StyleCardVersion(
            customer_id=customer_id,
            style_card_id=scene_card.id,
            version=1,
            content={"main_style_card_version_id": str(main_version.id)},
        )
        session.add(scene_version)
        session.add(
            SceneStyle(
                customer_id=customer_id,
                style_card_id=scene_card.id,
                name="Synthetic local bridge scene",
                active=True,
            )
        )
        session.commit()


def _positive_specs() -> tuple[tuple[str, str, tuple[FixturePhoto, ...]], ...]:
    return (
        (
            "product-01-scene",
            "test-product-scene",
            (
                FixturePhoto(
                    "studio-product-one.png",
                    (48, 48),
                    (32, 84, 136),
                    "real_photo",
                    "real_photo",
                ),
                FixturePhoto(
                    "scene-detail.png",
                    (48, 48),
                    (80, 42, 128),
                    "real_photo",
                    "scene_photo",
                    "scene_photo",
                ),
                FixturePhoto(
                    "catalog-product-two.png",
                    (48, 48),
                    (24, 116, 72),
                    "real_photo",
                    "real_photo",
                ),
            ),
        ),
        (
            "product-02-multi",
            "test-product-multi",
            (
                FixturePhoto(
                    "studio-product-three.png",
                    (48, 48),
                    (108, 52, 28),
                    "real_photo",
                    "real_photo",
                ),
                FixturePhoto(
                    "multi-set.png",
                    (48, 48),
                    (36, 108, 118),
                    "multi_product_photo",
                    "multi_product_photo",
                    "multi_product_photo",
                ),
                FixturePhoto(
                    "catalog-product-four.png",
                    (48, 48),
                    (118, 74, 34),
                    "real_photo",
                    "real_photo",
                ),
            ),
        ),
        (
            "product-03-wear",
            "test-product-wear",
            (
                FixturePhoto(
                    "studio-product-five.png",
                    (48, 48),
                    (56, 72, 136),
                    "real_photo",
                    "real_photo",
                ),
                FixturePhoto(
                    "wear-wrist.png",
                    (32, 64),
                    (118, 42, 72),
                    "wear_photo",
                    "wear_photo",
                ),
                FixturePhoto(
                    "catalog-product-six.png",
                    (48, 48),
                    (44, 126, 84),
                    "real_photo",
                    "real_photo",
                ),
            ),
        ),
    )


def _create_batch(client: TestClient, count: int) -> str:
    created = _expect(
        client.post("/local/api/upload-batches", json={"expected_count": count}),
        201,
        "create producer batch",
    )
    return str(cast(dict[str, object], created["batch"])["batch_id"])


def _upload_photo(
    client: TestClient,
    batch_id: str,
    position: int,
    fixture: FixturePhoto,
) -> dict[str, Any]:
    body = _png_bytes(fixture.size, fixture.color)
    response = client.post(
        f"/local/api/upload-batches/{batch_id}/images",
        data={"position": str(position)},
        files={"file": (fixture.filename, body, "image/png")},
    )
    uploaded = _expect(response, 201, f"upload {fixture.filename}")
    return cast(dict[str, Any], uploaded["item"])


def _latest_photo_fact(item: Mapping[str, Any]) -> dict[str, Any]:
    facts = cast(list[dict[str, Any]], item["photo_classification_facts"])
    if not facts:
        raise AssertionError("producer_photo_classification_missing")
    return facts[-1]


def _exercise_positive_product(
    client: TestClient,
    *,
    alias: str,
    product_id: str,
    fixtures: Sequence[FixturePhoto],
    expected_status: str | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
    batch_id = _create_batch(client, len(fixtures))
    uploaded = [
        _upload_photo(client, batch_id, position, fixture)
        for position, fixture in enumerate(fixtures)
    ]
    automatic = [
        str(_latest_photo_fact(item)["automatic_decision"]["category"])
        for item in uploaded
    ]
    if automatic != [fixture.expected_automatic for fixture in fixtures]:
        raise AssertionError("producer_automatic_category_drift")

    corrections: list[dict[str, object]] = []
    for fixture, item in zip(fixtures, uploaded, strict=True):
        if fixture.correction is None:
            continue
        before = _latest_photo_fact(item)
        corrected = _expect(
            client.post(
                f"/local/api/upload-batches/{batch_id}/items/"
                f"{item['item_id']}/photo-category-corrections",
                json={
                    "corrected_category": fixture.correction,
                    "reviewer_note": "synthetic acceptance exception confirmation",
                },
            ),
            200,
            "append photo category correction",
        )
        corrected_items = cast(list[dict[str, Any]], corrected["items"])
        updated = next(
            entry["item"]
            for entry in corrected_items
            if entry["item"]["item_id"] == item["item_id"]
        )
        after = _latest_photo_fact(cast(dict[str, Any], updated))
        corrections.append(
            {
                "automatic_category": before["automatic_decision"]["category"],
                "confidence": before["automatic_decision"]["confidence"],
                "confirmed_category": after["final_category"],
                "human_corrected": after["human_correction"] is not None,
            }
        )

    readiness = _expect(
        client.get(f"/local/api/upload-batches/{batch_id}/style-readiness"),
        200,
        "producer readiness",
    )
    if readiness["can_confirm"] is not True or readiness["missing_codes"] != [
        "product_confirmation_required",
        "main_photo_required",
        "transform_permission_required",
    ]:
        raise AssertionError(
            "producer_readiness_drift:"
            + json.dumps(readiness, ensure_ascii=False, sort_keys=True)
        )
    readiness_items = cast(list[dict[str, Any]], readiness["items"])
    confirmed = [str(item["final_category"]) for item in readiness_items]
    if confirmed != [fixture.expected_confirmed for fixture in fixtures]:
        raise AssertionError("producer_confirmed_category_drift")

    request = {
        "customer_test_id": "test-customer-local",
        "product_id": product_id,
        "main_photo_id": uploaded[0]["item_id"],
        "transform_permission": True,
    }
    submitted = _expect(
        client.post(
            f"/local/api/upload-batches/{batch_id}/confirm-and-generate",
            json=request,
        ),
        200,
        "producer handoff submission",
    )
    expected_status = expected_status or (
        "pending_real_model" if alias.endswith("wear") else "awaiting_review"
    )
    if (
        submitted["current_status"] != expected_status
        or submitted["created"] is not True
    ):
        raise AssertionError("producer_handoff_status_drift")
    replay = _expect(
        client.post(
            f"/local/api/upload-batches/{batch_id}/confirm-and-generate",
            json=request,
        ),
        200,
        "producer handoff replay",
    )
    if (
        replay["created"] is not False
        or replay["handoff_id"] != submitted["handoff_id"]
    ):
        raise AssertionError("producer_handoff_replay_drift")
    polled = _expect(
        client.get(f"/local/api/style-handoffs/{submitted['handoff_id']}"),
        200,
        "producer handoff poll",
    )
    if cast(dict[str, object], polled["handoff"])["current_status"] != expected_status:
        raise AssertionError("producer_handoff_poll_drift")

    trace: dict[str, object] = {
        "product": alias,
        "photo_count": len(fixtures),
        "automatic_categories": automatic,
        "confirmed_categories": confirmed,
        "correction": corrections[0] if corrections else None,
        "status": expected_status,
        "handoff_created": True,
        "replay_created": False,
    }
    internal = {
        "batch_id": batch_id,
        "main_photo_id": str(uploaded[0]["item_id"]),
        "alternate_main_photo_id": str(uploaded[-1]["item_id"]),
        "handoff_id": str(submitted["handoff_id"]),
        "output_job_ids": tuple(str(value) for value in submitted["output_job_ids"]),
    }
    return trace, internal


def _authenticity_failure_spec() -> tuple[FixturePhoto, ...]:
    return (
        FixturePhoto(
            "studio-authenticity-one.png",
            (48, 48),
            (64, 104, 144),
            "real_photo",
            "real_photo",
        ),
        FixturePhoto(
            "scene-authenticity.png",
            (64, 32),
            (144, 64, 104),
            "scene_photo",
            "scene_photo",
        ),
        FixturePhoto(
            "catalog-authenticity-two.png",
            (48, 48),
            (104, 144, 64),
            "real_photo",
            "real_photo",
        ),
    )


def _exercise_producer_negative_gates(
    client: TestClient,
) -> dict[str, dict[str, object]]:
    gates: dict[str, dict[str, object]] = {}

    corrupt_batch = _create_batch(client, 1)
    corrupt = client.post(
        f"/local/api/upload-batches/{corrupt_batch}/images",
        data={"position": "0"},
        files={"file": ("broken.jpg", b"\xff\xd8\xffbroken", "image/jpeg")},
    )
    corrupt_body = _expect(corrupt, 422, "corrupt image")
    if corrupt_body["error_code"] != "photo_decode_failed":
        raise AssertionError("corrupt_image_gate_drift")
    gates["corrupt_image_rejected"] = {
        "result": "PASS",
        "http_status": 422,
        "error_code": "photo_decode_failed",
    }

    video_batch = _create_batch(client, 1)
    video = client.post(
        f"/local/api/upload-batches/{video_batch}/images",
        data={"position": "0"},
        files={"file": ("explanation.mp4", b"synthetic-video", "video/mp4")},
    )
    video_body = _expect(video, 422, "video rejection")
    if video_body["error_code"] != "unsupported_image":
        raise AssertionError("video_gate_drift")
    gates["video_rejected"] = {
        "result": "PASS",
        "http_status": 422,
        "error_code": "unsupported_image",
    }

    duplicate_batch = _create_batch(client, 2)
    duplicate_fixture = FixturePhoto(
        "studio-duplicate.png",
        (40, 40),
        (68, 88, 108),
        "real_photo",
        "real_photo",
    )
    first = _upload_photo(client, duplicate_batch, 0, duplicate_fixture)
    duplicate_body = _png_bytes(duplicate_fixture.size, duplicate_fixture.color)
    duplicate = _expect(
        client.post(
            f"/local/api/upload-batches/{duplicate_batch}/images",
            data={"position": "1"},
            files={"file": ("duplicate-copy.png", duplicate_body, "image/png")},
        ),
        200,
        "duplicate upload",
    )
    if cast(dict[str, object], duplicate["item"])["status"] != "duplicate":
        raise AssertionError("duplicate_detection_drift")
    duplicate_readiness = _expect(
        client.get(f"/local/api/upload-batches/{duplicate_batch}/style-readiness"),
        200,
        "duplicate readiness",
    )
    if "photo_conflict_present" not in duplicate_readiness["missing_codes"]:
        raise AssertionError("duplicate_readiness_gate_missing")
    if first["status"] != "pending_review":
        raise AssertionError("duplicate_original_mutated")
    gates["duplicate_blocked"] = {
        "result": "PASS",
        "status": "duplicate",
        "missing_code": "photo_conflict_present",
    }

    missing_real_batch = _create_batch(client, 3)
    missing_real_fixtures = (
        FixturePhoto(
            "scene.png",
            (64, 32),
            (30, 60, 90),
            "scene_photo",
            "scene_photo",
        ),
        FixturePhoto(
            "wear.png",
            (32, 64),
            (90, 60, 30),
            "wear_photo",
            "wear_photo",
        ),
        FixturePhoto(
            "multi.png",
            (48, 48),
            (60, 90, 30),
            "multi_product_photo",
            "multi_product_photo",
        ),
    )
    for position, fixture in enumerate(missing_real_fixtures):
        _upload_photo(client, missing_real_batch, position, fixture)
    missing_real = _expect(
        client.get(f"/local/api/upload-batches/{missing_real_batch}/style-readiness"),
        200,
        "missing real readiness",
    )
    if "real_photo_required" not in missing_real["missing_codes"]:
        raise AssertionError("missing_real_gate_missing")
    gates["missing_real_photo_blocked"] = {
        "result": "PASS",
        "missing_code": "real_photo_required",
    }

    permission_batch = _create_batch(client, 1)
    permission_item = _upload_photo(
        client,
        permission_batch,
        0,
        FixturePhoto(
            "studio-permission.png",
            (40, 40),
            (84, 48, 112),
            "real_photo",
            "real_photo",
        ),
    )
    permission = _expect(
        client.post(
            f"/local/api/upload-batches/{permission_batch}/confirm-and-generate",
            json={
                "customer_test_id": "test-customer-local",
                "product_id": "test-product-negative",
                "main_photo_id": permission_item["item_id"],
                "transform_permission": False,
            },
        ),
        409,
        "missing permission",
    )
    if permission["missing_codes"] != ["transform_permission_required"]:
        raise AssertionError("permission_gate_drift")
    gates["missing_permission_blocked"] = {
        "result": "PASS",
        "http_status": 409,
        "missing_code": "transform_permission_required",
    }
    return gates


def _exercise_missing_pin(client: TestClient) -> dict[str, object]:
    fixtures = (
        FixturePhoto(
            "studio-no-pin.png",
            (48, 48),
            (42, 92, 122),
            "real_photo",
            "real_photo",
        ),
        FixturePhoto(
            "scene-no-pin.png",
            (64, 32),
            (122, 92, 42),
            "scene_photo",
            "scene_photo",
        ),
        FixturePhoto(
            "catalog-no-pin.png",
            (48, 48),
            (72, 122, 42),
            "real_photo",
            "real_photo",
        ),
    )
    batch_id = _create_batch(client, len(fixtures))
    items = [
        _upload_photo(client, batch_id, position, fixture)
        for position, fixture in enumerate(fixtures)
    ]
    response = client.post(
        f"/local/api/upload-batches/{batch_id}/confirm-and-generate",
        json={
            "customer_test_id": "test-customer-no-pin",
            "product_id": "test-product-no-pin",
            "main_photo_id": items[0]["item_id"],
            "transform_permission": True,
        },
    )
    body = _expect(response, 503, "missing style pin")
    if body["error_code"] != "local_style_scene_pin_required":
        raise AssertionError("missing_style_pin_gate_drift")
    return {
        "result": "PASS",
        "http_status": 503,
        "error_code": "local_style_scene_pin_required",
    }


def _single_real_handoff(
    *, receipt: Mapping[str, object], customer: str, product: str
) -> ClassificationStyleHandoffV1:
    photo_id = str(receipt["photo_id"])
    return ClassificationStyleHandoffV1.model_validate(
        {
            "schema_version": "classification-style-handoff-v1",
            "handoff_id": str(uuid4()),
            "customer_test_id": customer,
            "product_id": product,
            "product_category": "turquoise",
            "product_form": "bracelet",
            "classification_version": "classification-v7",
            "confirmation_version": 1,
            "main_photo_id": photo_id,
            "transform_permission": True,
            "photos": [
                {
                    "photo_id": photo_id,
                    "content_sha256": receipt["content_sha256"],
                    "mime_type": receipt["mime_type"],
                    "width": receipt["width"],
                    "height": receipt["height"],
                    "automatic_category": "real_photo",
                    "confidence": "high",
                    "human_corrected": False,
                    "confirmed_category": "real_photo",
                    "media_receipt_id": receipt["receipt_id"],
                }
            ],
            "idempotency_key": f"style-test:{product}:v1",
            "desensitized_test_only": True,
            "source": "classification_local_page",
            "publishable": False,
            "production_eligible": False,
        }
    )


def _exercise_expired_receipt(
    client: TestClient,
    stage: LocalMediaStage,
    nonce: str,
) -> dict[str, object]:
    media = _png_bytes((36, 36), (46, 76, 106))
    receipt = _expect(
        client.post(
            "/v1/local-test/classification-bridge/media",
            content=media,
            headers={
                "X-Photo-Id": str(uuid4()),
                "X-Content-Sha256": sha256(media).hexdigest(),
                "X-Local-Classification-Bridge-Nonce": nonce,
            },
        ),
        201,
        "stage expiry probe",
    )
    handoff = _single_real_handoff(
        receipt=receipt,
        customer="test-customer-expired",
        product="test-product-expired",
    )
    with patch.object(stage, "_now_utc", return_value=FUTURE_CLEANUP_TIME):
        response = client.post(
            "/v1/local-test/classification-bridge/handoffs",
            content=canonical_handoff_bytes(handoff),
            headers={"X-Local-Classification-Bridge-Nonce": nonce},
        )
    body = _expect(response, 503, "expired receipt")
    if body["error"]["code"] != "local_media_receipt_expired":
        raise AssertionError("expired_receipt_gate_drift")
    return {
        "result": "PASS",
        "http_status": 503,
        "error_code": "local_media_receipt_expired",
    }


def _exercise_cleanup_uncertainty(temp_root: Path) -> dict[str, str]:
    settings = LocalBridgeSettings(
        local_classification_bridge=True,
        local_classification_bridge_nonce="isolated-cleanup-probe",
        local_classification_bridge_stage_root=temp_root / "cleanup-probe-stage",
        local_classification_bridge_asset_root=temp_root / "cleanup-probe-assets",
        local_classification_bridge_receipt_ttl_seconds=1,
    )
    current = datetime(2026, 9, 1, tzinfo=UTC)
    stage = LocalMediaStage(
        settings=settings,
        caller_host="127.0.0.1",
        now=lambda: current,
    )
    media = _png_bytes((24, 24), (24, 64, 104))
    receipt = stage.stage(
        photo_id=uuid4(),
        claimed_sha256=sha256(media).hexdigest(),
        body=BytesIO(media),
    )
    with patch.object(stage, "_cleanup_receipt_files", return_value=False):
        first = stage.cleanup_expired(now=current.replace(year=2036))
    if first != (str(receipt.receipt_id),):
        raise AssertionError("cleanup_uncertainty_not_reported")
    second = stage.cleanup_expired(now=current.replace(year=2036))
    if second:
        raise AssertionError("cleanup_uncertainty_retry_failed")
    stage.close()
    return {
        "cleanup_uncertainty_first_result": ("cleanup_pending" if first else "cleaned"),
        "cleanup_uncertainty_retry_result": (
            "cleanup_pending" if second else "cleaned"
        ),
    }


def _seed_consumer_app(
    temp_root: Path, nonce: str
) -> tuple[FastAPI, sessionmaker[Session], Any]:
    database_path = temp_root / "consumer" / "bridge.db"
    database_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_database_engine(f"sqlite:///{database_path}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    app = _configured_consumer_app(temp_root, nonce)
    if app.state.classification_bridge_store is None:
        raise AssertionError("classification_bridge_store_not_initialized")
    _seed_local_style_pin(factory)
    return app, factory, engine


def _configured_consumer_app(temp_root: Path, nonce: str) -> FastAPI:
    database_path = temp_root / "consumer" / "bridge.db"
    stage_root = temp_root / "consumer" / "private-media"
    asset_root = temp_root / "consumer" / "private-assets"
    local_style_root = temp_root / "consumer" / "local-style-assets"
    with patch.dict(
        os.environ,
        {
            "DATABASE_URL": f"sqlite:///{database_path}",
            "LOCAL_CLASSIFICATION_BRIDGE": "true",
            "LOCAL_CLASSIFICATION_BRIDGE_NONCE": nonce,
            "LOCAL_CLASSIFICATION_BRIDGE_STAGE_ROOT": str(stage_root),
            "LOCAL_CLASSIFICATION_BRIDGE_ASSET_ROOT": str(asset_root),
            "LOCAL_STYLE_ASSET_ROOT": str(local_style_root),
            "LOCAL_CLASSIFICATION_BRIDGE_AUTHENTICITY_FAILURE_PRODUCT_ID": (
                "test-product-authenticity-failure"
            ),
        },
        clear=False,
    ):
        app = create_app()
    return app


def _default_off_gate(producer: ProducerRuntime, temp_root: Path) -> dict[str, object]:
    consumer_root = temp_root / "consumer-default-off"
    consumer_root.mkdir(parents=True, exist_ok=True)
    with patch.dict(
        os.environ,
        {
            "DATABASE_URL": f"sqlite:///{consumer_root / 'default-off.db'}",
            "LOCAL_STYLE_ASSET_ROOT": str(consumer_root / "style-assets"),
            "LOCAL_CLASSIFICATION_BRIDGE": "false",
            "LOCAL_CLASSIFICATION_BRIDGE_NONCE": "",
        },
        clear=False,
    ):
        consumer = create_app()
    with TestClient(consumer, client=("127.0.0.1", 51000)) as client:
        consumer_status = client.post(
            "/v1/local-test/classification-bridge/media"
        ).status_code
    producer_app = producer.create_local_app(
        temp_root / "producer-default-off",
        vision_provider=producer.unavailable_vision_provider(),
        style_test_bridge_enabled=False,
    )
    with TestClient(producer_app) as client:
        producer_status = client.get(
            "/local/api/upload-batches/disabled/style-readiness"
        ).status_code
    if (consumer_status, producer_status) != (404, 404):
        raise AssertionError("bridge_default_off_drift")
    return {
        "result": "PASS",
        "consumer_http_status": consumer_status,
        "producer_http_status": producer_status,
    }


def _nonce_gate(client: TestClient) -> dict[str, object]:
    media = _png_bytes((12, 12), (12, 34, 56))
    media_headers = {
        "X-Photo-Id": "00000000-0000-0000-0000-000000000901",
        "X-Content-Sha256": sha256(media).hexdigest(),
    }
    missing = client.post(
        "/v1/local-test/classification-bridge/media",
        content=media,
        headers=media_headers,
    )
    wrong = client.post(
        "/v1/local-test/classification-bridge/media",
        content=media,
        headers={
            **media_headers,
            "X-Local-Classification-Bridge-Nonce": "wrong-nonce",
        },
    )
    if missing.status_code != 401 or wrong.status_code != 403:
        raise AssertionError(
            "ephemeral_nonce_gate_drift: "
            f"missing={missing.status_code}:{missing.text}; "
            f"wrong={wrong.status_code}:{wrong.text}"
        )
    return {
        "result": "PASS",
        "missing_http_status": 401,
        "wrong_http_status": 403,
    }


def _external_target_gate(producer: ProducerRuntime) -> dict[str, object]:
    try:
        producer.style_bridge_client(
            enabled=True,
            base_url="https://example.invalid",
            session_nonce="local-probe-only",
        )
    except producer.style_bridge_configuration_error as error:
        code = str(error)
    else:
        raise AssertionError("external_bridge_target_was_accepted")
    if code != "style_bridge_loopback_required":
        raise AssertionError("external_target_gate_drift")
    return {
        "result": "PASS",
        "error_code": code,
        "connect_attempted": False,
    }


def _verify_captured_handoffs(
    transport: RecordingASGITransport,
    producer: ProducerRuntime,
    producer_root: Path,
    temp_root: Path,
) -> tuple[int, list[str]]:
    path_count = 0
    credential_fields: set[str] = set()
    roots = (str(CONSUMER_ROOT), str(producer_root), str(temp_root))
    for body in transport.handoff_bodies:
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise TypeError("handoff_body_not_object")
        encoded = body.decode("utf-8")
        path_count += sum(root in encoded for root in roots)
        credential_fields.update(FORBIDDEN_HANDOFF_FIELDS.intersection(payload))
        photos = payload.get("photos", [])
        if isinstance(photos, list):
            for photo in photos:
                if isinstance(photo, dict):
                    credential_fields.update(
                        FORBIDDEN_HANDOFF_FIELDS.intersection(photo)
                    )
        producer_handoff = producer.handoff_model.model_validate(payload)
        consumer_handoff = parse_classification_handoff_v1(body)
        if producer.canonical_handoff_bytes(producer_handoff) != body:
            raise AssertionError("captured_producer_handoff_not_canonical")
        if canonical_handoff_bytes(consumer_handoff) != body:
            raise AssertionError("captured_consumer_handoff_not_canonical")
    return path_count, sorted(credential_fields)


def _nonce_persisted(root: Path, nonce: str) -> bool:
    needle = nonce.encode("utf-8")
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            if needle in path.read_bytes():
                return True
        except OSError:
            continue
    return False


def _media_file_count(stage_root: Path) -> int:
    media_root = stage_root / "media"
    if not media_root.exists():
        return 0
    return sum(1 for path in media_root.rglob("*.media") if path.is_file())


def _receipt_ledger_observation(stage_root: Path) -> dict[str, object]:
    database = (stage_root / "receipts.sqlite3").resolve(strict=True)
    connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
    try:
        status_counts = {
            str(status): int(count)
            for status, count in connection.execute(
                "SELECT status, count(*) FROM media_receipts GROUP BY status "
                "ORDER BY status"
            )
        }
        total = int(
            connection.execute("SELECT count(*) FROM media_receipts").fetchone()[0]
        )
        active_pins = int(
            connection.execute(
                "SELECT count(*) FROM handoff_receipt_pins WHERE state='active'"
            ).fetchone()[0]
        )
    finally:
        connection.close()
    unresolved = sum(
        count
        for status_value, count in status_counts.items()
        if status_value != "failed"
    )
    retained_media = _media_file_count(stage_root)
    return {
        "total_rows": total,
        "status_counts": status_counts,
        "active_handoff_pins": active_pins,
        "unresolved_rows": unresolved,
        "retained_media_files": retained_media,
    }


def _database_observations(
    factory: sessionmaker[Session],
) -> tuple[dict[str, object], list[str]]:
    forbidden_terms = ("deploy", "external", "outbound", "publish", "send")
    with factory() as session:
        audit_counts = {
            str(event_type): int(count)
            for event_type, count in session.execute(
                select(AuditEvent.event_type, func.count(AuditEvent.id))
                .group_by(AuditEvent.event_type)
                .order_by(AuditEvent.event_type)
            )
        }
        job_status_counts = {
            f"{output_type}:{status_value}": int(count)
            for output_type, status_value, count in session.execute(
                select(
                    ImageOutputJob.output_type,
                    ImageOutputJob.status,
                    func.count(ImageOutputJob.id),
                )
                .group_by(ImageOutputJob.output_type, ImageOutputJob.status)
                .order_by(ImageOutputJob.output_type, ImageOutputJob.status)
            )
        }
        review_counts = {
            str(decision): int(count)
            for decision, count in session.execute(
                select(ImageReview.decision, func.count(ImageReview.id))
                .group_by(ImageReview.decision)
                .order_by(ImageReview.decision)
            )
        }
        association_count = int(
            session.scalar(select(func.count(ApprovedImageAssociation.id))) or 0
        )
    external_actions = sorted(
        event_type
        for event_type in audit_counts
        if any(term in event_type.lower() for term in forbidden_terms)
    )
    observations = {
        "audit_event_count": sum(audit_counts.values()),
        "audit_event_type_counts": audit_counts,
        "job_status_counts": job_status_counts,
        "review_decision_counts": review_counts,
        "approved_association_count": association_count,
    }
    return observations, external_actions


def _verify_storage_isolation(temp_root: Path) -> dict[str, object]:
    consumer_database = (temp_root / "consumer" / "bridge.db").resolve(strict=True)
    producer_store = (temp_root / "producer" / "batch-store").resolve(strict=True)
    if not consumer_database.is_file() or not producer_store.is_dir():
        raise AssertionError("two_repository_storage_missing")
    if (
        consumer_database == producer_store
        or producer_store in consumer_database.parents
        or consumer_database in producer_store.parents
    ):
        raise AssertionError("two_repository_storage_overlaps")
    consumer_identity = consumer_database.stat()
    for candidate in producer_store.rglob("*"):
        if not candidate.is_file() or candidate.suffix not in {
            ".db",
            ".sqlite",
            ".sqlite3",
        }:
            continue
        producer_identity = candidate.stat()
        if (producer_identity.st_dev, producer_identity.st_ino) == (
            consumer_identity.st_dev,
            consumer_identity.st_ino,
        ):
            raise AssertionError("two_repository_database_shared")
    return {
        "consumer_store": "temporary_sqlite",
        "producer_store": "temporary_local_batch_store",
        "shared_database": False,
    }


def _consumer_job_evidence(
    factory: sessionmaker[Session],
    client: TestClient,
    *,
    positive_job_ids: Sequence[UUID],
    wear_status: str,
) -> tuple[dict[str, object], dict[str, dict[str, object]]]:
    customer_id = _customer_id("test-customer-local")
    with factory() as session:
        jobs = tuple(
            session.scalars(
                select(ImageOutputJob)
                .where(
                    ImageOutputJob.customer_id == customer_id,
                    ImageOutputJob.id.in_(positive_job_ids),
                )
                .order_by(ImageOutputJob.output_type, ImageOutputJob.id)
            )
        )
        by_type: dict[str, list[ImageOutputJob]] = {}
        candidate_counts: list[int] = []
        for job in jobs:
            by_type.setdefault(job.output_type, []).append(job)
            candidate_count = session.scalar(
                select(func.count(ImageCandidate.id)).where(
                    ImageCandidate.customer_id == customer_id,
                    ImageCandidate.output_job_id == job.id,
                )
            )
            candidate_counts.append(int(candidate_count or 0))
            if candidate_count != 3:
                raise AssertionError("bridge_job_candidate_count_drift")
        type_counts = {key: len(value) for key, value in by_type.items()}
        if type_counts != {
            "multi_product": 1,
            "real_photo": 6,
            "scene": 1,
        }:
            raise AssertionError("bridge_job_routing_count_drift")
        generated = (*by_type["scene"], *by_type["multi_product"])
        if any(
            job.adapter_version != "deterministic-local-image-v1" for job in generated
        ):
            raise AssertionError("bridge_generation_adapter_drift")
        adapters = sorted({job.adapter_version for job in generated})
        # The scene candidate is exercised through the local review proxy below.
        # Use an untouched real-photo job for the independent restart/public API gate.
        scene_job = by_type["real_photo"][0]
        candidate = session.scalar(
            select(ImageCandidate)
            .where(
                ImageCandidate.customer_id == customer_id,
                ImageCandidate.output_job_id == scene_job.id,
            )
            .order_by(ImageCandidate.sequence_number)
        )
        if candidate is None:
            raise AssertionError("scene_candidate_missing")
        scene_job_id = scene_job.id
        candidate_id = candidate.id

    foreign = client.get(
        f"/v1/image-outputs/{scene_job_id}",
        headers={"X-Customer-Id": "99999999-9999-4999-8999-999999999999"},
    )
    if foreign.status_code != 404:
        raise AssertionError("cross_customer_job_visible")
    review = _expect(
        client.post(
            f"/v1/image-outputs/{scene_job_id}/candidates/{candidate_id}/review",
            headers={"X-Customer-Id": str(customer_id)},
            json={
                "decision": "approved",
                "reason": "synthetic local acceptance",
                "idempotency_key": "bridge-acceptance-review",
            },
        ),
        201,
        "local review approval",
    )
    if review["publishable"] is not False:
        raise AssertionError("local_review_became_publishable")
    routing = {
        "adapters": adapters,
        "candidate_counts": sorted(candidate_counts),
        "multi_product_jobs": type_counts.get("multi_product", 0),
        "real_photo_jobs": type_counts.get("real_photo", 0),
        "scene_jobs": type_counts.get("scene", 0),
        "wear_candidate_jobs": type_counts.get("wear", 0),
        "wear_status": wear_status,
    }
    gates = {
        "cross_customer_hidden": {
            "result": "PASS",
            "http_status": 404,
        },
        "review_approval_not_publishable": {
            "result": "PASS",
            "http_status": 201,
            "publishable": False,
        },
    }
    return routing, gates


def _candidate_review_loop(
    client: TestClient,
    factory: sessionmaker[Session],
    internals: Sequence[Mapping[str, object]],
    *,
    nonce: str,
    temp_root: Path,
    producer_root: Path,
) -> dict[str, object]:
    """Exercise real producer routes, client, consumer service, storage and DB."""
    media_reads = 0
    generator_calls = 0
    original_generate = DeterministicLocalImageGenerator.generate
    decisions: dict[str, tuple[str, dict[str, str], dict[str, Any]]] = {}

    def observed_generate(generator: Any, request: Any) -> Any:
        nonlocal generator_calls
        generator_calls += 1
        return original_generate(generator, request)

    def safe_json(response: Any, label: str) -> dict[str, Any]:
        body = _expect(response, 200, label)
        encoded = json.dumps(body, ensure_ascii=False)
        for forbidden in (
            nonce,
            str(temp_root),
            str(CONSUMER_ROOT),
            str(producer_root),
        ):
            if forbidden in encoded:
                raise AssertionError("review_response_private_value_leaked")

        def scan(value: Any) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    if key in FORBIDDEN_HANDOFF_FIELDS - {"url"} or "nonce" in key:
                        raise AssertionError("review_response_private_field_leaked")
                    if key == "publishable" and item is not False:
                        raise AssertionError("review_response_publishable")
                    if key == "synthetic_test_only" and item is not True:
                        raise AssertionError("review_response_not_synthetic")
                    if key == "content_url" and not str(item).startswith("/local/api/"):
                        raise AssertionError("review_response_nonlocal_media")
                    scan(item)
            elif isinstance(value, list):
                for item in value:
                    scan(item)

        scan(body)
        if (
            body.get("synthetic_test_only") is not True
            or body.get("publishable") is not False
        ):
            raise AssertionError("review_response_boundary_missing")
        return body

    def media(url: str, digest: str, mime: str) -> None:
        nonlocal media_reads
        response = client.get(url)
        if (
            response.status_code != 200
            or response.headers.get("content-type") != mime
            or response.headers.get("cache-control") != "no-store"
            or sha256(response.content).hexdigest() != digest
        ):
            raise AssertionError("review_media_content_mismatch")
        media_reads += 1

    def job(job_id: str) -> dict[str, Any]:
        result = safe_json(client.get(f"/local/api/style-jobs/{job_id}"), "review job")
        if len(result["candidates"]) != 3:
            raise AssertionError("review_candidate_count_drift")
        for candidate in result["candidates"]:
            media(
                candidate["content_url"],
                candidate["content_sha256"],
                candidate["mime_type"],
            )
        with factory() as session:
            for asset_id in result["source_asset_ids"]:
                asset = session.get(ProductAsset, UUID(asset_id))
                if asset is None:
                    raise AssertionError("review_source_asset_missing")
                media(
                    f"/local/api/style-jobs/{job_id}/assets/{asset_id}/image",
                    asset.content_sha256,
                    asset.mime_type,
                )
        return result

    def effects() -> tuple[dict[str, object], int, int]:
        with factory() as session:
            rounds = int(session.scalar(select(func.count(ImageReviewRound.id))) or 0)
        return _database_observations(factory)[0], rounds, generator_calls

    def decide(current: dict[str, Any], decision: str, key: str) -> dict[str, Any]:
        url = (
            f"/local/api/style-jobs/{current['job_id']}/candidates/"
            f"{current['candidates'][0]['candidate_id']}/review"
        )
        request = {
            "decision": decision,
            "reason": "synthetic acceptance",
            "idempotency_key": key,
        }
        result = safe_json(client.post(url, json=request), "review decision")
        decisions[key] = (url, request, result)
        after = effects()
        replay = safe_json(client.post(url, json=request), "review decision replay")
        if replay != result or effects() != after:
            raise AssertionError("review_replay_duplicate_effect")
        return result

    def delayed_replay(key: str, terminal_status: str) -> None:
        url, request, original = decisions[key]
        before = effects()
        replay = safe_json(client.post(url, json=request), "delayed review replay")
        historical = set(original) - {
            "current_job_id",
            "current_session_status",
            "current_regeneration_count",
        }
        if (
            {key: replay[key] for key in historical}
            != {key: original[key] for key in historical}
            or replay["current_session_status"] != terminal_status
            or before != effects()
        ):
            raise AssertionError("delayed_review_replay_drift")

    def initial(internal: Mapping[str, object], output_type: str) -> dict[str, Any]:
        with factory() as session:
            job_id = session.scalar(
                select(ImageOutputJob.id).where(
                    ImageOutputJob.id.in_(
                        [
                            UUID(str(value))
                            for value in cast(Sequence[str], internal["output_job_ids"])
                        ]
                    ),
                    ImageOutputJob.output_type == output_type,
                )
            )
        if job_id is None:
            raise AssertionError("review_initial_job_missing")
        return job(str(job_id))

    with patch.object(DeterministicLocalImageGenerator, "generate", observed_generate):
        current = initial(internals[0], "scene")
        source_ids = current["source_asset_ids"]
        for index, decision in enumerate(
            ("not_my_style", "product_detail_inaccurate", "not_my_style"), 1
        ):
            negative = decide(current, decision, f"approval-path-negative-{index}")
            if negative["regeneration_count"] != index or not negative["next_job_id"]:
                raise AssertionError("review_regeneration_drift")
            current = job(negative["next_job_id"])
            if current["source_asset_ids"] != source_ids:
                raise AssertionError("review_regeneration_source_changed")
        approved = decide(current, "approved", "approval-path-adopt")
        delayed_replay("approval-path-negative-1", "approved")
        if (
            approved["job_status"] != "customer_approved"
            or not approved["product_associations"]
        ):
            raise AssertionError("review_approval_missing_association")
        terminal = job(current["job_id"])
        if (
            terminal["approved_candidate_id"]
            != current["candidates"][0]["candidate_id"]
        ):
            raise AssertionError("review_approval_not_restored")
        views = {}
        for view in ("default", "real"):
            url = f"/local/api/style-products/test-product-scene/images?handoff_id={internals[0]['handoff_id']}&view={view}"
            value = safe_json(client.get(url), "product view")
            before = effects()
            if (
                safe_json(client.get(url), "product view replay") != value
                or effects() != before
            ):
                raise AssertionError("product_view_replay_mutated")
            for image in value["images"]:
                media(image["content_url"], image["content_sha256"], image["mime_type"])
            views[view] = value["resolved_view"]
        if views != {"default": "style_image", "real": "real_photo"}:
            raise AssertionError("review_dual_view_drift")

        current = initial(internals[1], "multi_product")
        for index in range(1, 4):
            negative = decide(current, "not_my_style", f"manual-path-negative-{index}")
            if negative["regeneration_count"] != index or not negative["next_job_id"]:
                raise AssertionError("manual_regeneration_drift")
            current = job(negative["next_job_id"])
        before = effects()
        manual = decide(current, "product_detail_inaccurate", "manual-path-negative-4")
        delayed_replay("manual-path-negative-1", "manual_attention_required")
        after = effects()
        if (
            manual["status"] != "manual_attention_required"
            or manual["next_job_id"] is not None
        ):
            raise AssertionError("manual_cutoff_missing")
        retained = job(current["job_id"])
        if (
            retained["status"] != "manual_attention_required"
            or retained["candidates"] != current["candidates"]
        ):
            raise AssertionError("manual_candidates_not_retained")

        def job_count(observation: tuple[dict[str, object], int, int]) -> int:
            return sum(
                cast(dict[str, int], observation[0]["job_status_counts"]).values()
            )

        jobs_on_fourth = job_count(after) - job_count(before)
        calls_on_fourth = after[2] - before[2]
        with factory() as session:
            round_count = int(
                session.scalar(
                    select(func.count(ImageReviewRound.id)).where(
                        ImageReviewRound.session_id == UUID(manual["session_id"])
                    )
                )
                or 0
            )
        if (
            jobs_on_fourth
            or calls_on_fourth
            or round_count != 4
            or generator_calls != 6
        ):
            raise AssertionError("manual_cutoff_created_extra_work")
    return {
        "candidate_count": len(terminal["candidates"]),
        "regeneration_count": approved["regeneration_count"],
        "final_status": approved["job_status"],
        "default_view": views["default"],
        "real_view": views["real"],
        "publishable": approved["publishable"],
        "synthetic_test_only": approved["synthetic_test_only"],
        "replay_effect_counts_unchanged": True,
        "media_hashes_verified": True,
        "verified_media_reads": media_reads,
        "generator_calls": generator_calls,
        "delayed_replay_after_approval": True,
        "delayed_replay_after_manual": True,
        "manual_cutoff": {
            "status": manual["status"],
            "regeneration_count": manual["regeneration_count"],
            "retained_candidate_count": len(retained["candidates"]),
            "jobs_created_on_fourth_rejection": jobs_on_fourth,
            "generator_calls_on_fourth_rejection": calls_on_fourth,
            "review_round_count": round_count,
        },
    }


def _review_edge_cases(
    client: TestClient,
    factory: sessionmaker[Session],
    *,
    nonce: str,
) -> dict[str, object]:
    """Real uploads exercise independent outputs and a failed generation handoff."""
    fixtures = (
        FixturePhoto(
            "studio-edge.png", (48, 48), (21, 31, 41), "real_photo", "real_photo"
        ),
        FixturePhoto(
            "scene-edge-one.png",
            (48, 48),
            (33, 99, 111),
            "scene_photo",
            "scene_photo",
            "scene_photo",
        ),
        FixturePhoto(
            "scene-edge-two.png",
            (48, 48),
            (38, 79, 91),
            "scene_photo",
            "scene_photo",
            "scene_photo",
        ),
    )
    _, multiple = _exercise_positive_product(
        client,
        alias="gate-multiple-scenes",
        product_id="test-product-multiple-scenes",
        fixtures=fixtures,
    )
    handoff = _expect(
        client.get(f"/local/api/style-handoffs/{multiple['handoff_id']}"),
        200,
        "multi-output handoff",
    )
    scene_ids = [
        item["job_id"]
        for item in handoff["handoff"]["output_jobs"]
        if item["output_type"] == "scene"
    ]
    if len(scene_ids) != 2:
        raise AssertionError("multi_output_reviewable_jobs_missing")
    sessions = set()
    approvals = 0
    for index, job_id in enumerate(scene_ids):
        opened = _expect(
            client.get(f"/local/api/style-jobs/{job_id}"), 200, "independent scene open"
        )
        sessions.add(opened["session_id"])
        if len(opened["candidates"]) != 3:
            raise AssertionError("independent_scene_candidates_missing")
        adopted = _expect(
            client.post(
                f"/local/api/style-jobs/{job_id}/candidates/{opened['candidates'][0]['candidate_id']}/review",
                json={
                    "decision": "approved",
                    "idempotency_key": f"independent-scene-{index}",
                },
            ),
            200,
            "independent scene adoption",
        )
        if (
            adopted["job_status"] != "customer_approved"
            or adopted["publishable"] is not False
        ):
            raise AssertionError("independent_scene_approval_missing")
        approvals += 1
    if len(sessions) != 2:
        raise AssertionError("independent_scene_sessions_collapsed")

    _, failed_batch = _exercise_positive_product(
        client,
        alias="gate-generation-failure",
        product_id="test-product-generation-failure",
        fixtures=fixtures[:2],
    )
    handoff = _expect(
        client.get(f"/local/api/style-handoffs/{failed_batch['handoff_id']}"),
        200,
        "failure handoff",
    )
    initial_id = next(
        item["job_id"]
        for item in handoff["handoff"]["output_jobs"]
        if item["output_type"] == "scene"
    )
    initial = _expect(
        client.get(f"/local/api/style-jobs/{initial_id}"), 200, "failure initial job"
    )
    calls = 0

    def fail_generation(generator: Any, request: Any) -> Any:
        nonlocal calls
        calls += 1
        raise ImageGenerationError("synthetic_acceptance_generation_failure")

    review_url = f"/local/api/style-jobs/{initial_id}/candidates/{initial['candidates'][0]['candidate_id']}/review"
    request = {
        "decision": "not_my_style",
        "idempotency_key": "generation-failure-intent",
    }
    with patch.object(DeterministicLocalImageGenerator, "generate", fail_generation):
        decision = _expect(
            client.post(review_url, json=request), 200, "durable generation failure"
        )
        before = _database_observations(factory)
        replay = _expect(
            client.post(review_url, json=request), 200, "generation failure replay"
        )
        if (
            decision != replay
            or before != _database_observations(factory)
            or calls != 1
        ):
            raise AssertionError("generation_failure_replay_created_work")
    failed = _expect(
        client.get(f"/local/api/style-jobs/{decision['current_job_id']}"),
        200,
        "failed job reload",
    )
    restored = _expect(
        client.get(f"/local/api/style-jobs/{initial_id}"),
        200,
        "failure initial cursor reload",
    )
    if (
        failed["status"] != "manual_attention_required"
        or failed["job_status"] != "generation_failed"
        or failed["candidates"]
        or restored["current_job_id"] != failed["job_id"]
        or decision["outcome"] != "manual_generation_unavailable"
    ):
        raise AssertionError("generation_failure_not_durable_manual")
    for payload in (initial, decision, failed, restored):
        if (
            payload["publishable"] is not False
            or payload["synthetic_test_only"] is not True
            or nonce in json.dumps(payload)
        ):
            raise AssertionError("generation_failure_boundary_invalid")

    # Pass only fields actually consumed by the UI to the Chromium replay test.
    # Stable aliases remove run-random IDs; no hand-written response replaces a
    # backend outcome, and no volatile candidate hash/path enters the manifest.
    aliases: dict[str, str] = {}

    def normalize(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: normalize(item) for key, item in value.items()}
        if isinstance(value, list):
            return [normalize(item) for item in value]
        if isinstance(value, str):
            try:
                UUID(value)
            except ValueError:
                return value
            return aliases.setdefault(
                value, str(uuid5(NAMESPACE_URL, f"review-failure-ui:{len(aliases)}"))
            )
        return value

    def project(payload: dict[str, Any]) -> dict[str, Any]:
        keys = (
            "job_id",
            "current_job_id",
            "handoff_id",
            "session_id",
            "output_type",
            "status",
            "job_status",
            "regeneration_count",
            "current_session_status",
            "current_regeneration_count",
            "source_asset_ids",
            "source_product_ids",
            "synthetic_test_only",
            "publishable",
            "next_job_id",
            "outcome",
            "approved_candidate_id",
            "approved_associations",
        )
        result = {key: payload[key] for key in keys if key in payload}
        if "candidates" in payload:
            result["candidates"] = [
                {
                    key: card[key]
                    for key in (
                        "candidate_id",
                        "sequence_number",
                        "synthetic_test_only",
                        "publishable",
                    )
                }
                for card in payload["candidates"]
            ]
        return cast(dict[str, Any], normalize(result))

    return {
        "independent_scene_sessions": len(sessions),
        "independent_scene_approvals": approvals,
        "generation_failure_status": failed["status"],
        "generation_failure_job_status": failed["job_status"],
        "generation_failure_candidate_count": len(failed["candidates"]),
        "generation_failure_generator_calls": calls,
        "generation_failure_replay_unchanged": True,
        "failure_ui_fixture": {
            "origin": "real_consumer_client_proxy_projection",
            "initial": project(initial),
            "decision": project(decision),
            "failed": project(failed),
            "restored": project(restored),
        },
    }


def _authenticity_failure_evidence(
    factory: sessionmaker[Session],
    client: TestClient,
    *,
    output_job_ids: Sequence[str],
    shared_context_observed: bool,
) -> dict[str, object]:
    customer_id = _customer_id("test-customer-local")
    parsed_ids = tuple(UUID(value) for value in output_job_ids)
    with factory() as session:
        jobs = tuple(
            session.scalars(
                select(ImageOutputJob)
                .where(
                    ImageOutputJob.customer_id == customer_id,
                    ImageOutputJob.id.in_(parsed_ids),
                )
                .order_by(ImageOutputJob.output_type, ImageOutputJob.id)
            )
        )
        failed = [job for job in jobs if job.status == "authenticity_failed"]
        if len(failed) != 1 or failed[0].output_type != "scene":
            raise AssertionError("bridge_authenticity_failure_job_drift")
        candidate = session.scalar(
            select(ImageCandidate)
            .where(
                ImageCandidate.customer_id == customer_id,
                ImageCandidate.output_job_id == failed[0].id,
            )
            .order_by(ImageCandidate.sequence_number)
        )
        if candidate is None:
            raise AssertionError("bridge_authenticity_failure_candidate_missing")
        fixture = candidate.artifact_metadata.get("local_authenticity_fixture")
        if (
            not isinstance(fixture, dict)
            or fixture.get("subject_complete") is not False
        ):
            raise AssertionError("bridge_authenticity_failure_fixture_drift")
        failed_job_id = failed[0].id
        candidate_id = candidate.id
        failed_status = failed[0].status

    review = client.post(
        f"/v1/image-outputs/{failed_job_id}/candidates/{candidate_id}/review",
        headers={"X-Customer-Id": str(customer_id)},
        json={
            "decision": "approved",
            "reason": "synthetic authenticity failure remains blocked",
            "idempotency_key": "bridge-authenticity-failure-review",
        },
    )
    body = _expect(review, 409, "authenticity failure review block")
    if body["error"]["code"] != "review_job_not_awaiting_customer":
        raise AssertionError("bridge_authenticity_failure_review_gate_drift")
    if not shared_context_observed:
        raise AssertionError("bridge_authenticity_failure_context_drift")
    return {
        "result": "PASS",
        "source": "producer_confirmed_handoff",
        "job_status": failed_status,
        "review_http_status": review.status_code,
        "review_error_code": body["error"]["code"],
        "same_app_database_store_transport": shared_context_observed,
    }


def _run_bridge_acceptance(
    temp_root: Path,
    producer_root: Path,
    *,
    consumer_head: str,
    producer_head: str,
) -> dict[str, Any]:
    if not isinstance(PREFLIGHT_EVIDENCE, dict):
        raise TypeError("repository_preflight_missing")
    _validate_repository_pins(
        producer_root,
        consumer_head=consumer_head,
        producer_head=producer_head,
    )
    producer = _copy_and_load_producer(producer_root, temp_root)
    _verify_contract_pin(producer)
    gates: dict[str, dict[str, object]] = {
        "bridge_default_off": _default_off_gate(producer, temp_root),
        "external_target_rejected_before_connect": _external_target_gate(producer),
    }

    nonce = secrets.token_urlsafe(32)
    consumer_app, factory, engine = _seed_consumer_app(temp_root, nonce)
    transport = RecordingASGITransport(app=consumer_app, nonce=nonce)
    bridge_client = producer.style_bridge_client(
        enabled=True,
        base_url="http://style-bridge.test",
        transport=transport,
        session_nonce=nonce,
    )
    producer_app = producer.create_local_app(
        temp_root / "producer" / "batch-store",
        vision_provider=producer.unavailable_vision_provider(),
        style_test_bridge_enabled=True,
        style_test_catalog=_catalog(),
        style_bridge_client=bridge_client,
    )
    product_traces: list[dict[str, object]] = []
    internals: list[dict[str, object]] = []
    stage_root = temp_root / "consumer" / "private-media"
    try:
        with ExitStack() as stack:
            consumer_client = stack.enter_context(
                TestClient(
                    consumer_app,
                    client=("127.0.0.1", 51000),
                )
            )
            producer_client = stack.enter_context(TestClient(producer_app))
            gates["ephemeral_nonce_required"] = _nonce_gate(consumer_client)

            positive_media_start = transport.media_count
            for alias, product_id, fixtures in _positive_specs():
                trace, internal = _exercise_positive_product(
                    producer_client,
                    alias=alias,
                    product_id=product_id,
                    fixtures=fixtures,
                )
                product_traces.append(trace)
                internals.append(internal)
            positive_staged_count = transport.media_count - positive_media_start
            expected_positive_staged = sum(
                int(trace["photo_count"]) for trace in product_traces
            )
            if positive_staged_count != expected_positive_staged:
                raise AssertionError("positive_media_staging_count_drift")

            conflict_request = {
                "customer_test_id": "test-customer-local",
                "product_id": "test-product-scene",
                "main_photo_id": internals[0]["alternate_main_photo_id"],
                "transform_permission": True,
            }
            producer_conflict = _expect(
                producer_client.post(
                    f"/local/api/upload-batches/{internals[0]['batch_id']}"
                    "/confirm-and-generate",
                    json=conflict_request,
                ),
                409,
                "producer changed-fact replay",
            )
            if producer_conflict["error_code"] != "style_handoff_fingerprint_conflict":
                raise AssertionError("producer_conflicting_replay_drift")

            positive_handoffs = list(transport.handoff_bodies)
            if len(positive_handoffs) != len(product_traces):
                raise AssertionError("positive_handoff_count_drift")
            first_handoff = json.loads(positive_handoffs[0])
            changed_handoff = deepcopy(first_handoff)
            changed_handoff["product_form"] = "ring"
            changed_model = ClassificationStyleHandoffV1.model_validate(changed_handoff)
            consumer_conflict = _expect(
                consumer_client.post(
                    "/v1/local-test/classification-bridge/handoffs",
                    content=canonical_handoff_bytes(changed_model),
                    headers={
                        "X-Local-Classification-Bridge-Nonce": nonce,
                    },
                ),
                409,
                "consumer conflicting replay",
            )
            if (
                consumer_conflict["error"]["code"]
                != "classification_handoff_idempotency_conflict"
            ):
                raise AssertionError("consumer_conflicting_replay_drift")
            gates["conflicting_replay_rejected"] = {
                "result": "PASS",
                "producer_http_status": 409,
                "consumer_http_status": 409,
            }

            gates.update(_exercise_producer_negative_gates(producer_client))
            gates["missing_style_pin_blocked"] = _exercise_missing_pin(producer_client)

            _, authenticity_internal = _exercise_positive_product(
                producer_client,
                alias="gate-authenticity-failure",
                product_id="test-product-authenticity-failure",
                fixtures=_authenticity_failure_spec(),
                expected_status="needs_confirmation",
            )

            stage = cast(
                LocalMediaStage,
                consumer_app.state.classification_bridge_stage,
            )
            gates["expired_receipt_rejected"] = _exercise_expired_receipt(
                consumer_client,
                stage,
                nonce,
            )
            positive_job_ids = tuple(
                UUID(value)
                for internal in internals
                for value in cast(Sequence[str], internal["output_job_ids"])
            )
            wear_status = str(product_traces[-1]["status"])
            authenticity_job_ids = tuple(
                str(value)
                for value in cast(
                    Sequence[str], authenticity_internal["output_job_ids"]
                )
            )
            shared_bridge_context_observed = (
                consumer_client.app is consumer_app
                and transport.forwarded_app is consumer_app
                and str(cast(Any, factory.kw["bind"]).url)
                == str(consumer_app.state.classification_bridge_database_engine.url)
                and consumer_app.state.classification_bridge_store is not None
            )

            review_loop = _candidate_review_loop(
                producer_client,
                factory,
                internals,
                nonce=nonce,
                temp_root=temp_root,
                producer_root=producer_root,
            )
            review_edges = _review_edge_cases(producer_client, factory, nonce=nonce)

            path_count, credential_fields = _verify_captured_handoffs(
                transport, producer, producer_root, temp_root
            )
            if path_count or credential_fields:
                raise AssertionError("handoff_boundary_scan_failed")
            if any(value != nonce for value in transport.nonce_headers):
                raise AssertionError("ephemeral_nonce_transport_drift")

            pending = stage.cleanup_expired(now=FUTURE_CLEANUP_TIME)
            if pending:
                raise AssertionError("main_receipt_cleanup_pending")
            retained_media = _media_file_count(stage_root)
            if retained_media:
                raise AssertionError("main_receipt_media_retained")
            receipt_ledger = _receipt_ledger_observation(stage_root)
            if (
                receipt_ledger["unresolved_rows"] != 0
                or receipt_ledger["retained_media_files"] != 0
                or receipt_ledger["active_handoff_pins"] != 0
            ):
                raise AssertionError("main_receipt_ledger_not_clean")
            nonce_persisted = _nonce_persisted(temp_root, nonce)
            if nonce_persisted:
                raise AssertionError("ephemeral_nonce_persisted")
            storage_boundary = _verify_storage_isolation(temp_root)
            all_nonce_authenticated = bool(transport.nonce_headers) and all(
                value == nonce for value in transport.nonce_headers
            )
            transport_observations = {
                "request_counts": dict(sorted(transport.request_counts.items())),
                "media_post_count": transport.media_count,
                "handoff_post_count": len(transport.handoff_bodies),
                "handoff_get_count": sum(
                    count
                    for key, count in transport.request_counts.items()
                    if key.startswith("GET ") and "/handoffs/" in key
                ),
            }

        restarted_app = _configured_consumer_app(temp_root, nonce)
        recovered_job_ids = {
            str(value)
            for value in restarted_app.state.classification_bridge_store.recovered_job_ids
        }
        expected_recovered = {
            *(str(value) for value in positive_job_ids),
            *authenticity_job_ids,
        }
        restart_recovered_artifacts = expected_recovered <= recovered_job_ids
        if not restart_recovered_artifacts:
            raise AssertionError("classification_bridge_restart_artifacts_missing")
        with TestClient(
            restarted_app,
            client=("127.0.0.1", 51001),
        ) as restarted_client:
            routing, consumer_gates = _consumer_job_evidence(
                factory,
                restarted_client,
                positive_job_ids=positive_job_ids,
                wear_status=wear_status,
            )
            gates.update(consumer_gates)
            gates["authenticity_failure_via_bridge"] = _authenticity_failure_evidence(
                factory,
                restarted_client,
                output_job_ids=authenticity_job_ids,
                shared_context_observed=shared_bridge_context_observed,
            )
        gates["restart_safe_public_review"] = {
            "result": "PASS",
            "recovered_artifacts": restart_recovered_artifacts,
            "review_http_status": consumer_gates["review_approval_not_publishable"][
                "http_status"
            ],
        }
        database_observations, external_actions = _database_observations(factory)
        if external_actions:
            raise AssertionError("external_action_audit_observed")
    finally:
        engine.dispose()

    cleanup_probe = _exercise_cleanup_uncertainty(temp_root)
    gates["cleanup_uncertainty_fails_closed"] = {
        "result": "PASS",
        **cleanup_probe,
    }

    OUTBOUND_GUARD.assert_clean()

    heic_available = Image.registered_extensions().get(".heic") == "HEIC"
    manifest: dict[str, Any] = {
        "schema_version": ACCEPTANCE_SCHEMA,
        "result": "PASS",
        "classification": "synthetic_test_only",
        "publishable": False,
        "production_eligible": False,
        "repository_pins": {
            "consumer_head": consumer_head,
            "producer_head": producer_head,
            "handoff_golden_sha256": GOLDEN_HANDOFF_SHA256,
        },
        "preflight": PREFLIGHT_EVIDENCE,
        "runtime": {
            "bridge_default": "disabled",
            "nonce": "ephemeral_in_memory_not_persisted",
            "positive_photo_count": sum(
                int(trace["photo_count"]) for trace in product_traces
            ),
            "positive_product_count": len(product_traces),
            "transport": "in_process_asgi_no_socket",
        },
        "products": product_traces,
        "routing": routing,
        "candidate_review_loop": review_loop,
        "review_edge_cases": review_edges,
        "gates": dict(sorted(gates.items())),
        "cleanup": {
            **cleanup_probe,
            "retained_media_files": receipt_ledger["retained_media_files"],
            "unresolved_receipts": receipt_ledger["unresolved_rows"],
        },
        "boundary": {
            **storage_boundary,
            "credential_fields_in_handoff": credential_fields,
            "external_actions": external_actions,
            "nonce_persisted": nonce_persisted,
            "source_paths_in_handoff": path_count,
        },
        "media_receipts": {
            "positive_staged_count": positive_staged_count,
            "all_requests_nonce_authenticated": all_nonce_authenticated,
            "ledger": receipt_ledger,
        },
        "database_observations": database_observations,
        "transport_observations": transport_observations,
        "format_support": {
            "jpeg": "decoded_and_tested",
            "png": "decoded_and_tested",
            "heic": (
                "decoder_available_decode_still_required"
                if heic_available
                else "conditional_local_decoder_not_installed"
            ),
            "video": "rejected",
        },
        "outbound_guard": OUTBOUND_GUARD.snapshot(),
        "file_side_effect_guard": FILE_SIDE_EFFECT_GUARD.snapshot(),
        "non_claims": [
            "not_deployed",
            "not_production_ai_classification",
            "not_real_image_model_output",
            "not_real_customer_authorization",
            "not_real_customer_trial",
            "not_published",
        ],
    }
    return manifest


def _failure_manifest(error: Exception) -> dict[str, object]:
    return {
        "schema_version": ACCEPTANCE_SCHEMA,
        "result": "FAIL",
        "classification": "synthetic_test_only",
        "publishable": False,
        "production_eligible": False,
        "error_type": type(error).__name__,
        "outbound_guard": OUTBOUND_GUARD.snapshot(),
        "file_side_effect_guard": FILE_SIDE_EFFECT_GUARD.snapshot(),
    }


def main() -> int:
    args = _parse_args()
    producer_root = args.producer_root.resolve(strict=True)
    if args.output is not None:
        FILE_SIDE_EFFECT_GUARD.allow_file(args.output, "explicit_output")
    try:
        with tempfile.TemporaryDirectory(
            prefix="classification-style-acceptance-"
        ) as temp_name:
            temp_root = Path(temp_name).resolve(strict=True)
            FILE_SIDE_EFFECT_GUARD.allow_root(
                temp_root, "classification_style_acceptance_temp_root"
            )
            FILE_SIDE_EFFECT_GUARD.activate()
            manifest = _run_bridge_acceptance(
                temp_root,
                producer_root,
                consumer_head=args.consumer_head,
                producer_head=args.producer_head,
            )
        OUTBOUND_GUARD.assert_clean()
    except Exception as error:  # noqa: BLE001 - acceptance must emit a FAIL receipt
        traceback.print_exc()
        manifest = _failure_manifest(error)
        exit_code = 1
    else:
        exit_code = 0
    _emit(manifest, args.output)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
