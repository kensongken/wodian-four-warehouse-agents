import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

WORKER_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKER_ROOT))

from protocol import ProtocolError, build_unconfigured_receipt, validate_job  # noqa: E402


def valid_job(**overrides):
    job = {
        "schemaVersion": 1,
        "jobId": "job-1",
        "customerId": "customer-a",
        "sessionId": "session-1",
        "sourceAssetId": "asset-a",
        "sourceDigest": "a" * 64,
        "sourcePath": "/authorized-media/customer-a/asset-a.mp4",
        "sourceStartMs": 1000,
        "sourceEndMs": 4000,
        "layer": "vision",
        "operationKey": "analyze-asset-a-vision-v1",
        "requestedAt": "2026-09-12T02:00:00.000Z",
    }
    job.update(overrides)
    return job


class ProtocolTest(unittest.TestCase):
    def test_accepts_only_the_exact_job_shape(self):
        self.assertEqual(validate_job(valid_job()), valid_job())

        for unknown_key in ("browserPath", "browserCustomerId", "rawTranscript"):
            with self.subTest(unknown_key=unknown_key):
                with self.assertRaisesRegex(ProtocolError, "^studio-model-worker-invalid-job$"):
                    validate_job(valid_job(**{unknown_key: "private browser value"}))

    def test_rejects_invalid_types_ranges_and_bindings_without_echoing_values(self):
        invalid_jobs = [
            valid_job(schemaVersion=2),
            valid_job(sourceDigest="not-a-digest"),
            valid_job(sourceStartMs=True),
            valid_job(sourceStartMs=-1),
            valid_job(sourceEndMs=1000),
            valid_job(layer="identity"),
            valid_job(layer=[]),
            valid_job(requestedAt="not-a-date"),
            valid_job(requestedAt="2026-99-99T02:00:00Z"),
            valid_job(requestedAt="2026-09-12T10:00:00+08:00"),
            valid_job(sourcePath="   "),
        ]
        for job in invalid_jobs:
            with self.subTest(job=job):
                with self.assertRaisesRegex(ProtocolError, "^studio-model-worker-invalid-job$"):
                    validate_job(job)

    def test_builds_a_strict_source_bound_fallback_receipt(self):
        job = valid_job()
        receipt = build_unconfigured_receipt(
            job,
            started_at="2026-09-12T02:00:01.000Z",
            completed_at="2026-09-12T02:00:01.120Z",
            runtime_ms=120,
        )

        for key in (
            "jobId", "customerId", "sessionId", "sourceAssetId", "sourceDigest",
            "sourcePath", "sourceStartMs", "sourceEndMs", "layer", "operationKey",
        ):
            self.assertEqual(receipt[key], job[key])
        self.assertEqual(receipt["status"], "failed-manual-review")
        self.assertEqual(receipt["providerMode"], "offline-fallback")
        self.assertEqual(receipt["failure"], {"code": "studio-model-adapter-not-configured"})
        self.assertNotIn("confirmedFacts", receipt)

    def test_worker_writes_exactly_one_receipt_line_per_job_line(self):
        jobs = [valid_job(), valid_job(jobId="job-2", operationKey="operation-2")]
        result = subprocess.run(
            [sys.executable, str(WORKER_ROOT / "worker.py")],
            input="".join(json.dumps(job) + "\n" for job in jobs),
            text=True,
            capture_output=True,
            check=False,
            timeout=2,
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        self.assertTrue(result.stdout.endswith("\n"))
        lines = result.stdout.splitlines()
        self.assertEqual(len(lines), 2)
        receipts = [json.loads(line) for line in lines]
        self.assertEqual([receipt["jobId"] for receipt in receipts], ["job-1", "job-2"])

    def test_worker_bootstraps_sibling_modules_under_safe_path(self):
        environment = dict(os.environ)
        environment.pop("PYTHONPATH", None)
        environment.update({
            "PYTHONNOUSERSITE": "1",
            "PYTHONSAFEPATH": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        with TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, str(WORKER_ROOT / "worker.py")],
                input=json.dumps(valid_job()) + "\n",
                text=True,
                capture_output=True,
                check=False,
                cwd=directory,
                env=environment,
                timeout=2,
            )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        self.assertEqual(len(result.stdout.splitlines()), 1)

    def test_worker_rejects_unknown_keys_without_leaking_input(self):
        private_value = "private transcript must not escape"
        result = subprocess.run(
            [sys.executable, str(WORKER_ROOT / "worker.py")],
            input=json.dumps(valid_job(browserPath=private_value)) + "\n",
            text=True,
            capture_output=True,
            check=False,
            timeout=2,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "studio-model-worker-invalid-job\n")
        self.assertNotIn(private_value, result.stderr)


if __name__ == "__main__":
    unittest.main()
