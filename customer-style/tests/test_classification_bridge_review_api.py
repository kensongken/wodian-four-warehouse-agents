"""Real handoffs and stores exercise the local customer review boundary."""

from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from test_classification_bridge_api import (
    _active_style_pin,
    _enabled_client,
    _jpeg_bytes,
    _payload,
)
from test_classification_bridge_api import (
    api_session_factory as database_fixture,
)

from opc_style.contracts.classification_handoff import (
    ClassificationStyleHandoffV1,
    canonical_handoff_bytes,
)
from opc_style.models import ImageCandidate, ImageOutputJob, ImageReview, ProductAsset

BASE = "/v1/local-test/classification-bridge"
api_session_factory = database_fixture


def _accept(
    client,
    factory,
    *,
    customer="test-customer-local",
    product="test-product-001",
    version=3,
    create_pin=True,
    scene_count=1,
):
    if create_pin:
        _active_style_pin(factory, customer_test_id=customer)
    body = _jpeg_bytes()
    receipts = [
        client.post(
            f"{BASE}/media",
            content=body,
            headers={
                "X-Photo-Id": str(uuid4()),
                "X-Content-Sha256": sha256(body).hexdigest(),
            },
        ).json()
        for _ in range(1 + scene_count)
    ]
    payload = _payload(
        receipt_id=receipts[0]["receipt_id"], content_sha256=sha256(body).hexdigest()
    )
    payload.update(
        handoff_id=str(uuid4()),
        customer_test_id=customer,
        product_id=product,
        idempotency_key=f"style-test:{product}:v{version}",
        confirmation_version=version,
        main_photo_id=receipts[0]["photo_id"],
    )
    payload["photos"][0]["photo_id"] = receipts[0]["photo_id"]
    scene = deepcopy(payload["photos"][0])
    scene.update(
        photo_id=receipts[1]["photo_id"],
        media_receipt_id=receipts[1]["receipt_id"],
        automatic_category="scene_photo",
        confirmed_category="scene_photo",
    )
    payload["photos"].append(scene)
    for receipt in receipts[2:]:
        extra = deepcopy(scene)
        extra.update(
            photo_id=receipt["photo_id"], media_receipt_id=receipt["receipt_id"]
        )
        payload["photos"].append(extra)
    response = client.post(
        f"{BASE}/handoffs",
        content=canonical_handoff_bytes(
            ClassificationStyleHandoffV1.model_validate(payload)
        ),
    )
    assert response.status_code == 201, response.text
    ids = [UUID(value) for value in response.json()["output_job_ids"]]
    with factory() as session:
        job = (
            session.query(ImageOutputJob)
            .filter(ImageOutputJob.id.in_(ids), ImageOutputJob.output_type == "scene")
            .first()
        )
        candidate = (
            session.query(ImageCandidate)
            .filter_by(output_job_id=job.id, sequence_number=1)
            .one()
        )
        asset = (
            session.query(ProductAsset)
            .filter_by(
                customer_id=job.customer_id,
                product_id=product,
                identity="original_real_photo",
                photo_id=receipts[0]["photo_id"],
            )
            .one()
        )
        return {
            "handoff_id": payload["handoff_id"],
            "job_id": str(job.id),
            "candidate_id": str(candidate.id),
            "asset_id": str(asset.id),
            "customer_id": str(job.customer_id),
            "product_id": product,
            "artifact_path": candidate.artifact_path,
            "output_job_ids": [str(value) for value in ids],
        }


def test_multiple_scene_outputs_in_one_handoff_have_independent_review_sessions(
    monkeypatch,
    tmp_path,
    api_session_factory,
):
    with _enabled_client(monkeypatch, tmp_path, api_session_factory) as client:
        seeded = _accept(client, api_session_factory, scene_count=2)
        summaries = client.get(f"{BASE}/handoffs/{seeded['handoff_id']}").json()[
            "output_jobs"
        ]
        scene_ids = [
            item["job_id"] for item in summaries if item["output_type"] == "scene"
        ]
        assert len(scene_ids) == 2
        sessions = []
        for index, job_id in enumerate(scene_ids):
            opened = client.get(f"{BASE}/image-outputs/{job_id}")
            assert opened.status_code == 200, opened.text
            job = opened.json()
            sessions.append(job["session_id"])
            adopted = client.post(
                f"{BASE}/image-outputs/{job_id}/candidates/{job['candidates'][0]['candidate_id']}/review",
                json={
                    "decision": "approved",
                    "idempotency_key": f"independent-{index}",
                },
            )
            assert adopted.status_code == 201, adopted.text
            assert adopted.json()["job_status"] == "customer_approved"
        assert len(set(sessions)) == 2


@pytest.mark.parametrize("terminal", ["approved", "manual_attention_required"])
def test_delayed_negative_replay_separates_historical_outcome_from_current_session(
    review_bridge, terminal
):
    client, _, seeded = review_bridge
    initial = client.get(f"{BASE}/image-outputs/{seeded['job_id']}").json()
    url = f"{BASE}/image-outputs/{initial['job_id']}/candidates/{initial['candidates'][0]['candidate_id']}/review"
    request = {"decision": "not_my_style", "idempotency_key": "delayed-negative"}
    first = client.post(url, json=request).json()
    current_id = first["next_job_id"]
    for index in range(1, 5):
        current = client.get(f"{BASE}/image-outputs/{current_id}").json()
        result = client.post(
            f"{BASE}/image-outputs/{current_id}/candidates/{current['candidates'][0]['candidate_id']}/review",
            json={
                "decision": "approved" if terminal == "approved" else "not_my_style",
                "idempotency_key": f"delayed-terminal-{index}",
            },
        ).json()
        if result["status"] == terminal:
            break
        current_id = result["next_job_id"]
    replay = client.post(url, json=request)
    assert replay.status_code == 200, replay.text
    historical_fields = (
        "status",
        "regeneration_count",
        "next_job_id",
        "decision",
        "outcome",
    )
    assert {key: replay.json()[key] for key in historical_fields} == {
        key: first[key] for key in historical_fields
    }
    assert replay.json()["status"] == "awaiting_review"
    assert replay.json()["current_session_status"] == terminal
    assert replay.json()["current_job_id"] == current_id


def test_failed_regeneration_is_a_durable_manual_outcome_with_empty_job(
    review_bridge, monkeypatch
):
    from opc_style.domain.image_generator import (
        DeterministicLocalImageGenerator,
        ImageGenerationError,
    )

    client, _, seeded = review_bridge
    calls = []

    def fail(self, request):
        calls.append(request)
        raise ImageGenerationError("synthetic_failure")

    monkeypatch.setattr(DeterministicLocalImageGenerator, "generate", fail)
    url = f"{BASE}/image-outputs/{seeded['job_id']}/candidates/{seeded['candidate_id']}/review"
    request = {"decision": "not_my_style", "idempotency_key": "failed-generation"}
    response = client.post(url, json=request)
    assert response.status_code == 201, response.text
    result = response.json()
    assert result["outcome"] == "manual_generation_unavailable"
    assert result["status"] == "manual_attention_required"
    failed = client.get(f"{BASE}/image-outputs/{result['next_job_id']}")
    assert failed.status_code == 200
    assert failed.json()["job_status"] == "generation_failed"
    assert failed.json()["candidates"] == []
    assert client.post(url, json=request).json() == result
    assert len(calls) == 1


@pytest.fixture
def review_bridge(monkeypatch, tmp_path, api_session_factory):
    with _enabled_client(monkeypatch, tmp_path, api_session_factory) as client:
        yield client, api_session_factory, _accept(client, api_session_factory)


def test_bridge_lists_three_candidates_and_streams_owned_content(review_bridge):
    client, _, seeded = review_bridge
    response = client.get(f"{BASE}/image-outputs/{seeded['job_id']}")
    assert response.status_code == 200
    data = response.json()
    assert data["job_id"] == seeded["job_id"]
    assert len(data["candidates"]) == 3
    assert data["session_id"] and data["regeneration_count"] == 0
    assert data["publishable"] is False
    assert "artifact_path" not in response.text
    assert seeded["artifact_path"] not in response.text
    for candidate in data["candidates"]:
        image = client.get(candidate["content_url"])
        assert image.status_code == 200
        assert image.headers["content-type"] == candidate["mime_type"]
        assert image.headers["cache-control"] == "no-store"
        assert sha256(image.content).hexdigest() == candidate["content_sha256"]
    real = client.get(
        f"{BASE}/assets/{seeded['asset_id']}/content",
        params={"job_id": seeded["job_id"]},
    )
    assert real.status_code == 200
    assert real.content == _jpeg_bytes()


def test_handoff_identifies_normalized_real_and_reviewable_scene_outputs(review_bridge):
    client, factory, seeded = review_bridge
    response = client.get(f"{BASE}/handoffs/{seeded['handoff_id']}")
    assert response.status_code == 200
    payload = response.json()
    with factory() as session:
        jobs = [
            session.get(ImageOutputJob, UUID(job_id))
            for job_id in payload["output_job_ids"]
        ]
        assert payload["output_jobs"] == [
            {
                "job_id": str(job.id),
                "output_type": job.output_type,
                "status": job.status,
            }
            for job in jobs
        ]
    assert {item["output_type"] for item in payload["output_jobs"]} == {
        "real_photo",
        "scene",
    }


def test_job_reload_restores_its_own_adopted_candidate_and_durable_associations(
    review_bridge,
):
    client, factory, seeded = review_bridge
    first = client.get(f"{BASE}/image-outputs/{seeded['job_id']}").json()
    candidate_id = first["candidates"][1]["candidate_id"]
    approved = client.post(
        f"{BASE}/image-outputs/{seeded['job_id']}/candidates/{candidate_id}/review",
        json={"decision": "approved", "idempotency_key": "restore-own-approval"},
    )
    assert approved.status_code == 201
    # A later handoff for the same product must not change this job's adopted card.
    newer = _accept(
        client, factory, product=seeded["product_id"], version=4, create_pin=False
    )
    later = client.post(
        f"{BASE}/image-outputs/{newer['job_id']}/candidates/{newer['candidate_id']}/review",
        json={"decision": "approved", "idempotency_key": "restore-later-approval"},
    )
    assert later.status_code == 201
    restored = client.get(f"{BASE}/image-outputs/{seeded['job_id']}").json()
    assert restored["approved_candidate_id"] == candidate_id
    assert restored["approved_associations"] == [
        {
            "product_id": seeded["product_id"],
            "candidate_id": candidate_id,
            "asset_id": seeded["asset_id"],
        }
    ]


@pytest.mark.parametrize("kind", ["job", "candidate", "asset", "review", "product"])
def test_every_review_route_requires_nonce_and_loopback(review_bridge, kind):
    client, _, seeded = review_bridge
    paths = {
        "job": f"/image-outputs/{seeded['job_id']}",
        "candidate": f"/image-outputs/{seeded['job_id']}/candidates/{seeded['candidate_id']}/content",
        "asset": f"/assets/{seeded['asset_id']}/content?job_id={seeded['job_id']}",
        "review": f"/image-outputs/{seeded['job_id']}/candidates/{seeded['candidate_id']}/review",
        "product": f"/products/{seeded['product_id']}/images?handoff_id={seeded['handoff_id']}",
    }
    method = client.post if kind == "review" else client.get
    kwargs = (
        {"json": {"decision": "approved", "idempotency_key": "gated"}}
        if kind == "review"
        else {}
    )
    client.headers.pop("X-Local-Classification-Bridge-Nonce")
    assert method(BASE + paths[kind], **kwargs).status_code == 401
    assert (
        method(
            BASE + paths[kind],
            headers={"X-Local-Classification-Bridge-Nonce": "wrong"},
            **kwargs,
        ).status_code
        == 403
    )
    client.headers["X-Local-Classification-Bridge-Nonce"] = "test-bridge-nonce"
    client._transport.client = ("192.0.2.1", 51000)
    assert method(BASE + paths[kind], **kwargs).status_code == 403


def test_two_accepted_customers_cannot_mix_candidate_asset_or_product(review_bridge):
    client, factory, first = review_bridge
    second = _accept(
        client, factory, customer="test-customer-other", product="test-product-002"
    )
    # Caller identity cannot override the trusted job's accepted provenance.
    client.headers["X-Customer-Id"] = second["customer_id"]
    assert client.get(f"{BASE}/image-outputs/{first['job_id']}").status_code == 200
    assert client.get(f"{BASE}/image-outputs/{second['job_id']}").status_code == 200
    assert (
        client.get(
            f"{BASE}/image-outputs/{first['job_id']}/candidates/{second['candidate_id']}/content"
        ).status_code
        == 404
    )
    assert (
        client.get(
            f"{BASE}/assets/{second['asset_id']}/content?job_id={first['job_id']}"
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"{BASE}/image-outputs/{first['job_id']}/candidates/{second['candidate_id']}/review",
            json={"decision": "approved", "idempotency_key": "foreign"},
        ).status_code
        == 404
    )
    assert (
        client.get(
            f"{BASE}/products/{second['product_id']}/images?handoff_id={first['handoff_id']}"
        ).status_code
        == 404
    )
    assert (
        client.get(f"{BASE}/products/{first['product_id']}/images").status_code == 422
    )
    with factory() as session:
        assert session.query(ImageReview).count() == 0


def test_rejection_replay_advances_once_then_approval_exposes_dual_images(
    review_bridge,
):
    client, _, seeded = review_bridge
    path = f"{BASE}/image-outputs/{seeded['job_id']}/candidates/{seeded['candidate_id']}/review"
    body = {
        "decision": "not_my_style",
        "reason": "lighter please",
        "idempotency_key": "round-1",
    }
    response = client.post(path, json=body)
    assert response.status_code == 201
    data = response.json()
    assert "suite_state" not in data
    assert data["status"] == "awaiting_review"
    assert data["regeneration_count"] == 1
    assert data["next_job_id"] != seeded["job_id"]
    assert data["product_associations"] == []
    assert data["publishable"] is False
    replay = client.post(path, json=body)
    assert replay.status_code == 200
    assert replay.json() == data
    next_job = client.get(f"{BASE}/image-outputs/{data['next_job_id']}")
    assert next_job.status_code == 200
    candidate = next_job.json()["candidates"][0]
    approved = client.post(
        f"{BASE}/image-outputs/{data['next_job_id']}/candidates/{candidate['candidate_id']}/review",
        json={"decision": "approved", "idempotency_key": "round-2"},
    )
    assert approved.status_code == 201
    approved_data = approved.json()
    assert approved_data["status"] == "approved"
    assert approved_data["next_job_id"] is None
    assert (
        approved_data["product_associations"][0]["product_id"] == seeded["product_id"]
    )
    assert approved_data["publishable"] is False
    for view, expected in [("default", "style_image"), ("real", "real_photo")]:
        response = client.get(
            f"{BASE}/products/{seeded['product_id']}/images",
            params={"handoff_id": seeded["handoff_id"], "view": view},
        )
        assert response.status_code == 200
        assert response.json()["resolved_view"] == expected
        assert response.json()["publishable"] is False
        assert (
            client.get(response.json()["images"][0]["content_url"]).status_code == 200
        )


def test_fourth_rejection_retains_terminal_media_with_ownership_and_hash_checks(
    review_bridge,
):
    client, factory, seeded = review_bridge
    job_id = seeded["job_id"]
    earlier_urls = []
    terminal_before = {}
    for round_number in range(1, 5):
        payload = client.get(f"{BASE}/image-outputs/{job_id}").json()
        assert len(payload["candidates"]) == 3
        for candidate in payload["candidates"]:
            image = client.get(candidate["content_url"])
            assert image.status_code == 200
            if round_number == 4:
                terminal_before[candidate["candidate_id"]] = image.content
            else:
                earlier_urls.append(candidate["content_url"])
        path = f"{BASE}/image-outputs/{job_id}/candidates/{payload['candidates'][0]['candidate_id']}/review"
        body = {
            "decision": "not_my_style",
            "idempotency_key": f"terminal-media-{round_number}",
        }
        response = client.post(path, json=body)
        assert response.status_code == 201, response.text
        if round_number < 4:
            job_id = response.json()["next_job_id"]
        else:
            assert response.json()["status"] == "manual_attention_required"
            assert response.json()["next_job_id"] is None
            replay = client.post(path, json=body)
            assert replay.status_code == 200
            assert replay.json() == response.json()
    terminal = client.get(f"{BASE}/image-outputs/{job_id}").json()
    assert terminal["status"] == "manual_attention_required"
    assert terminal["job_status"] == "customer_rejected"
    for candidate in terminal["candidates"]:
        image = client.get(candidate["content_url"])
        assert image.status_code == 200, image.text
        assert image.content == terminal_before[candidate["candidate_id"]]
        assert sha256(image.content).hexdigest() == candidate["content_sha256"]
        assert image.headers["content-type"] == candidate["mime_type"]
    for earlier_url in earlier_urls:
        assert client.get(earlier_url).status_code == 503
    # Session retention does not authorize another job's candidate or asset.
    assert (
        client.get(
            f"{BASE}/image-outputs/{job_id}/candidates/{seeded['candidate_id']}/content"
        ).status_code
        == 404
    )
    foreign = _accept(
        client,
        factory,
        customer="test-customer-terminal-other",
        product="test-product-terminal-other",
        version=3,
    )
    assert (
        client.get(
            f"{BASE}/assets/{foreign['asset_id']}/content?job_id={job_id}"
        ).status_code
        == 404
    )
    # Retention is not a substitute for re-reading and hashing the actual bytes.
    with factory() as session:
        candidate = session.get(
            ImageCandidate, UUID(terminal["candidates"][1]["candidate_id"])
        )
        candidate_path = Path(candidate.artifact_path)
    candidate_path.write_bytes(b"changed terminal image")
    corrupted = client.get(terminal["candidates"][1]["content_url"])
    assert corrupted.status_code == 503
    assert str(candidate_path) not in corrupted.text


def test_unaccepted_job_and_missing_ids_are_hidden(review_bridge):
    client, factory, seeded = review_bridge
    from opc_style.models import AuditEvent

    with factory() as session:
        session.query(AuditEvent).filter_by(
            event_type="classification_bridge.accepted"
        ).delete()
        session.commit()
    assert client.get(f"{BASE}/image-outputs/{seeded['job_id']}").status_code == 404
    assert client.get(f"{BASE}/image-outputs/{uuid4()}").status_code == 404


def test_candidate_tamper_fails_closed_without_path_leak(review_bridge):
    client, _, seeded = review_bridge
    Path(seeded["artifact_path"]).write_bytes(b"tampered")
    response = client.get(
        f"{BASE}/image-outputs/{seeded['job_id']}/candidates/{seeded['candidate_id']}/content"
    )
    assert response.status_code == 503
    assert seeded["artifact_path"] not in response.text


def test_candidate_content_rejects_symlink_even_for_matching_bytes(
    review_bridge, tmp_path
):
    client, _, seeded = review_bridge
    candidate_path = Path(seeded["artifact_path"])
    expected = candidate_path.read_bytes()
    path = f"{BASE}/image-outputs/{seeded['job_id']}/candidates/{seeded['candidate_id']}/content"
    assert client.get(path).content == expected
    duplicate = tmp_path / "same-content.jpg"
    duplicate.write_bytes(expected)
    candidate_path.unlink()
    candidate_path.symlink_to(duplicate)
    assert client.get(path).status_code == 503


def test_product_real_view_returns_only_this_handoffs_originals(review_bridge):
    client, factory, first = review_bridge
    _accept(client, factory, version=4, create_pin=False)
    response = client.get(
        f"{BASE}/products/{first['product_id']}/images?handoff_id={first['handoff_id']}&view=real"
    )
    assert response.status_code == 200
    assert [item["asset_id"] for item in response.json()["images"]] == [
        first["asset_id"]
    ]
    for item in response.json()["images"]:
        assert client.get(item["content_url"]).status_code == 200


@pytest.mark.parametrize("regenerate", [False, True])
def test_product_default_never_borrows_another_handoffs_approved_candidate(
    review_bridge, regenerate
):
    client, factory, first = review_bridge
    second = _accept(client, factory, version=4, create_pin=False)
    job_id, candidate_id = second["job_id"], second["candidate_id"]
    if regenerate:
        rejected = client.post(
            f"{BASE}/image-outputs/{job_id}/candidates/{candidate_id}/review",
            json={"decision": "not_my_style", "idempotency_key": "handoff-b-redo"},
        )
        assert rejected.status_code == 201
        job_id = rejected.json()["next_job_id"]
        candidate_id = client.get(f"{BASE}/image-outputs/{job_id}").json()[
            "candidates"
        ][0]["candidate_id"]
    approved = client.post(
        f"{BASE}/image-outputs/{job_id}/candidates/{candidate_id}/review",
        json={"decision": "approved", "idempotency_key": "handoff-b-approved"},
    )
    assert approved.status_code == 201
    first_view = client.get(
        f"{BASE}/products/{first['product_id']}/images",
        params={"handoff_id": first["handoff_id"], "view": "default"},
    )
    assert first_view.status_code == 200
    assert first_view.json()["resolved_view"] == "real_photo"
    assert [item["asset_id"] for item in first_view.json()["images"]] == [
        first["asset_id"]
    ]
    assert candidate_id not in first_view.text
    assert (
        client.get(first_view.json()["images"][0]["content_url"]).content
        == _jpeg_bytes()
    )
    second_view = client.get(
        f"{BASE}/products/{second['product_id']}/images",
        params={"handoff_id": second["handoff_id"], "view": "default"},
    )
    assert second_view.status_code == 200
    assert second_view.json()["resolved_view"] == "style_image"
    assert [item["candidate_id"] for item in second_view.json()["images"]] == [
        candidate_id
    ]
    assert client.get(second_view.json()["images"][0]["content_url"]).status_code == 200


def test_real_content_tamper_and_foreign_request_fields_fail_closed(review_bridge):
    client, _, seeded = review_bridge
    store = client.app.state.classification_bridge_store
    original = store.resolve_original(
        UUID(seeded["customer_id"]), UUID(seeded["asset_id"])
    )
    original.write_bytes(b"private replacement")
    response = client.get(
        f"{BASE}/assets/{seeded['asset_id']}/content?job_id={seeded['job_id']}"
    )
    assert response.status_code == 503
    assert str(original) not in response.text
    response = client.post(
        f"{BASE}/image-outputs/{seeded['job_id']}/candidates/{seeded['candidate_id']}/review",
        json={
            "decision": "approved",
            "idempotency_key": "forged-customer",
            "customer_id": str(uuid4()),
            "publishable": True,
        },
    )
    assert response.status_code == 422


def test_same_product_ids_stay_scoped_to_each_accepted_handoff(review_bridge):
    client, factory, first = review_bridge
    second = _accept(
        client, factory, customer="test-customer-other", product=first["product_id"]
    )
    for item in (first, second):
        response = client.get(
            f"{BASE}/products/{item['product_id']}/images?handoff_id={item['handoff_id']}"
        )
        assert response.status_code == 200
        assert [image["asset_id"] for image in response.json()["images"]] == [
            item["asset_id"]
        ]


def test_direct_service_reads_real_store_and_bound_job_still_requires_accepted_provenance(
    review_bridge,
):
    from opc_style.domain.image_generator import DeterministicLocalImageGenerator
    from opc_style.models import AuditEvent
    from opc_style.services.image_reviews import ImageReviewNotFoundError
    from opc_style.services.review_sessions import ReviewSessionService

    client, factory, seeded = review_bridge
    with factory() as session:
        service = ReviewSessionService(
            session,
            client.app.state.classification_bridge_store,
            DeterministicLocalImageGenerator(),
        )
        output = service.get_job(UUID(seeded["job_id"]))
        assert output.job.customer_id == UUID(seeded["customer_id"])
        content, mime = service.read_candidate(
            output.job.id, UUID(seeded["candidate_id"])
        )
        assert sha256(content).hexdigest() == output.candidates[0].content_sha256
        assert mime == "image/jpeg"
        assert service.read_real_asset(output.job.id, UUID(seeded["asset_id"])) == (
            _jpeg_bytes(),
            "image/jpeg",
        )
        session.query(AuditEvent).filter_by(
            event_type="classification_bridge.accepted"
        ).delete()
        session.commit()
        with pytest.raises(ImageReviewNotFoundError):
            service.get_job(output.job.id)


def test_disabled_bridge_has_no_review_routes(monkeypatch):
    from fastapi.testclient import TestClient

    from opc_style.main import create_app

    monkeypatch.delenv("LOCAL_CLASSIFICATION_BRIDGE", raising=False)
    with TestClient(create_app()) as client:
        assert client.get(f"{BASE}/image-outputs/{uuid4()}").status_code == 404
