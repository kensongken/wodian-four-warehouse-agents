from uuid import UUID

import httpx
import pytest


def test_vertical_slice_runs_all_steps_and_replays_inbound_events() -> None:
    from scripts.integration_smoke import PilotConfig, run_vertical_slice

    calls: list[tuple[str, str]] = []
    dispositions = {"album": 0, "usage": 0, "screenshot": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        assert request.headers.get("X-Signature") or request.method == "PUT"
        if request.method == "POST" and request.url.path.endswith("product-image-sets"):
            dispositions["album"] += 1
            disposition = "accepted" if dispositions["album"] == 1 else "duplicate"
            return httpx.Response(
                202 if disposition == "accepted" else 200,
                json={"disposition": disposition, "package_version": 1},
            )
        if request.method == "GET" and request.url.path.endswith("materials/latest"):
            return httpx.Response(
                200,
                json={
                    "status": "source_ready",
                    "content_status": "pending",
                    "price_status": "pending",
                    "package_version": 1,
                    "source_assets": [
                        {"material_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"}
                    ],
                },
            )
        if request.method == "POST" and request.url.path.endswith("material-usage"):
            dispositions["usage"] += 1
            disposition = "accepted" if dispositions["usage"] == 1 else "duplicate"
            return httpx.Response(
                202 if disposition == "accepted" else 200,
                json={"disposition": disposition},
            )
        if request.method == "POST" and request.url.path.endswith(
            "screenshots/uploads"
        ):
            return httpx.Response(
                201,
                json={
                    "object_key": "tenants/tenant/screenshots/pilot.png",
                    "url": "http://storage.test/pilot.png",
                    "headers": {"Content-Type": "image/png"},
                    "expires_in_seconds": 900,
                },
            )
        if request.method == "PUT" and request.url.host == "storage.test":
            return httpx.Response(200)
        if request.method == "POST" and request.url.path.endswith(
            "screenshots/imports"
        ):
            dispositions["screenshot"] += 1
            disposition = "accepted" if dispositions["screenshot"] == 1 else "duplicate"
            return httpx.Response(
                202 if disposition == "accepted" else 200,
                json={"disposition": disposition},
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = run_vertical_slice(
            client,
            PilotConfig(
                base_url="http://local.test",
                tenant_id=UUID("22222222-2222-4222-8222-222222222222"),
                album_secret=b"album-secret-for-local-pilot-only",
                sales_secret=b"sales-secret-for-local-pilot-only",
            ),
            UUID("11111111-1111-4111-8111-111111111111"),
        )

    assert result.product_status == "source_ready"
    assert result.album_dispositions == ("accepted", "duplicate")
    assert result.usage_dispositions == ("accepted", "duplicate")
    assert result.screenshot_dispositions == ("accepted", "duplicate")
    assert calls == [
        ("POST", "/v1/integrations/album/product-image-sets"),
        ("POST", "/v1/integrations/album/product-image-sets"),
        ("GET", "/v1/products/PILOT-111111111111/materials/latest"),
        ("POST", "/v1/integrations/sales/material-usage"),
        ("POST", "/v1/integrations/sales/material-usage"),
        ("POST", "/v1/integrations/sales/screenshots/uploads"),
        ("PUT", "/pilot.png"),
        ("POST", "/v1/integrations/sales/screenshots/imports"),
        ("POST", "/v1/integrations/sales/screenshots/imports"),
    ]


def test_local_vertical_slice_rejects_public_upload_target() -> None:
    from scripts.integration_smoke import PilotConfig, PilotFailure, run_vertical_slice

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("product-image-sets"):
            disposition = (
                "accepted" if request.headers.get("X-Replay") is None else "duplicate"
            )
            return httpx.Response(
                202 if disposition == "accepted" else 200,
                json={"disposition": disposition},
            )
        raise AssertionError("request should not reach a public upload target")

    responses = [
        httpx.Response(202, json={"disposition": "accepted"}),
        httpx.Response(200, json={"disposition": "duplicate"}),
        httpx.Response(
            200,
            json={
                "status": "source_ready",
                "content_status": "pending",
                "price_status": "pending",
                "package_version": 1,
                "source_assets": [
                    {"material_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"}
                ],
            },
        ),
        httpx.Response(202, json={"disposition": "accepted"}),
        httpx.Response(200, json={"disposition": "duplicate"}),
        httpx.Response(
            201,
            json={
                "object_key": "tenants/tenant/screenshots/pilot.png",
                "url": "https://public.example/pilot.png",
                "headers": {},
            },
        ),
    ]

    def sequenced_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "127.0.0.1"
        return responses.pop(0)

    with (
        httpx.Client(transport=httpx.MockTransport(sequenced_handler)) as client,
        pytest.raises(PilotFailure, match="unsafe_screenshot_upload_target"),
    ):
        run_vertical_slice(
            client,
            PilotConfig(
                base_url="http://127.0.0.1:8000",
                tenant_id=UUID("22222222-2222-4222-8222-222222222222"),
                album_secret=b"album-secret-for-local-pilot-only",
                sales_secret=b"sales-secret-for-local-pilot-only",
            ),
            UUID("11111111-1111-4111-8111-111111111111"),
        )


def test_remote_full_pilot_requires_https() -> None:
    from scripts.integration_smoke import PilotFailure, validate_target

    with pytest.raises(PilotFailure, match="remote_full_requires_https"):
        validate_target("http://pilot.example", allow_remote=True, full=True)

    validate_target("https://pilot.example", allow_remote=True, full=True)
