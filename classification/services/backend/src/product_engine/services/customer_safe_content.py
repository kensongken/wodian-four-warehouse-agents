import unicodedata
from collections.abc import Iterable


class UnsafeCustomerContentError(ValueError):
    pass


_FORBIDDEN_SEMANTIC_PATTERNS = (
    # 真伪、产地与品质承诺
    "保真",
    "真品",
    "正品",
    "经鉴定",
    "鉴定为",
    "天然a货",
    "纯天然",
    "产自",
    "原产于",
    "正宗",
    "官方认证",
    "品质保证",
    "质量保证",
    "顶级品质",
    "authentic",
    "genuine",
    "certifiednatural",
    # 固定价承诺
    "一口价",
    "固定售价",
    "固定价格",
    "fixedprice",
    # 保值、升值与回购承诺
    "保值",
    "升值",
    "投资收益",
    "回购",
    "buyback",
    "repurchase",
    "guaranteedvalue",
    # 系统自动对外发送承诺
    "自动微信",
    "微信自动",
    "系统自动发送",
    "系统自动外发",
    "自动外发",
    "自动发给客户",
    "自动推送客户",
    "autowechat",
    "automaticwechat",
    "autosendwechat",
    "systemautomaticsend",
)


def canonicalize_customer_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).casefold()
    return "".join(character for character in normalized if character.isalnum())


def assert_customer_safe_content(values: Iterable[str]) -> None:
    for value in values:
        canonical = canonicalize_customer_text(value)
        if any(pattern in canonical for pattern in _FORBIDDEN_SEMANTIC_PATTERNS):
            raise UnsafeCustomerContentError("forbidden_unsupported_claim")
