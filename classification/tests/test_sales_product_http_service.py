import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from wsgiref.util import setup_testing_defaults

from sales_contract.catalog import SalesCatalog
from sales_contract.http_service import create_sales_product_contract_app
from scripts.serve_sales_product_contract import load_catalog


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = ROOT / "fixtures" / "v0.1" / "product-passports.json"
FIRST_SALE_ID = "10000000-0000-4000-8000-000000000001"
LEGACY_ID = "20000000-0000-4000-8000-000000000001"
RESOLUTION_PATH = "/v0.1/sales/product-code-resolution"
PACKAGE_PATH = f"/v0.1/product-packages/{FIRST_SALE_ID}"
FORBIDDEN_RESPONSE_MARKERS = (
    "object_key",
    "customer_return_photo",
    "warehouse_acceptance_photo",
    "source_ticket_id",
    "customer",
    "order",
    "bead_id",
    "media_kind",
    "captured_at",
)


def request_json_with_headers(
    app,
    method,
    path,
    payload=None,
    headers=None,
    *,
    raw_body=None,
    content_type="application/json",
):
    """Exercise the real WSGI contract without opening a network port."""
    if raw_body is not None:
        body = raw_body.encode("utf-8") if isinstance(raw_body, str) else raw_body
    else:
        body = b"" if payload is None else json.dumps(payload).encode("utf-8")
    environ = {}
    setup_testing_defaults(environ)
    environ.update(
        {
            "REQUEST_METHOD": method,
            "PATH_INFO": path.partition("?")[0],
            "QUERY_STRING": path.partition("?")[2],
            "CONTENT_TYPE": content_type,
            "CONTENT_LENGTH": str(len(body)),
            "wsgi.input": io.BytesIO(body),
        }
    )
    for name, value in (headers or {}).items():
        environ[f"HTTP_{name.upper().replace('-', '_')}"] = value

    captured = {}

    def start_response(status, response_headers):
        captured["status"] = status
        captured["headers"] = dict(response_headers)

    raw_response = b"".join(app(environ, start_response))
    response_payload = json.loads(raw_response.decode("utf-8"))
    serialized = json.dumps(response_payload, ensure_ascii=False)
    for forbidden in FORBIDDEN_RESPONSE_MARKERS:
        if forbidden in serialized:
            raise AssertionError(f"response leaked forbidden field: {forbidden}")
    return int(captured["status"].split()[0]), response_payload, captured["headers"]


def request_json(app, method, path, payload=None, headers=None):
    status, payload, _ = request_json_with_headers(app, method, path, payload, headers)
    return status, payload


class SalesProductHttpServiceTests(unittest.TestCase):
    def setUp(self):
        records = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        self.app = create_sales_product_contract_app(
            SalesCatalog.from_records(records, tenant_id="tenant-a")
        )
        self.tenant_headers = {"X-Tenant-Id": "tenant-a"}

    def test_resolves_a_sales_sku_for_the_bound_tenant(self):
        """Changing the route or exposing a product without tenant binding is a bug."""
        status, payload = request_json(
            self.app,
            "POST",
            RESOLUTION_PATH,
            {
                "contract_version": "0.1",
                "tenant_id": "tenant-a",
                "code_type": "sku",
                "code": "OPC-FSB-NH-5MM-001",
                "purpose": "sales_journey",
            },
            self.tenant_headers,
        )

        self.assertEqual(status, 200)
        self.assertEqual(payload["resolution_status"], "resolved")
        self.assertEqual(payload["product_id"], FIRST_SALE_ID)
        self.assertEqual(payload["resolved_from"], "sku")

    def test_projects_a_sales_package_without_private_or_operational_fields(self):
        """Returning raw ProductPassport fields from the HTTP endpoint is a bug."""
        status, payload = request_json(
            self.app,
            "GET",
            PACKAGE_PATH + "?audience=sales_journey",
            headers=self.tenant_headers,
        )
        serialized = json.dumps(payload, ensure_ascii=False)

        self.assertEqual(status, 200)
        self.assertEqual(payload["audience"], "sales_journey")
        self.assertEqual(payload["product_id"], FIRST_SALE_ID)
        self.assertEqual(payload["package_version"], 1)
        self.assertEqual(
            payload["evidence_ids"],
            ["11000000-0000-4000-8000-000000000001"],
        )
        self.assertEqual(
            payload["validity"]["valid_until"], "2099-12-31T23:59:59Z"
        )
        self.assertEqual(
            payload["forbidden_claims"],
            ["不得承诺保值、升值或投资收益", "不得承诺固定回购价格"],
        )
        self.assertEqual(
            payload["product_passport"],
            {
                "sku": "OPC-FSB-NH-5MM-001",
                "category": "南红",
                "form": "身份珠",
                "title": "柿子红南红身份珠 5mm",
                "description": "5mm柿子红南红基础款身份珠商品，支持扫码进入身份与服务页面。",
                "selling_points": [
                    "柿子红色调，5mm日常基础款",
                    "身份珠扫码可进入服务页面",
                ],
                "service_summary": [
                    "购买后可扫码进入身份与服务页面",
                    "可提交旧珠改款申请，最终以人工确认结果为准",
                ],
                "conservative_price_range": {
                    "currency": "CNY",
                    "minimum": "1200.00",
                    "maximum": "1500.00",
                    "basis": "基于已批准的商品规格与销售策略",
                    "evidence_level": "merchant_declared",
                    "evidence_ids": [
                        "11000000-0000-4000-8000-000000000001"
                    ],
                },
                "public_media": [
                    {
                        "media_id": "11000000-0000-4000-8000-000000000001",
                        "public_url": "https://fixtures.opc.example/products/fsb-001/main.jpg",
                    }
                ],
            },
        )
        self.assertEqual(
            payload["product_passport"]["public_media"][0]["public_url"],
            "https://fixtures.opc.example/products/fsb-001/main.jpg",
        )
        for forbidden in FORBIDDEN_RESPONSE_MARKERS:
            self.assertNotIn(forbidden, serialized)

    def test_rejects_cross_tenant_and_sales_ineligible_requests(self):
        """Checking a code before authorization or serving non-sales data is a bug."""
        cross_tenant_payload = {
            "contract_version": "0.1",
            "tenant_id": "tenant-b",
            "code_type": "sku",
            "code": "OPC-FSB-NH-5MM-001",
            "purpose": "sales_journey",
        }

        self.assertEqual(
            request_json(
                self.app, "POST", RESOLUTION_PATH, cross_tenant_payload, {"X-Tenant-Id": "tenant-b"}
            )[0],
            403,
        )
        self.assertEqual(
            request_json(
                self.app,
                "GET",
                f"/v0.1/product-packages/{LEGACY_ID}?audience=sales_journey",
                headers=self.tenant_headers,
            ),
            (422, {"status": "not_sellable"}),
        )
        self.assertEqual(
            request_json(
                self.app,
                "GET",
                PACKAGE_PATH + "?audience=saas",
                headers=self.tenant_headers,
            )[0],
            403,
        )

    def test_preserves_each_authorized_resolution_failure_status(self):
        """Collapsing business failures to not_found invites unsafe client fallback."""
        base_payload = {
            "contract_version": "0.1",
            "tenant_id": "tenant-a",
            "purpose": "sales_journey",
        }
        cases = (
            (
                {**base_payload, "code_type": "sku", "code": "OPC-UNKNOWN-001"},
                404,
                "not_found",
            ),
            (
                {**base_payload, "code_type": "product_id", "code": LEGACY_ID},
                422,
                "not_sellable",
            ),
            (
                {**base_payload, "code_type": "bead_id", "code": "B-2026-000831"},
                409,
                "ambiguous",
            ),
        )
        for request_payload, expected_http_status, expected_resolution_status in cases:
            with self.subTest(expected_resolution_status=expected_resolution_status):
                status, payload = request_json(
                    self.app,
                    "POST",
                    RESOLUTION_PATH,
                    request_payload,
                    self.tenant_headers,
                )
                self.assertEqual(status, expected_http_status)
                self.assertEqual(
                    payload,
                    {
                        "contract_version": "0.1",
                        "resolution_status": expected_resolution_status,
                    },
                )

        records = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        retired = json.loads(json.dumps(records[0]))
        retired["product_id"] = "90000000-0000-4000-8000-000000000001"
        retired["sku"] = "OPC-FSB-NH-RETIRED-001"
        retired["bead_id"] = "B-2026-009999"
        retired["status"] = "retired"
        retired["media"][0]["product_id"] = retired["product_id"]
        retired["media"][0]["bead_id"] = retired["bead_id"]
        retired["sales_journey_projection"]["product_id"] = retired["product_id"]
        retired_app = create_sales_product_contract_app(
            SalesCatalog.from_records(records + [retired], tenant_id="tenant-a")
        )
        status, payload = request_json(
            retired_app,
            "POST",
            RESOLUTION_PATH,
            {
                **base_payload,
                "code_type": "sku",
                "code": "OPC-FSB-NH-RETIRED-001",
            },
            self.tenant_headers,
        )
        self.assertEqual(status, 410)
        self.assertEqual(
            payload,
            {"contract_version": "0.1", "resolution_status": "retired"},
        )

    def test_rejects_malformed_contracts_before_catalog_business_rules(self):
        """Passing unknown or malformed contracts to catalog resolution is a bug."""
        valid_payload = {
            "contract_version": "0.1",
            "tenant_id": "tenant-a",
            "code_type": "sku",
            "code": "OPC-FSB-NH-5MM-001",
            "purpose": "sales_journey",
        }
        malformed_cases = (
            ({**valid_payload, "extra": "not allowed"}, self.tenant_headers),
            ({**valid_payload, "contract_version": "1.0"}, self.tenant_headers),
            ({**valid_payload, "purpose": "inventory"}, self.tenant_headers),
            ({**valid_payload, "tenant_id": "tenant-b"}, self.tenant_headers),
            ({**valid_payload, "code": ["not-a-string"]}, self.tenant_headers),
        )
        for payload, headers in malformed_cases:
            with self.subTest(payload=payload):
                self.assertEqual(
                    request_json(self.app, "POST", RESOLUTION_PATH, payload, headers)[0],
                    400 if payload["tenant_id"] == "tenant-a" else 403,
                )

        self.assertEqual(
            request_json(self.app, "PUT", RESOLUTION_PATH, valid_payload, self.tenant_headers)[0],
            405,
        )
        self.assertEqual(
            request_json(self.app, "GET", PACKAGE_PATH + "/extra?audience=sales_journey", headers=self.tenant_headers)[0],
            404,
        )

    def test_rejects_extra_query_separators_and_sets_route_specific_allow_header(self):
        """Permissive query parsing or a broad Allow header would widen the contract."""
        for query in (
            "?audience=sales_journey&",
            "?audience=sales_journey&&",
            "?&audience=sales_journey",
        ):
            with self.subTest(query=query):
                self.assertEqual(
                    request_json(
                        self.app,
                        "GET",
                        PACKAGE_PATH + query,
                        headers=self.tenant_headers,
                    )[0],
                    400,
                )

        resolution_response = request_json_with_headers(
            self.app, "PUT", RESOLUTION_PATH, headers=self.tenant_headers
        )
        package_response = request_json_with_headers(
            self.app, "POST", PACKAGE_PATH + "?audience=sales_journey", headers=self.tenant_headers
        )
        self.assertEqual(resolution_response[0], 405)
        self.assertEqual(resolution_response[2]["Allow"], "POST")
        self.assertEqual(package_response[0], 405)
        self.assertEqual(package_response[2]["Allow"], "GET")

    def test_requires_exact_json_media_type_and_rejects_duplicate_keys(self):
        """A loose media type or last-key-wins JSON parser widens the signed contract."""
        valid_payload = {
            "contract_version": "0.1",
            "tenant_id": "tenant-a",
            "code_type": "sku",
            "code": "OPC-FSB-NH-5MM-001",
            "purpose": "sales_journey",
        }
        exact_response = request_json_with_headers(
            self.app,
            "POST",
            RESOLUTION_PATH,
            valid_payload,
            self.tenant_headers,
        )
        self.assertEqual(exact_response[0], 200)
        self.assertEqual(exact_response[2]["Content-Type"], "application/json")

        for content_type in (
            "application/json; charset=utf-8",
            "application/json-patch+json",
        ):
            with self.subTest(content_type=content_type):
                self.assertEqual(
                    request_json_with_headers(
                        self.app,
                        "POST",
                        RESOLUTION_PATH,
                        valid_payload,
                        self.tenant_headers,
                        content_type=content_type,
                    )[0],
                    400,
                )

        duplicate_code_body = (
            '{"contract_version":"0.1","tenant_id":"tenant-a",'
            '"code_type":"sku","code":"OPC-UNKNOWN-001",'
            '"code":"OPC-FSB-NH-5MM-001","purpose":"sales_journey"}'
        )
        self.assertEqual(
            request_json_with_headers(
                self.app,
                "POST",
                RESOLUTION_PATH,
                headers=self.tenant_headers,
                raw_body=duplicate_code_body,
            )[0],
            400,
        )

    def test_rejects_noncanonical_product_id_codes(self):
        """Accepting alternate UUID spellings creates multiple signed forms of one code."""
        base_payload = {
            "contract_version": "0.1",
            "tenant_id": "tenant-a",
            "code_type": "product_id",
            "purpose": "sales_journey",
        }
        for code in (
            "10000000000040008000000000000001",
            "{10000000-0000-4000-8000-000000000001}",
        ):
            with self.subTest(code=code):
                self.assertEqual(
                    request_json(
                        self.app,
                        "POST",
                        RESOLUTION_PATH,
                        {**base_payload, "code": code},
                        self.tenant_headers,
                    )[0],
                    400,
                )

    def test_server_script_requires_a_synthetic_catalog_and_tenant_binding(self):
        """Removing either startup binding argument would expose an unscoped service."""
        result = subprocess.run(
            [sys.executable, "scripts/serve_sales_product_contract.py", "--help"],
            cwd=ROOT,
            capture_output=True,
            check=False,
            text=True,
        )

        self.assertEqual(result.returncode, 0)
        self.assertIn("--catalog", result.stdout)
        self.assertIn("--tenant-id", result.stdout)

    def test_server_loader_rejects_unpinned_catalog_or_tenant(self):
        """Loading a valid file from another source or tenant is a provenance bug."""
        with self.assertRaises(ValueError):
            load_catalog(FIXTURE_PATH, "tenant-b")

        with tempfile.TemporaryDirectory() as temporary_directory:
            copied_fixture = Path(temporary_directory) / "product-passports.json"
            copied_fixture.write_text(FIXTURE_PATH.read_text(encoding="utf-8"), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_catalog(copied_fixture, "tenant-a")


if __name__ == "__main__":
    unittest.main()
