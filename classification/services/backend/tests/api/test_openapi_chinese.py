from fastapi.testclient import TestClient

from product_engine.api.main import create_app


def test_root_introduces_the_product_in_chinese() -> None:
    response = TestClient(create_app()).get("/")

    assert response.status_code == 200
    assert response.json() == {
        "product_name": "商品图片内容与销售反馈引擎",
        "description": (
            "接收相册整理输出的商品图片编码与素材，形成可追溯的商品内容，"
            "并向销售之旅提供稳定的销售素材和反馈入口。"
        ),
        "docs_url": "/docs",
    }


def test_openapi_explains_product_and_health_routes_in_chinese() -> None:
    document = TestClient(create_app()).get("/openapi.json").json()

    assert document["info"]["title"] == "商品图片内容与销售反馈引擎"
    assert "相册整理" in document["info"]["description"]
    assert "销售之旅" in document["info"]["description"]
    assert document["tags"] == [
        {
            "name": "health",
            "description": "检查服务进程以及数据库、图片存储等依赖是否可用。",
        }
    ]
    assert document["paths"]["/health/live"]["get"]["summary"] == "检查服务进程"
    assert "服务进程正常" in document["paths"]["/health/live"]["get"]["description"]
    assert document["paths"]["/health/ready"]["get"]["summary"] == "检查服务是否可接收请求"
    assert "依赖服务" in document["paths"]["/health/ready"]["get"]["description"]


def test_health_payload_machine_fields_remain_english() -> None:
    client = TestClient(create_app())

    assert client.get("/health/live").json() == {"status": "ok"}
    assert client.get("/health/ready").json() == {"status": "ready"}
