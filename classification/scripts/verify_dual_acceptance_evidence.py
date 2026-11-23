"""Fail closed when a dual-repository acceptance bundle mixes or invents evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


REQUIRED_BROWSER_STEPS = {
    "upload",
    "classification",
    "human_correction",
    "product_identity",
    "main_photo",
    "conversion_authorization",
    "handoff",
    "generation",
    "customer_review",
    "original_style_toggle",
    "refresh_restore",
}
MEASUREMENT = "playwright_request_observer_and_provider_ledger"
SCHEMA_SHA256 = "d5772ea9bee5e16f767e287bbb9906ff96a603107330f81885e4f8d781933e07"


def _object(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def verify(manifest_path: Path) -> list[str]:
    try:
        payload = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError):
        return ["manifest_unreadable"]
    if not isinstance(payload, dict):
        return ["manifest_invalid"]

    issues: list[str] = []
    batch_id = payload.get("acceptance_batch_id")
    browser = _object(payload.get("browser"))
    if not isinstance(batch_id, str) or not batch_id:
        issues.append("acceptance_batch_missing")
    if browser.get("acceptance_batch_id") != batch_id:
        issues.append("browser_batch_mismatch")
    if payload.get("schema_sha256") != SCHEMA_SHA256:
        issues.append("schema_sha256_mismatch")
    observed = browser.get("backend_calls_observed")
    if not isinstance(observed, list) or not REQUIRED_BROWSER_STEPS.issubset(observed):
        issues.append("browser_backend_steps_incomplete")

    freeze = _object(payload.get("repository_freeze"))
    for side in ("producer", "consumer"):
        state = _object(freeze.get(side))
        before = state.get("head_before")
        after = state.get("head_after")
        if not isinstance(before, str) or len(before) != 40:
            issues.append(f"{side}_head_missing")
        if before != after:
            issues.append(f"{side}_head_drifted")
        if state.get("status_before") != "" or state.get("status_after") != "":
            issues.append(f"{side}_worktree_not_clean")

    steps = _object(payload.get("acceptance_steps"))
    for number in range(1, 16):
        key = str(number)
        if key not in steps:
            issues.append(f"acceptance_step_{number}_missing")
        elif _object(steps.get(key)).get("status") != "pass":
            issues.append(f"acceptance_step_{number}_not_passed")

    idempotency = _object(payload.get("idempotency"))
    if (
        idempotency.get("before") != idempotency.get("after")
        or idempotency.get("duplicate_created") is not False
    ):
        issues.append("handoff_replay_not_idempotent")

    authenticity = _object(payload.get("authenticity"))
    completed_checks = authenticity.get("completed_checks")
    if (
        not isinstance(completed_checks, int)
        or isinstance(completed_checks, bool)
        or completed_checks < 1
    ):
        issues.append("authenticity_completion_missing")

    marketplace = _object(payload.get("marketplace_display"))
    default_view = _object(marketplace.get("default"))
    real_view = _object(marketplace.get("real"))
    if (
        default_view.get("requested_view") != "default"
        or default_view.get("resolved_view") != "style_image"
    ):
        issues.append("marketplace_default_not_style_image")
    if (
        real_view.get("requested_view") != "real"
        or real_view.get("resolved_view") != "real_photo"
    ):
        issues.append("marketplace_real_view_not_real_photo")

    safety = _object(payload.get("safety_scenarios"))
    for name in ("failure", "timeout", "uncertain"):
        scenario = _object(safety.get(name))
        first = scenario.get("first_outbound_attempts")
        replay = scenario.get("replay_outbound_attempts")
        if not isinstance(first, int) or isinstance(first, bool) or first < 0:
            issues.append(f"{name}_outbound_attempts_invalid")
        elif replay != first:
            issues.append(f"{name}_replay_repeated_provider_call")
        if scenario.get("duplicate_charge") is not False:
            issues.append(f"{name}_duplicate_charge")
        if scenario.get("replay_blocked") is not True:
            issues.append(f"{name}_replay_not_blocked")
        if scenario.get("evidence_source") != "pytest_junit_properties":
            issues.append(f"{name}_safety_evidence_not_structured")

    accounting = _object(payload.get("runtime_accounting"))
    if accounting.get("measurement") != MEASUREMENT:
        issues.append("runtime_accounting_not_measured")
    for field in ("non_loopback_requests", "provider_calls", "fees_usd"):
        value = accounting.get(field)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            issues.append(f"runtime_accounting_{field}_invalid")

    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        issues.append("artifacts_missing")
    else:
        root = manifest_path.parent.resolve()
        for artifact in artifacts:
            entry = _object(artifact)
            relative = entry.get("path")
            expected = entry.get("sha256")
            if not isinstance(relative, str) or not isinstance(expected, str):
                issues.append("artifact_entry_invalid")
                continue
            path = (root / relative).resolve()
            if root not in path.parents or not path.is_file():
                issues.append("artifact_missing")
                continue
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                issues.append("artifact_sha256_mismatch")

    if payload.get("synthetic_test_only") is not True:
        issues.append("synthetic_boundary_missing")
    if payload.get("publishable") is not False:
        issues.append("publishable_boundary_broken")
    if payload.get("result") != "PASS":
        issues.append("acceptance_result_not_pass")
    return sorted(set(issues))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    issues = verify(args.manifest.resolve())
    print(
        json.dumps(
            {"status": "fail" if issues else "pass", "issue_codes": issues},
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
