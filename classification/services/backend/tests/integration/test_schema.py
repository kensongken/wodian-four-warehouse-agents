EXPECTED_TABLES = {
    "inbox_events",
    "outbox_events",
    "products",
    "source_images",
    "material_packages",
    "material_package_assets",
    "material_evidence_uploads",
    "material_evidence_records",
    "material_evidence_revocations",
    "photo_templates",
    "sales_material_usage",
    "screenshot_imports",
    "product_classification_versions",
    "product_classification_assets",
    "sales_strategy_inputs_v2",
    "sales_strategy_inputs_v3",
    "warehouse_handoffs",
    "warehouse_handoffs_v3",
    "sales_warehouse_receipts",
    "sales_warehouse_receipts_v3",
    "style_eligibility_inputs_v2",
    "style_transform_authorizations",
    "style_warehouse_handoffs",
    "style_warehouse_decision_receipts",
    "style_eligibility_inputs_v3",
    "style_warehouse_handoffs_v3",
    "style_warehouse_decision_receipts_v3",
}

TENANT_INDEXES = {
    "ix_outbox_events_tenant_status_available",
    "ix_products_tenant_product_code",
    "ix_source_images_tenant_product_id",
    "ix_material_packages_tenant_product_id",
    "ix_material_package_assets_tenant_package_id",
    "ix_material_evidence_uploads_tenant_product_status",
    "ix_material_evidence_tenant_product_created",
    "ix_material_evidence_revocations_tenant_evidence",
    "ix_photo_templates_tenant_template_code",
    "ix_sales_material_usage_tenant_session_id",
    "ix_screenshot_imports_tenant_created_at",
    "ix_classification_versions_tenant_product_version",
    "ix_classification_assets_tenant_version_position",
    "ix_sales_strategy_inputs_v2_tenant_product_version",
    "ix_sales_inputs_v3_tenant_product_version",
    "ix_warehouse_handoffs_tenant_status",
    "ix_warehouse_handoffs_v3_tenant_status",
    "ix_sales_warehouse_receipts_tenant_accepted",
    "ix_sales_receipts_v3_tenant_accepted",
    "ix_style_inputs_tenant_product_version",
    "ix_style_authorizations_tenant_source_scope",
    "ix_style_handoffs_tenant_status",
    "ix_style_decisions_tenant_decided",
    "ix_style_inputs_v3_tenant_product_version",
    "ix_style_handoffs_v3_tenant_status",
    "ix_style_decisions_v3_tenant_decided",
}


def test_phase_one_schema_sql_contains_tables_constraints_and_tenant_indexes(
    alembic_runner,
) -> None:
    result = alembic_runner("upgrade", "head", "--sql")

    assert result.returncode == 0, result.stderr
    sql = result.stdout.lower()
    for table in EXPECTED_TABLES:
        assert f"create table {table}" in sql
    for constraint in (
        "ck_outbox_events_status",
        "ck_material_packages_status",
        "ck_material_packages_content_status",
        "ck_material_packages_price_status",
    ):
        assert constraint in sql
    for index in TENANT_INDEXES:
        assert index in sql
        statement = next(line for line in sql.splitlines() if index in line)
        assert "(tenant_id" in statement

    for constraint in (
        "uq_classification_versions_tenant_product_version",
        "uq_classification_versions_tenant_request",
        "uq_classification_versions_tenant_product_content",
        "uq_classification_assets_version_source",
        "uq_classification_assets_version_position",
        "uq_material_evidence_upload_tenant_ref",
        "uq_material_evidence_tenant_request",
        "uq_material_evidence_tenant_product_content",
        "uq_material_evidence_tenant_ref",
        "uq_material_evidence_revocations_tenant_evidence",
        "uq_material_evidence_revocations_tenant_request",
        "uq_sales_strategy_inputs_v2_tenant_product_version",
        "uq_sales_strategy_inputs_v2_tenant_product_content",
        "uq_sales_strategy_inputs_v2_tenant_classification",
        "uq_sales_inputs_v3_tenant_product_version",
        "uq_sales_inputs_v3_tenant_product_content",
        "uq_sales_strategy_inputs_v3_tenant_classification",
        "uq_warehouse_handoffs_tenant_key",
        "uq_warehouse_handoffs_v3_tenant_key",
        "uq_warehouse_handoffs_v3_tenant_input",
        "uq_sales_warehouse_receipts_tenant_handoff",
        "uq_sales_warehouse_receipts_tenant_key",
        "uq_sales_receipts_v3_tenant_handoff",
        "uq_sales_receipts_v3_tenant_key",
        "uq_style_inputs_tenant_product_version",
        "uq_style_inputs_tenant_product_content",
        "uq_style_authorizations_tenant_request",
        "uq_style_handoffs_tenant_key",
        "uq_style_handoffs_tenant_input",
        "uq_style_decisions_tenant_handoff",
        "uq_style_decisions_tenant_key",
        "uq_style_inputs_v3_tenant_product_version",
        "uq_style_inputs_v3_tenant_product_content",
        "uq_style_inputs_v3_tenant_classification",
        "uq_style_handoffs_v3_tenant_key",
        "uq_style_handoffs_v3_tenant_input",
        "uq_style_decisions_v3_tenant_handoff",
        "uq_style_decisions_v3_tenant_key",
    ):
        assert constraint in sql

    assert "foreign key(product_id) references products (id)" in sql
    assert "foreign key(base_material_package_id) references material_packages (id)" in sql
    assert "foreign key(source_image_id) references source_images (id)" in sql
    assert (
        "foreign key(source_classification_id) references product_classification_versions (id)"
    ) in sql
    assert "foreign key(strategy_input_id) references sales_strategy_inputs_v2 (id)" in sql
    assert "foreign key(handoff_id) references warehouse_handoffs (id)" in sql


def test_migration_can_be_applied_twice(alembic_runner) -> None:
    first = alembic_runner("upgrade", "head", "--sql")
    second = alembic_runner("upgrade", "head", "--sql")

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert second.stdout == first.stdout


def test_existing_inbox_events_are_upgraded_to_tenant_scope(alembic_runner) -> None:
    result = alembic_runner("upgrade", "head", "--sql")

    assert result.returncode == 0, result.stderr
    sql = result.stdout.lower()
    assert "alter table inbox_events add column tenant_id uuid" in sql
    assert "uq_inbox_events_tenant_source_event" in sql
    assert "if exists (select 1 from inbox_events)" in sql
    assert "manual tenant backfill required" in sql
