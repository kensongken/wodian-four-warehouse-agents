from importlib import import_module
from uuid import UUID

from sqlalchemy.dialects import postgresql


def normalized_sql(statement) -> str:
    return " ".join(str(statement.compile(dialect=postgresql.dialect())).split())


def test_product_and_package_queries_are_tenant_bound() -> None:
    repository = import_module("product_engine.repositories.products")
    tenant_id = UUID("22222222-2222-4222-8222-222222222222")

    product_sql = normalized_sql(repository.product_lookup_statement(tenant_id, "SP-00831"))
    package_sql = normalized_sql(repository.latest_package_statement(tenant_id, "SP-00831"))

    assert "products.tenant_id =" in product_sql
    assert "products.product_code =" in product_sql
    assert "products.tenant_id =" in package_sql
    assert "products.product_code =" in package_sql
    assert "material_packages.tenant_id =" in package_sql


def test_material_package_event_lookup_is_tenant_product_and_event_bound() -> None:
    repository = import_module("product_engine.repositories.products")
    statement = repository.material_package_by_event_statement(
        UUID("22222222-2222-4222-8222-222222222222"),
        UUID("33333333-3333-4333-8333-333333333333"),
        "BCNHDIY914",
    )

    sql = normalized_sql(statement)

    assert "material_packages.tenant_id =" in sql
    assert "material_packages.published_event_id =" in sql
    assert "products.tenant_id =" in sql
    assert "products.product_code =" in sql
