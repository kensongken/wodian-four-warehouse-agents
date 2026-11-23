from typing import Literal

from pydantic import BaseModel, ConfigDict

from product_engine.services.pilot_release_gate import PilotReleaseDecision


class PilotReleaseReasonView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str
    message_zh: str
    required_action_zh: str


class PilotReleaseStatusView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    signal: Literal["green", "yellow", "red"]
    title_zh: str
    summary_zh: str
    reasons: list[PilotReleaseReasonView]


REASON_VIEWS: dict[str, tuple[str, str]] = {
    "human_confirmation_required": (
        "商品资料尚未完成人工确认。",
        "由训练员核对当前商品资料并保存新版本。",
    ),
    "material_classification_missing": (
        "材质类目仍是候选或信息不足。",
        "由训练员确认首批六品类之一，或继续保留信息不足。",
    ),
    "main_image_missing": ("尚未确认商品主图。", "选择当前商品图片并由训练员确认。"),
    "main_image_mismatch": ("主图不属于当前商品。", "重新选择当前商品编码下的图片。"),
    "product_type_missing": ("商品类型尚未选择。", "选择首售身份珠、旧料或改款成品。"),
    "specification_missing": ("商品规格尚未录入。", "人工测量并填写适用品类规格。"),
    "quality_defect_review_missing": (
        "品质与瑕疵观察尚未完成。",
        "记录图片可见品质和瑕疵，不作鉴定结论。",
    ),
    "price_range_missing": (
        "人工建议价格区间尚未录入。",
        "填写价格标准并完成人工确认。",
    ),
    "price_confirmation_required": (
        "价格尚未完成本版本人工确认。",
        "核对价格标准、确认人与时间后重新确认价格。",
    ),
    "inventory_missing": ("库存状态尚未确认。", "由商家选择现货、预留或暂无库存。"),
    "service_rules_missing": ("客户可见服务规则尚未补充。", "补充适用的退换、扫码或改款规则。"),
    "forbidden_unsupported_claim": (
        "资料包含无法支持的真伪、产地、升值或固定售价承诺。",
        "删除越界承诺，只保留可见事实或待验证说明。",
    ),
}


def build_pilot_release_status(decision: PilotReleaseDecision) -> PilotReleaseStatusView:
    reasons = [
        PilotReleaseReasonView(
            code=code,
            message_zh=REASON_VIEWS.get(code, ("存在尚未处理的资料问题。", "请由负责人复核。"))[0],
            required_action_zh=REASON_VIEWS.get(
                code, ("存在尚未处理的资料问题。", "请由负责人复核。")
            )[1],
        )
        for code in decision.reason_codes
    ]
    if decision.decision == "allowed_for_local_pilot":
        return PilotReleaseStatusView(
            signal="green",
            title_zh="可进入脱敏本地小试",
            summary_zh="人工资料已通过本地小试检查，但仍禁止真实客户数据和外部动作。",
            reasons=reasons,
        )
    if decision.decision == "blocked":
        return PilotReleaseStatusView(
            signal="red",
            title_zh="禁止进入小试",
            summary_zh="发现越界内容，修正并重新人工确认前不得继续。",
            reasons=reasons,
        )
    return PilotReleaseStatusView(
        signal="yellow",
        title_zh="还需人工补充",
        summary_zh="请完成下列资料后重新检查；当前不能进入本地小试。",
        reasons=reasons,
    )
