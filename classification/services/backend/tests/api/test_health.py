from fastapi.testclient import TestClient

from product_engine.api.main import create_app


async def ready() -> bool:
    return True


async def not_ready() -> bool:
    return False


def test_live_health_is_process_only() -> None:
    client = TestClient(create_app(readiness_probe=not_ready))
    response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_health_rejects_unavailable_dependencies_in_chinese() -> None:
    client = TestClient(create_app(readiness_probe=not_ready))
    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {
        "error_code": "dependencies_unavailable",
        "message_zh": "数据库或图片存储服务暂时不可用。",
        "next_step_zh": "请稍后重试；如果问题持续，请联系系统管理员并提供请求时间。",
    }
    assert all(
        forbidden not in response.text
        for forbidden in ("Traceback", "SECRET", "/Users/", "s3://", "postgresql://")
    )


def test_ready_health_accepts_available_dependencies() -> None:
    client = TestClient(create_app(readiness_probe=ready))
    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
