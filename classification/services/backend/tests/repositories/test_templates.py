from importlib import import_module
from uuid import UUID

from sqlalchemy.dialects import postgresql


def normalized_sql(statement) -> str:
    return " ".join(str(statement.compile(dialect=postgresql.dialect())).split())


def test_template_version_and_list_queries_are_tenant_bound() -> None:
    repository = import_module("product_engine.repositories.templates")
    tenant_id = UUID("22222222-2222-4222-8222-222222222222")

    version_sql = normalized_sql(
        repository.template_version_statement(
            tenant_id,
            "NANHONG-BRACELET-MAIN",
            1,
        )
    )
    list_sql = normalized_sql(repository.template_list_statement(tenant_id, "南红", "active"))

    assert "photo_templates.tenant_id =" in version_sql
    assert "photo_templates.template_code =" in version_sql
    assert "photo_templates.version =" in version_sql
    assert "photo_templates.tenant_id =" in list_sql
    assert "photo_templates.category =" in list_sql
    assert "photo_templates.status =" in list_sql
