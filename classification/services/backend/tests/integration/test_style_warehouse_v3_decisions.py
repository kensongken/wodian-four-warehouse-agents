def test_style_v3_migration_contains_isolated_tables(alembic_runner) -> None:
    result = alembic_runner("upgrade", "head", "--sql")
    assert result.returncode == 0, result.stderr
    sql = result.stdout.lower()
    assert "create table style_eligibility_inputs_v3" in sql
    assert "create table style_warehouse_handoffs_v3" in sql
    assert "create table style_warehouse_decision_receipts_v3" in sql
    assert "generation_job_id is null" in sql
