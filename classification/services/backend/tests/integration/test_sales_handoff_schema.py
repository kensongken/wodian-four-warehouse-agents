from sqlalchemy import CheckConstraint, UniqueConstraint

from product_engine.models.sales_handoffs import (
    SalesStrategyInputV2Record,
    SalesWarehouseReceiptRecord,
    WarehouseHandoffRecord,
)


def constraint_names(model: type) -> set[str | None]:
    return {constraint.name for constraint in model.__table__.constraints}


def test_sales_handoff_models_keep_sender_and_receiver_receipts_separate() -> None:
    assert SalesStrategyInputV2Record.__tablename__ == "sales_strategy_inputs_v2"
    assert WarehouseHandoffRecord.__tablename__ == "warehouse_handoffs"
    assert SalesWarehouseReceiptRecord.__tablename__ == "sales_warehouse_receipts"
    assert WarehouseHandoffRecord.__table__ is not SalesWarehouseReceiptRecord.__table__


def test_v2_producer_has_preview_and_immutable_source_constraints() -> None:
    checks = {
        constraint.name
        for constraint in SalesStrategyInputV2Record.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }
    uniques = {
        constraint.name
        for constraint in SalesStrategyInputV2Record.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }

    assert checks == {"ck_sales_strategy_inputs_v2_preview_only"}
    assert {
        "uq_sales_strategy_inputs_v2_tenant_product_version",
        "uq_sales_strategy_inputs_v2_tenant_product_content",
        "uq_sales_strategy_inputs_v2_tenant_classification",
    }.issubset(uniques)


def test_sender_handoff_has_closed_state_and_target_constraints() -> None:
    names = constraint_names(WarehouseHandoffRecord)

    assert "ck_warehouse_handoffs_sales" in names
    assert "ck_warehouse_handoffs_status" in names
    assert "uq_warehouse_handoffs_tenant_key" in names


def test_receiver_has_its_own_replay_constraints() -> None:
    names = constraint_names(SalesWarehouseReceiptRecord)

    assert "uq_sales_warehouse_receipts_tenant_handoff" in names
    assert "uq_sales_warehouse_receipts_tenant_key" in names
