from fastapi.testclient import TestClient

from opc_style.main import create_app


def test_health_reports_service_identity() -> None:
    response = TestClient(create_app()).get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "opc-style-agent",
        "sourceSha": "development",
    }
