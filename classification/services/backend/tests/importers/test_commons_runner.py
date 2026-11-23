import hashlib
from collections.abc import AsyncIterator
from importlib import import_module

import httpx


class StaticCommonsClient:
    def __init__(self, http: httpx.AsyncClient, assets: list[object]) -> None:
        self.http = http
        self.assets = assets
        self.delays: list[float] = []

    async def sleep(self, delay: float) -> None:
        self.delays.append(delay)

    async def iter_assets(
        self,
        roots: tuple[str, ...],
        max_depth: int = 4,
    ) -> AsyncIterator[object]:
        del roots, max_depth
        for asset in self.assets:
            yield asset


def asset(commons, page_id: int):
    return commons.CommonsAsset(
        page_id=page_id,
        title=f"File:Jewellery {page_id}.jpg",
        page_url=f"https://commons.wikimedia.org/wiki/File%3AJewellery_{page_id}.jpg",
        original_url=f"https://upload.wikimedia.org/original-{page_id}.jpg",
        thumb_url=f"https://upload.wikimedia.org/thumb-{page_id}.jpg",
        mime_type="image/jpeg",
        width=1200,
        height=800,
        license_name="CC BY 4.0",
        license_url="https://creativecommons.org/licenses/by/4.0/",
        artist="Example Museum",
        credit="Museum collection",
        source_category="Category:Bracelets",
    )


def prior_success(commons, page_id: int):
    return commons.ImportSuccess(
        page_id=page_id,
        title=f"File:Jewellery {page_id}.jpg",
        page_url=f"https://commons.wikimedia.org/wiki/File%3AJewellery_{page_id}.jpg",
        thumb_url=f"https://upload.wikimedia.org/thumb-{page_id}.jpg",
        license_name="CC BY 4.0",
        license_url="https://creativecommons.org/licenses/by/4.0/",
        artist="Example Museum",
        source_category="Category:Bracelets",
        template_code=f"LOCAL-{page_id}",
        object_key=f"tenants/local/templates/LOCAL-{page_id}/v1/source.jpg",
        sha256="a" * 64,
        byte_size=100,
        uploaded_at="2026-08-16T06:00:00Z",
    )


async def test_importer_downloads_uploads_and_writes_traceable_report(tmp_path) -> None:
    commons = import_module("product_engine.importers.commons")
    content = b"\xff\xd8\xffcommons-jewellery"
    digest = hashlib.sha256(content).hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "upload.wikimedia.org":
            return httpx.Response(200, content=content, headers={"Content-Type": "image/jpeg"})
        if request.url.host == "127.0.0.1":
            assert content in request.content
            return httpx.Response(
                201,
                json={
                    "ok": True,
                    "template": {
                        "template_code": "LOCAL-UPLOADED",
                        "object_key": "tenants/local/templates/LOCAL-UPLOADED/v1/source.jpg",
                        "sha256": digest,
                        "byte_size": len(content),
                        "status": "active",
                    },
                },
            )
        raise AssertionError(str(request.url))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = StaticCommonsClient(http, [asset(commons, 101)])
        manifest = commons.ManifestStore(tmp_path)
        importer = commons.CommonsImporter(client, manifest, "http://127.0.0.1/upload")
        summary = await importer.run(target=1)

    assert summary.success_count == 1
    assert summary.pause_reason == "target_reached"
    assert manifest.successful_page_ids() == {101}
    record = manifest.success_records()[0]
    assert record["sha256"] == digest
    assert record["template_code"] == "LOCAL-UPLOADED"
    assert record["page_url"].startswith("https://commons.wikimedia.org/wiki/")
    assert (tmp_path / "report.json").exists()
    assert "成功上传：1 张" in (tmp_path / "REPORT.md").read_text(encoding="utf-8")


async def test_importer_retries_download_then_resumes_without_duplicate_calls(
    tmp_path,
) -> None:
    commons = import_module("product_engine.importers.commons")
    manifest = commons.ManifestStore(tmp_path)
    manifest.append_success(prior_success(commons, 101))
    content = b"\xff\xd8\xffretry-success"
    digest = hashlib.sha256(content).hexdigest()
    downloads: list[int] = []
    uploads: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "upload.wikimedia.org":
            page_id = int(request.url.path.split("-")[-1].split(".")[0])
            downloads.append(page_id)
            if page_id == 102 and downloads.count(102) == 1:
                return httpx.Response(503)
            return httpx.Response(200, content=content)
        if request.url.host == "127.0.0.1":
            uploads.append(1)
            return httpx.Response(
                201,
                json={
                    "ok": True,
                    "template": {
                        "template_code": f"LOCAL-{len(uploads)}",
                        "object_key": f"tenants/local/templates/{len(uploads)}/source.jpg",
                        "sha256": digest,
                        "byte_size": len(content),
                        "status": "active",
                    },
                },
            )
        raise AssertionError(str(request.url))

    assets = [asset(commons, page_id) for page_id in range(101, 106)]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = StaticCommonsClient(http, assets)
        importer = commons.CommonsImporter(client, manifest, "http://127.0.0.1/upload")
        summary = await importer.run(target=3)

    assert summary.success_count == 3
    assert downloads == [102, 103, 102]
    assert len(uploads) == 2
    assert client.delays == [1.0]


async def test_importer_pauses_when_local_service_is_unavailable(tmp_path) -> None:
    commons = import_module("product_engine.importers.commons")
    content = b"\xff\xd8\xfflocal-down"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "upload.wikimedia.org":
            return httpx.Response(200, content=content)
        raise httpx.ConnectError("local service unavailable", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = StaticCommonsClient(http, [asset(commons, 201), asset(commons, 202)])
        manifest = commons.ManifestStore(tmp_path)
        importer = commons.CommonsImporter(client, manifest, "http://127.0.0.1/upload")
        summary = await importer.run(target=2)

    assert summary.success_count == 0
    assert summary.pause_reason == "local_service_unavailable"
    assert manifest.stats().failure_count == 1


async def test_importer_stops_before_crossing_byte_limit(tmp_path) -> None:
    commons = import_module("product_engine.importers.commons")
    content = b"\xff\xd8\xfftoo-large-for-limit"
    upload_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal upload_calls
        if request.url.host == "upload.wikimedia.org":
            return httpx.Response(200, content=content)
        upload_calls += 1
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = StaticCommonsClient(http, [asset(commons, 301)])
        manifest = commons.ManifestStore(tmp_path)
        importer = commons.CommonsImporter(
            client,
            manifest,
            "http://127.0.0.1/upload",
            byte_limit=len(content) - 1,
        )
        summary = await importer.run(target=1)

    assert summary.pause_reason == "byte_limit_reached"
    assert upload_calls == 0
    assert manifest.stats().success_count == 0


async def test_intermediate_report_says_running_instead_of_source_exhausted(
    tmp_path,
) -> None:
    commons = import_module("product_engine.importers.commons")
    content = b"\xff\xd8\xffstill-running"
    digest = hashlib.sha256(content).hexdigest()

    class RecordingManifest(commons.ManifestStore):
        def __init__(self, root):
            super().__init__(root)
            self.reasons = []

        def write_reports(self, summary):
            self.reasons.append(summary.pause_reason)
            super().write_reports(summary)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "upload.wikimedia.org":
            return httpx.Response(200, content=content)
        return httpx.Response(
            201,
            json={
                "ok": True,
                "template": {
                    "template_code": "LOCAL-RUNNING",
                    "object_key": "tenants/local/templates/running/source.jpg",
                    "sha256": digest,
                    "byte_size": len(content),
                    "status": "active",
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = StaticCommonsClient(http, [asset(commons, 401), asset(commons, 402)])
        manifest = RecordingManifest(tmp_path)
        importer = commons.CommonsImporter(
            client,
            manifest,
            "http://127.0.0.1/upload",
            concurrency=1,
        )
        await importer.run(target=2)

    assert manifest.reasons == ["running", "target_reached"]


async def test_download_failure_records_http_status_for_operational_tuning(
    tmp_path,
) -> None:
    commons = import_module("product_engine.importers.commons")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = StaticCommonsClient(http, [asset(commons, 501)])
        manifest = commons.ManifestStore(tmp_path)
        importer = commons.CommonsImporter(
            client,
            manifest,
            "http://127.0.0.1/upload",
            concurrency=1,
        )
        await importer.run(target=1)

    failures = [row for row in manifest._records() if row.get("status") == "failure"]
    assert failures[0]["error_code"] == "HTTP_429"


async def test_download_honors_retry_after_header(tmp_path) -> None:
    commons = import_module("product_engine.importers.commons")
    content = b"\xff\xd8\xffretry-after"
    digest = hashlib.sha256(content).hexdigest()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if request.url.host == "upload.wikimedia.org":
            calls += 1
            if calls == 1:
                return httpx.Response(429, headers={"Retry-After": "7"})
            return httpx.Response(200, content=content)
        return httpx.Response(
            201,
            json={
                "ok": True,
                "template": {
                    "template_code": "LOCAL-RETRY-AFTER",
                    "object_key": "tenants/local/templates/retry-after/source.jpg",
                    "sha256": digest,
                    "byte_size": len(content),
                    "status": "active",
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = StaticCommonsClient(http, [asset(commons, 601)])
        importer = commons.CommonsImporter(
            client,
            commons.ManifestStore(tmp_path),
            "http://127.0.0.1/upload",
            concurrency=1,
        )
        summary = await importer.run(target=1)

    assert summary.success_count == 1
    assert client.delays == [7.0]


async def test_importer_paces_sequential_download_batches(tmp_path) -> None:
    commons = import_module("product_engine.importers.commons")
    content = b"\xff\xd8\xffpaced"
    digest = hashlib.sha256(content).hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "upload.wikimedia.org":
            return httpx.Response(200, content=content)
        return httpx.Response(
            201,
            json={
                "ok": True,
                "template": {
                    "template_code": "LOCAL-PACED",
                    "object_key": "tenants/local/templates/paced/source.jpg",
                    "sha256": digest,
                    "byte_size": len(content),
                    "status": "active",
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = StaticCommonsClient(
            http,
            [asset(commons, 701), asset(commons, 702)],
        )
        importer = commons.CommonsImporter(
            client,
            commons.ManifestStore(tmp_path),
            "http://127.0.0.1/upload",
            concurrency=1,
            request_interval=2.0,
        )
        summary = await importer.run(target=2)

    assert summary.success_count == 2
    assert client.delays == [2.0]
