"""Executable acceptance contract for the synthetic image-output demo."""

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEMO_SCRIPT = REPOSITORY_ROOT / "scripts" / "verify_image_output_demo.py"


def _run_demo(tmp_path: Path, name: str, *, cwd: Path | None = None) -> dict[str, Any]:
    manifest_path = tmp_path / f"{name}.json"
    completed = subprocess.run(
        [sys.executable, str(DEMO_SCRIPT), "--output", str(manifest_path)],
        cwd=cwd or REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert manifest_path.is_file()
    manifest = cast(
        dict[str, Any], json.loads(manifest_path.read_text(encoding="utf-8"))
    )
    assert json.loads(completed.stdout) == manifest
    return manifest


def test_demo_disables_interpreter_bytecode_before_side_effect_guard(
    tmp_path: Path,
) -> None:
    """A clean interpreter cache must not trip the local write guard."""
    environment = os.environ.copy()
    environment["PYTHONPYCACHEPREFIX"] = str(tmp_path / "fresh-pycache")
    completed = subprocess.run(
        [sys.executable, str(DEMO_SCRIPT), "--output", str(tmp_path / "result.json")],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_guarded_demo_constructs_local_app_inside_the_write_allowlist(
    tmp_path: Path,
) -> None:
    """Local sandbox startup must use the demo's declared temporary root."""
    manifest_path = tmp_path / "guarded-app-construction.json"
    completed = subprocess.run(
        [sys.executable, str(DEMO_SCRIPT), "--output", str(manifest_path)],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )

    assert manifest_path.is_file(), completed.stderr or completed.stdout
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["file_side_effect_guard"]["blocked_attempts"] == []
    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_demo_proves_happy_path_and_all_fail_closed_gates(tmp_path: Path) -> None:
    """Removing any business-gate assertion must make the manifest incomplete."""
    manifest = _run_demo(tmp_path, "acceptance")

    assert manifest["result"] == "PASS"
    assert manifest["classification"] == "synthetic_test_only"
    assert manifest["publishable"] is False
    assert manifest["outbound_guard"]["attempt_count"] == 0
    assert manifest["outbound_guard"]["attempts"] == []
    assert manifest["file_side_effect_guard"]["blocked_attempt_count"] == 0
    assert manifest["file_side_effect_guard"]["blocked_attempts"] == []
    assert manifest["external_publish_actions"] == []
    assert manifest["storage"] == {
        "database": "temporary_sqlite",
        "artifacts": "temporary_local_synthetic_store",
        "retained_media_files": 0,
    }

    flows = manifest["flows"]
    assert flows["scene"]["candidate_count"] == 3
    assert flows["scene"]["status_after_authenticity"] == ("awaiting_customer_review")
    assert flows["multi_product"]["candidate_count"] == 3
    assert flows["multi_product"]["status_after_review"] == "customer_approved"
    assert flows["multi_product"]["association_count"] == 2
    assert flows["multi_product"]["default_view"] == "style_image"
    assert flows["multi_product"]["explicit_real_view"] == "real_photo"
    selected = flows["multi_product"]["customer_selection"]
    assert selected["candidate_id"]
    assert len(selected["content_sha256"]) == 64
    assert selected["review_id"]
    associations = flows["multi_product"]["product_associations"]
    assert len(associations) == 2
    assert [item["product_id"] for item in associations] == [
        "product-multi-a",
        "product-multi-b",
    ]
    assert all(
        item["approved_style_candidate"]["candidate_id"] == selected["candidate_id"]
        and item["approved_style_candidate"]["content_sha256"]
        == selected["content_sha256"]
        and item["confirmed_original_asset"]["identity"] == "original_real_photo"
        and item["confirmed_original_asset"]["asset_id"] == item["source_asset_id"]
        for item in associations
    )
    assert len(flows["multi_product"]["source_asset_ids"]) == 2
    assert flows["multi_product"]["main_style_card_version_id"]
    assert flows["multi_product"]["scene_style_card_version_id"]
    assert flows["multi_product"]["job_id"]
    assert flows["multi_product"]["checker_version"] == (
        "deterministic-local-authenticity-rules-v1"
    )
    assert all(
        item["synthetic_test_only"] is True and item["publishable"] is False
        for flow in flows.values()
        for item in flow["artifacts"]
    )

    assert manifest["gates"] == {
        "cross_customer_hidden": {"http_status": 404, "result": "PASS"},
        "no_publish_api_or_action": {"http_status": None, "result": "PASS"},
        "no_real_photo_no_masquerade": {"http_status": 404, "result": "PASS"},
        "not_approved_style_hidden": {
            "http_status": 200,
            "resolved_view": "real_photo",
            "result": "PASS",
        },
        "quality_failure_blocks_review": {
            "http_status": 409,
            "job_status": "authenticity_failed",
            "result": "PASS",
        },
        "transform_permission_required": {
            "error_code": "transform_permission_denied",
            "http_status": 409,
            "result": "PASS",
        },
        "unconfirmed_asset_rejected": {"http_status": 422, "result": "PASS"},
    }

    evidence = manifest["no_publish_evidence"]
    assert evidence["publish_capable_api_operations"] == []
    assert evidence["publish_or_outbox_tables"] == []
    assert evidence["publish_audit_events"] == []
    assert evidence["observed_outbound_attempts"] == []
    assert evidence["observed_side_effect_actions"] == []
    assert evidence["api_operations"]
    assert evidence["database_tables"]
    assert evidence["audit_event_types"]

    contract = manifest["prerequisite_contract"]
    assert contract["http_completed"] == [
        "asset_intake_confirmation_permission",
        "main_style_generation",
        "scene_style_creation",
        "style_sample_authorization",
    ]
    assert contract["required_api_operations_present"] is True
    assert contract["seeded_preconditions"] == [
        {
            "name": "ai_only_negative_fixture",
            "reason": "no public API may create an AI asset that masquerades as real",
        },
        {
            "name": "synthetic_original_store_registration",
            "reason": (
                "the ordinary app uses UnconfiguredAssetContentStore and has no "
                "media-upload API; acceptance registers temporary synthetic bytes"
            ),
        },
    ]

    scans = manifest["repository_scans"]
    for scan in scans.values():
        assert scan["command"]
        assert scan["rules"]
        assert scan["match_count"] == 0
        assert scan["exclusions"]
    assert manifest["runner"] == {
        "supported_platforms": ["linux", "macos"],
        "portability": "posix_only",
        "reason": "outbound interception and local artifact controls rely on POSIX",
    }


def test_fresh_demo_runs_are_byte_deterministic_and_manifest_has_no_paths(
    tmp_path: Path,
) -> None:
    """A new database must preserve trace IDs, artifact hashes, and exact JSON."""
    first = _run_demo(tmp_path, "fresh-first", cwd=tmp_path)
    second = _run_demo(tmp_path, "fresh-second", cwd=tmp_path)

    assert (tmp_path / "fresh-first.json").read_bytes() == (
        tmp_path / "fresh-second.json"
    ).read_bytes()
    for flow in first["flows"].values():
        assert flow["replay_matches_original"] is True
        assert [item["sequence_number"] for item in flow["artifacts"]] == [1, 2, 3]
    assert first["flows"] == second["flows"]
    encoded = json.dumps(first, sort_keys=True)
    assert "artifact_path" not in encoded
    assert "storage_path" not in encoded
    assert str(tmp_path) not in encoded


def test_demo_defaults_to_stdout_from_repository_external_cwd(
    tmp_path: Path,
) -> None:
    """No output flag means stdout-only delivery from any working directory."""
    completed = subprocess.run(
        [sys.executable, str(DEMO_SCRIPT)],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    manifest = json.loads(completed.stdout)
    assert manifest["result"] == "PASS"
    assert list(tmp_path.iterdir()) == []


def test_caught_outbound_attempt_forces_fail_manifest_and_nonzero_exit(
    tmp_path: Path,
) -> None:
    """A swallowed business exception must not erase outbound evidence."""
    manifest_path = tmp_path / "blocked.json"
    environment = os.environ.copy()
    environment["OPC_DEMO_INJECT_CAUGHT_OUTBOUND"] = "all"
    completed = subprocess.run(
        [sys.executable, str(DEMO_SCRIPT), "--output", str(manifest_path)],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["result"] == "FAIL"
    assert manifest["outbound_guard"]["attempt_count"] >= 6
    channels = {item["channel"] for item in manifest["outbound_guard"]["attempts"]}
    assert {
        "os.system",
        "socket.connect",
        "socket.connect_ex",
        "socket.create_connection",
        "socket.sendto",
        "subprocess.Popen",
    } <= channels


def test_publish_audit_inventory_forces_gate_and_overall_failure(
    tmp_path: Path,
) -> None:
    """A locally observed publish audit must never coexist with PASS."""
    manifest_path = tmp_path / "publish-audit.json"
    environment = os.environ.copy()
    environment["OPC_DEMO_INJECT_PUBLISH_AUDIT"] = "storefront.publish"
    completed = subprocess.run(
        [sys.executable, str(DEMO_SCRIPT), "--output", str(manifest_path)],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["result"] == "FAIL"
    assert manifest["gates"]["no_publish_api_or_action"]["result"] == "FAIL"
    assert manifest["external_publish_actions"] == ["audit:storefront.publish"]


def test_caught_outside_file_write_forces_fail_and_is_observable(
    tmp_path: Path,
) -> None:
    """A swallowed storefront-file block must remain visible and fail overall."""
    manifest_path = tmp_path / "outside-write.json"
    environment = os.environ.copy()
    environment["OPC_DEMO_INJECT_CAUGHT_FILE_WRITE"] = "storefront-publish.queue"
    completed = subprocess.run(
        [sys.executable, str(DEMO_SCRIPT), "--output", str(manifest_path)],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["result"] == "FAIL"
    assert manifest["gates"]["no_publish_api_or_action"]["result"] == "FAIL"
    assert manifest["file_side_effect_guard"]["blocked_attempt_count"] >= 1
    attempt = manifest["file_side_effect_guard"]["blocked_attempts"][0]
    assert attempt["operation"] == "open"
    assert attempt["target"] == "outside_allowlist/storefront-publish.queue"
    assert (
        "file_write:open:outside_allowlist/storefront-publish.queue"
        in manifest["external_publish_actions"]
    )
    assert not (tmp_path / "storefront-publish.queue").exists()


def test_sqlite_file_uri_outside_allowlist_still_fails_closed(
    tmp_path: Path,
) -> None:
    """URI parsing must identify the real file without widening write scope."""
    target = tmp_path / "outside-uri.sqlite3"
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONPATH"] = os.pathsep.join(
        [
            str(REPOSITORY_ROOT / "scripts"),
            str(REPOSITORY_ROOT / "src"),
            environment.get("PYTHONPATH", ""),
        ]
    )
    probe = """
import json
import sqlite3
import sys
from pathlib import Path

from verify_image_output_demo import (
    FILE_SIDE_EFFECT_GUARD,
    FileSideEffectBlockedError,
)

target = Path(sys.argv[1]).resolve()
FILE_SIDE_EFFECT_GUARD.activate()
try:
    sqlite3.connect(f"{target.as_uri()}?mode=rwc", uri=True)
except FileSideEffectBlockedError:
    pass
else:
    raise AssertionError("sqlite file URI bypassed the write allowlist")
finally:
    FILE_SIDE_EFFECT_GUARD.deactivate()
print(json.dumps(FILE_SIDE_EFFECT_GUARD.snapshot(), sort_keys=True))
"""
    completed = subprocess.run(
        [sys.executable, "-c", probe, str(target)],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    snapshot = json.loads(completed.stdout)
    assert snapshot["blocked_attempts"] == [
        {
            "blocked": True,
            "operation": "sqlite3.connect",
            "sequence": 1,
            "target": "outside_allowlist/outside-uri.sqlite3",
        }
    ]
    assert not target.exists()


def test_preopened_outside_queue_fd_write_after_approval_fails_closed(
    tmp_path: Path,
) -> None:
    """An fd obtained before activation cannot bypass approval-stage policy."""
    queue_path = tmp_path / "preopened-storefront.queue"
    queue_path.write_bytes(b"before\n")
    manifest_path = tmp_path / "preopened-fd.json"
    environment = os.environ.copy()
    environment["OPC_DEMO_INJECT_PREOPENED_FD_WRITE"] = str(queue_path)
    completed = subprocess.run(
        [sys.executable, str(DEMO_SCRIPT), "--output", str(manifest_path)],
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["result"] == "FAIL"
    assert manifest["gates"]["no_publish_api_or_action"]["result"] == "FAIL"
    attempt = manifest["file_side_effect_guard"]["blocked_attempts"][0]
    assert attempt == {
        "blocked": True,
        "operation": "os.write",
        "sequence": 1,
        "target": "outside_allowlist/preopened-storefront.queue",
    }
    assert manifest["external_publish_actions"] == [
        "file_write:os.write:outside_allowlist/preopened-storefront.queue"
    ]
    assert queue_path.read_bytes() == b"before\n"


@pytest.mark.parametrize("method", ["pwrite", "writev", "pwritev"])
def test_preopened_outside_fd_write_variants_fail_closed(
    tmp_path: Path,
    method: str,
) -> None:
    """Every exposed positional-write variant applies the same FD policy."""
    queue_path = tmp_path / f"preopened-{method}.queue"
    queue_path.write_bytes(b"before\n")
    manifest_path = tmp_path / f"preopened-{method}.json"
    environment = os.environ.copy()
    environment["OPC_DEMO_INJECT_PREOPENED_FD_WRITE"] = str(queue_path)
    environment["OPC_DEMO_INJECT_PREOPENED_FD_METHOD"] = method
    completed = subprocess.run(
        [sys.executable, str(DEMO_SCRIPT), "--output", str(manifest_path)],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["result"] == "FAIL"
    attempt = manifest["file_side_effect_guard"]["blocked_attempts"][0]
    assert attempt["operation"] == f"os.{method}"
    assert attempt["target"] == f"outside_allowlist/{queue_path.name}"
    assert queue_path.read_bytes() == b"before\n"


@pytest.mark.parametrize(
    ("fixture", "expected_target"),
    [
        ("closed", "unresolved_fd/closed"),
        ("pipe", "unsupported_fd/pipe"),
        ("socket", "unsupported_fd/socket"),
    ],
)
def test_unverifiable_integer_fd_writes_fail_closed(
    tmp_path: Path,
    fixture: str,
    expected_target: str,
) -> None:
    """Closed, pipe, and socket descriptors never become implicit allowlist."""
    manifest_path = tmp_path / f"fd-{fixture}.json"
    environment = os.environ.copy()
    environment["OPC_DEMO_INJECT_CAUGHT_INTEGER_FD_WRITE"] = fixture
    completed = subprocess.run(
        [sys.executable, str(DEMO_SCRIPT), "--output", str(manifest_path)],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["result"] == "FAIL"
    attempt = manifest["file_side_effect_guard"]["blocked_attempts"][0]
    assert attempt["operation"] == "os.write"
    assert attempt["target"] == expected_target


def test_unresolved_dir_fd_never_falls_back_to_process_cwd(tmp_path: Path) -> None:
    """A stale relative-open base must be recorded instead of using cwd."""
    manifest_path = tmp_path / "closed-dir-fd.json"
    environment = os.environ.copy()
    environment["OPC_DEMO_INJECT_CAUGHT_CLOSED_DIR_FD_WRITE"] = "1"
    completed = subprocess.run(
        [sys.executable, str(DEMO_SCRIPT), "--output", str(manifest_path)],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["result"] == "FAIL"
    attempt = manifest["file_side_effect_guard"]["blocked_attempts"][0]
    assert attempt == {
        "blocked": True,
        "operation": "open",
        "sequence": 1,
        "target": "unresolved_path/unresolvable",
    }
    assert not (tmp_path / "must-not-fall-back.queue").exists()


def test_scan_only_is_an_executable_structured_command() -> None:
    """The manifest scan command must be runnable and return real counts."""
    scan_command = [
        sys.executable,
        "scripts/verify_image_output_demo.py",
        "--scan-only",
    ]
    completed = subprocess.run(
        scan_command,
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    scan_result = json.loads(completed.stdout)
    assert scan_result["schema_version"] == "repository-scan-v1"
    assert scan_result["result"] == "PASS"
    assert scan_result["command"] == shlex.join(scan_command)
    assert scan_result["repository_scans"]["secret_scan"]["match_count"] == 0
    assert scan_result["repository_scans"]["real_media_scan"]["match_count"] == 0
    assert scan_result["repository_scans"]["real_media_scan"][
        "allowed_exact_matches"
    ] == [
        {
            "path": (
                "docs/verification/assets/"
                "automatic-product-identity-card-cost-gate.jpg"
            ),
            "sha256": (
                "bca4bf11171e1ed144806a794bdbaf15cb32c299e2d5e99eaca7274cfe0f6fd3"
            ),
        },
        {
            "path": (
                "docs/verification/assets/"
                "automatic-product-identity-card-refresh.jpg"
            ),
            "sha256": (
                "540e6ddd17d63427d9ea128f8ee86f573f1ec05ffc91c0361deddc92c86742eb"
            ),
        },
        {
            "path": "tests/fixtures/foreground/simple-product.png",
            "sha256": (
                "59ebff6e9adb1f02d0ab130593952443234fe9efa16c4e97a599ec79b3b6554f"
            ),
        },
    ]
    assert (
        scan_result["repository_scans"]["secret_scan"]["command"]
        == (scan_result["command"])
    )


def test_scan_only_rejects_unlisted_media_inside_verification_assets() -> None:
    media_path = (
        REPOSITORY_ROOT / "docs/verification/assets/test-customer-photo.jpg"
    )
    media_path.write_bytes(b"synthetic unlisted customer-photo sentinel")
    try:
        completed = subprocess.run(
            [
                ".venv/bin/python",
                "scripts/verify_image_output_demo.py",
                "--scan-only",
            ],
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
    finally:
        media_path.unlink()

    assert completed.returncode == 1
    scan_result = json.loads(completed.stdout)
    assert scan_result["result"] == "FAIL"
    assert scan_result["repository_scans"]["real_media_scan"]["matches"] == [
        "docs/verification/assets/test-customer-photo.jpg"
    ]


def test_scan_only_rejects_hash_drift_of_exactly_named_acceptance_image() -> None:
    media_path = (
        REPOSITORY_ROOT
        / "docs/verification/assets/automatic-product-identity-card-refresh.jpg"
    )
    original = media_path.read_bytes()
    media_path.write_bytes(original + b"tampered")
    try:
        completed = subprocess.run(
            [
                ".venv/bin/python",
                "scripts/verify_image_output_demo.py",
                "--scan-only",
            ],
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
    finally:
        media_path.write_bytes(original)

    assert completed.returncode == 1
    scan_result = json.loads(completed.stdout)
    assert scan_result["result"] == "FAIL"
    assert scan_result["repository_scans"]["real_media_scan"]["matches"] == [
        "docs/verification/assets/automatic-product-identity-card-refresh.jpg"
    ]
