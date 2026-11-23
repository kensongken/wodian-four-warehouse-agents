import hashlib
import json
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "scripts/verify_dual_acceptance_evidence.py"
RUNNER = ROOT / "scripts/run_dual_repository_acceptance_v2.py"

sys.path.insert(0, str(ROOT))
from scripts.run_dual_repository_acceptance_v2 import parse_safety_junit  # noqa: E402


def _write_evidence(tmp_path: Path) -> Path:
    screenshot = tmp_path / "screenshots" / "approved.png"
    screenshot.parent.mkdir()
    screenshot.write_bytes(b"synthetic screenshot")
    database = tmp_path / "state.sqlite3"
    database.write_bytes(b"synthetic database")
    media = tmp_path / "media" / "candidate.png"
    media.parent.mkdir()
    media.write_bytes(b"synthetic candidate")
    network = tmp_path / "network.json"
    network.write_text(
        json.dumps(
            {
                "guard": "playwright_request_observer",
                "observed_requests": ["http://127.0.0.1:41001/health"],
                "non_loopback_requests": [],
                "provider_requests": [],
            }
        )
    )

    def artifact(path: Path) -> dict[str, str]:
        return {
            "path": str(path.relative_to(tmp_path)),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }

    payload = {
        "result": "PASS",
        "acceptance_batch_id": "dual-v2-one-batch",
        "schema_sha256": "d5772ea9bee5e16f767e287bbb9906ff96a603107330f81885e4f8d781933e07",
        "browser": {
            "acceptance_batch_id": "dual-v2-one-batch",
            "backend_calls_observed": [
                "upload",
                "classification",
                "human_correction",
                "product_identity",
                "main_photo",
                "conversion_authorization",
                "handoff",
                "generation",
                "authenticity_check",
                "customer_review",
                "original_style_toggle",
                "refresh_restore",
            ],
        },
        "repository_freeze": {
            "producer": {
                "head_before": "a" * 40,
                "head_after": "a" * 40,
                "status_before": "",
                "status_after": "",
            },
            "consumer": {
                "head_before": "b" * 40,
                "head_after": "b" * 40,
                "status_before": "",
                "status_after": "",
            },
        },
        "acceptance_steps": {str(index): {"status": "pass"} for index in range(1, 16)},
        "idempotency": {"before": 1, "after": 1, "duplicate_created": False},
        "authenticity": {"completed_checks": 3, "failed_checks": 0},
        "marketplace_display": {
            "default": {"requested_view": "default", "resolved_view": "style_image"},
            "real": {"requested_view": "real", "resolved_view": "real_photo"},
        },
        "safety_scenarios": {
            name: {
                "first_outbound_attempts": attempts,
                "replay_outbound_attempts": attempts,
                "duplicate_charge": False,
                "replay_blocked": True,
                "evidence_source": "pytest_junit_properties",
            }
            for name, attempts in (("failure", 0), ("timeout", 1), ("uncertain", 1))
        },
        "runtime_accounting": {
            "measurement": "playwright_request_observer_and_provider_ledger",
            "non_loopback_requests": 0,
            "provider_calls": 0,
            "fees_usd": 0,
        },
        "artifacts": [
            artifact(screenshot),
            artifact(database),
            artifact(media),
            artifact(network),
        ],
        "synthetic_test_only": True,
        "publishable": False,
    }
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(payload))
    return manifest


def _run(manifest: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(manifest)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_acceptance_runner_uses_separate_python_runtimes() -> None:
    result = subprocess.run(
        [sys.executable, str(RUNNER), "--help"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "--producer-python" in result.stdout
    assert "--consumer-python" in result.stdout


def test_safety_results_are_parsed_from_junit_properties(tmp_path: Path) -> None:
    suite = ElementTree.Element("testsuite")
    for name, attempts in (("failure", "0"), ("timeout", "1"), ("uncertain", "1")):
        case = ElementTree.SubElement(suite, "testcase", {"name": name})
        properties = ElementTree.SubElement(case, "properties")
        for key, value in {
            "scenario": name,
            "first_outbound_attempts": attempts,
            "replay_outbound_attempts": attempts,
            "duplicate_charge": "False",
            "replay_blocked": "True",
        }.items():
            ElementTree.SubElement(properties, "property", {"name": key, "value": value})
    report = tmp_path / "safety.xml"
    ElementTree.ElementTree(suite).write(report)

    assert parse_safety_junit(report) == {
        name: {
            "first_outbound_attempts": attempts,
            "replay_outbound_attempts": attempts,
            "duplicate_charge": False,
            "replay_blocked": True,
            "evidence_source": "pytest_junit_properties",
        }
        for name, attempts in (("failure", 0), ("timeout", 1), ("uncertain", 1))
    }


def test_accepts_one_batch_with_measured_runtime_and_retained_artifacts(tmp_path: Path) -> None:
    result = _run(_write_evidence(tmp_path))

    assert result.returncode == 0
    assert json.loads(result.stdout)["status"] == "pass"


def test_rejects_browser_evidence_from_another_batch(tmp_path: Path) -> None:
    manifest = _write_evidence(tmp_path)
    payload = json.loads(manifest.read_text())
    payload["browser"]["acceptance_batch_id"] = "another-batch"
    manifest.write_text(json.dumps(payload))

    result = _run(manifest)

    assert result.returncode == 1
    assert "browser_batch_mismatch" in json.loads(result.stdout)["issue_codes"]


def test_rejects_handwritten_zero_accounting(tmp_path: Path) -> None:
    manifest = _write_evidence(tmp_path)
    payload = json.loads(manifest.read_text())
    payload["runtime_accounting"]["measurement"] = "declared"
    manifest.write_text(json.dumps(payload))

    result = _run(manifest)

    assert result.returncode == 1
    assert "runtime_accounting_not_measured" in json.loads(result.stdout)["issue_codes"]


def test_rejects_missing_browser_backend_step(tmp_path: Path) -> None:
    manifest = _write_evidence(tmp_path)
    payload = json.loads(manifest.read_text())
    payload["browser"]["backend_calls_observed"].remove("upload")
    manifest.write_text(json.dumps(payload))

    result = _run(manifest)

    assert "browser_backend_steps_incomplete" in json.loads(result.stdout)["issue_codes"]


def test_rejects_deleted_or_changed_artifact(tmp_path: Path) -> None:
    manifest = _write_evidence(tmp_path)
    payload = json.loads(manifest.read_text())
    (tmp_path / payload["artifacts"][0]["path"]).write_bytes(b"changed")

    result = _run(manifest)

    assert "artifact_sha256_mismatch" in json.loads(result.stdout)["issue_codes"]


def test_rejects_dirty_or_drifting_fixed_versions(tmp_path: Path) -> None:
    manifest = _write_evidence(tmp_path)
    payload = json.loads(manifest.read_text())
    payload["repository_freeze"]["producer"]["status_before"] = " M local_app.py"
    payload["repository_freeze"]["consumer"]["head_after"] = "c" * 40
    manifest.write_text(json.dumps(payload))

    issues = json.loads(_run(manifest).stdout)["issue_codes"]

    assert "producer_worktree_not_clean" in issues
    assert "consumer_head_drifted" in issues


def test_rejects_any_missing_or_failed_original_acceptance_step(tmp_path: Path) -> None:
    manifest = _write_evidence(tmp_path)
    payload = json.loads(manifest.read_text())
    del payload["acceptance_steps"]["14"]
    payload["acceptance_steps"]["15"]["status"] = "fail"
    manifest.write_text(json.dumps(payload))

    issues = json.loads(_run(manifest).stdout)["issue_codes"]

    assert "acceptance_step_14_missing" in issues
    assert "acceptance_step_15_not_passed" in issues


def test_rejects_review_toggle_in_place_of_marketplace_display(tmp_path: Path) -> None:
    manifest = _write_evidence(tmp_path)
    payload = json.loads(manifest.read_text())
    payload["marketplace_display"]["default"]["resolved_view"] = "real_photo"
    payload["marketplace_display"]["real"]["resolved_view"] = "style_image"
    manifest.write_text(json.dumps(payload))

    issues = json.loads(_run(manifest).stdout)["issue_codes"]

    assert "marketplace_default_not_style_image" in issues
    assert "marketplace_real_view_not_real_photo" in issues


def test_rejects_replay_that_creates_duplicate_handoff(tmp_path: Path) -> None:
    manifest = _write_evidence(tmp_path)
    payload = json.loads(manifest.read_text())
    payload["idempotency"]["after"] = 2
    payload["idempotency"]["duplicate_created"] = True
    manifest.write_text(json.dumps(payload))

    assert "handoff_replay_not_idempotent" in json.loads(_run(manifest).stdout)["issue_codes"]


def test_rejects_missing_authenticity_completion(tmp_path: Path) -> None:
    manifest = _write_evidence(tmp_path)
    payload = json.loads(manifest.read_text())
    payload["authenticity"]["completed_checks"] = 0
    manifest.write_text(json.dumps(payload))

    assert "authenticity_completion_missing" in json.loads(_run(manifest).stdout)["issue_codes"]


def test_rejects_safety_replay_that_retries_or_duplicates_charge(tmp_path: Path) -> None:
    manifest = _write_evidence(tmp_path)
    payload = json.loads(manifest.read_text())
    payload["safety_scenarios"]["timeout"]["replay_outbound_attempts"] = 2
    payload["safety_scenarios"]["uncertain"]["duplicate_charge"] = True
    payload["safety_scenarios"]["failure"]["replay_blocked"] = False
    manifest.write_text(json.dumps(payload))

    issues = json.loads(_run(manifest).stdout)["issue_codes"]

    assert "timeout_replay_repeated_provider_call" in issues
    assert "uncertain_duplicate_charge" in issues
    assert "failure_replay_not_blocked" in issues


def test_rejects_hardcoded_safety_summary_without_structured_source(tmp_path: Path) -> None:
    manifest = _write_evidence(tmp_path)
    payload = json.loads(manifest.read_text())
    del payload["safety_scenarios"]["failure"]["evidence_source"]
    manifest.write_text(json.dumps(payload))

    assert (
        "failure_safety_evidence_not_structured" in json.loads(_run(manifest).stdout)["issue_codes"]
    )
