"""Deterministic, local-only draft generation for the human-review workbench."""

from copy import deepcopy


PRODUCT_TYPES = {"first_sale_bead", "legacy_material", "redesign_output"}
OBJECT_TYPES = {
    "ProductPassport",
    "BeadPassport",
    "OrderRecord",
    "ServiceTicket",
    "SalesDecisionCard",
}

TOP_LEVEL_FIELDS = {
    "request_id",
    "customer_question_summary",
    "product_passport",
    "bead_passport",
    "service_ticket",
    "sales_decision_card",
}
PRODUCT_FIELDS = {
    "product_passport_id",
    "merchant_id",
    "version",
    "type",
    "sku",
    "title",
    "material_category",
    "lifecycle_status",
    "source_product_passport_ids",
    "evidence_refs",
    "updated_at",
}
BEAD_FIELDS = {
    "bead_passport_id",
    "merchant_id",
    "version",
    "issued_product_passport_id",
    "issued_order_record_id",
    "current_product_passport_id",
    "status",
    "scan_service_status",
    "public_record_version",
    "last_verified_at",
    "evidence_refs",
}
SERVICE_FIELDS = {
    "service_ticket_id",
    "merchant_id",
    "version",
    "service_type",
    "status",
    "product_passport_id",
    "bead_passport_id",
    "order_record_id",
    "logistics_status",
    "warehouse_decision",
    "warehouse_reason_code",
    "evidence_refs",
    "updated_at",
}
DECISION_FIELDS = {
    "sales_decision_card_id",
    "merchant_id",
    "version",
    "scenario",
    "customer_intent",
    "sales_stage",
    "recommended_strategy",
    "allowed_facts",
    "prohibited_claims",
    "seller_style_rules",
    "confidence",
    "requires_human_confirmation",
    "human_confirmation_reasons",
    "knowledge_version",
    "evidence_refs",
    "generated_at",
}
EVIDENCE_FIELDS = {"object_type", "object_id", "object_version", "field_path"}

REQUIRED_TOP_LEVEL_FIELDS = TOP_LEVEL_FIELDS
REQUIRED_PRODUCT_FIELDS = PRODUCT_FIELDS
REQUIRED_BEAD_FIELDS = BEAD_FIELDS
REQUIRED_SERVICE_FIELDS = SERVICE_FIELDS
REQUIRED_DECISION_FIELDS = DECISION_FIELDS

FIELD_LABELS = {
    "type": "商品类型",
    "material_category": "材质类目",
    "lifecycle_status": "商品生命周期",
    "status": "当前状态",
    "scan_service_status": "扫码服务状态",
    "public_record_version": "公开记录版本",
    "logistics_status": "物流状态",
    "warehouse_decision": "仓库验收状态",
}


class ValidationError(ValueError):
    """Raised when an input exceeds the contract's data boundary."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message

    def as_dict(self):
        return {"error": self.code, "message": self.message}


def _require_mapping(value, name):
    if not isinstance(value, dict):
        raise ValidationError("invalid_type", "%s 必须是对象" % name)


def _validate_fields(value, allowed, required, name):
    _require_mapping(value, name)
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValidationError(
            "unknown_field",
            "%s 包含未允许字段：%s" % (name, ", ".join(unknown)),
        )
    missing = sorted(required - set(value))
    if missing:
        raise ValidationError(
            "missing_field",
            "%s 缺少必填字段：%s" % (name, ", ".join(missing)),
        )


def _validate_string_list(value, name):
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValidationError("invalid_type", "%s 必须是字符串数组" % name)


def _validate_evidence(refs, name):
    if not isinstance(refs, list):
        raise ValidationError("invalid_type", "%s 必须是数组" % name)
    for index, ref in enumerate(refs):
        label = "%s[%d]" % (name, index)
        _validate_fields(ref, EVIDENCE_FIELDS, EVIDENCE_FIELDS, label)
        if ref["object_type"] not in OBJECT_TYPES:
            raise ValidationError("invalid_object_type", "%s.object_type 不受支持" % label)
        if not isinstance(ref["object_version"], int) or ref["object_version"] < 1:
            raise ValidationError("invalid_version", "%s.object_version 必须大于 0" % label)


def _validate_payload(payload):
    _validate_fields(payload, TOP_LEVEL_FIELDS, REQUIRED_TOP_LEVEL_FIELDS, "request")

    question = payload["customer_question_summary"]
    if not isinstance(question, str) or not question.strip() or len(question) > 500:
        raise ValidationError("invalid_question", "客户问题摘要必须为 1–500 字")

    product = payload["product_passport"]
    _validate_fields(product, PRODUCT_FIELDS, REQUIRED_PRODUCT_FIELDS, "product_passport")
    if product["type"] not in PRODUCT_TYPES:
        raise ValidationError("invalid_product_type", "ProductPassport.type 不受支持")
    _validate_evidence(product["evidence_refs"], "product_passport.evidence_refs")
    _validate_string_list(
        product["source_product_passport_ids"],
        "product_passport.source_product_passport_ids",
    )

    bead = payload["bead_passport"]
    if bead is not None:
        _validate_fields(bead, BEAD_FIELDS, REQUIRED_BEAD_FIELDS, "bead_passport")
        _validate_evidence(bead["evidence_refs"], "bead_passport.evidence_refs")

    service = payload["service_ticket"]
    if service is not None:
        _validate_fields(service, SERVICE_FIELDS, REQUIRED_SERVICE_FIELDS, "service_ticket")
        _validate_evidence(service["evidence_refs"], "service_ticket.evidence_refs")

    card = payload["sales_decision_card"]
    _validate_fields(card, DECISION_FIELDS, REQUIRED_DECISION_FIELDS, "sales_decision_card")
    for field in (
        "allowed_facts",
        "prohibited_claims",
        "seller_style_rules",
        "human_confirmation_reasons",
    ):
        _validate_string_list(card[field], "sales_decision_card.%s" % field)
    _validate_evidence(card["evidence_refs"], "sales_decision_card.evidence_refs")
    confidence = card["confidence"]
    if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        raise ValidationError("invalid_confidence", "confidence 必须在 0.0–1.0 之间")
    if card["requires_human_confirmation"] is not True:
        raise ValidationError(
            "manual_review_required",
            "v0.1 的 requires_human_confirmation 必须为 true",
        )

    merchant_ids = {product["merchant_id"], card["merchant_id"]}
    if bead is not None:
        merchant_ids.add(bead["merchant_id"])
    if service is not None:
        merchant_ids.add(service["merchant_id"])
    if len(merchant_ids) != 1:
        raise ValidationError("tenant_mismatch", "共同对象 merchant_id 必须一致")

    return deepcopy(payload)


def _collect_evidence(payload):
    sources = [
        payload["sales_decision_card"],
        payload.get("service_ticket"),
        payload.get("bead_passport"),
        payload["product_passport"],
    ]
    citations = []
    seen = set()
    for source in sources:
        if not source:
            continue
        for ref in source.get("evidence_refs", []):
            key = (
                ref["object_type"],
                ref["object_id"],
                ref["object_version"],
                ref["field_path"],
            )
            if key in seen:
                continue
            seen.add(key)
            citations.append(
                {
                    "ref": deepcopy(ref),
                    "supports": FIELD_LABELS.get(ref["field_path"], ref["field_path"]),
                }
            )
            if len(citations) == 4:
                return citations
    return citations


def _common_response(payload, status, drafts, risk_codes):
    return {
        "request_id": payload["request_id"],
        "status": status,
        "drafts": drafts,
        "risk_codes": risk_codes,
        "manual_review_required": True,
        "channel_policy": {
            "draft_only": True,
            "wechat_auto_send_allowed": False,
            "allowed_actions": ["copy", "export"],
        },
        "knowledge_version": payload["sales_decision_card"]["knowledge_version"],
    }


def _draft(text, evidence, risks, reasons, variant):
    return {
        "variant": variant,
        "text": text,
        "cited_evidence": deepcopy(evidence),
        "risk_warnings": list(risks),
        "manual_confirmation_reasons": list(reasons),
    }


def _requires_pending_legacy_block(payload):
    product = payload["product_passport"]
    service = payload["service_ticket"] or {}
    card = payload["sales_decision_card"]
    requested_text = "%s %s" % (
        payload["customer_question_summary"],
        card["customer_intent"],
    )
    risky_request = any(
        token in requested_text
        for token in ("保证", "回收", "估价", "值多少", "一定", "价格")
    )
    return (
        product["type"] == "legacy_material"
        and service.get("warehouse_decision") == "pending"
        and risky_request
    )


def _blocked_response(payload, risk_codes=None):
    card = payload["sales_decision_card"]
    reasons = card["human_confirmation_reasons"] or ["需要本人确认"]
    evidence = _collect_evidence(payload)
    text = (
        "这件旧料目前还需要等待仓库收到实物并完成验收，"
        "现在不能提前保证回收、给出估价或确认改款结果。"
        "仓库状态更新后，再为您核对下一步。"
    )
    risks = ["旧料尚未完成仓库验收", "不得保证回收", "不得提前估价"]
    return _common_response(
        payload,
        "blocked",
        [_draft(text, evidence, risks, reasons, "safe_holding")],
        risk_codes or ["warehouse_pending", "guarantee_or_valuation_requested"],
    )


def _first_sale_response(payload):
    product = payload["product_passport"]
    card = payload["sales_decision_card"]
    evidence = _collect_evidence(payload)
    reasons = card["human_confirmation_reasons"]
    risks = list(card["prohibited_claims"])
    texts = [
        (
            "这颗是 %s，记录的材质类目是%s。购买后会建立对应的身份记录，"
            "并可进入后续扫码服务；库存和当前服务内容还需要我再确认一下。"
        )
        % (product["title"], product["material_category"]),
        (
            "简单说，这颗身份珠不只是商品本身，还会关联后续的身份记录和扫码服务。"
            "它现在记录为%s，具体库存与服务说明我确认后再回复您。"
        )
        % product["material_category"],
    ]
    drafts = [
        _draft(texts[0], evidence, risks, reasons, "direct"),
        _draft(texts[1], evidence, risks, reasons, "warm"),
    ]
    return _common_response(payload, "draft_ready", drafts, [])


def _scan_response(payload):
    bead = payload["bead_passport"]
    card = payload["sales_decision_card"]
    evidence = _collect_evidence(payload)
    reasons = card["human_confirmation_reasons"]
    risks = ["扫码记录不是法定鉴定结论", "不得索取二维码原始令牌"]
    record_version = bead["public_record_version"] if bead else "未知"
    texts = [
        (
            "您扫到的是这颗身份珠对应的公开记录。目前扫码服务已开放，"
            "公开记录版本是 %s；如果页面显示不同，我再帮您核对。"
        )
        % record_version,
        (
            "现在可以正常查看这颗身份珠的公开记录。您先按页面提示操作即可；"
            "扫码记录用于服务查询，不等同于法定鉴定结论。"
        ),
    ]
    drafts = [
        _draft(texts[0], evidence, risks, reasons, "direct"),
        _draft(texts[1], evidence, risks, reasons, "concise"),
    ]
    return _common_response(payload, "draft_ready", drafts, [])


def _generic_response(payload):
    card = payload["sales_decision_card"]
    evidence = _collect_evidence(payload)
    facts = "；".join(card["allowed_facts"][:3])
    text = "%s 当前可确认的信息是：%s。" % (card["recommended_strategy"], facts)
    draft = _draft(
        text,
        evidence,
        card["prohibited_claims"],
        card["human_confirmation_reasons"],
        "direct",
    )
    return _common_response(payload, "draft_ready", [draft], [])


def generate_drafts(payload):
    """Validate a Sales Journey payload and return copy-only draft candidates."""

    normalized = _validate_payload(payload)
    if normalized["sales_decision_card"]["confidence"] < 0.80:
        return _blocked_response(normalized, ["low_confidence"])
    if _requires_pending_legacy_block(normalized):
        return _blocked_response(normalized)

    scenario = normalized["sales_decision_card"]["scenario"]
    if scenario == "first_sale":
        return _first_sale_response(normalized)
    if scenario == "scan_service":
        return _scan_response(normalized)
    return _generic_response(normalized)
