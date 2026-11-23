import asyncio
import json
from pathlib import Path
from typing import Any
from uuid import UUID

from product_engine.domain.errors import TemplateVersionConflict
from product_engine.services.template_registry import TemplateRecord


class LocalTemplateRepository:
    def __init__(self, index_path: Path) -> None:
        self.index_path = index_path
        self._lock = asyncio.Lock()

    def _read_sync(self) -> list[TemplateRecord]:
        if not self.index_path.exists():
            return []
        raw: list[dict[str, Any]] = json.loads(self.index_path.read_text(encoding="utf-8"))
        return [
            TemplateRecord(
                id=UUID(item["id"]),
                tenant_id=UUID(item["tenant_id"]),
                template_code=item["template_code"],
                version=item["version"],
                category=item["category"],
                image_type=item["image_type"],
                object_key=item["object_key"],
                sha256=item["sha256"],
                mime_type=item["mime_type"],
                byte_size=item["byte_size"],
                status=item["status"],
            )
            for item in raw
        ]

    def _write_sync(self, records: list[TemplateRecord]) -> None:
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.index_path.with_name(f".{self.index_path.name}.writing")
        serialized = [
            {
                "id": str(record.id),
                "tenant_id": str(record.tenant_id),
                "template_code": record.template_code,
                "version": record.version,
                "category": record.category,
                "image_type": record.image_type,
                "object_key": record.object_key,
                "sha256": record.sha256,
                "mime_type": record.mime_type,
                "byte_size": record.byte_size,
                "status": record.status,
            }
            for record in records
        ]
        temporary.write_text(
            json.dumps(serialized, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        temporary.replace(self.index_path)

    async def exists_version(
        self,
        tenant_id: UUID,
        template_code: str,
        version: int,
    ) -> bool:
        records = await asyncio.to_thread(self._read_sync)
        return any(
            record.tenant_id == tenant_id
            and record.template_code == template_code
            and record.version == version
            for record in records
        )

    async def create(self, record: TemplateRecord) -> TemplateRecord:
        async with self._lock:
            records = await asyncio.to_thread(self._read_sync)
            if any(
                item.tenant_id == record.tenant_id
                and item.template_code == record.template_code
                and item.version == record.version
                for item in records
            ):
                raise TemplateVersionConflict("template_version_exists")
            records.append(record)
            await asyncio.to_thread(self._write_sync, records)
        return record

    async def get_by_code(self, template_code: str) -> TemplateRecord | None:
        records = await asyncio.to_thread(self._read_sync)
        matching = [record for record in records if record.template_code == template_code]
        if not matching:
            return None
        return max(matching, key=lambda record: record.version)

    async def list_active(
        self,
        tenant_id: UUID,
        category: str,
        status: str,
    ) -> list[TemplateRecord]:
        records = await asyncio.to_thread(self._read_sync)
        return [
            record
            for record in records
            if record.tenant_id == tenant_id
            and record.category == category
            and record.status == status
        ]
