import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "scripts" / "build_candidate_consumer_views.py"
SOURCE_FIXTURE = ROOT / "fixtures" / "v0.1" / "candidate-batch-10-sku-output.json"
OUTPUT_FIXTURE = ROOT / "fixtures" / "v0.1" / "candidate-consumer-views-6-sku.json"
SCHEMA = ROOT / "contracts" / "v0.1" / "candidate-consumer-views.schema.json"


class CandidateConsumerViewsTests(unittest.TestCase):
    def run_builder(self, payload=None):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            input_path = SOURCE_FIXTURE
            if payload is not None:
                input_path = temp_root / "input.json"
                input_path.write_text(
                    json.dumps(payload, ensure_ascii=False), encoding="utf-8"
                )
            output_path = temp_root / "output.json"
            result = subprocess.run(
                [
                    sys.executable,
                    str(BUILDER),
                    str(input_path),
                    "--output",
                    str(output_path),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            output = (
                json.loads(output_path.read_text(encoding="utf-8"))
                if output_path.exists()
                else None
            )
            return result, output

    def source_payload(self):
        return json.loads(SOURCE_FIXTURE.read_text(encoding="utf-8"))

    def first_candidate(self, payload):
        return next(
            item["candidate"]
            for item in payload["results"]
            if item["outcome"] == "passed"
        )

    def test_builds_six_stable_mall_and_sales_views(self):
        result, output = self.run_builder()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(
            output,
            json.loads(OUTPUT_FIXTURE.read_text(encoding="utf-8")),
        )
        self.assertEqual(
            output["summary"], {"items": 6, "mall_views": 6, "sales_views": 6}
        )

    def test_both_consumers_keep_candidates_out_of_conclusion_fields(self):
        _, output = self.run_builder()

        for view_name in ("mall_views", "sales_views"):
            self.assertEqual(len(output[view_name]), 6)
            for view in output[view_name]:
                self.assertEqual(
                    view["category_candidate"]["semantics"],
                    "catalog_candidate_not_material_conclusion",
                )
                self.assertFalse(view["category_candidate"]["conclusion_allowed"])
                self.assertTrue(view["category_candidate"]["human_review_required"])
                self.assertEqual(
                    view["price_advisory"]["semantics"],
                    "advisory_range_not_fixed_price",
                )
                self.assertFalse(view["price_advisory"]["final_price_allowed"])
                self.assertTrue(view["price_advisory"]["human_review_required"])
                self.assertEqual(
                    view["verification_boundaries"],
                    {
                        "authenticity": "not_verified",
                        "origin": "not_verified",
                        "quality": "not_verified",
                        "warehouse": "pending_warehouse_verification",
                    },
                )
                self.assertTrue(view["candidate_only"])
                self.assertFalse(view["automatic_publication_allowed"])

        for view in output["sales_views"]:
            self.assertEqual(view["usage"], "draft_support_only")
            self.assertEqual(
                view["conclusions_allowed"],
                {
                    "authenticity": False,
                    "final_price": False,
                    "material": False,
                    "quality": False,
                    "warehouse": False,
                },
            )

    def test_consumer_gate_rejects_boundary_upgrades_before_output(self):
        mutations = (
            (
                "automatic_publication_forbidden",
                lambda candidate: candidate.__setitem__(
                    "automatic_publication_allowed", True
                ),
            ),
            (
                "candidate_flag_required",
                lambda candidate: candidate.__setitem__("candidate_only", False),
            ),
            (
                "fixed_or_final_price_forbidden",
                lambda candidate: candidate["price_advisory"].__setitem__(
                    "semantics", "fixed_price"
                ),
            ),
            (
                "verification_conclusion_forbidden",
                lambda candidate: candidate["verification_boundaries"].__setitem__(
                    "authenticity", "verified"
                ),
            ),
            (
                "material_conclusion_forbidden",
                lambda candidate: candidate.__setitem__(
                    "material_conclusion", "天然绿松石"
                ),
            ),
            (
                "source_version_invalid",
                lambda candidate: candidate.__setitem__("source_version", 0),
            ),
            (
                "verification_conclusion_forbidden",
                lambda candidate: candidate["selling_point_candidates"].append(
                    "经 鉴 定 为 天然Ａ货"
                ),
            ),
            (
                "verification_conclusion_forbidden",
                lambda candidate: candidate["selling_point_candidates"].append(
                    "品 质 顶 级"
                ),
            ),
            (
                "verification_conclusion_forbidden",
                lambda candidate: candidate["selling_point_candidates"].append(
                    "已 通 过 仓 库 验 收"
                ),
            ),
            (
                "fixed_or_final_price_forbidden",
                lambda candidate: candidate["selling_point_candidates"].append(
                    "固 定 价 格１２００元"
                ),
            ),
            (
                "material_conclusion_forbidden",
                lambda candidate: candidate["selling_point_candidates"].append(
                    "材 质 确 认 绿松石"
                ),
            ),
            (
                "customer_contact_forbidden",
                lambda candidate: candidate["selling_point_candidates"].append(
                    "联系微信：designer_123"
                ),
            ),
        )

        for expected_error, mutate in mutations:
            with self.subTest(expected_error=expected_error):
                payload = copy.deepcopy(self.source_payload())
                mutate(self.first_candidate(payload))
                result, output = self.run_builder(payload)

                self.assertEqual(result.returncode, 1)
                self.assertIsNone(output)
                self.assertIn(f"INVALID error={expected_error}", result.stdout)

    def test_output_is_explicitly_synthetic_and_non_operating(self):
        _, output = self.run_builder()
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))

        self.assertEqual(output["evidence_scope"], "synthetic_technical_readiness_only")
        self.assertEqual(output["operating_results"], 0)
        self.assertEqual(output["source_contract"], "candidate-batch-0.1")
        self.assertEqual(schema["$id"], "candidate-consumer-views.schema.json")
        self.assertEqual(schema["properties"]["operating_results"]["const"], 0)

    def test_sales_schema_directly_allows_every_generated_sales_field(self):
        _, output = self.run_builder()
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        sales_schema = schema["$defs"]["sales_view"]

        self.assertFalse(sales_schema["additionalProperties"])
        self.assertEqual(set(sales_schema["properties"]), set(output["sales_views"][0]))


if __name__ == "__main__":
    unittest.main()
