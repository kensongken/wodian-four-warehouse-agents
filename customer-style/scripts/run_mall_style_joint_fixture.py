"""Local-only synthetic HTTP receiver for a two-repository joint batch.

Not imported by production app. It calls no provider and binds only 127.0.0.1.
Its signed fixture-only seeder inserts zero-provider test candidates into real
models, leaving the normal result routes to validate tenant/authority/lineage.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import subprocess
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import uvicorn
from alembic.config import Config
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from PIL import Image
from pydantic import SecretStr
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from opc_style.api.routes_mall_style_handoff_v1 import _authenticate, router
from opc_style.domain.image_normalizer import NormalizedPayload
from opc_style.models import (
    Customer,
    ImageCandidate,
    ImageOutputJob,
    MallStyleGenerationQueueItem,
    SceneStyle,
    StyleCard,
    StyleCardSampleLink,
    StyleCardVersion,
    StyleSample,
    StyleTemplateSlot,
)
from opc_style.services.mall_style_results_v1 import MallStyleResultService
from opc_style.services.real_photos import LocalSyntheticAssetStore
from opc_style.settings import StyleWarehouseV3Settings

TEST_SIGNING_SECRET = "mall-style-joint-local-fixture-only"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--merchant-id", type=UUID, required=True)
    parser.add_argument("--opc-id", type=UUID, required=True)
    parser.add_argument("--port", type=int, default=8038)
    args = parser.parse_args()
    root = args.root.resolve()
    if not root.is_relative_to(Path("/private/tmp")) or root.exists():
        raise SystemExit(
            "Use a fresh root under /private/tmp; existing data is preserved."
        )
    root.mkdir(mode=0o700)
    repository = Path(__file__).resolve().parents[1]
    database_url = f"sqlite:///{root / 'fixture.db'}"
    config = Config(str(repository / "alembic.ini"))
    config.set_main_option("script_location", str(repository / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")
    engine = create_engine(database_url)
    sessions = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    store = LocalSyntheticAssetStore(root / "private-media")
    product = Image.new("RGBA", (120, 90), (0, 0, 0, 0))
    for y in range(20, 70):
        for x in range(30, 90):
            product.putpixel((x, y), (30, 140, 125, 255))
    product.save(root / "synthetic-product.png")
    mask = Image.new("L", product.size, 0)
    for y in range(20, 70):
        for x in range(30, 90):
            mask.putpixel((x, y), 255)
    mask.save(root / "synthetic-reviewed-mask.png")
    Image.new("RGB", (120, 90), (245, 240, 225)).save(root / "synthetic-label.png")
    sample_bytes = (root / "synthetic-label.png").read_bytes()
    with sessions() as session:
        customer = Customer(id=args.merchant_id, display_name="Synthetic joint fixture")
        sample = StyleSample(
            customer_id=args.merchant_id,
            photo_id=f"synthetic-joint-{uuid4()}",
            authorized_at=datetime.now(UTC),
            consent_provenance="synthetic-test-only",
            consent_text_version="synthetic-v1",
            content_sha256=hashlib.sha256(sample_bytes).hexdigest(),
            mime_type="image/png",
            width=120,
            height=90,
            asset_store_namespace=store.store_namespace,
        )
        card = StyleCard(customer_id=args.merchant_id, kind="scene", active=True)
        session.add_all([customer, sample, card])
        session.flush()
        scene = SceneStyle(
            id=card.id,
            customer_id=args.merchant_id,
            style_card_id=card.id,
            name="Synthetic joint style",
            style_origin="customer_upload",
            active=True,
            rules={},
        )
        version_id, slot_id = uuid4(), uuid4()
        rules = {"aspect_ratio": "1:1", "background": ["white"]}
        slot_snapshot = {
            "id": str(slot_id),
            "customer_id": str(args.merchant_id),
            "scene_style_id": str(scene.id),
            "style_card_version_id": str(version_id),
            "slot_key": "hero",
            "label": "主图",
            "rules": rules,
            "active": True,
            "position": 0,
        }
        version = StyleCardVersion(
            id=version_id,
            customer_id=args.merchant_id,
            style_card_id=card.id,
            version=1,
            content={
                "name": scene.name,
                "main_style_card_version_id": None,
                "historical_style": True,
                "style_origin": "customer_upload",
                "stability": "customer_confirmed",
                "slots": [slot_snapshot],
                "mall_default_slot_key": "hero",
            },
        )
        slot = StyleTemplateSlot(
            id=slot_id,
            customer_id=args.merchant_id,
            scene_style_id=scene.id,
            style_card_version_id=version_id,
            slot_key="hero",
            label="主图",
            rules=rules,
            active=True,
            position=0,
        )
        session.add_all([scene, version, slot])
        session.flush()
        session.add(
            StyleCardSampleLink(
                customer_id=args.merchant_id,
                style_card_version_id=version_id,
                style_sample_id=sample.id,
            )
        )
        session.commit()
        store.persist_style_sample_content(
            args.merchant_id, sample.id, sample_bytes, sample.content_sha256
        )
    source_sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repository, text=True
    ).strip()
    fixture = {
        "synthetic_test_only": True,
        "provider_calls": 0,
        "source_sha": source_sha,
        "batch_id": root.name,
        "base_url": f"http://127.0.0.1:{args.port}",
        "merchant_id": str(args.merchant_id),
        "opc_id": str(args.opc_id),
        "scene_style_id": str(scene.id),
        "style_card_version_id": str(version_id),
        "slot_key": "hero",
        "product_image": str(root / "synthetic-product.png"),
        "label_image": str(root / "synthetic-label.png"),
        "reviewed_mask": str(root / "synthetic-reviewed-mask.png"),
        "database": str(root / "fixture.db"),
        "candidate_estimate_usd": "0.120000",
    }
    (root / "fixture.json").write_text(json.dumps(fixture, indent=2) + "\n")
    app = FastAPI(title="LOCAL SYNTHETIC JOINT FIXTURE — no providers")
    app.state.style_warehouse_v3_session_factory = sessions
    app.state.style_warehouse_v3_settings = StyleWarehouseV3Settings(
        enabled=True, signing_secret=SecretStr(TEST_SIGNING_SECRET)
    )
    app.state.style_suite_planning_settings = SimpleNamespace(
        candidate_estimate_usd=Decimal("0.12")
    )
    app.state.mall_style_asset_store = store
    app.state.mall_style_subject_segmenter = None  # Exercise the honest manual path.
    app.include_router(router)

    @app.post("/_joint_fixture/receipts/{receipt_id}/seed-candidates")
    def seed_candidates(receipt_id: UUID, request: Request) -> JSONResponse:
        authenticated = _authenticate(request, b"")
        if isinstance(authenticated, JSONResponse):
            return authenticated
        merchant_id, opc_id = authenticated
        with sessions() as session:
            queue = session.scalar(
                select(MallStyleGenerationQueueItem).where(
                    MallStyleGenerationQueueItem.receipt_id == receipt_id,
                    MallStyleGenerationQueueItem.merchant_id == merchant_id,
                    MallStyleGenerationQueueItem.opc_id == opc_id,
                )
            )
            if queue is None:
                return JSONResponse(
                    status_code=404, content={"error_code": "queue_not_found"}
                )
            result = MallStyleResultService(session).by_receipt(
                merchant_id=merchant_id,
                opc_id=opc_id,
                product_id=queue.product_id,
                receipt_id=receipt_id,
            )
            if queue.generation_job_id is not None:
                return JSONResponse(status_code=200, content=jsonable_encoder(result))
            if result.status != "pending_execution":
                return JSONResponse(
                    status_code=409,
                    content={"error_code": "fixture_authority_not_current"},
                )
            metadata = {
                "publishable": False,
                "synthetic_test_only": True,
                "mall_generation_request_id": str(queue.id),
                "generation_approval_reference": queue.approval_reference,
                "rights_reference": queue.rights_reference,
                "provider": queue.provider,
                "model": queue.model,
                "source_product_ids": [queue.product_id],
                "source_asset_ids": [str(queue.product_asset_id)],
                "scene_style_id": str(queue.scene_style_id),
                "style_card_version_id": str(queue.style_card_version_id),
                "slot_key": queue.slot_key,
                "provider_calls": 0,
                "actual_cost_usd": "0",
            }
            job = ImageOutputJob(
                customer_id=merchant_id,
                output_type="product_style",
                style_card_version_id=queue.style_card_version_id,
                scene_style_id=queue.scene_style_id,
                subject_seal_id=queue.subject_seal_id,
                identity_lock_phase="candidates_attested",
                output_ratio="1:1",
                adapter_version="joint-zero-provider-v1",
                status="awaiting_customer_review",
                request_metadata=metadata,
                idempotency_key=f"synthetic-joint:{queue.id}",
            )
            session.add(job)
            session.flush()
            for sequence in range(1, queue.candidate_count + 1):
                output = io.BytesIO()
                Image.new("RGB", (120, 90), (80 + sequence, 100, 120)).save(
                    output, format="PNG"
                )
                content = output.getvalue()
                digest = hashlib.sha256(content).hexdigest()
                target = store.reserve_derivative(
                    merchant_id, job.id, sequence, extension="png"
                )
                artifact = store.create_derivative(
                    target,
                    NormalizedPayload(
                        content=content,
                        content_sha256=digest,
                        mime_type="image/png",
                        width=120,
                        height=90,
                        metadata=metadata,
                    ),
                )
                session.add(
                    ImageCandidate(
                        customer_id=merchant_id,
                        output_job_id=job.id,
                        sequence_number=sequence,
                        identity="synthetic_style_image_candidate",
                        artifact_path=str(artifact.output_path),
                        content_sha256=digest,
                        mime_type="image/png",
                        width=120,
                        height=90,
                        adapter_version="joint-zero-provider-v1",
                        artifact_metadata=metadata,
                    )
                )
            queue.generation_job_id = job.id
            queue.status = "dispatched"
            session.commit()
            result = MallStyleResultService(session).by_receipt(
                merchant_id=merchant_id,
                opc_id=opc_id,
                product_id=queue.product_id,
                receipt_id=receipt_id,
            )
            return JSONResponse(status_code=201, content=jsonable_encoder(result))

    print(f"fixture_file={root / 'fixture.json'}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
