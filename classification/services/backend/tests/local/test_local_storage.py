from uuid import UUID

from product_engine.services.template_registry import TemplateRecord


async def test_local_storage_and_index_survive_reconstruction(tmp_path) -> None:
    from product_engine.repositories.local_templates import LocalTemplateRepository
    from product_engine.storage.local import LocalObjectStorage

    tenant_id = UUID("22222222-2222-4222-8222-222222222222")
    object_key = f"tenants/{tenant_id}/templates/LOCAL-ABC/v1/source.jpg"
    content = b"\xff\xd8\xfflocal-jpeg"
    sha256 = "882b2851a9d81067b34ff1a7fcf67dd5030902bb7fc2a8abba3de27c2d744195"
    storage = LocalObjectStorage(tmp_path / "objects")
    await storage.save(object_key, content, "image/jpeg", sha256)

    stored = await storage.inspect(object_key)
    assert stored.object_key == object_key
    assert stored.mime_type == "image/jpeg"
    assert stored.byte_size == len(content)
    assert stored.sha256 == sha256

    index_path = tmp_path / "templates.json"
    repository = LocalTemplateRepository(index_path)
    record = TemplateRecord(
        id=UUID("11111111-1111-4111-8111-111111111111"),
        tenant_id=tenant_id,
        template_code="LOCAL-ABC",
        version=1,
        category="待识别",
        image_type="main",
        object_key=object_key,
        sha256=sha256,
        mime_type="image/jpeg",
        byte_size=len(content),
    )
    await repository.create(record)

    reconstructed = LocalTemplateRepository(index_path)
    items = await reconstructed.list_active(tenant_id, "待识别", "active")
    assert items == [record]
    assert await reconstructed.exists_version(tenant_id, "LOCAL-ABC", 1) is True
