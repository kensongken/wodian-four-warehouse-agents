from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from product_engine.domain.errors import TemplateVersionConflict
from product_engine.models.templates import PhotoTemplate
from product_engine.services.template_registry import TemplateRecord


def template_version_statement(
    tenant_id: UUID,
    template_code: str,
    version: int,
) -> Select[tuple[UUID]]:
    return select(PhotoTemplate.id).where(
        PhotoTemplate.tenant_id == tenant_id,
        PhotoTemplate.template_code == template_code,
        PhotoTemplate.version == version,
    )


def template_list_statement(
    tenant_id: UUID,
    category: str,
    status: str,
) -> Select[tuple[PhotoTemplate]]:
    return (
        select(PhotoTemplate)
        .where(
            PhotoTemplate.tenant_id == tenant_id,
            PhotoTemplate.category == category,
            PhotoTemplate.status == status,
        )
        .order_by(PhotoTemplate.template_code, PhotoTemplate.version.desc())
    )


class TemplateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def exists_version(
        self,
        tenant_id: UUID,
        template_code: str,
        version: int,
    ) -> bool:
        statement = template_version_statement(tenant_id, template_code, version)
        return (await self.session.execute(statement)).scalar_one_or_none() is not None

    async def create(self, record: TemplateRecord) -> TemplateRecord:
        self.session.add(
            PhotoTemplate(
                id=record.id,
                tenant_id=record.tenant_id,
                template_code=record.template_code,
                version=record.version,
                category=record.category,
                image_type=record.image_type,
                object_key=record.object_key,
                sha256=record.sha256,
                mime_type=record.mime_type,
                byte_size=record.byte_size,
                status=record.status,
            )
        )
        try:
            await self.session.flush()
        except IntegrityError as error:
            raise TemplateVersionConflict("template_version_exists") from error
        return record

    async def list_active(
        self,
        tenant_id: UUID,
        category: str,
        status: str,
    ) -> list[TemplateRecord]:
        statement = template_list_statement(tenant_id, category, status)
        rows = (await self.session.execute(statement)).scalars().all()
        return [
            TemplateRecord(
                id=row.id,
                tenant_id=row.tenant_id,
                template_code=row.template_code,
                version=row.version,
                category=row.category,
                image_type=row.image_type,
                object_key=row.object_key,
                sha256=row.sha256,
                mime_type=row.mime_type,
                byte_size=row.byte_size,
                status=row.status,
            )
            for row in rows
        ]
