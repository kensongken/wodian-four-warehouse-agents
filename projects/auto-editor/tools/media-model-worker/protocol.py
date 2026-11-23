"""Strict, dependency-free JSON protocol for the isolated media model worker."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any


class ProtocolError(ValueError):
    """Safe protocol error whose message never contains request data."""


JOB_KEYS = frozenset(
    {
        "schemaVersion",
        "jobId",
        "customerId",
        "sessionId",
        "sourceAssetId",
        "sourceDigest",
        "sourcePath",
        "sourceStartMs",
        "sourceEndMs",
        "layer",
        "operationKey",
        "requestedAt",
    }
)
IDENTITY_KEYS = (
    "jobId",
    "customerId",
    "sessionId",
    "sourceAssetId",
    "operationKey",
)
BINDING_KEYS = (
    "jobId",
    "customerId",
    "sessionId",
    "sourceAssetId",
    "sourceDigest",
    "sourcePath",
    "sourceStartMs",
    "sourceEndMs",
    "layer",
    "operationKey",
)
LAYERS = frozenset({"technical", "scene", "transcript", "embedding", "vision"})
DIGEST_PATTERN = re.compile(r"^[a-f0-9]{64}$")
DATETIME_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$"
)


def _invalid_job() -> ProtocolError:
    return ProtocolError("studio-model-worker-invalid-job")


def validate_job(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != JOB_KEYS:
        raise _invalid_job()
    if value["schemaVersion"] != 1 or type(value["schemaVersion"]) is not int:
        raise _invalid_job()

    normalized = dict(value)
    for key in IDENTITY_KEYS:
        item = value[key]
        if not isinstance(item, str) or not item.strip():
            raise _invalid_job()
        normalized[key] = item.strip()

    source_path = value["sourcePath"]
    if (
        not isinstance(source_path, str)
        or not source_path.strip()
        or len(source_path.strip()) > 4096
    ):
        raise _invalid_job()
    normalized["sourcePath"] = source_path.strip()

    digest = value["sourceDigest"]
    if not isinstance(digest, str) or DIGEST_PATTERN.fullmatch(digest) is None:
        raise _invalid_job()

    start = value["sourceStartMs"]
    end = value["sourceEndMs"]
    if (
        type(start) is not int
        or type(end) is not int
        or start < 0
        or end <= 0
        or end <= start
    ):
        raise _invalid_job()
    if not isinstance(value["layer"], str) or value["layer"] not in LAYERS:
        raise _invalid_job()
    requested_at = value["requestedAt"]
    if not isinstance(requested_at, str) or DATETIME_PATTERN.fullmatch(requested_at) is None:
        raise _invalid_job()
    try:
        datetime.fromisoformat(requested_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise _invalid_job() from error
    return normalized


def build_unconfigured_receipt(
    job_input: Any,
    *,
    started_at: str,
    completed_at: str,
    runtime_ms: int,
) -> dict[str, Any]:
    job = validate_job(job_input)
    canonical_job = json.dumps(job, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    receipt = {
        "schemaVersion": 1,
        **{key: job[key] for key in BINDING_KEYS},
        "adapterName": "unconfigured-local-worker",
        "modelName": "unconfigured",
        "modelVersion": "none",
        "checkpointDigest": None,
        "inputDigest": hashlib.sha256(canonical_job.encode("utf-8")).hexdigest(),
        "promptVersion": "studio-worker-v1",
        "observations": [],
        "suggestedTags": [],
        "qualitySignals": {},
        "confidence": 0,
        "warnings": ["local-model-not-configured"],
        "startedAt": started_at,
        "completedAt": completed_at,
        "runtimeMs": runtime_ms,
        "status": "failed-manual-review",
        "failure": {"code": "studio-model-adapter-not-configured"},
        "synthetic": True,
        "providerMode": "offline-fallback",
    }
    return receipt
