import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = ROOT / "scripts" / "validate_product_passports.py"


def first_sale_record():
    product_id = "10000000-0000-4000-8000-000000000001"
    return {
        "product_id": product_id,
        "sku": "OPC-FSB-NH-5MM-001",
        "type": "first_sale_bead",
        "status": "published",
        "title": "柿子红南红身份珠 5mm",
        "category": "南红",
        "form": "身份珠",
        "bead_id": "B-2026-000831",
        "source_bead_id": None,
        "source_ticket_id": None,
        "identity_status": "issued",
        "origin": "first_sale",
        "service_rules": [
            {
                "service_code": "scan_identity",
                "eligibility": "eligible",
                "customer_summary": "购买后可扫码进入身份与服务页面",
            }
        ],
        "sales_journey_projection": approved_sales_projection(),
        "evidence_level": "merchant_declared",
        "conservative_price_range": {
            "currency": "CNY",
            "minimum": "1200.00",
            "maximum": "1500.00",
            "basis": "商家标准图、商品规格与销售策略",
            "evidence_level": "merchant_declared",
        },
        "selling_points": ["身份珠扫码可发起服务"],
        "description": "身份珠首售商品，支持扫码进入身份与服务页面。",
        "media": [
            {
                "media_id": "11000000-0000-4000-8000-000000000001",
                "media_kind": "product_photo",
                "object_key": "products/10000000/main.jpg",
                "public_url": "https://fixtures.opc.example/products/fsb-001/main.jpg",
                "sha256": "a" * 64,
                "product_id": product_id,
                "bead_id": "B-2026-000831",
                "ticket_id": None,
                "evidence_level": "image_observed",
                "visibility": "public",
                "authorization_status": "merchant_owned",
                "captured_at": "2026-08-15T08:00:00Z",
            }
        ],
        "claims": [],
        "package_version": 1,
        "updated_at": "2026-08-15T08:00:00Z",
    }


def approved_sales_projection():
    return {
        "product_id": "10000000-0000-4000-8000-000000000001",
        "package_version": 1,
        "sku": {"text": "OPC-FSB-NH-5MM-001", "approval_status": "approved"},
        "title": {"text": "柿子红南红身份珠 5mm", "approval_status": "approved"},
        "category": {"text": "南红", "approval_status": "approved"},
        "form": {"text": "身份珠", "approval_status": "approved"},
        "description": {
            "text": "身份珠首售商品，支持扫码进入身份与服务页面。",
            "approval_status": "approved",
        },
        "selling_points": [{"text": "身份珠扫码可发起服务", "approval_status": "approved"}],
        "service_summary": [
            {
                "text": "购买后可扫码进入身份与服务页面",
                "approval_status": "approved",
            }
        ],
        "evidence_level": "merchant_declared",
        "evidence_ids": ["11000000-0000-4000-8000-000000000001"],
        "conservative_price_range": {
            "currency": "CNY",
            "minimum": "1200.00",
            "maximum": "1500.00",
            "basis": {
                "text": "基于已批准的商品规格与销售策略",
                "approval_status": "approved",
            },
            "evidence_level": "merchant_declared",
            "evidence_ids": ["11000000-0000-4000-8000-000000000001"],
        },
        "validity": {
            "valid_from": "2026-08-15T08:00:00Z",
            "valid_until": "2099-12-31T23:59:59Z",
        },
        "forbidden_claims": [
            {
                "text": "不得承诺保值、升值或投资收益",
                "approval_status": "approved",
            },
            {"text": "不得承诺固定回购价格", "approval_status": "approved"},
        ],
        "public_media": [
            {
                "media_id": "11000000-0000-4000-8000-000000000001",
                "public_url": {
                    "text": "https://fixtures.opc.example/products/fsb-001/main.jpg",
                    "approval_status": "approved",
                },
            }
        ],
    }


def redesign_record():
    product_id = "30000000-0000-4000-8000-000000000001"
    ticket_id = "40000000-0000-4000-8000-000000000001"
    return {
        "product_id": product_id,
        "sku": "OPC-RDO-NH-001",
        "type": "redesign_output",
        "status": "published",
        "title": "南红旧珠焕新手串",
        "category": "南红",
        "form": "改款成品",
        "bead_id": "B-2026-001092",
        "source_bead_id": "B-2026-000831",
        "source_ticket_id": ticket_id,
        "identity_status": "reissued",
        "origin": "redesign",
        "service_rules": [
            {
                "service_code": "scan_identity",
                "eligibility": "eligible",
                "customer_summary": "扫码可查看本次改款服务谱系摘要",
            }
        ],
        "sales_journey_projection": {
            "product_id": product_id,
            "package_version": 1,
            "sku": {"text": "OPC-RDO-NH-001", "approval_status": "approved"},
            "title": {"text": "南红旧珠焕新手串", "approval_status": "approved"},
            "category": {"text": "南红", "approval_status": "approved"},
            "form": {"text": "改款成品", "approval_status": "approved"},
            "description": {
                "text": "改款成品签发新的 Bead ID，支持扫码查看服务摘要。",
                "approval_status": "approved",
            },
            "selling_points": [{"text": "改款成品签发新的 Bead ID", "approval_status": "approved"}],
            "service_summary": [
                {
                    "text": "扫码可查看本次改款服务谱系摘要",
                    "approval_status": "approved",
                }
            ],
            "evidence_level": "warehouse_verified",
            "evidence_ids": ["31000000-0000-4000-8000-000000000001"],
            "conservative_price_range": {
                "currency": "CNY",
                "minimum": "1200.00",
                "maximum": "1500.00",
                "basis": {
                    "text": "基于已批准的改款工艺与成品状态",
                    "approval_status": "approved",
                },
                "evidence_level": "warehouse_verified",
                "evidence_ids": ["31000000-0000-4000-8000-000000000001"],
            },
            "validity": {
                "valid_from": "2026-08-15T10:00:00Z",
                "valid_until": "2099-12-31T23:59:59Z",
            },
            "forbidden_claims": [
                {
                    "text": "不得承诺保值、升值或投资收益",
                    "approval_status": "approved",
                },
                {"text": "不得承诺固定回购价格", "approval_status": "approved"},
            ],
            "public_media": [
                {
                    "media_id": "31000000-0000-4000-8000-000000000001",
                    "public_url": {
                        "text": "https://fixtures.opc.example/products/rdo-001/output.jpg",
                        "approval_status": "approved",
                    },
                }
            ],
        },
        "evidence_level": "warehouse_verified",
        "conservative_price_range": {
            "currency": "CNY",
            "minimum": "1200.00",
            "maximum": "1500.00",
            "basis": "仓验通过的来源记录、改款工艺与成品状态",
            "evidence_level": "warehouse_verified",
        },
        "selling_points": ["保留旧珠来源工单与仓验影像谱系"],
        "description": "旧珠经仓库验收后完成改款，并签发新的 Bead ID。",
        "media": [
            {
                "media_id": "31000000-0000-4000-8000-000000000001",
                "media_kind": "redesign_output_photo",
                "object_key": "products/30000000/output.jpg",
                "public_url": "https://fixtures.opc.example/products/rdo-001/output.jpg",
                "sha256": "b" * 64,
                "product_id": product_id,
                "bead_id": "B-2026-001092",
                "ticket_id": ticket_id,
                "evidence_level": "image_observed",
                "visibility": "public",
                "authorization_status": "merchant_owned",
                "captured_at": "2026-08-15T10:00:00Z",
            },
            {
                "media_id": "41000000-0000-4000-8000-000000000001",
                "media_kind": "warehouse_acceptance_photo",
                "object_key": "tickets/40000000/warehouse.jpg",
                "public_url": None,
                "sha256": "c" * 64,
                "product_id": "20000000-0000-4000-8000-000000000001",
                "bead_id": "B-2026-000831",
                "ticket_id": ticket_id,
                "evidence_level": "warehouse_verified",
                "visibility": "private",
                "authorization_status": "warehouse_authorized",
                "captured_at": "2026-08-15T09:00:00Z",
            },
        ],
        "claims": [],
        "package_version": 1,
        "updated_at": "2026-08-15T10:00:00Z",
    }


class ValidatorContractTests(unittest.TestCase):
    def run_validator_raw(self, raw_json):
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "records.json"
            input_path.write_text(raw_json, encoding="utf-8")
            return subprocess.run(
                [sys.executable, str(VALIDATOR), str(input_path)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

    def run_validator(self, records):
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "records.json"
            input_path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
            return subprocess.run(
                [sys.executable, str(VALIDATOR), str(input_path)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

    def assert_rejected(self, records, error_code):
        result = self.run_validator(records)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(error_code, result.stdout + result.stderr)

    def test_valid_first_sale_and_redesign_are_accepted(self):
        result = self.run_validator([first_sale_record(), redesign_record()])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("VALID records=2", result.stdout)

    def test_sellable_product_requires_approved_sales_projection(self):
        record = first_sale_record()
        del record["sales_journey_projection"]
        self.assert_rejected([record], "schema_validation_failed")

    def test_unapproved_sales_text_is_rejected(self):
        record = first_sale_record()
        record["sales_journey_projection"]["title"]["approval_status"] = "draft"
        self.assert_rejected([record], "schema_validation_failed")

    def test_unapproved_display_sku_or_media_url_is_rejected(self):
        record = first_sale_record()
        record["sales_journey_projection"]["sku"]["approval_status"] = "draft"
        self.assert_rejected([record], "schema_validation_failed")

        record = first_sale_record()
        record["sales_journey_projection"]["public_media"][0]["public_url"]["approval_status"] = (
            "draft"
        )
        self.assert_rejected([record], "schema_validation_failed")

    def test_approved_projection_still_rejects_promises_and_unsafe_urls(self):
        record = first_sale_record()
        record["sales_journey_projection"]["description"]["text"] = "购买后保证升值"
        self.assert_rejected([record], "forbidden_value_promise")

        record = first_sale_record()
        record["sales_journey_projection"]["public_media"][0]["public_url"]["text"] = (
            "javascript:alert(1)"
        )
        self.assert_rejected([record], "invalid_sales_projection_url")

    def test_invalid_media_public_url_is_rejected(self):
        record = first_sale_record()
        record["media"][0]["public_url"] = 123
        self.assert_rejected([record], "schema_validation_failed")

        record = first_sale_record()
        record["media"][0]["public_url"] = "not-a-uri"
        self.assert_rejected([record], "schema_validation_failed")

    def test_contract_shape_fails_closed_before_business_validation(self):
        record = first_sale_record()
        record["package_version"] = 0
        self.assert_rejected([record], "schema_validation_failed")

        self.assert_rejected([["not-a-product-passport"]], "schema_validation_failed")

        record = first_sale_record()
        record["media"] = [123]
        self.assert_rejected([record], "schema_validation_failed")

    def test_contract_requires_canonical_uuid_and_utc_datetime_text(self):
        for product_id in (
            "AAAAAAAA-0000-4000-8000-000000000001",
            "10000000000040008000000000000001",
        ):
            with self.subTest(product_id=product_id):
                record = first_sale_record()
                record["product_id"] = product_id
                record["media"][0]["product_id"] = product_id
                record["sales_journey_projection"]["product_id"] = product_id
                self.assert_rejected([record], "schema_validation_failed")

        for updated_at in (
            "2026-08-15 08:00:00+00:00",
            "2026-08-15T08:00:00+00:00",
            "2026-08-15",
        ):
            with self.subTest(updated_at=updated_at):
                record = first_sale_record()
                record["updated_at"] = updated_at
                self.assert_rejected([record], "schema_validation_failed")

    def test_contract_json_rejects_duplicate_object_keys(self):
        raw_json = json.dumps([first_sale_record()], ensure_ascii=False)
        raw_json = raw_json.replace(
            '"sku": "OPC-FSB-NH-5MM-001"',
            '"sku": "OPC-DUPLICATE", "sku": "OPC-FSB-NH-5MM-001"',
            1,
        )

        result = self.run_validator_raw(raw_json)

        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("duplicate_json_key", result.stdout + result.stderr)

    def test_invalid_type_is_rejected(self):
        record = first_sale_record()
        record["type"] = "ordinary_jewelry"
        self.assert_rejected([record], "schema_validation_failed")

    def test_unauthorized_customer_photo_is_rejected(self):
        record = first_sale_record()
        record["media"][0].update(
            {
                "media_kind": "customer_return_photo",
                "ticket_id": "40000000-0000-4000-8000-000000000001",
                "visibility": "private",
                "authorization_status": "authorization_unknown",
            }
        )
        self.assert_rejected([record], "schema_validation_failed")

    def test_redesign_without_source_ticket_is_rejected(self):
        record = redesign_record()
        record["source_ticket_id"] = None
        self.assert_rejected([record], "schema_validation_failed")

    def test_redesign_must_issue_new_bead_id(self):
        record = redesign_record()
        record["bead_id"] = record["source_bead_id"]
        record["media"][0]["bead_id"] = record["bead_id"]
        self.assert_rejected([record], "redesign_requires_new_bead_id")

    def test_one_to_many_redesign_is_out_of_scope(self):
        first = redesign_record()
        second = copy.deepcopy(first)
        second["product_id"] = "30000000-0000-4000-8000-000000000002"
        second["sku"] = "OPC-RDO-NH-002"
        second["bead_id"] = "B-2026-001093"
        second["media"][0]["product_id"] = second["product_id"]
        second["media"][0]["bead_id"] = second["bead_id"]
        second["media"][0]["media_id"] = "31000000-0000-4000-8000-000000000002"
        second["sales_journey_projection"]["product_id"] = second["product_id"]
        second["sales_journey_projection"]["public_media"][0]["media_id"] = second["media"][0][
            "media_id"
        ]
        second["sales_journey_projection"]["evidence_ids"] = [second["media"][0]["media_id"]]
        second["sales_journey_projection"]["conservative_price_range"]["evidence_ids"] = [
            second["media"][0]["media_id"]
        ]
        self.assert_rejected([first, second], "redesign_one_to_many_not_supported")

    def test_image_content_cannot_promise_authenticity_origin_or_value(self):
        for forbidden_claim in ("保真", "产自保山", "保证升值"):
            with self.subTest(forbidden_claim=forbidden_claim):
                record = first_sale_record()
                record["selling_points"] = [forbidden_claim]
                self.assert_rejected([record], "forbidden_unsupported_claim")

    def test_equal_price_bounds_are_rejected_as_fixed_price_promise(self):
        record = first_sale_record()
        record["conservative_price_range"]["maximum"] = "1200.00"
        self.assert_rejected([record], "fixed_price_promise_not_supported")


class FixtureTests(unittest.TestCase):
    def run_fixture_validator(self, filename):
        return subprocess.run(
            [
                sys.executable,
                str(VALIDATOR),
                str(ROOT / "fixtures" / "v0.1" / filename),
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_canonical_fixture_has_exactly_three_product_types(self):
        path = ROOT / "fixtures" / "v0.1" / "product-passports.json"
        records = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(
            {record["type"] for record in records},
            {"first_sale_bead", "legacy_material", "redesign_output"},
        )
        self.assertEqual(len(records), 3)

    def test_canonical_fixture_is_pinned_synthetic_provider_input(self):
        fixture_path = ROOT / "fixtures" / "v0.1" / "product-passports.json"
        manifest_path = ROOT / "fixtures" / "v0.1" / "synthetic-product-passports.manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(manifest["data_classification"], "synthetic")
        self.assertEqual(manifest["catalog"], "fixtures/v0.1/product-passports.json")
        self.assertEqual(manifest["tenant_id"], "tenant-a")
        self.assertEqual(hashlib.sha256(fixture_path.read_bytes()).hexdigest(), manifest["sha256"])

        result = self.run_fixture_validator("product-passports.json")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("VALID records=3", result.stdout)

    def test_mall_import_fixture_contains_ten_sellable_skus(self):
        path = ROOT / "fixtures" / "v0.1" / "mall-import-10-sku.json"
        records = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(len(records), 10)
        self.assertTrue(all(record["type"] != "legacy_material" for record in records))
        self.assertTrue(all(record["status"] == "published" for record in records))
        self.assertTrue(all("lineage_image_refs" not in record for record in records))
        self.assertTrue(
            all(
                record["lineage_evidence_available"] is (record["type"] == "redesign_output")
                for record in records
            )
        )

    def test_mall_import_json_and_csv_both_pass_validator(self):
        for filename in ("mall-import-10-sku.json", "mall-import-10-sku.csv"):
            with self.subTest(filename=filename):
                result = self.run_fixture_validator(filename)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("VALID records=10", result.stdout)

    def test_content_package_contract_is_frozen_for_mall_and_sales(self):
        path = ROOT / "contracts" / "v0.1" / "product-passport-content-package.freeze.json"
        contract = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(contract["version"], "0.1.0")
        self.assertEqual(contract["status"], "frozen")
        self.assertEqual(
            set(contract["product_types"]),
            {"first_sale_bead", "legacy_material", "redesign_output"},
        )
        self.assertIn("mall", contract["audiences"])
        self.assertIn("sales_guidance", contract["audiences"])
        self.assertEqual(contract["price_semantics"]["kind"], "advisory_range")


if __name__ == "__main__":
    unittest.main()
