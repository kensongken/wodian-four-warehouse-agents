import copy
import json
import unittest
from pathlib import Path

from apps.reply_workbench.engine import ValidationError, generate_drafts


SAMPLES = Path(__file__).resolve().parents[1] / "samples"


def load_sample(name):
    with (SAMPLES / name).open(encoding="utf-8") as handle:
        return json.load(handle)


class DraftEngineTest(unittest.TestCase):
    def test_identity_bead_sample_generates_evidenced_copy_only_drafts(self):
        result = generate_drafts(load_sample("identity_bead.json"))

        self.assertEqual("draft_ready", result["status"])
        self.assertGreaterEqual(len(result["drafts"]), 1)
        self.assertLessEqual(len(result["drafts"]), 3)
        self.assertTrue(result["drafts"][0]["cited_evidence"])
        self.assertTrue(result["manual_review_required"])
        self.assertFalse(result["channel_policy"]["wechat_auto_send_allowed"])
        self.assertEqual(["copy", "export"], result["channel_policy"]["allowed_actions"])

    def test_scan_service_sample_exposes_risk_and_manual_confirmation(self):
        result = generate_drafts(load_sample("scan_service.json"))

        self.assertEqual("draft_ready", result["status"])
        self.assertTrue(result["drafts"][0]["cited_evidence"])
        self.assertIn("扫码记录不是法定鉴定结论", result["drafts"][0]["risk_warnings"])
        self.assertEqual(
            ["确认页面版本与客户看到的一致"],
            result["drafts"][0]["manual_confirmation_reasons"],
        )

    def test_pending_legacy_valuation_and_repurchase_request_is_blocked(self):
        result = generate_drafts(load_sample("pending_legacy.json"))

        self.assertEqual("blocked", result["status"])
        self.assertEqual(1, len(result["drafts"]))
        self.assertIn("warehouse_pending", result["risk_codes"])
        self.assertIn("仓库", result["drafts"][0]["text"])
        self.assertNotIn("一定可以", result["drafts"][0]["text"])
        self.assertFalse(result["channel_policy"]["wechat_auto_send_allowed"])

    def test_unknown_nested_field_is_rejected_before_generation(self):
        payload = copy.deepcopy(load_sample("identity_bead.json"))
        payload["product_passport"]["customer_phone"] = "13800138000"

        with self.assertRaises(ValidationError) as caught:
            generate_drafts(payload)

        self.assertEqual("unknown_field", caught.exception.code)


if __name__ == "__main__":
    unittest.main()
