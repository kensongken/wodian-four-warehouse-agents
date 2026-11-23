import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = ROOT / "scripts" / "validate_approved_product_passports.py"
FIXTURE = ROOT / "fixtures" / "v0.1" / "approved-product-passports-10-sku.json"
SAMPLE = ROOT / "fixtures" / "v0.1" / "mall-approved-product-passport-sample.json"
EVENTS = ROOT / "fixtures" / "v0.1" / "product-passport-review-events.json"
FIXED_FIELDS = {
    "product_passport_id",
    "sku_id",
    "approved_attributes",
    "evidence_refs",
    "taxonomy_version",
    "availability_status",
    "reviewer",
    "version",
}


class ApprovedProductPassportTests(unittest.TestCase):
    def run_validator(self, records):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "approved.json"
            path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
            return subprocess.run(
                [sys.executable, str(VALIDATOR), str(path)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

    def load_records(self):
        return json.loads(FIXTURE.read_text(encoding="utf-8"))

    def assert_rejected(self, records, error_code):
        result = self.run_validator(records)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(error_code, result.stdout + result.stderr)

    def test_ten_synthetic_skus_have_exact_fixed_fields_and_validate(self):
        records = self.load_records()

        self.assertEqual(len(records), 10)
        self.assertEqual(len({record["sku_id"] for record in records}), 10)
        self.assertTrue(all(set(record) == FIXED_FIELDS for record in records))
        result = self.run_validator(records)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("VALID approved_product_passports=10", result.stdout)

    def test_every_fixed_field_is_required(self):
        original = self.load_records()[0]
        for field in FIXED_FIELDS:
            with self.subTest(field=field):
                record = copy.deepcopy(original)
                del record[field]
                self.assert_rejected([record], "required_field_missing")

    def test_only_human_confirmed_available_records_can_reach_mall(self):
        original = self.load_records()[0]

        ai_reviewed = copy.deepcopy(original)
        ai_reviewed["reviewer"]["reviewer_type"] = "ai"
        self.assert_rejected([ai_reviewed], "reviewer_not_human")

        withdrawn = copy.deepcopy(original)
        withdrawn["availability_status"] = "withdrawn"
        self.assert_rejected([withdrawn], "unavailable_product_not_publishable")

        private_evidence = copy.deepcopy(original)
        private_evidence["evidence_refs"][0]["visibility"] = "private"
        self.assert_rejected([private_evidence], "private_evidence_not_publishable")

    def test_automatic_sensitive_decisions_are_never_publishable(self):
        for field in ("material", "quality", "authenticity", "price_advisory"):
            with self.subTest(field=field):
                record = copy.deepcopy(self.load_records()[0])
                record["approved_attributes"][field]["source"] = "ai_inferred"
                self.assert_rejected(
                    [record],
                    f"automatic_{field}_decision_not_publishable",
                )

    def test_mall_sample_is_one_confirmed_record_from_the_ten_sku_fixture(self):
        records = self.load_records()
        sample = json.loads(SAMPLE.read_text(encoding="utf-8"))

        self.assertEqual(sample, records[0])
        self.assertEqual(sample["reviewer"]["reviewer_type"], "human")
        self.assertEqual(sample["availability_status"], "available")

    def test_candidate_confirmation_and_withdrawal_are_separate_and_versioned(self):
        records = self.load_records()
        events = json.loads(EVENTS.read_text(encoding="utf-8"))
        record_ids = {record["product_passport_id"] for record in records}

        for record in records:
            sku_events = [
                event for event in events if event["sku_id"] == record["sku_id"]
            ]
            self.assertEqual(
                [event["event_type"] for event in sku_events],
                ["candidate_created", "human_confirmed"],
            )
            self.assertNotIn("product_passport_id", sku_events[0])
            self.assertEqual(
                sku_events[1]["product_passport_id"], record["product_passport_id"]
            )
            self.assertEqual(sku_events[1]["version"], record["version"])
            self.assertEqual(sku_events[1]["reviewer_type"], "human")

        withdrawn_events = [
            event for event in events if event["sku_id"] == "OPC-WITHDRAWN-DEMO-001"
        ]
        self.assertEqual(
            [event["event_type"] for event in withdrawn_events],
            ["candidate_created", "human_confirmed", "withdrawn"],
        )
        self.assertEqual(
            [event.get("version") for event in withdrawn_events], [None, 1, 2]
        )
        self.assertNotIn(withdrawn_events[1]["product_passport_id"], record_ids)


if __name__ == "__main__":
    unittest.main()
