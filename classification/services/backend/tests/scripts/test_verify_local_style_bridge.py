import json
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "scripts" / "verify_local_style_bridge.py"
CANDIDATE = b"synthetic-candidate-image"
CANDIDATE_SHA = sha256(CANDIDATE).hexdigest()
JOB_ID = "11111111-1111-4111-8111-111111111111"
CANDIDATE_ID = "22222222-2222-4222-8222-222222222222"


@contextmanager
def bridge_servers(*, advertised_sha: str = CANDIDATE_SHA) -> Iterator[tuple[str, str]]:
    class ClassificationHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/local/templates":
                self._send(200, b"local page", "text/html")
                return
            if self.path == "/local/api/latest-style-handoff":
                self._json(
                    {
                        "ok": True,
                        "handoff": {
                            "handoff_id": "33333333-3333-4333-8333-333333333333",
                            "batch_id": "BATCH-SYNTHETIC",
                            "current_status": "awaiting_review",
                            "publishable": False,
                            "output_job_ids": [JOB_ID],
                            "output_jobs": [
                                {
                                    "job_id": JOB_ID,
                                    "output_type": "scene",
                                    "status": "awaiting_customer_review",
                                }
                            ],
                        },
                    }
                )
                return
            if self.path == f"/local/api/style-jobs/{JOB_ID}":
                self._json(
                    {
                        "job_id": JOB_ID,
                        "output_type": "scene",
                        "status": "awaiting_review",
                        "current_job_id": JOB_ID,
                        "synthetic_test_only": True,
                        "publishable": False,
                        "candidates": [
                            {
                                "candidate_id": CANDIDATE_ID,
                                "content_sha256": advertised_sha,
                                "mime_type": "image/jpeg",
                                "width": 512,
                                "height": 512,
                                "synthetic_test_only": True,
                                "publishable": False,
                                "content_url": (
                                    f"/local/api/style-jobs/{JOB_ID}/candidates/"
                                    f"{CANDIDATE_ID}/image"
                                ),
                            }
                        ],
                    }
                )
                return
            if self.path.endswith(f"/candidates/{CANDIDATE_ID}/image"):
                self._send(200, CANDIDATE, "image/jpeg")
                return
            self._json({"detail": "not found"}, status=404)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

        def _json(self, payload: dict[str, object], status: int = 200) -> None:
            self._send(
                status,
                json.dumps(payload, separators=(",", ":")).encode(),
                "application/json",
            )

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    class StyleHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            body = b'{"status":"ok","service":"opc-style-agent"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    classification = ThreadingHTTPServer(("127.0.0.1", 0), ClassificationHandler)
    style = ThreadingHTTPServer(("127.0.0.1", 0), StyleHandler)
    threads = [
        Thread(target=classification.serve_forever, daemon=True),
        Thread(target=style.serve_forever, daemon=True),
    ]
    for thread in threads:
        thread.start()
    try:
        yield (
            f"http://127.0.0.1:{classification.server_port}",
            f"http://127.0.0.1:{style.server_port}",
        )
    finally:
        classification.shutdown()
        style.shutdown()
        classification.server_close()
        style.server_close()


def run_verifier(classification_url: str, style_url: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--classification-url",
            classification_url,
            "--style-url",
            style_url,
            "--confirm-synthetic",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_live_verifier_checks_both_services_job_and_candidate_bytes() -> None:
    with bridge_servers() as (classification_url, style_url):
        result = run_verifier(classification_url, style_url)

    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report == {
        "status": "pass",
        "mode": "synthetic_test_only",
        "publishable": False,
        "classification_page": "reachable",
        "style_service": "healthy",
        "handoff_id": "33333333-3333-4333-8333-333333333333",
        "batch_id": "BATCH-SYNTHETIC",
        "reviewable_jobs": 1,
        "candidates_checked": 1,
        "issue_codes": [],
    }


def test_live_verifier_fails_closed_when_candidate_hash_does_not_match() -> None:
    with bridge_servers(advertised_sha="0" * 64) as (classification_url, style_url):
        result = run_verifier(classification_url, style_url)

    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["status"] == "fail"
    assert report["issue_codes"] == ["candidate_sha256_mismatch"]
    assert report["candidates_checked"] == 0


def test_live_verifier_rejects_remote_targets_without_network_request() -> None:
    result = run_verifier("https://example.com", "http://127.0.0.1:18119")

    assert result.returncode == 2
    assert json.loads(result.stdout)["issue_codes"] == ["loopback_target_required"]
