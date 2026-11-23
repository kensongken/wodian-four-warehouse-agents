from importlib import import_module

from sqlalchemy import CheckConstraint, UniqueConstraint

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
    "sales_strategy_inputs",
    "sales_strategy_inputs_v2",
    "sales_strategy_inputs_v3",
    "warehouse_handoffs",
    "warehouse_handoffs_v3",
    "sales_warehouse_receipts",
    "sales_warehouse_receipts_v3",
    "product_classification_versions",
    "product_classification_assets",
    "style_eligibility_inputs_v2",
    "style_transform_authorizations",
    "style_warehouse_handoffs",
    "style_warehouse_decision_receipts",
    "style_eligibility_inputs_v3",
    "style_warehouse_handoffs_v3",
    "style_warehouse_decision_receipts_v3",
}

TENANT_TABLES = EXPECTED_TABLES - {"inbox_events"}


def test_model_metadata_matches_phase_one_tables_and_tenant_indexes() -> None:
    models = import_module("product_engine.models")
    models.load_all_models()
    base = import_module("product_engine.models.base")

    assert set(base.Base.metadata.tables) == EXPECTED_TABLES
    for table_name in TENANT_TABLES:
        table = base.Base.metadata.tables[table_name]
        assert table.indexes, f"{table_name} 缺少租户索引"
        assert all(tuple(index.columns)[0].name == "tenant_id" for index in table.indexes)

    outbox = base.Base.metadata.tables["outbox_events"]
    packages = base.Base.metadata.tables["material_packages"]
    check_names = {
        constraint.name
        for table in (outbox, packages)
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert check_names >= {
        "ck_outbox_events_status",
        "ck_material_packages_status",
        "ck_material_packages_content_status",
        "ck_material_packages_price_status",
    }

    unique_names = {
        constraint.name
        for table in base.Base.metadata.tables.values()
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert unique_names >= {
        "uq_inbox_events_tenant_source_event",
        "uq_products_tenant_product_code",
        "uq_source_images_tenant_upstream_asset",
        "uq_material_packages_tenant_published_event",
        "uq_material_packages_tenant_product_version",
        "uq_material_package_assets_source",
        "uq_material_package_assets_position",
        "uq_material_evidence_upload_tenant_ref",
        "uq_material_evidence_tenant_request",
        "uq_material_evidence_tenant_product_content",
        "uq_material_evidence_tenant_ref",
        "uq_material_evidence_revocations_tenant_evidence",
        "uq_material_evidence_revocations_tenant_request",
        "uq_photo_templates_tenant_code_version",
        "uq_sales_material_usage_tenant_event",
        "uq_screenshot_imports_tenant_sha256",
        "uq_sales_strategy_inputs_tenant_product_version",
        "uq_sales_strategy_inputs_tenant_product_content",
        "uq_sales_strategy_inputs_v2_tenant_product_version",
        "uq_sales_strategy_inputs_v2_tenant_product_content",
        "uq_sales_strategy_inputs_v2_tenant_classification",
        "uq_sales_inputs_v3_tenant_product_version",
        "uq_sales_inputs_v3_tenant_product_content",
        "uq_sales_strategy_inputs_v3_tenant_classification",
        "uq_warehouse_handoffs_tenant_key",
        "uq_warehouse_handoffs_tenant_input_target",
        "uq_sales_warehouse_receipts_tenant_handoff",
        "uq_sales_warehouse_receipts_tenant_key",
        "uq_warehouse_handoffs_v3_tenant_key",
        "uq_warehouse_handoffs_v3_tenant_input",
        "uq_sales_receipts_v3_tenant_handoff",
        "uq_sales_receipts_v3_tenant_key",
        "uq_classification_versions_tenant_product_version",
        "uq_classification_versions_tenant_request",
        "uq_classification_versions_tenant_product_content",
        "uq_classification_assets_version_source",
        "uq_classification_assets_version_position",
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
    }

    strategy = base.Base.metadata.tables["sales_strategy_inputs"]
    strategy_checks = {
        constraint.name
        for constraint in strategy.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert "ck_sales_strategy_inputs_preview_only" in strategy_checks

    classifications = base.Base.metadata.tables["product_classification_versions"]
    classification_checks = {
        constraint.name
        for constraint in classifications.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert classification_checks >= {
        "ck_classification_versions_review_status",
        "ck_classification_versions_price_status",
        "ck_classification_versions_material_provenance",
    }
