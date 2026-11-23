"""Phase 1 database models."""


def load_all_models() -> None:
    from product_engine.models import (
        classifications,
        events,
        material_evidence,
        products,
        sales_handoffs,
        sales_handoffs_v3,
        screenshots,
        style_handoffs,
        style_handoffs_v3,
        templates,
        usage,
    )

    _ = (
        classifications,
        events,
        material_evidence,
        products,
        sales_handoffs,
        sales_handoffs_v3,
        screenshots,
        style_handoffs,
        style_handoffs_v3,
        templates,
        usage,
    )
