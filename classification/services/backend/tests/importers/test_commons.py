from importlib import import_module
from urllib.parse import parse_qs

import httpx
import pytest


def commons_page(
    *,
    page_id: int = 101,
    mime: str = "image/jpeg",
    width: int = 1200,
    height: int = 800,
    license_name: str = "CC BY 4.0",
) -> dict[str, object]:
    return {
        "pageid": page_id,
        "title": "File:Jade bracelet.jpg",
        "imageinfo": [
            {
                "mime": mime,
                "width": width,
                "height": height,
                "url": "https://upload.wikimedia.org/original.jpg",
                "thumburl": "https://upload.wikimedia.org/thumb-640.jpg",
                "extmetadata": {
                    "LicenseShortName": {"value": license_name},
                    "LicenseUrl": {"value": "https://creativecommons.org/licenses/by/4.0/"},
                    "Artist": {"value": "<b>Auckland Museum</b>"},
                    "Credit": {"value": "Museum collection"},
                },
            }
        ],
    }


@pytest.mark.parametrize(
    "license_name",
    ["CC0 1.0", "Public domain", "CC BY 4.0", "CC BY-SA 3.0"],
)
def test_license_policy_accepts_only_reusable_licenses(license_name) -> None:
    commons = import_module("product_engine.importers.commons")

    assert commons.is_allowed_license(license_name) is True


@pytest.mark.parametrize(
    "license_name",
    ["CC BY-NC 4.0", "CC BY-ND 4.0", "All rights reserved", ""],
)
def test_license_policy_rejects_restricted_or_unknown_licenses(license_name) -> None:
    commons = import_module("product_engine.importers.commons")

    assert commons.is_allowed_license(license_name) is False


def test_parse_commons_page_builds_traceable_asset() -> None:
    commons = import_module("product_engine.importers.commons")

    asset = commons.parse_commons_page(commons_page(), "Category:Bracelets")

    assert asset == commons.CommonsAsset(
        page_id=101,
        title="File:Jade bracelet.jpg",
        page_url="https://commons.wikimedia.org/wiki/File%3AJade_bracelet.jpg",
        original_url="https://upload.wikimedia.org/original.jpg",
        thumb_url="https://upload.wikimedia.org/thumb-640.jpg",
        mime_type="image/jpeg",
        width=1200,
        height=800,
        license_name="CC BY 4.0",
        license_url="https://creativecommons.org/licenses/by/4.0/",
        artist="Auckland Museum",
        credit="Museum collection",
        source_category="Category:Bracelets",
    )


@pytest.mark.parametrize(
    "page",
    [
        commons_page(mime="image/svg+xml"),
        commons_page(width=499, height=499),
        commons_page(license_name="CC BY-NC 4.0"),
    ],
)
def test_parse_commons_page_filters_ineligible_files(page) -> None:
    commons = import_module("product_engine.importers.commons")

    assert commons.parse_commons_page(page, "Category:Jewellery") is None


def test_manifest_persists_success_dedupe_and_failure_counts(tmp_path) -> None:
    commons = import_module("product_engine.importers.commons")
    manifest = commons.ManifestStore(tmp_path)
    first = commons.ImportSuccess(
        page_id=101,
        title="File:Jade bracelet.jpg",
        page_url="https://commons.wikimedia.org/wiki/File%3AJade_bracelet.jpg",
        thumb_url="https://upload.wikimedia.org/thumb-640.jpg",
        license_name="CC BY 4.0",
        license_url="https://creativecommons.org/licenses/by/4.0/",
        artist="Auckland Museum",
        source_category="Category:Bracelets",
        template_code="LOCAL-ONE",
        object_key="tenants/local/templates/LOCAL-ONE/v1/source.jpg",
        sha256="a" * 64,
        byte_size=120_000,
        uploaded_at="2026-08-16T06:00:00Z",
    )
    second = commons.ImportSuccess(
        **{
            **first.__dict__,
            "page_id": 102,
            "thumb_url": "https://upload.wikimedia.org/thumb-640-2.jpg",
            "template_code": "LOCAL-TWO",
            "byte_size": 80_000,
        }
    )
    manifest.append_success(first)
    manifest.append_failure(
        commons.ImportFailure(
            page_id=103,
            title="File:Broken.jpg",
            stage="download",
            error_code="timeout",
            occurred_at="2026-08-16T06:01:00Z",
        )
    )
    manifest.append_success(second)

    reconstructed = commons.ManifestStore(tmp_path)

    assert reconstructed.successful_page_ids() == {101, 102}
    assert reconstructed.successful_thumb_urls() == {
        "https://upload.wikimedia.org/thumb-640.jpg",
        "https://upload.wikimedia.org/thumb-640-2.jpg",
    }
    assert reconstructed.stats() == commons.ManifestStats(
        success_count=2,
        failure_count=1,
        total_bytes=200_000,
    )


async def test_commons_client_traverses_pages_and_subcategories_without_duplicates() -> None:
    commons = import_module("product_engine.importers.commons")
    seen_queries: list[dict[str, list[str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        query = parse_qs(request.url.query.decode())
        seen_queries.append(query)
        category = query.get("gcmtitle", query.get("cmtitle", [""]))[0]
        if query.get("generator") == ["categorymembers"]:
            continuation = query.get("gcmcontinue")
            if category == "Category:Jewellery" and continuation is None:
                return httpx.Response(
                    200,
                    json={
                        "continue": {"gcmcontinue": "page-2", "continue": "gcmcontinue||"},
                        "query": {"pages": [commons_page(page_id=101)]},
                    },
                )
            if category == "Category:Jewellery":
                return httpx.Response(
                    200,
                    json={"query": {"pages": [commons_page(page_id=102)]}},
                )
            if category == "Category:Bracelets":
                return httpx.Response(
                    200,
                    json={
                        "query": {"pages": [commons_page(page_id=101), commons_page(page_id=103)]}
                    },
                )
        if query.get("list") == ["categorymembers"]:
            members = (
                [{"pageid": 201, "ns": 14, "title": "Category:Bracelets"}]
                if category == "Category:Jewellery"
                else []
            )
            return httpx.Response(200, json={"query": {"categorymembers": members}})
        raise AssertionError(f"unexpected query: {query}")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://commons.wikimedia.org",
    ) as http:
        client = commons.CommonsClient(http)
        assets = [asset async for asset in client.iter_assets(("Category:Jewellery",), max_depth=1)]

    assert [asset.page_id for asset in assets] == [101, 102, 103]
    assert all(
        query.get("iiurlwidth") == ["640"]
        for query in seen_queries
        if query.get("generator") == ["categorymembers"]
    )


async def test_commons_client_retries_rate_limit_with_retry_after() -> None:
    commons = import_module("product_engine.importers.commons")
    attempts = 0
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "2"})
        query = parse_qs(request.url.query.decode())
        if query.get("generator") == ["categorymembers"]:
            return httpx.Response(
                200,
                json={"query": {"pages": [commons_page(page_id=301)]}},
            )
        return httpx.Response(200, json={"query": {"categorymembers": []}})

    async def record_delay(delay: float) -> None:
        delays.append(delay)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://commons.wikimedia.org",
    ) as http:
        client = commons.CommonsClient(http, sleep=record_delay)
        assets = [asset async for asset in client.iter_assets(("Category:Jewellery",), max_depth=0)]

    assert [asset.page_id for asset in assets] == [301]
    assert attempts == 2
    assert delays == [2.0]
