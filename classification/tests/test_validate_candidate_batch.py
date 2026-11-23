import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = ROOT / "scripts" / "validate_candidate_batch.py"
INPUT_FIXTURE = ROOT / "fixtures" / "v0.1" / "candidate-batch-10-sku-input.json"
OUTPUT_FIXTURE = ROOT / "fixtures" / "v0.1" / "candidate-batch-10-sku-output.json"
SCHEMA = ROOT / "contracts" / "v0.1" / "candidate-batch-output.schema.json"


class CandidateBatchValidatorTests(unittest.TestCase):
    def run_validator(self, input_path=INPUT_FIXTURE):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "output.json"
            result = subprocess.run(
                [
                    sys.executable,
                    str(VALIDATOR),
                    str(input_path),
                    "--output",
                    str(output_path),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            payload = (
                json.loads(output_path.read_text(encoding="utf-8"))
                if output_path.exists()
                else None
            )
            return result, payload

    def test_ten_synthetic_cases_match_committed_batch_output(self):
        result, payload = self.run_validator()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("VALID synthetic_cases=10 passed=6 rejected=4", result.stdout)
        self.assertEqual(
            payload,
            json.loads(OUTPUT_FIXTURE.read_text(encoding="utf-8")),
        )

    def test_six_categories_pass_with_guarded_candidate_boundaries(self):
        _, payload = self.run_validator()
        passed = [item for item in payload["results"] if item["outcome"] == "passed"]

        self.assertEqual(len(passed), 6)
        self.assertEqual(
            {item["candidate"]["category_code"] for item in passed},
            {
                "agarwood",
                "turquoise",
                "nanhong",
                "hetian_jade",
                "colored_gemstone",
                "jadeite",
            },
        )
        for item in passed:
            candidate = item["candidate"]
            self.assertTrue(candidate["candidate_only"])
            self.assertFalse(candidate["automatic_publication_allowed"])
            self.assertEqual(
                candidate["price_advisory"]["semantics"],
                "advisory_range_not_fixed_price",
            )
            self.assertEqual(
                candidate["verification_boundaries"]["authenticity"], "not_verified"
            )
            self.assertEqual(
                candidate["verification_boundaries"]["origin"], "not_verified"
            )
            self.assertEqual(
                candidate["verification_boundaries"]["quality"], "not_verified"
            )
            self.assertEqual(
                candidate["verification_boundaries"]["warehouse"],
                "pending_warehouse_verification",
            )

    def test_four_negative_cases_fail_closed_with_expected_codes(self):
        _, payload = self.run_validator()
        rejected = {
            item["case_id"]: item["error_code"]
            for item in payload["results"]
            if item["outcome"] == "rejected"
        }

        self.assertEqual(
            rejected,
            {
                "SYN-REJECT-MAIN-001": "main_image_missing",
                "SYN-REJECT-PRICE-001": "invalid_price_range",
                "SYN-REJECT-CLAIM-001": "forbidden_unsupported_claim",
                "SYN-REJECT-VERSION-001": "source_version_mismatch",
            },
        )

    def test_fixture_and_schema_are_explicitly_synthetic_and_non_operating(self):
        input_payload = json.loads(INPUT_FIXTURE.read_text(encoding="utf-8"))
        output_payload = json.loads(OUTPUT_FIXTURE.read_text(encoding="utf-8"))
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))

        self.assertEqual(
            input_payload["evidence_scope"], "synthetic_technical_readiness_only"
        )
        self.assertEqual(
            output_payload["evidence_scope"], "synthetic_technical_readiness_only"
        )
        self.assertEqual(output_payload["operating_results"], 0)
        self.assertEqual(schema["$id"], "candidate-batch-output.schema.json")
        self.assertEqual(schema["properties"]["operating_results"]["const"], 0)


if __name__ == "__main__":
    unittest.main()
