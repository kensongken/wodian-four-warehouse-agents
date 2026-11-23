export const SALES_SCENARIOS = [
  "first_consultation", "needs_discovery", "product_selection",
  "product_comparison", "wearing_or_gifting", "size_and_fit",
  "budget_or_price", "purchase_confirmation", "store_visit_appointment",
  "promotion_or_coupon",
  "stock_availability", "order_detail_confirmation", "change_before_production",
  "production_progress", "shipping_arrangement", "logistics_or_receipt",
  "deposit_or_payment", "delivery_deadline",
  "custom_design", "engraving", "resizing", "bead_add_or_remove",
  "exchange_style", "swap_or_redesign", "resetting",
  "appearance_difference", "scratch_or_chip", "loose_or_fallen_stone",
  "breakage", "wrong_size", "missing_or_mismatch",
  "complaint_escalation",
  "cleaning_or_care", "polishing_or_refinishing", "repair",
  "part_replacement", "service_eligibility", "service_progress",
  "identity_bead", "scan_service", "record_check", "service_record_question",
  "consignment_inquiry", "consignment_materials", "consignment_preliminary_review",
  "consignment_physical_inspection", "consignment_eligibility",
  "consignment_listing_or_unsold", "consignment_settlement",
  "controlled_reuse_or_return",
  "hesitation_followup", "silent_customer_followup", "post_discussion_followup",
  "repeat_purchase", "festival_or_referral",
  "referral_tracking",
  "other_needs_review",
] as const;

export const CUSTOMER_INTENTS = [
  "learn_identity",
  "verify_service",
  "compare_options",
  "request_reuse_or_return",
  "price_or_value_question",
  "needs_human_review",
  "explore_purchase",
  "confirm_purchase_or_order",
  "request_customization",
  "report_product_issue",
  "request_after_sales",
  "request_consignment",
  "follow_up_or_repurchase",
] as const;

export const VERIFICATION_FIELDS = [
  "material", "quality", "origin", "certificate", "service_policy",
  "reuse_or_return_terms", "price_or_payment", "inventory_or_timeline",
  "order_or_delivery", "customization_feasibility", "condition_or_defect",
  "after_sales_policy", "consignment_terms",
] as const;

export const SALES_STAGES = [
  "discover",
  "explain",
  "evaluate",
  "advance",
  "follow_up",
] as const;

export const SAFETY_RULE_IDS = [
  "COPY_ONLY_DELIVERY",
  "REUSE_OR_RETURN_NO_FIXED_PRICE",
  "REUSE_OR_RETURN_HUMAN_REVIEW",
  "EVIDENCE_REQUIRED_FOR_FACT",
  "SERVICE_POLICY_VERIFICATION",
  "PRICE_OR_PAYMENT_HUMAN_REVIEW",
  "ORDER_STATUS_VERIFICATION",
  "CUSTOMIZATION_FEASIBILITY_VERIFICATION",
  "DEFECT_OR_DISPUTE_HUMAN_REVIEW",
  "AFTER_SALES_POLICY_VERIFICATION",
  "CONSIGNMENT_HUMAN_REVIEW",
  "SCENARIO_HUMAN_REVIEW",
] as const;

export type SalesScenario = (typeof SALES_SCENARIOS)[number];
export type CustomerIntent = (typeof CUSTOMER_INTENTS)[number];
export type SalesStage = (typeof SALES_STAGES)[number];
export type VerificationField = (typeof VERIFICATION_FIELDS)[number];
export type SafetyRuleId = (typeof SAFETY_RULE_IDS)[number];

/** Runtime validation is the source of truth for the remaining v1 shapes. */
export type {
  ConversationTurn,
  DecisionCard,
  DecisionCardRequest,
  EvidenceRef,
  ReplyOption,
} from "./schema";
