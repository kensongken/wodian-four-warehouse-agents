import asyncio
import hashlib
import html
import json
import re
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote

import httpx

from product_engine.services.local_upload import MAX_UPLOAD_BYTES, detect_image_mime

Sleep = Callable[[float], Awaitable[None]]
USER_AGENT = "ProductImageEngineLocalTest/0.1 (local QA; Wikimedia Commons)"
COMMONS_API_PATH = "/w/api.php"
DEFAULT_ROOTS = (
    "Category:Jewellery",
    "Category:Bracelets",
    "Category:Necklaces",
    "Category:Rings (jewellery)",
    "Category:Earrings",
    "Category:Gemstones",
    "Category:Beaded jewellery",
    "Category:Jade jewellery",
)


@dataclass(frozen=True)
class CommonsAsset:
    page_id: int
    title: str
    page_url: str
    original_url: str
    thumb_url: str
    mime_type: str
    width: int
    height: int
    license_name: str
    license_url: str
    artist: str
    credit: str
    source_category: str


@dataclass(frozen=True)
class ImportSuccess:
    page_id: int
    title: str
    page_url: str
    thumb_url: str
    license_name: str
    license_url: str
    artist: str
    source_category: str
    template_code: str
    object_key: str
    sha256: str
    byte_size: int
    uploaded_at: str


@dataclass(frozen=True)
class ImportFailure:
    page_id: int | None
    title: str
    stage: str
    error_code: str
    occurred_at: str


@dataclass(frozen=True)
class ManifestStats:
    success_count: int
    failure_count: int
    total_bytes: int


@dataclass(frozen=True)
class ImportSummary:
    success_count: int
    failure_count: int
    total_bytes: int
    duplicate_count: int
    discovered_count: int
    elapsed_seconds: float
    pause_reason: str


def is_allowed_license(name: str) -> bool:
    normalized = re.sub(r"[_-]+", " ", name.upper()).strip()
    if not normalized or " NC" in f" {normalized}" or " ND" in f" {normalized}":
        return False
    return (
        normalized.startswith("CC0")
        or "PUBLIC DOMAIN" in normalized
        or normalized.startswith("CC BY")
    )


def _plain_text(value: str) -> str:
    without_tags = re.sub(r"<[^>]*>", "", value)
    return re.sub(r"\s+", " ", html.unescape(without_tags)).strip()


def _metadata_value(metadata: dict[str, Any], key: str) -> str:
    raw = metadata.get(key, {})
    if not isinstance(raw, dict):
        return ""
    value = raw.get("value", "")
    return _plain_text(value) if isinstance(value, str) else ""


def parse_commons_page(
    page: dict[str, object],
    category: str,
) -> CommonsAsset | None:
    page_id = page.get("pageid")
    title = page.get("title")
    image_info_list = page.get("imageinfo")
    if (
        not isinstance(page_id, int)
        or not isinstance(title, str)
        or not isinstance(image_info_list, list)
        or not image_info_list
        or not isinstance(image_info_list[0], dict)
    ):
        return None
    image_info: dict[str, Any] = image_info_list[0]
    mime_type = image_info.get("mime")
    width = image_info.get("width")
    height = image_info.get("height")
    original_url = image_info.get("url")
    thumb_url = image_info.get("thumburl") or original_url
    metadata = image_info.get("extmetadata", {})
    if (
        mime_type not in {"image/jpeg", "image/png"}
        or not isinstance(width, int)
        or not isinstance(height, int)
        or max(width, height) < 500
        or not isinstance(original_url, str)
        or not isinstance(thumb_url, str)
        or not isinstance(metadata, dict)
    ):
        return None
    license_name = _metadata_value(metadata, "LicenseShortName")
    if not is_allowed_license(license_name):
        return None
    encoded_title = quote(title.replace(" ", "_"), safe="")
    return CommonsAsset(
        page_id=page_id,
        title=title,
        page_url=f"https://commons.wikimedia.org/wiki/{encoded_title}",
        original_url=original_url,
        thumb_url=thumb_url,
        mime_type=mime_type,
        width=width,
        height=height,
        license_name=license_name,
        license_url=_metadata_value(metadata, "LicenseUrl"),
        artist=_metadata_value(metadata, "Artist"),
        credit=_metadata_value(metadata, "Credit"),
        source_category=category,
    )


class ManifestStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.manifest_path = root / "manifest.jsonl"

    def _append(self, status: str, record: ImportSuccess | ImportFailure) -> None:
        payload = {"status": status, **asdict(record)}
        with self.manifest_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            stream.write("\n")

    def append_success(self, record: ImportSuccess) -> None:
        self._append("success", record)

    def append_failure(self, record: ImportFailure) -> None:
        self._append("failure", record)

    def _records(self) -> list[dict[str, Any]]:
        if not self.manifest_path.exists():
            return []
        records: list[dict[str, Any]] = []
        for line in self.manifest_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
        return records

    def successful_page_ids(self) -> set[int]:
        return {
            record["page_id"]
            for record in self._records()
            if record.get("status") == "success" and isinstance(record.get("page_id"), int)
        }

    def successful_thumb_urls(self) -> set[str]:
        return {
            record["thumb_url"]
            for record in self._records()
            if record.get("status") == "success" and isinstance(record.get("thumb_url"), str)
        }

    def success_records(self) -> list[dict[str, Any]]:
        return [record for record in self._records() if record.get("status") == "success"]

    def stats(self) -> ManifestStats:
        records = self._records()
        successes = [record for record in records if record.get("status") == "success"]
        return ManifestStats(
            success_count=len(successes),
            failure_count=sum(record.get("status") == "failure" for record in records),
            total_bytes=sum(
                record.get("byte_size", 0)
                for record in successes
                if isinstance(record.get("byte_size", 0), int)
            ),
        )

    def write_reports(self, summary: ImportSummary) -> None:
        report = asdict(summary)
        self._atomic_write(
            self.root / "report.json",
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        pause_labels = {
            "running": "正在导入",
            "target_reached": "已达到目标",
            "source_exhausted": "可用来源已遍历完",
            "local_service_unavailable": "本地上传服务暂不可用",
            "byte_limit_reached": "已达到本批容量上限",
        }
        markdown = (
            "# Wikimedia Commons 珠宝图片导入报告\n\n"
            f"- 成功上传：{summary.success_count} 张\n"
            f"- 失败记录：{summary.failure_count} 条\n"
            f"- 去重跳过：{summary.duplicate_count} 张\n"
            f"- 累计容量：{summary.total_bytes} 字节\n"
            f"- 本次状态：{pause_labels.get(summary.pause_reason, summary.pause_reason)}\n\n"
            "每张图片的来源页、许可证、作者、源分类、本地编码和 SHA-256 "
            "均保存在 `manifest.jsonl`。\n"
        )
        self._atomic_write(self.root / "REPORT.md", markdown)
        self._atomic_write(
            self.root / "state.json",
            json.dumps(
                {
                    "updated_at": _now(),
                    "success_count": summary.success_count,
                    "failure_count": summary.failure_count,
                    "total_bytes": summary.total_bytes,
                    "pause_reason": summary.pause_reason,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _error_code(error: BaseException) -> str:
    if isinstance(error, httpx.HTTPStatusError):
        return f"HTTP_{error.response.status_code}"
    return type(error).__name__


class CommonsImporter:
    def __init__(
        self,
        client: Any,
        manifest: ManifestStore,
        upload_url: str,
        *,
        concurrency: int = 4,
        byte_limit: int = 2 * 1024**3,
        request_interval: float = 0.0,
    ) -> None:
        self.client = client
        self.manifest = manifest
        self.upload_url = upload_url
        self.concurrency = max(1, concurrency)
        self.byte_limit = byte_limit
        self.request_interval = max(0.0, request_interval)

    async def _download(self, asset: CommonsAsset) -> bytes:
        for attempt in range(3):
            retry_delay = float(2**attempt)
            try:
                response = await self.client.http.get(
                    asset.thumb_url,
                    headers={"User-Agent": USER_AGENT},
                    timeout=30.0,
                    follow_redirects=True,
                )
            except httpx.RequestError:
                if attempt == 2:
                    raise
            else:
                if response.is_success:
                    return cast(bytes, response.content)
                if response.status_code not in {429, 500, 502, 503, 504}:
                    response.raise_for_status()
                if attempt == 2:
                    response.raise_for_status()
                if response.status_code == 429:
                    retry_after = response.headers.get("Retry-After")
                    try:
                        retry_delay = (
                            min(float(retry_after), 30.0)
                            if retry_after
                            else min(5.0 * 2**attempt, 30.0)
                        )
                    except ValueError:
                        retry_delay = min(5.0 * 2**attempt, 30.0)
            await self.client.sleep(retry_delay)
            # Let the other downloads in this bounded batch make progress before retrying.
            await asyncio.sleep(0)
        raise RuntimeError("unreachable download retry state")

    async def _upload(self, asset: CommonsAsset, content: bytes) -> ImportSuccess:
        mime_type = detect_image_mime(content)
        if mime_type not in {"image/jpeg", "image/png"}:
            raise ValueError("unsupported_or_invalid_image")
        if len(content) > MAX_UPLOAD_BYTES:
            raise ValueError("file_too_large")
        extension = "jpg" if mime_type == "image/jpeg" else "png"
        response = await self.client.http.post(
            self.upload_url,
            files={"file": (f"commons-{asset.page_id}.{extension}", content, mime_type)},
            timeout=30.0,
        )
        response.raise_for_status()
        payload = response.json()
        template = payload.get("template", {}) if isinstance(payload, dict) else {}
        digest = hashlib.sha256(content).hexdigest()
        if (
            not isinstance(template, dict)
            or template.get("status") != "active"
            or template.get("sha256") != digest
            or template.get("byte_size") != len(content)
            or not isinstance(template.get("template_code"), str)
            or not isinstance(template.get("object_key"), str)
        ):
            raise ValueError("invalid_local_upload_response")
        return ImportSuccess(
            page_id=asset.page_id,
            title=asset.title,
            page_url=asset.page_url,
            thumb_url=asset.thumb_url,
            license_name=asset.license_name,
            license_url=asset.license_url,
            artist=asset.artist,
            source_category=asset.source_category,
            template_code=template["template_code"],
            object_key=template["object_key"],
            sha256=digest,
            byte_size=len(content),
            uploaded_at=_now(),
        )

    async def run(
        self,
        *,
        target: int,
        roots: tuple[str, ...] = DEFAULT_ROOTS,
        max_depth: int = 4,
    ) -> ImportSummary:
        started = asyncio.get_running_loop().time()
        stats = self.manifest.stats()
        known_pages = self.manifest.successful_page_ids()
        known_urls = self.manifest.successful_thumb_urls()
        duplicate_count = 0
        discovered_count = 0
        pause_reason = "running"
        iterator = self.client.iter_assets(roots, max_depth=max_depth).__aiter__()

        while stats.success_count < target:
            batch: list[CommonsAsset] = []
            while len(batch) < min(self.concurrency, target - stats.success_count):
                try:
                    asset = await iterator.__anext__()
                except StopAsyncIteration:
                    break
                discovered_count += 1
                if asset.page_id in known_pages or asset.thumb_url in known_urls:
                    duplicate_count += 1
                    continue
                batch.append(asset)
            if not batch:
                pause_reason = "source_exhausted"
                break

            downloaded = await asyncio.gather(
                *(self._download(asset) for asset in batch),
                return_exceptions=True,
            )
            should_pause = False
            for asset, result in zip(batch, downloaded, strict=True):
                if isinstance(result, BaseException):
                    self.manifest.append_failure(
                        ImportFailure(
                            page_id=asset.page_id,
                            title=asset.title,
                            stage="download",
                            error_code=_error_code(result),
                            occurred_at=_now(),
                        )
                    )
                    continue
                if stats.total_bytes + len(result) > self.byte_limit:
                    pause_reason = "byte_limit_reached"
                    should_pause = True
                    break
                try:
                    success = await self._upload(asset, result)
                except httpx.ConnectError:
                    self.manifest.append_failure(
                        ImportFailure(
                            page_id=asset.page_id,
                            title=asset.title,
                            stage="upload",
                            error_code="local_service_unavailable",
                            occurred_at=_now(),
                        )
                    )
                    pause_reason = "local_service_unavailable"
                    should_pause = True
                    break
                except (httpx.HTTPError, ValueError, json.JSONDecodeError) as error:
                    self.manifest.append_failure(
                        ImportFailure(
                            page_id=asset.page_id,
                            title=asset.title,
                            stage="upload",
                            error_code=str(error) or type(error).__name__,
                            occurred_at=_now(),
                        )
                    )
                    continue
                self.manifest.append_success(success)
                known_pages.add(asset.page_id)
                known_urls.add(asset.thumb_url)
                stats = self.manifest.stats()
                if stats.success_count % 25 == 0:
                    print(
                        f"已导入 {stats.success_count}/{target} 张，"
                        f"累计 {stats.total_bytes / 1024 / 1024:.1f} MiB",
                        flush=True,
                    )
                if stats.success_count >= target:
                    pause_reason = "target_reached"
                    should_pause = True
                    break
            stats = self.manifest.stats()
            summary = ImportSummary(
                success_count=stats.success_count,
                failure_count=stats.failure_count,
                total_bytes=stats.total_bytes,
                duplicate_count=duplicate_count,
                discovered_count=discovered_count,
                elapsed_seconds=round(asyncio.get_running_loop().time() - started, 3),
                pause_reason=pause_reason,
            )
            self.manifest.write_reports(summary)
            if should_pause:
                return summary
            if self.request_interval:
                await self.client.sleep(self.request_interval)

        if stats.success_count >= target:
            pause_reason = "target_reached"
        summary = ImportSummary(
            success_count=stats.success_count,
            failure_count=stats.failure_count,
            total_bytes=stats.total_bytes,
            duplicate_count=duplicate_count,
            discovered_count=discovered_count,
            elapsed_seconds=round(asyncio.get_running_loop().time() - started, 3),
            pause_reason=pause_reason,
        )
        self.manifest.write_reports(summary)
        return summary


class CommonsClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.http = http
        self.sleep = sleep
        self._api_slots = asyncio.Semaphore(2)

    async def _request_json(self, params: dict[str, str]) -> dict[str, Any]:
        response: httpx.Response | None = None
        for attempt in range(3):
            try:
                async with self._api_slots:
                    response = await self.http.get(
                        COMMONS_API_PATH,
                        params=params,
                        headers={"User-Agent": USER_AGENT},
                    )
            except httpx.RequestError:
                if attempt == 2:
                    raise
                await self.sleep(float(2**attempt))
                continue
            if response.is_success:
                return cast(dict[str, Any], response.json())
            if response.status_code not in {429, 500, 502, 503, 504} or attempt == 2:
                request = httpx.Request("GET", str(response.url))
                raise httpx.HTTPStatusError(
                    f"Commons API returned {response.status_code}",
                    request=request,
                    response=response,
                )
            retry_after = response.headers.get("Retry-After")
            try:
                delay = min(float(retry_after), 30.0) if retry_after else float(2**attempt)
            except ValueError:
                delay = float(2**attempt)
            await self.sleep(delay)
        raise RuntimeError("unreachable Commons retry state")

    async def _category_files(self, category: str) -> AsyncIterator[CommonsAsset]:
        continuation: str | None = None
        while True:
            params = {
                "action": "query",
                "format": "json",
                "formatversion": "2",
                "generator": "categorymembers",
                "gcmtitle": category,
                "gcmtype": "file",
                "gcmlimit": "500",
                "prop": "imageinfo",
                "iiprop": "url|mime|size|extmetadata",
                "iiurlwidth": "640",
            }
            if continuation:
                params["gcmcontinue"] = continuation
            payload = await self._request_json(params)
            query = payload.get("query", {})
            pages = query.get("pages", []) if isinstance(query, dict) else []
            if isinstance(pages, list):
                for page in pages:
                    if isinstance(page, dict):
                        asset = parse_commons_page(page, category)
                        if asset is not None:
                            yield asset
            continuation_data = payload.get("continue", {})
            continuation = (
                continuation_data.get("gcmcontinue")
                if isinstance(continuation_data, dict)
                else None
            )
            if not isinstance(continuation, str):
                break

    async def _subcategories(self, category: str) -> AsyncIterator[str]:
        continuation: str | None = None
        while True:
            params = {
                "action": "query",
                "format": "json",
                "formatversion": "2",
                "list": "categorymembers",
                "cmtitle": category,
                "cmtype": "subcat",
                "cmlimit": "500",
            }
            if continuation:
                params["cmcontinue"] = continuation
            payload = await self._request_json(params)
            query = payload.get("query", {})
            members = query.get("categorymembers", []) if isinstance(query, dict) else []
            if isinstance(members, list):
                for member in members:
                    title = member.get("title") if isinstance(member, dict) else None
                    if isinstance(title, str) and title.startswith("Category:"):
                        yield title
            continuation_data = payload.get("continue", {})
            continuation = (
                continuation_data.get("cmcontinue") if isinstance(continuation_data, dict) else None
            )
            if not isinstance(continuation, str):
                break

    async def iter_assets(
        self,
        roots: tuple[str, ...],
        max_depth: int = 4,
    ) -> AsyncIterator[CommonsAsset]:
        queue = deque((root, 0) for root in roots)
        seen_categories: set[str] = set()
        seen_pages: set[int] = set()
        while queue and len(seen_categories) < 2000:
            category, depth = queue.popleft()
            if category in seen_categories:
                continue
            seen_categories.add(category)
            async for asset in self._category_files(category):
                if asset.page_id not in seen_pages:
                    seen_pages.add(asset.page_id)
                    yield asset
            if depth >= max_depth:
                continue
            async for subcategory in self._subcategories(category):
                if subcategory not in seen_categories:
                    queue.append((subcategory, depth + 1))
