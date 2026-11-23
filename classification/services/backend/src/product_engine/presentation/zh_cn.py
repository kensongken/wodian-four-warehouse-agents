from dataclasses import dataclass

APP_TITLE = "商品图片内容与销售反馈引擎"
APP_DESCRIPTION = (
    "接收相册整理输出的商品图片编码与素材，形成可追溯的商品内容，"
    "并向销售之旅提供稳定的销售素材和反馈入口。"
)
HEALTH_TAG_DESCRIPTION = "检查服务进程以及数据库、图片存储等依赖是否可用。"

TERMS = {
    "source_ready": "原始图片已就绪",
    "pending": "等待处理",
    "loaded": "已调用素材",
    "copied": "已复制素材",
    "share_initiated": "已发起分享",
    "queued": "已进入处理队列",
    "duplicate": "已接收，无需重复处理",
    "dependencies_unavailable": "依赖服务暂时不可用",
    "product_code": "商品编码",
    "material_package": "销售素材包",
    "transaction_screenshot": "成交截图",
}


@dataclass(frozen=True)
class UserMessage:
    message_zh: str
    next_step_zh: str


USER_MESSAGES = {
    "dependencies_unavailable": UserMessage(
        message_zh="数据库或图片存储服务暂时不可用。",
        next_step_zh="请稍后重试；如果问题持续，请联系系统管理员并提供请求时间。",
    )
}


def term(value: str) -> str:
    """返回统一的简体中文术语，未知内部值保持原样。"""
    return TERMS.get(value, value)


def user_message(error_code: str) -> UserMessage:
    """根据稳定错误码返回安全的中文说明。"""
    return USER_MESSAGES[error_code]
