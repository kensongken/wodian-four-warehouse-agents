import json
from datetime import UTC, datetime
from io import BytesIO
from uuid import UUID

from fastapi.testclient import TestClient
from PIL import Image

from product_engine.api.local_app import create_local_app
from product_engine.services.album_review_inbox import (
    AlbumReviewImage,
    AlbumReviewImageContent,
    AlbumReviewInboxFailure,
    AlbumReviewProduct,
)

TENANT_ID = UUID("22222222-2222-4222-8222-222222222222")
ASSET_ID = UUID("44444444-4444-4444-8444-444444444444")
DETAIL_ASSET_ID = UUID("55555555-5555-4555-8555-555555555555")
HANDOFF_ID = UUID("77777777-7777-4777-8777-777777777777")
RECEIPT_ID = UUID("88888888-8888-4888-8888-888888888888")


class Inbox:
    async def list_latest(self, tenant_id: UUID):
        assert tenant_id == TENANT_ID
        return [
            AlbumReviewProduct(
                product_code="SYNTHETIC-001",
                category="绿松石",
                form="手串",
                package_version=2,
                received_at=datetime(2026, 9, 7, 8, 30, tzinfo=UTC),
                sales_strategy_status="preview_only",
                sales_strategy_version=3,
                sales_delivery_status="delivered",
                sales_delivered_at=datetime(2026, 9, 7, 8, 31, tzinfo=UTC),
                sales_handoff_id=HANDOFF_ID,
                sales_handoff_status="accepted",
                sales_receiver_receipt_id=RECEIPT_ID,
                images=(
                    AlbumReviewImage(
                        asset_id=ASSET_ID,
                        mime_type="image/png",
                        role="main_candidate",
                    ),
                ),
            )
        ]

    async def read_image(
        self,
        tenant_id: UUID,
        product_code: str,
        package_version: int,
        asset_id: UUID,
    ) -> AlbumReviewImageContent:
        if (tenant_id, product_code, package_version, asset_id) != (
            TENANT_ID,
            "SYNTHETIC-001",
            2,
            ASSET_ID,
        ):
            raise AlbumReviewInboxFailure("album_image_not_found")
        return AlbumReviewImageContent(b"synthetic-image", "image/png")


class ReviewInbox(Inbox):
    async def list_latest(self, tenant_id: UUID):
        assert tenant_id == TENANT_ID
        return [
            AlbumReviewProduct(
                product_code="SYNTHETIC-MULTI-001",
                category="南红",
                form="手串",
                package_version=3,
                received_at=datetime(2026, 9, 8, 1, 0, tzinfo=UTC),
                sales_strategy_status="preview_only",
                sales_strategy_version=3,
                images=(
                    AlbumReviewImage(
                        asset_id=ASSET_ID,
                        mime_type="image/jpeg",
                        role="main_candidate",
                    ),
                    AlbumReviewImage(
                        asset_id=DETAIL_ASSET_ID,
                        mime_type="image/jpeg",
                        role="detail",
                    ),
                ),
            )
        ]

    async def read_image(
        self,
        tenant_id: UUID,
        product_code: str,
        package_version: int,
        asset_id: UUID,
    ) -> AlbumReviewImageContent:
        assert (tenant_id, product_code, package_version) == (
            TENANT_ID,
            "SYNTHETIC-MULTI-001",
            3,
        )
        assert asset_id in {ASSET_ID, DETAIL_ASSET_ID}
        content = BytesIO()
        Image.new("RGB", (320, 320), (176, 42, 42)).save(content, format="JPEG")
        return AlbumReviewImageContent(content.getvalue(), "image/jpeg")


def test_unconfigured_page_reports_safe_empty_inbox(tmp_path) -> None:
    client = TestClient(create_local_app(tmp_path))

    response = client.get("/local/api/album-review-inbox")

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "status": "unconfigured",
        "message_zh": "当前页面尚未连接正式相册收件箱。",
        "products": [],
        "summary": {
            "image_count": 0,
            "latest_received_at": None,
            "product_count": 0,
        },
    }


def test_runtime_builder_keeps_local_page_available_without_opt_in(monkeypatch) -> None:
    from product_engine.api.local_app import build_runtime_album_review_inbox

    monkeypatch.delenv("LOCAL_ALBUM_SYNC_TENANT_ID", raising=False)

    assert build_runtime_album_review_inbox() == (None, None)


def test_connected_inbox_returns_safe_metadata_and_same_origin_image_path(tmp_path) -> None:
    client = TestClient(
        create_local_app(
            tmp_path,
            album_review_inbox=Inbox(),
            album_sync_tenant_id=TENANT_ID,
        )
    )

    response = client.get("/local/api/album-review-inbox")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "connected"
    assert payload["summary"] == {
        "image_count": 1,
        "latest_received_at": "2026-09-07T08:30:00Z",
        "product_count": 1,
    }
    assert payload["products"][0]["product_code"] == "SYNTHETIC-001"
    assert payload["products"][0]["sales_strategy_status"] == "preview_only"
    assert payload["products"][0]["sales_strategy_version"] == 3
    assert payload["products"][0]["sales_delivery_status"] == "delivered"
    assert payload["products"][0]["sales_delivered_at"] == "2026-09-07T08:31:00Z"
    assert payload["products"][0]["sales_handoff_id"] == str(HANDOFF_ID)
    assert payload["products"][0]["sales_handoff_status"] == "accepted"
    assert payload["products"][0]["sales_receiver_receipt_id"] == str(RECEIPT_ID)
    assert payload["products"][0]["classification_source"] == "album_candidate"
    assert payload["products"][0]["classification_id"] is None
    assert payload["products"][0]["classification_version"] is None
    image = payload["products"][0]["images"][0]
    assert image["image_url"] == (
        f"/local/api/album-review-inbox/SYNTHETIC-001/versions/2/assets/{ASSET_ID}/image"
    )
    assert "object_key" not in str(payload)
    assert "sha256" not in str(payload)


def test_image_proxy_returns_exact_bytes_and_tenant_neutral_missing_error(tmp_path) -> None:
    client = TestClient(
        create_local_app(
            tmp_path,
            album_review_inbox=Inbox(),
            album_sync_tenant_id=TENANT_ID,
        )
    )
    valid_path = f"/local/api/album-review-inbox/SYNTHETIC-001/versions/2/assets/{ASSET_ID}/image"

    valid = client.get(valid_path)
    missing = client.get(valid_path.replace("SYNTHETIC-001", "UNKNOWN"))

    assert (valid.status_code, valid.content, valid.headers["content-type"]) == (
        200,
        b"synthetic-image",
        "image/png",
    )
    assert missing.status_code == 404
    assert missing.json() == {
        "ok": False,
        "error_code": "album_image_not_found",
        "message_zh": "这张相册图片当前不可读取。",
        "next_step_zh": "请回到相册整理确认图片仍然可用后重新同步。",
    }


def test_open_album_product_creates_one_review_batch_with_all_images(tmp_path) -> None:
    client = TestClient(
        create_local_app(
            tmp_path,
            album_review_inbox=ReviewInbox(),
            album_sync_tenant_id=TENANT_ID,
            style_test_bridge_enabled=True,
        )
    )

    response = client.post(
        "/local/api/album-review-inbox/SYNTHETIC-MULTI-001/versions/3/open-review"
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["replayed"] is False
    assert payload["batch"]["expected_count"] == 2
    assert payload["batch"]["product_count"] == 1
    assert [item["商品编码"] for item in payload["batch"]["items"]] == [
        "SYNTHETIC-MULTI-001",
        "SYNTHETIC-MULTI-001",
    ]
    assert [item["source_system"] for item in payload["batch"]["items"]] == [
        "album_organizer",
        "album_organizer",
    ]
    assert [item["图片角色"] for item in payload["batch"]["items"]] == ["主图", "附图"]
    assert [item["initial_result"]["material_label"] for item in payload["batch"]["items"]] == [
        "南红",
        "南红",
    ]
    assert [item["initial_result"]["product_form"] for item in payload["batch"]["items"]] == [
        "手串",
        "手串",
    ]
    assert [
        item["photo_classification_facts"][0]["final_category"]
        for item in payload["batch"]["items"]
    ] == ["real_photo", "real_photo"]
    readiness = client.get(
        f"/local/api/upload-batches/{payload['batch']['batch_id']}/style-readiness"
    )
    assert readiness.status_code == 200
    assert "photo_classification_required" not in readiness.json()["missing_codes"]
    assert "real_photo_required" not in readiness.json()["missing_codes"]


def test_open_album_product_normalizes_chinese_unknown_form(tmp_path) -> None:
    class UnknownFormReviewInbox(ReviewInbox):
        async def list_latest(self, tenant_id: UUID):
            products = await super().list_latest(tenant_id)
            return [products[0].model_copy(update={"form": "未知"})]

    client = TestClient(
        create_local_app(
            tmp_path,
            album_review_inbox=UnknownFormReviewInbox(),
            album_sync_tenant_id=TENANT_ID,
        )
    )

    response = client.post(
        "/local/api/album-review-inbox/SYNTHETIC-MULTI-001/versions/3/open-review"
    )

    assert response.status_code == 201
    assert {
        item["initial_result"]["product_form"] for item in response.json()["batch"]["items"]
    } == {"unknown"}


def test_open_album_product_keeps_review_available_when_source_image_cannot_be_classified(
    tmp_path,
) -> None:
    class BrokenReviewInbox(Inbox):
        async def read_image(
            self,
            tenant_id: UUID,
            product_code: str,
            package_version: int,
            asset_id: UUID,
        ) -> AlbumReviewImageContent:
            return AlbumReviewImageContent(b"\x89PNG\r\n\x1a\ntruncated", "image/png")

    client = TestClient(
        create_local_app(
            tmp_path,
            album_review_inbox=BrokenReviewInbox(),
            album_sync_tenant_id=TENANT_ID,
            style_test_bridge_enabled=True,
        )
    )

    response = client.post("/local/api/album-review-inbox/SYNTHETIC-001/versions/2/open-review")

    assert response.status_code == 201
    item = response.json()["batch"]["items"][0]
    assert item["source_system"] == "album_organizer"
    assert item["photo_classification_facts"] == []


def test_open_album_product_is_idempotent_for_same_package_version(tmp_path) -> None:
    client = TestClient(
        create_local_app(
            tmp_path,
            album_review_inbox=ReviewInbox(),
            album_sync_tenant_id=TENANT_ID,
            style_test_bridge_enabled=True,
        )
    )
    path = "/local/api/album-review-inbox/SYNTHETIC-MULTI-001/versions/3/open-review"

    first = client.post(path)
    stored_batches = json.loads((tmp_path / "upload-batches.json").read_text())
    for item in stored_batches[0]["items"]:
        item["photo_classification_facts"] = []
    (tmp_path / "upload-batches.json").write_text(json.dumps(stored_batches))
    second = client.post(path)

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["replayed"] is True
    assert second.json()["batch"]["batch_id"] == first.json()["batch"]["batch_id"]
    assert all(
        item["photo_classification_facts"]
        for item in second.json()["batch"]["items"]
        if item["status"] in {"pending_review", "reviewed"}
    )
    assert len(json.loads((tmp_path / "upload-batches.json").read_text())) == 1
