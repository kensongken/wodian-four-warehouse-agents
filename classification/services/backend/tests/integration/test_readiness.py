from importlib import import_module

import pytest
from fastapi.testclient import TestClient

from product_engine.api.main import create_app


def dependency_probe(database_available: bool, object_storage_available: bool):
    dependencies = import_module("product_engine.api.dependencies")

    async def check_database() -> None:
        if not database_available:
            raise RuntimeError("sensitive-database-address")

    def check_object_storage() -> None:
        if not object_storage_available:
            raise RuntimeError("sensitive-object-storage-address")

    return dependencies.DependencyProbe(check_database, check_object_storage)


@pytest.mark.parametrize(
    ("database_available", "object_storage_available"),
    [(False, True), (True, False)],
)
def test_ready_fails_safely_when_a_dependency_is_unavailable(
    database_available: bool,
    object_storage_available: bool,
    caplog,
) -> None:
    probe = dependency_probe(database_available, object_storage_available)
    response = TestClient(create_app(readiness_probe=probe)).get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {
        "error_code": "dependencies_unavailable",
        "message_zh": "数据库或图片存储服务暂时不可用。",
        "next_step_zh": "请稍后重试；如果问题持续，请联系系统管理员并提供请求时间。",
    }
    assert "sensitive-database-address" not in caplog.text
    assert "sensitive-object-storage-address" not in caplog.text
    assert "RuntimeError" in caplog.text


def test_ready_accepts_requests_when_both_dependencies_are_available() -> None:
    probe = dependency_probe(True, True)

    response = TestClient(create_app(readiness_probe=probe)).get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
