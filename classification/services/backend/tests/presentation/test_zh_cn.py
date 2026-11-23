import pytest

from product_engine.presentation import zh_cn


@pytest.mark.parametrize(
    ("internal_value", "expected_zh"),
    [
        ("source_ready", "原始图片已就绪"),
        ("pending", "等待处理"),
        ("loaded", "已调用素材"),
        ("copied", "已复制素材"),
        ("share_initiated", "已发起分享"),
        ("queued", "已进入处理队列"),
        ("duplicate", "已接收，无需重复处理"),
        ("dependencies_unavailable", "依赖服务暂时不可用"),
        ("product_code", "商品编码"),
        ("material_package", "销售素材包"),
        ("transaction_screenshot", "成交截图"),
    ],
)
def test_term_returns_the_approved_translation(internal_value: str, expected_zh: str) -> None:
    assert zh_cn.term(internal_value) == expected_zh


def test_term_preserves_unknown_machine_value() -> None:
    assert zh_cn.term("future_status") == "future_status"
