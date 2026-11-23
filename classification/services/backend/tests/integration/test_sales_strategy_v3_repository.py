def test_sales_v3_migration_creates_independent_tables(alembic_runner) -> None:
    result = alembic_runner("upgrade", "head", "--sql")

    assert result.returncode == 0, result.stderr
    sql = result.stdout.lower()
    assert "create table sales_strategy_inputs_v3" in sql
    assert "create table warehouse_handoffs_v3" in sql
    assert "create table sales_warehouse_receipts_v3" in sql
    assert "uq_sales_strategy_inputs_v3_tenant_classification" in sql
    assert "sales-v3" not in sql
