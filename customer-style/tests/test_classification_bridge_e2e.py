"""Two-repository executable acceptance for the removable local test bridge."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest

CONSUMER_ROOT = Path(__file__).resolve().parents[1]
ACCEPTANCE_SCRIPT = CONSUMER_ROOT / "scripts" / "verify_classification_style_bridge.py"
PRODUCER_ENV = "CLASSIFICATION_STYLE_PRODUCER_ROOT"


def _producer_root() -> Path:
    configured = os.environ.get(PRODUCER_ENV)
    if not configured:
        pytest.skip(f"set {PRODUCER_ENV} to run the explicit two-repository acceptance")
    root = Path(configured).resolve()
    if not (root / "services" / "backend" / "pyproject.toml").is_file():
        pytest.fail(f"{PRODUCER_ENV} is not a 分门别类 checkout: {root}")
    return root


def _head(root: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _run_acceptance(
    tmp_path: Path,
    *,
    name: str,
    producer_root: Path,
    consumer_head: str,
    producer_head: str,
) -> tuple[dict[str, Any], bytes]:
    output = tmp_path / f"{name}.json"
    interpreter = producer_root / "services" / "backend" / ".venv" / "bin" / "python"
    if not interpreter.is_file():
        pytest.fail("producer .venv is required by the pinned two-repository command")
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    for key in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "OPENAI_API_KEY",
    ):
        environment.pop(key, None)
    completed = subprocess.run(
        [
            str(interpreter),
            str(ACCEPTANCE_SCRIPT),
            "--producer-root",
            str(producer_root),
            "--consumer-head",
            consumer_head,
            "--producer-head",
            producer_head,
            "--output",
            str(output),
        ],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert output.is_file()
    body = output.read_bytes()
    manifest = cast(dict[str, Any], json.loads(body))
    assert json.loads(completed.stdout) == manifest
    return manifest, body


def test_two_repository_acceptance_is_deterministic_and_fail_closed(
    tmp_path: Path,
) -> None:
    """Removing a route, gate, cleanup receipt, or stable trace breaks this test."""
    producer_root = _producer_root()
    consumer_head = _head(CONSUMER_ROOT)
    producer_head = _head(producer_root)

    first, first_bytes = _run_acceptance(
        tmp_path,
        name="first",
        producer_root=producer_root,
        consumer_head=consumer_head,
        producer_head=producer_head,
    )
    second, second_bytes = _run_acceptance(
        tmp_path,
        name="second",
        producer_root=producer_root,
        consumer_head=consumer_head,
        producer_head=producer_head,
    )

    assert first_bytes == second_bytes
    assert first == second
    assert first["schema_version"] == "classification-style-bridge-acceptance-v1"
    assert first["result"] == "PASS"
    assert first["classification"] == "synthetic_test_only"
    assert first["publishable"] is False
    assert first["production_eligible"] is False
    assert first["repository_pins"] == {
        "consumer_head": consumer_head,
        "producer_head": producer_head,
        "handoff_golden_sha256": (
            "95c62a43872a4befd1728a041c1ffb434ec0c9ae473c7d0898b9949e590ef201"
        ),
    }
    assert first["runtime"]["bridge_default"] == "disabled"
    assert first["runtime"]["nonce"] == "ephemeral_in_memory_not_persisted"
    assert first["runtime"]["transport"] == "in_process_asgi_no_socket"
    assert first["runtime"]["positive_photo_count"] == sum(
        product["photo_count"] for product in first["products"]
    )
    assert first["runtime"]["positive_product_count"] == len(first["products"])

    preflight = first["preflight"]
    assert preflight["result"] == "PASS"
    assert [receipt["result"] for receipt in preflight["lock_checks"]] == [
        "PASS",
        "PASS",
    ]
    assert preflight["runtime"]["implementation"] == "CPython"
    assert preflight["repositories"]["consumer"]["clean"] is True
    assert preflight["repositories"]["producer"]["clean"] is True
    assert preflight["repositories"]["consumer"]["head"] == consumer_head
    assert preflight["repositories"]["producer"]["head"] == producer_head
    assert len(preflight["repositories"]["consumer"]["lint_baseline_waiver"]) == 3
    assert len(preflight["repositories"]["producer"]["lint_baseline_waiver"]) == 12
    assert all(
        item["task_base_sha256"] == item["current_sha256"]
        for repository in preflight["repositories"].values()
        for item in repository["lint_baseline_waiver"]
    )
    assert all(
        repository["tracked_file_count"] == len(repository["tracked_files"])
        and repository["tracked_tree_sha256"]
        for repository in preflight["repositories"].values()
    )

    products = first["products"]
    assert [product["photo_count"] for product in products] == [3, 3, 3]
    assert [product["status"] for product in products] == [
        "awaiting_review",
        "awaiting_review",
        "pending_real_model",
    ]
    assert products[0]["correction"] == {
        "automatic_category": "real_photo",
        "confidence": "low",
        "confirmed_category": "scene_photo",
        "human_corrected": True,
    }
    assert all(product["replay_created"] is False for product in products)

    assert first["routing"]["adapters"] == ["deterministic-local-image-v1"]
    assert first["routing"]["candidate_counts"] == [3] * 8
    assert first["routing"]["multi_product_jobs"] == 1
    assert first["routing"]["real_photo_jobs"] == 6
    assert first["routing"]["scene_jobs"] == 1
    assert first["routing"]["wear_candidate_jobs"] == 0
    assert first["routing"]["wear_status"] == "pending_real_model"
    assert first["cleanup"]["cleanup_uncertainty_first_result"] == "cleanup_pending"
    assert first["cleanup"]["cleanup_uncertainty_retry_result"] == "cleaned"
    assert first["cleanup"]["retained_media_files"] == 0
    assert first["cleanup"]["unresolved_receipts"] == 0
    assert first["boundary"] == {
        "consumer_store": "temporary_sqlite",
        "credential_fields_in_handoff": [],
        "external_actions": [],
        "nonce_persisted": False,
        "producer_store": "temporary_local_batch_store",
        "shared_database": False,
        "source_paths_in_handoff": 0,
    }
    assert first["outbound_guard"]["attempt_count"] == 0
    assert first["file_side_effect_guard"]["blocked_attempt_count"] == 0
    receipt_observation = first["media_receipts"]
    assert receipt_observation["positive_staged_count"] == 9
    assert receipt_observation["all_requests_nonce_authenticated"] is True
    assert receipt_observation["ledger"]["total_rows"] == sum(
        receipt_observation["ledger"]["status_counts"].values()
    )
    assert receipt_observation["ledger"]["unresolved_rows"] == 0
    assert receipt_observation["ledger"]["active_handoff_pins"] == 0
    assert receipt_observation["ledger"]["retained_media_files"] == 0

    transport = first["transport_observations"]
    assert (
        transport["media_post_count"] == receipt_observation["ledger"]["total_rows"] - 1
    )
    assert (
        transport["media_post_count"]
        == transport["request_counts"][
            "POST /v1/local-test/classification-bridge/media"
        ]
    )
    assert (
        transport["handoff_post_count"]
        == transport["request_counts"][
            "POST /v1/local-test/classification-bridge/handoffs"
        ]
    )
    assert transport["handoff_get_count"] > 0
    assert (
        transport["handoff_get_count"]
        == transport["request_counts"][
            "GET /v1/local-test/classification-bridge/handoffs/{handoff_id}"
        ]
    )
    assert sum(transport["request_counts"].values()) >= transport["media_post_count"]
    assert first["database_observations"]["audit_event_count"] == sum(
        first["database_observations"]["audit_event_type_counts"].values()
    )
    assert first["database_observations"]["review_decision_counts"] == {
        "approved": 4,
        "not_my_style": 6,
        "product_detail_inaccurate": 2,
    }

    review_loop = first["candidate_review_loop"]
    assert review_loop["candidate_count"] == 3
    assert review_loop["regeneration_count"] == 3
    assert review_loop["final_status"] == "customer_approved"
    assert review_loop["default_view"] == "style_image"
    assert review_loop["real_view"] == "real_photo"
    assert review_loop["publishable"] is False
    assert review_loop["synthetic_test_only"] is True
    assert review_loop["replay_effect_counts_unchanged"] is True
    assert review_loop["media_hashes_verified"] is True
    assert review_loop["delayed_replay_after_approval"] is True
    assert review_loop["delayed_replay_after_manual"] is True
    edge = first["review_edge_cases"]
    assert edge["independent_scene_sessions"] == 2
    assert edge["independent_scene_approvals"] == 2
    assert edge["generation_failure_status"] == "manual_attention_required"
    assert edge["generation_failure_job_status"] == "generation_failed"
    assert edge["generation_failure_candidate_count"] == 0
    assert edge["generation_failure_generator_calls"] == 1
    assert edge["generation_failure_replay_unchanged"] is True
    assert review_loop["manual_cutoff"] == {
        "status": "manual_attention_required",
        "regeneration_count": 3,
        "retained_candidate_count": 3,
        "jobs_created_on_fourth_rejection": 0,
        "generator_calls_on_fourth_rejection": 0,
        "review_round_count": 4,
    }

    required_gates = {
        "bridge_default_off",
        "cleanup_uncertainty_fails_closed",
        "conflicting_replay_rejected",
        "corrupt_image_rejected",
        "cross_customer_hidden",
        "duplicate_blocked",
        "ephemeral_nonce_required",
        "expired_receipt_rejected",
        "external_target_rejected_before_connect",
        "missing_permission_blocked",
        "missing_real_photo_blocked",
        "missing_style_pin_blocked",
        "authenticity_failure_via_bridge",
        "restart_safe_public_review",
        "review_approval_not_publishable",
        "video_rejected",
    }
    assert required_gates <= set(first["gates"])
    assert all(first["gates"][name]["result"] == "PASS" for name in required_gates)
    assert first["gates"]["authenticity_failure_via_bridge"] == {
        "result": "PASS",
        "source": "producer_confirmed_handoff",
        "job_status": "authenticity_failed",
        "review_http_status": 409,
        "review_error_code": "review_job_not_awaiting_customer",
        "same_app_database_store_transport": True,
    }
    assert first["gates"]["restart_safe_public_review"]["recovered_artifacts"] is True

    encoded = json.dumps(first, ensure_ascii=False, sort_keys=True)
    assert str(tmp_path) not in encoded
    assert str(CONSUMER_ROOT) not in encoded
    assert str(producer_root) not in encoded
    assert "X-Local-Classification-Bridge-Nonce" not in encoded
    assert re.search(r"\bsk-[A-Za-z0-9_-]{20,}\b", encoded) is None
