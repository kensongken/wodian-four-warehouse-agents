import copy
import json
import unittest
from pathlib import Path

from sales_contract.catalog import SalesCatalog
from scripts.validate_product_passports import validate_dataset


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = ROOT / "fixtures" / "v0.1" / "product-passports.json"
FIRST_SALE_ID = "10000000-0000-4000-8000-000000000001"
LEGACY_ID = "20000000-0000-4000-8000-000000000001"
REDESIGN_ID = "30000000-0000-4000-8000-000000000001"


class SalesCatalogTests(unittest.TestCase):
    def setUp(self):
        records = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        self.catalog = SalesCatalog.from_records(records, tenant_id="tenant-a")

    def test_resolves_sku_and_rejects_ambiguous_bead(self):
        """Changing SKU lookup or choosing a record for duplicate Bead IDs is a bug."""
        result = self.catalog.resolve("tenant-a", "sku", "OPC-FSB-NH-5MM-001")

        self.assertEqual(result["resolution_status"], "resolved")
        self.assertEqual(result["product_id"], FIRST_SALE_ID)
        self.assertEqual(result["package_version"], 1)
        self.assertEqual(result["resolved_from"], "sku")
        self.assertEqual(
            self.catalog.resolve("tenant-a", "bead_id", "B-2026-000831")[
                "resolution_status"
            ],
            "ambiguous",
        )

    def test_resolves_current_product_id_and_published_redesign(self):
        """Changing product ID matching or rejecting a published redesign is a bug."""
        by_product_id = self.catalog.resolve("tenant-a", "product_id", FIRST_SALE_ID)
        redesign = self.catalog.resolve("tenant-a", "sku", "OPC-RDO-NH-001")

        self.assertEqual(by_product_id["resolution_status"], "resolved")
        self.assertEqual(by_product_id["resolved_from"], "product_id")
        self.assertEqual(redesign["resolution_status"], "resolved")
        self.assertEqual(redesign["product_id"], REDESIGN_ID)

    def test_source_bead_id_never_becomes_a_direct_redesign_alias(self):
        """Treating lineage-only source_bead_id as the redesign's active code is a bug."""
        records = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        source_bead_id = "B-2026-009991"
        records[2]["source_bead_id"] = source_bead_id
        records[2]["media"][1]["bead_id"] = source_bead_id

        catalog = SalesCatalog.from_records(records, tenant_id="tenant-a")
        resolution = catalog.resolve("tenant-a", "bead_id", source_bead_id)

        self.assertEqual(resolution, {"resolution_status": "ambiguous"})

    def test_fails_closed_for_unknown_legacy_and_retired_records(self):
        """Returning sellable facts for an unknown, legacy, or retired record is a bug."""
        records = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        retired = copy.deepcopy(records[0])
        retired["product_id"] = "90000000-0000-4000-8000-000000000001"
        retired["sku"] = "OPC-FSB-NH-RETIRED-001"
        retired["bead_id"] = "B-2026-009999"
        retired["status"] = "retired"
        retired["media"][0]["product_id"] = retired["product_id"]
        retired["media"][0]["bead_id"] = retired["bead_id"]
        retired["sales_journey_projection"]["product_id"] = retired["product_id"]
        catalog = SalesCatalog.from_records(records + [retired], tenant_id="tenant-a")

        self.assertEqual(
            catalog.resolve("tenant-a", "sku", "OPC-UNKNOWN-001")["resolution_status"],
            "not_found",
        )
        self.assertEqual(
            catalog.resolve("tenant-a", "product_id", LEGACY_ID)["resolution_status"],
            "not_sellable",
        )
        self.assertEqual(
            catalog.resolve("tenant-a", "sku", "OPC-FSB-NH-RETIRED-001")[
                "resolution_status"
            ],
            "retired",
        )

    def test_tenant_mismatch_never_discloses_catalog_records(self):
        """Checking the code before tenant binding would disclose another tenant's catalog."""
        resolution = self.catalog.resolve("tenant-b", "sku", "OPC-FSB-NH-5MM-001")
        package = self.catalog.sales_package("tenant-b", FIRST_SALE_ID)

        self.assertEqual(resolution, {"resolution_status": "tenant_mismatch"})
        self.assertEqual(package, {"status": "tenant_mismatch"})

    def test_sales_package_projects_only_sales_safe_fields(self):
        """Copying a raw passport or private media into a sales package is a bug."""
        package = self.catalog.sales_package("tenant-a", FIRST_SALE_ID)
        serialized = json.dumps(package, ensure_ascii=False)

        self.assertEqual(package["audience"], "sales_journey")
        self.assertEqual(package["product_id"], FIRST_SALE_ID)
        self.assertEqual(package["package_version"], 1)
        self.assertEqual(package["evidence_level"], "merchant_declared")
        self.assertEqual(
            package["evidence_ids"],
            ["11000000-0000-4000-8000-000000000001"],
        )
        self.assertEqual(
            package["validity"],
            {
                "valid_from": "2026-08-15T08:00:00Z",
                "valid_until": "2099-12-31T23:59:59Z",
            },
        )
        self.assertEqual(
            package["forbidden_claims"],
            ["不得承诺保值、升值或投资收益", "不得承诺固定回购价格"],
        )
        self.assertEqual(package["product_passport"]["sku"], "OPC-FSB-NH-5MM-001")
        self.assertEqual(
            package["product_passport"]["conservative_price_range"],
            {
                "currency": "CNY",
                "minimum": "1200.00",
                "maximum": "1500.00",
                "basis": "基于已批准的商品规格与销售策略",
                "evidence_level": "merchant_declared",
                "evidence_ids": ["11000000-0000-4000-8000-000000000001"],
            },
        )
        self.assertEqual(
            package["product_passport"]["public_media"],
            [
                {
                    "media_id": "11000000-0000-4000-8000-000000000001",
                    "public_url": "https://fixtures.opc.example/products/fsb-001/main.jpg",
                }
            ],
        )
        for forbidden in (
            "bead_id",
            "object_key",
            "media_kind",
            "captured_at",
            "customer_return_photo",
            "warehouse_acceptance_photo",
            "source_ticket_id",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_sales_package_rejects_non_sellable_product_ids(self):
        """Projecting a legacy record as a sales package is a bug."""
        self.assertEqual(
            self.catalog.sales_package("tenant-a", LEGACY_ID),
            {"status": "not_sellable"},
        )
        self.assertEqual(
            self.catalog.sales_package("tenant-a", "unknown-product"),
            {"status": "not_found"},
        )

    def test_expired_projection_is_not_sellable(self):
        """Resolving or projecting an expired approved package is a sales safety bug."""
        records = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        records[0]["sales_journey_projection"]["validity"]["valid_until"] = (
            "2026-08-16T08:00:00Z"
        )
        catalog = SalesCatalog.from_records(records, tenant_id="tenant-a")

        self.assertEqual(
            catalog.resolve("tenant-a", "sku", "OPC-FSB-NH-5MM-001"),
            {"resolution_status": "not_sellable"},
        )
        self.assertEqual(
            catalog.sales_package("tenant-a", FIRST_SALE_ID),
            {"status": "not_sellable"},
        )

    def test_redesign_projection_removes_private_operational_text(self):
        """Passing source-ticket or warehouse text through a safe field is a bug."""
        serialized = json.dumps(
            self.catalog.sales_package("tenant-a", REDESIGN_ID), ensure_ascii=False
        )

        for forbidden_text in (
            "source_ticket",
            "warehouse_acceptance_photo",
            "工单",
            "仓验",
            "私有",
        ):
            self.assertNotIn(forbidden_text, serialized)

    def test_projects_only_explicitly_approved_sales_text(self):
        """Using raw customer/order display values instead of the projection is a bug."""
        records = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        records[0]["title"] = "客户订单原始标题"
        records[0]["service_rules"][0]["customer_summary"] = "customer order note"
        records[0]["sku"] = "order-987"
        records[0]["media"][0]["public_url"] = "https://customer@example.com/private.jpg"
        projection = records[0]["sales_journey_projection"]
        projection["sku"] = {"text": "OPC-SAFE-001", "approval_status": "approved"}
        projection["title"] = {"text": "已批准对客标题", "approval_status": "approved"}
        projection["description"] = {
            "text": "已批准对客介绍",
            "approval_status": "approved",
        }
        projection["selling_points"] = [
            {"text": "已批准卖点", "approval_status": "approved"}
        ]
        projection["service_summary"] = [
            {"text": "已批准服务摘要", "approval_status": "approved"}
        ]
        projection["public_media"][0]["public_url"] = {
            "text": "https://fixtures.opc.example/sales-safe/main.jpg",
            "approval_status": "approved",
        }
        validate_dataset(records)

        catalog = SalesCatalog.from_records(records, tenant_id="tenant-a")
        package = catalog.sales_package("tenant-a", FIRST_SALE_ID)
        serialized = json.dumps(package, ensure_ascii=False)

        self.assertEqual(
            catalog.resolve("tenant-a", "sku", "order-987")["product_id"], FIRST_SALE_ID
        )
        self.assertEqual(package["product_passport"]["sku"], "OPC-SAFE-001")
        self.assertEqual(package["product_passport"]["title"], "已批准对客标题")
        self.assertEqual(package["product_passport"]["service_summary"], ["已批准服务摘要"])
        self.assertEqual(
            package["product_passport"]["public_media"][0]["public_url"],
            "https://fixtures.opc.example/sales-safe/main.jpg",
        )
        self.assertNotIn("客户订单原始标题", serialized)
        self.assertNotIn("customer order note", serialized)
        self.assertNotIn("order-987", serialized)
        self.assertNotIn("customer@example.com", serialized)

    def test_rejects_unapproved_display_sku_or_media_url(self):
        """Loading a display SKU or image URL without approval is a bug."""
        records = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        records[0]["sales_journey_projection"]["sku"]["approval_status"] = "draft"

        with self.assertRaises(ValueError):
            SalesCatalog.from_records(records, tenant_id="tenant-a")

    def test_rejects_forbidden_promises_and_unsafe_urls_inside_approved_projection(self):
        """Approval metadata must not bypass promise or URL safety policy."""
        records = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        records[0]["sales_journey_projection"]["description"]["text"] = (
            "购买后保证升值"
        )
        with self.assertRaises(ValueError):
            SalesCatalog.from_records(records, tenant_id="tenant-a")

        records = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        records[0]["sales_journey_projection"]["conservative_price_range"]["basis"][
            "text"
        ] = "此价格保证保值"
        with self.assertRaises(ValueError):
            SalesCatalog.from_records(records, tenant_id="tenant-a")

        records = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        records[0]["sales_journey_projection"]["public_media"][0]["public_url"][
            "text"
        ] = "javascript:alert(1)"
        with self.assertRaises(ValueError):
            SalesCatalog.from_records(records, tenant_id="tenant-a")

    def test_approved_projection_requires_media_ids_to_reach_the_package(self):
        """Keeping evidence IDs while omitting every approved media anchor is a bug."""
        records = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        records[0]["sales_journey_projection"]["public_media"] = []

        with self.assertRaises(ValueError):
            SalesCatalog.from_records(records, tenant_id="tenant-a")

        records = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        records[0]["sales_journey_projection"]["public_media"][0]["public_url"][
            "approval_status"
        ] = "draft"

        with self.assertRaises(ValueError):
            SalesCatalog.from_records(records, tenant_id="tenant-a")

    def test_rejects_media_urls_that_fail_complete_passport_validation(self):
        """Loading a non-string or malformed public URL is a validation boundary bug."""
        for public_url in (123, "not-a-uri"):
            with self.subTest(public_url=public_url):
                records = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
                records[0]["media"][0]["public_url"] = public_url

                with self.assertRaises(ValueError):
                    SalesCatalog.from_records(records, tenant_id="tenant-a")

    def test_rejects_records_that_have_not_passed_product_passport_validation(self):
        """Loading an arbitrary record without upstream validation is a bug."""
        with self.assertRaises(ValueError):
            SalesCatalog.from_records(
                [{"product_id": FIRST_SALE_ID}], tenant_id="tenant-a"
            )

    def test_rejects_malformed_contract_shapes_at_catalog_load(self):
        """A malformed root, media item, or zero version must not raise a raw Python error."""
        records = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        records[0]["package_version"] = 0
        with self.assertRaises(ValueError):
            SalesCatalog.from_records(records, tenant_id="tenant-a")

        with self.assertRaises(ValueError):
            SalesCatalog.from_records([["not-a-product-passport"]], tenant_id="tenant-a")

        records = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        records[0]["media"] = [123]
        with self.assertRaises(ValueError):
            SalesCatalog.from_records(records, tenant_id="tenant-a")


if __name__ == "__main__":
    unittest.main()
