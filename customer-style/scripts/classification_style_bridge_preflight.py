"""Standard-library preflight before bridge acceptance guards are installed."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Any

# The original Task 9 baseline was superseded by the repository's deliberate
# migration-import normalization. Pin the last legitimate byte change so the
# waiver remains fail-closed without making every newer clean checkout fail.
CONSUMER_TASK_BASE = "b8decd8f7561da538aaaac8322d17be2588f1c6a"
PRODUCER_TASK_BASE = "a303fd22563cbabd98716fb175f37a1cf7612110"
CONSUMER_LINT_WAIVER_PATHS = (
    "alembic/versions/0001_style_base.py",
    "alembic/versions/0002_audit_events_immutable.py",
    "alembic/versions/0003_style_card_sample_links_immutable.py",
)
PRODUCER_LINT_WAIVER_PATHS = (
    "services/backend/src/product_engine/api/routes/sales_strategy_inputs.py",
    "services/backend/src/product_engine/repositories/sales_strategy_inputs.py",
    "services/backend/src/product_engine/services/local_pilot_acceptances.py",
    "services/backend/src/product_engine/services/outbox_relay.py",
    "services/backend/tests/api/test_sales_strategy_inputs.py",
    "services/backend/tests/contracts/test_sales_strategy_contracts.py",
    "services/backend/tests/local/test_local_pilot_acceptances.py",
    "services/backend/tests/repositories/test_sales_strategy_inputs.py",
    "services/backend/tests/services/test_album_ingestion.py",
    "services/backend/tests/services/test_outbox_relay.py",
    "services/backend/tests/services/test_three_stage_handoff_fixture.py",
    "services/backend/tests/workers/test_outbox_publisher.py",
)

_RELEVANT_SUFFIXES = {
    ".cfg",
    ".css",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".lock",
    ".md",
    ".mjs",
    ".py",
    ".pyi",
    ".sql",
    ".toml",
    ".yaml",
    ".yml",
}
_RELEVANT_NAMES = {"Dockerfile", "Makefile", "README", ".gitignore"}
_RUNTIME_DISTRIBUTIONS = (
    "fastapi",
    "httpx",
    "pillow",
    "pydantic",
    "pydantic-settings",
    "sqlalchemy",
    "uvicorn",
)


class PreflightError(ValueError):
    """A stable fail-closed repository preflight error."""


def runtime_summary() -> dict[str, Any]:
    """Observe the interpreter and bridge-critical installed distributions."""
    dependencies: list[dict[str, str]] = []
    for name in _RUNTIME_DISTRIBUTIONS:
        try:
            version = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError as error:
            raise PreflightError(f"runtime_dependency_missing:{name}") from error
        dependencies.append({"name": name, "version": version})
    return {
        "implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "cache_tag": sys.implementation.cache_tag,
        "platform_system": platform.system(),
        "platform_machine": platform.machine(),
        "dependencies": sorted(dependencies, key=lambda item: item["name"]),
    }


def verify_lock(root: Path, *, label: str) -> dict[str, object]:
    """Run the repository's lock validation with networking disabled."""
    command = ["uv", "lock", "--check", "--offline"]
    executable = shutil.which("uv")
    if executable is None:
        raise PreflightError(f"{label}_uv_unavailable")
    environment = os.environ.copy()
    environment["UV_OFFLINE"] = "1"
    environment["UV_NO_PROGRESS"] = "1"
    try:
        completed = subprocess.run(
            [executable, *command[1:]],
            cwd=root,
            env=environment,
            check=False,
            capture_output=True,
        )
    except OSError as error:
        raise PreflightError(f"{label}_lock_check_unavailable") from error
    if completed.returncode != 0:
        raise PreflightError(f"{label}_lock_check_failed")
    return {
        "label": label,
        "command": command,
        "exit_code": completed.returncode,
        "result": "PASS",
    }


def _git(root: Path, *args: str) -> bytes:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=root,
            check=False,
            capture_output=True,
        )
    except OSError as error:
        raise PreflightError("git_command_unavailable") from error
    if completed.returncode != 0:
        raise PreflightError("git_command_failed")
    return completed.stdout


def _is_relevant(path: str) -> bool:
    candidate = PurePosixPath(path)
    return (
        candidate.suffix.lower() in _RELEVANT_SUFFIXES
        or candidate.name in _RELEVANT_NAMES
        or candidate.name.startswith("README.")
    )


def inspect_repository(
    root: Path,
    *,
    label: str,
    expected_head: str,
    task_base: str,
    lint_waiver_paths: Sequence[str],
) -> dict[str, Any]:
    """Require one clean pin and seal all relevant tracked working-tree bytes."""
    resolved = root.resolve(strict=True)
    head = _git(resolved, "rev-parse", "HEAD").decode("ascii").strip()
    if head != expected_head:
        raise PreflightError(f"{label}_head_mismatch")
    if _git(resolved, "status", "--porcelain=v1", "-z", "--untracked-files=all"):
        raise PreflightError(f"{label}_worktree_not_clean")

    tracked = _git(resolved, "ls-files", "-z").decode("utf-8").split("\0")
    sealed: list[dict[str, str]] = []
    for relative in sorted(path for path in tracked if path and _is_relevant(path)):
        target = resolved / relative
        if target.is_symlink() or not target.is_file():
            raise PreflightError(f"{label}_tracked_file_unavailable")
        sealed.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            }
        )
    tree_payload = "".join(
        f"{item['path']}\0{item['sha256']}\n" for item in sealed
    ).encode("utf-8")

    waiver: list[dict[str, object]] = []
    for relative in lint_waiver_paths:
        if relative not in {item["path"] for item in sealed}:
            raise PreflightError(f"{label}_lint_waiver_file_unsealed")
        current = hashlib.sha256((resolved / relative).read_bytes()).hexdigest()
        baseline = hashlib.sha256(
            _git(resolved, "show", f"{task_base}:{relative}")
        ).hexdigest()
        if current != baseline:
            raise PreflightError(f"{label}_lint_waiver_changed")
        waiver.append(
            {
                "path": relative,
                "task_base_sha256": baseline,
                "current_sha256": current,
                "unchanged": True,
            }
        )

    return {
        "clean": True,
        "head": head,
        "task_base": task_base,
        "tracked_file_count": len(sealed),
        "tracked_files": sealed,
        "tracked_tree_sha256": hashlib.sha256(tree_payload).hexdigest(),
        "lint_baseline_waiver": waiver,
    }


def build_preflight(
    *,
    consumer_root: Path,
    producer_root: Path,
    consumer_head: str,
    producer_head: str,
) -> dict[str, Any]:
    """Run offline lock checks, then bind both clean worktrees and runtime."""
    lock_checks = [
        verify_lock(consumer_root, label="consumer"),
        verify_lock(producer_root / "services" / "backend", label="producer"),
    ]
    repositories = {
        "consumer": inspect_repository(
            consumer_root,
            label="consumer",
            expected_head=consumer_head,
            task_base=CONSUMER_TASK_BASE,
            lint_waiver_paths=CONSUMER_LINT_WAIVER_PATHS,
        ),
        "producer": inspect_repository(
            producer_root,
            label="producer",
            expected_head=producer_head,
            task_base=PRODUCER_TASK_BASE,
            lint_waiver_paths=PRODUCER_LINT_WAIVER_PATHS,
        ),
    }
    return {
        "result": "PASS",
        "guard_boundary": "preflight_before_business_guard_activation",
        "lock_checks": lock_checks,
        "repositories": repositories,
        "runtime": runtime_summary(),
    }


def emit_bootstrap_failure(
    *, error: Exception, output: Path | None, schema_version: str
) -> int:
    """Emit a bounded pre-guard failure receipt without exposing local paths."""
    manifest = {
        "schema_version": schema_version,
        "result": "FAIL",
        "classification": "synthetic_test_only",
        "publishable": False,
        "production_eligible": False,
        "error_type": type(error).__name__,
        "error_code": str(error),
        "failure_phase": "repository_preflight",
    }
    encoded = (
        json.dumps(
            manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        + b"\n"
    )
    if output is not None:
        output.write_bytes(encoded)
    sys.stdout.buffer.write(encoded)
    return 1
