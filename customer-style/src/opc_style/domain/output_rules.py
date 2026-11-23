"""Business rules shared by product image output workflows."""

from collections.abc import Hashable, Iterable

from opc_style.domain.errors import DomainRuleError

JOB_STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset(
        {"generating", "insufficient_information", "permission_unknown", "disabled"}
    ),
    "generating": frozenset(
        {
            "machine_check",
            "generation_failed",
            "generation_uncertain",
            "cleanup_pending",
            "cleanup_abandoned",
            "disabled",
        }
    ),
    "cleanup_pending": frozenset({"generation_failed", "cleanup_abandoned"}),
    "machine_check": frozenset(
        {"awaiting_customer_review", "authenticity_failed", "disabled"}
    ),
    "awaiting_customer_review": frozenset(
        {"customer_approved", "customer_rejected", "disabled"}
    ),
    # Compatibility state used by early local pixel-preserving jobs. The only
    # legal move is into the shared customer-review gate; no approval is implied.
    "awaiting_review": frozenset({"awaiting_customer_review"}),
    # A suite retry reuses the same sealed child job and idempotency key.
    "generation_failed": frozenset({"generating"}),
}


def validate_job_status_transition(current_status: str, new_status: str) -> None:
    """Reject status jumps that bypass required generation and review gates."""
    if new_status not in JOB_STATUS_TRANSITIONS.get(current_status, frozenset()):
        raise DomainRuleError("invalid_image_output_job_status_transition")


def validate_product_count[ProductId: Hashable](
    output_type: str, product_ids: Iterable[ProductId]
) -> tuple[ProductId, ...]:
    """Return a stable product selection after enforcing output cardinality."""
    selected = tuple(product_ids)
    if output_type in {"scene", "real_photo", "wear"}:
        if len(selected) != 1 or len(set(selected)) != 1:
            code = (
                "wear_requires_one_product"
                if output_type == "wear"
                else "scene_requires_one_product"
            )
            raise DomainRuleError(code)
        return selected
    if output_type == "product_style":
        if not 1 <= len(selected) <= 5 or len(set(selected)) != 1:
            raise DomainRuleError("product_style_requires_one_product")
        return selected
    if output_type in {"multi_product", "multi-product"}:
        if not 2 <= len(selected) <= 5 or len(set(selected)) != len(selected):
            raise DomainRuleError("multi_product_requires_2_to_5_distinct_products")
        return selected
    raise DomainRuleError("unsupported_output_type")
