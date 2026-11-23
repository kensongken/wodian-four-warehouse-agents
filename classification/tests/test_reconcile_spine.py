import os
import subprocess
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


def test_reconciliation_fails_closed_without_database_url() -> None:
    root = Path(__file__).resolve().parents[1]
    environment = {
        key: value for key, value in os.environ.items() if key != "DATABASE_URL"
    }
    result = subprocess.run(
        [sys.executable, "scripts/reconcile_spine.py", "--check"],
        cwd=root,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2
    assert result.stdout == "database_url_missing=1\n"
    assert result.stderr == ""


def test_reconciliation_requires_explicit_check_flag() -> None:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "scripts/reconcile_spine.py"],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2
    assert "--check" in result.stderr


def test_reconciliation_keeps_all_phase_one_error_codes() -> None:
    root = Path(__file__).resolve().parents[1]
    spec = spec_from_file_location(
        "reconcile_spine", root / "scripts/reconcile_spine.py"
    )
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    assert set(module.CHECKS) == {
        "product_without_package",
        "source_without_product",
        "usage_without_product_or_package",
        "queued_screenshot_without_outbox",
        "delivered_without_timestamp",
        "pending_exhausted",
        "failed_before_exhausted",
    }
