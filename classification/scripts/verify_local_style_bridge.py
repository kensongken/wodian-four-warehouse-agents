"""Fail-closed live verifier for the local classification-to-style bridge."""

from __future__ import annotations

import argparse
import ipaddress
import json
from hashlib import sha256
from typing import Any
from urllib.parse import urlsplit

import httpx


class VerificationFailure(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _loopback_origin(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise VerificationFailure("loopback_target_required")
    try:
        is_loopback = ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        is_loopback = parsed.hostname == "localhost"
    if not is_loopback:
        raise VerificationFailure("loopback_target_required")
    try:
        port = parsed.port
    except ValueError as error:
        raise VerificationFailure("loopback_target_required") from error
    if port is None:
        raise VerificationFailure("loopback_target_required")
    return f"http://{parsed.hostname}:{port}"


def _json_object(response: httpx.Response, code: str) -> dict[str, Any]:
    if response.status_code != 200:
        raise VerificationFailure(code)
    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError) as error:
        raise VerificationFailure(code) from error
    if not isinstance(payload, dict):
        raise VerificationFailure(code)
    return payload


def _get_json(client: httpx.Client, url: str, code: str) -> dict[str, Any]:
    try:
        response = client.get(url)
    except httpx.HTTPError as error:
        raise VerificationFailure(code) from error
    return _json_object(response, code)


def _get_bytes(client: httpx.Client, url: str) -> bytes:
    try:
        response = client.get(url)
    except httpx.HTTPError as error:
        raise VerificationFailure("candidate_content_unreachable") from error
    if response.status_code != 200:
        raise VerificationFailure("candidate_content_unreachable")
    media_type = response.headers.get("content-type", "").split(";", 1)[0]
    if media_type not in {"image/jpeg", "image/png"} or not response.content:
        raise VerificationFailure("candidate_content_invalid")
    return response.content


def verify(
    client: httpx.Client,
    *,
    classification_url: str,
    style_url: str,
) -> dict[str, object]:
    page = client.get(f"{classification_url}/local/templates")
    if page.status_code != 200:
        raise VerificationFailure("classification_page_unreachable")

    health = _get_json(client, f"{style_url}/health", "style_service_unhealthy")
    if health != {"status": "ok", "service": "opc-style-agent"}:
        raise VerificationFailure("style_service_unhealthy")

    latest = _get_json(
        client,
        f"{classification_url}/local/api/latest-style-handoff",
        "latest_handoff_unavailable",
    )
    handoff = latest.get("handoff")
    if latest.get("ok") is not True or not isinstance(handoff, dict):
        raise VerificationFailure("latest_handoff_missing")
    if handoff.get("publishable") is not False:
        raise VerificationFailure("synthetic_publishable_boundary_broken")

    output_jobs = handoff.get("output_jobs")
    if not isinstance(output_jobs, list) or not output_jobs:
        raise VerificationFailure("review_job_missing")

    reviewable_jobs = 0
    candidates_checked = 0
    for summary in output_jobs:
        if not isinstance(summary, dict) or summary.get("output_type") not in {
            "scene",
            "multi_product",
        }:
            continue
        job_id = summary.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            raise VerificationFailure("review_job_invalid")
        job = _get_json(
            client,
            f"{classification_url}/local/api/style-jobs/{job_id}",
            "review_job_unavailable",
        )
        current_job_id = job.get("current_job_id")
        if isinstance(current_job_id, str) and current_job_id and current_job_id != job_id:
            job = _get_json(
                client,
                f"{classification_url}/local/api/style-jobs/{current_job_id}",
                "current_review_job_unavailable",
            )
        if job.get("synthetic_test_only") is not True or job.get("publishable") is not False:
            raise VerificationFailure("synthetic_job_boundary_broken")
        candidates = job.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise VerificationFailure("candidate_batch_missing")
        reviewable_jobs += 1
        for candidate in candidates:
            if not isinstance(candidate, dict):
                raise VerificationFailure("candidate_invalid")
            if (
                candidate.get("synthetic_test_only") is not True
                or candidate.get("publishable") is not False
            ):
                raise VerificationFailure("synthetic_candidate_boundary_broken")
            content_url = candidate.get("content_url")
            expected_sha = candidate.get("content_sha256")
            if not isinstance(content_url, str) or not content_url.startswith("/"):
                raise VerificationFailure("candidate_content_url_invalid")
            if not isinstance(expected_sha, str) or len(expected_sha) != 64:
                raise VerificationFailure("candidate_sha256_invalid")
            content = _get_bytes(client, f"{classification_url}{content_url}")
            if sha256(content).hexdigest() != expected_sha:
                raise VerificationFailure("candidate_sha256_mismatch")
            candidates_checked += 1

    if reviewable_jobs == 0:
        raise VerificationFailure("reviewable_job_missing")
    return {
        "status": "pass",
        "mode": "synthetic_test_only",
        "publishable": False,
        "classification_page": "reachable",
        "style_service": "healthy",
        "handoff_id": handoff.get("handoff_id"),
        "batch_id": handoff.get("batch_id"),
        "reviewable_jobs": reviewable_jobs,
        "candidates_checked": candidates_checked,
        "issue_codes": [],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="验证本机分类到风格图的完整交接链。")
    parser.add_argument("--classification-url", default="http://127.0.0.1:8124")
    parser.add_argument("--style-url", default="http://127.0.0.1:18119")
    parser.add_argument("--confirm-synthetic", action="store_true")
    args = parser.parse_args()

    report: dict[str, object] = {
        "status": "fail",
        "mode": "synthetic_test_only",
        "publishable": False,
        "classification_page": "unknown",
        "style_service": "unknown",
        "handoff_id": None,
        "batch_id": None,
        "reviewable_jobs": 0,
        "candidates_checked": 0,
        "issue_codes": [],
    }
    if not args.confirm_synthetic:
        report["issue_codes"] = ["synthetic_confirmation_required"]
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 2
    try:
        classification_url = _loopback_origin(args.classification_url)
        style_url = _loopback_origin(args.style_url)
    except VerificationFailure as error:
        report["issue_codes"] = [error.code]
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 2

    try:
        with httpx.Client(timeout=8.0, follow_redirects=False, trust_env=False) as client:
            report = verify(
                client,
                classification_url=classification_url,
                style_url=style_url,
            )
    except (VerificationFailure, httpx.HTTPError) as error:
        code = error.code if isinstance(error, VerificationFailure) else "network_error"
        report["issue_codes"] = [code]
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 1

    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
