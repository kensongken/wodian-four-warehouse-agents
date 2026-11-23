from __future__ import annotations

import importlib.util
import json
import re
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "scripts/run_v2_three_stage_acceptance.py"


def load_script():
    spec = importlib.util.spec_from_file_location("v2_acceptance", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def run(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *arguments],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_requires_explicit_synthetic_confirmation() -> None:
    result = run("--check-only")

    assert result.returncode == 2
    assert json.loads(result.stdout) == {
        "status": "refused",
        "issue_codes": ["synthetic_confirmation_required"],
        "synthetic_only": False,
    }
    assert result.stderr == ""


def test_check_only_accepts_clean_isolated_ports() -> None:
    result = run("--check-only", "--confirm-synthetic")

    assert result.returncode == 0
    assert json.loads(result.stdout) == {
        "status": "ready",
        "issue_codes": [],
        "ports": {"product_engine": 18021, "sales_journey": 3003},
        "synthetic_only": True,
    }


def test_product_runtime_uses_an_independent_style_secret(tmp_path: Path) -> None:
    module = load_script()
    driver = module.LocalProcessDriver(tmp_path / "album", tmp_path / "sales")

    environment = driver._product_runtime_environment()

    assert environment["STYLE_SIGNING_SECRET"]
    assert environment["STYLE_SIGNING_SECRET"] != environment["SALES_SIGNING_SECRET"]


def test_check_only_refuses_an_occupied_product_engine_port() -> None:
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 18021))
    listener.listen()
    try:
        result = run("--check-only", "--confirm-synthetic")
    finally:
        listener.close()

    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert payload["status"] == "refused"
    assert payload["issue_codes"] == ["acceptance_port_in_use"]
    assert payload["synthetic_only"] is True


class FakeDriver:
    def __init__(self, *, fail_publish: bool = False) -> None:
        self.fail_publish = fail_publish
        self.stopped = False
        self.failure_evidence_path: Path | None = None
        self.lifecycle: list[str] = []

    def start(self) -> None:
        return None

    def publish_album_v2(self) -> dict[str, object]:
        if self.fail_publish:
            raise RuntimeError("synthetic publish failed")
        return {"status_code": 202, "event_id": "album-v2"}

    def relay_sales_v2(self) -> dict[str, object]:
        return {
            "delivered": 2,
            "retried": 0,
            "failed": 0,
            "worker_restart_recovered": True,
        }

    def replay_album_v2(self) -> dict[str, object]:
        return {"status_code": 200}

    def replay_sales_v2(self) -> dict[str, object]:
        return {"status_code": 200, "outcome": "duplicate"}

    def relay_material_usage(self) -> dict[str, object]:
        return {"status": "accepted_then_duplicate"}

    def inspect_continuity(self) -> dict[str, object]:
        return {
            "product_code": "SYNTHETIC-001",
            "product_rows": 1,
            "package_versions": [1, 2],
            "strategy_versions": [1, 2],
            "pending_sales_events": 0,
            "delivered_sales_events": 2,
        }

    def stop(self) -> None:
        self.lifecycle.append("stop")
        self.stopped = True

    def preserve_failure_evidence(self) -> Path:
        self.lifecycle.append("preserve")
        self.failure_evidence_path = Path("/tmp/synthetic-three-stage-failure")
        return self.failure_evidence_path


def test_execute_proves_version_two_and_always_stops_services() -> None:
    module = load_script()
    driver = FakeDriver()

    result = module.execute(driver)

    assert result["status"] == "completed"
    assert result["product_code"] == "SYNTHETIC-001"
    assert result["package_versions"] == [1, 2]
    assert result["strategy_versions"] == [1, 2]
    assert result["album_replay"] == "duplicate"
    assert result["sales_replay"] == "duplicate"
    assert result["material_usage_feedback"] == "accepted_then_duplicate"
    assert result["outbox_delivered"] == 2
    assert result["outbox_pending"] == 0
    assert result["worker_restart_recovered"] is True
    assert driver.stopped is True


def test_execute_stops_services_when_album_publish_fails() -> None:
    module = load_script()
    driver = FakeDriver(fail_publish=True)

    with pytest.raises(RuntimeError, match="synthetic publish failed"):
        module.execute(driver)

    assert driver.stopped is True
    assert driver.lifecycle == ["preserve", "stop"]


def test_failure_issue_code_exposes_only_stable_internal_codes() -> None:
    module = load_script()

    assert (
        module.failure_issue_code(RuntimeError("outbox_worker_delivery_timeout"))
        == "outbox_worker_delivery_timeout"
    )
    assert (
        module.failure_issue_code(RuntimeError("secret path /tmp/customer.json"))
        == "version_two_acceptance_failed"
    )
    assert (
        module.failure_issue_code(OSError("connection refused at customer host"))
        == "version_two_acceptance_failed"
    )


def test_local_driver_preserves_logs_before_temporary_cleanup(tmp_path: Path) -> None:
    module = load_script()
    sales_root = tmp_path / "sales"
    sales_root.mkdir()
    driver = module.LocalProcessDriver(
        tmp_path / "album", sales_root, failure_evidence_root=tmp_path
    )
    driver.temp_directory = tempfile.TemporaryDirectory(prefix="v2-test-")
    source_log = Path(driver.temp_directory.name) / "product.log"
    source_log.write_text("synthetic failure evidence\n")

    evidence_path = driver.preserve_failure_evidence()
    driver.stop()

    assert evidence_path == driver.failure_evidence_path
    assert evidence_path.is_dir()
    assert (evidence_path / "product.log").read_text() == "synthetic failure evidence\n"
    assert not source_log.exists()


def test_local_driver_restores_next_generated_type_file(tmp_path: Path) -> None:
    module = load_script()
    sales_root = tmp_path / "sales"
    sales_root.mkdir()
    generated = sales_root / "next-env.d.ts"
    generated.write_bytes(b"original\n")
    driver = module.LocalProcessDriver(tmp_path / "album", sales_root)
    driver._capture_generated_files()
    generated.write_bytes(b"next-dev-generated\n")

    driver.stop()

    assert generated.read_bytes() == b"original\n"


def test_health_wait_uses_a_direct_connection_without_environment_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_script()
    opened: list[tuple[str, float]] = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            del args

    class Opener:
        def open(self, url: str, timeout: float):
            opened.append((url, timeout))
            return Response()

    monkeypatch.setattr(module.urllib.request, "build_opener", lambda *handlers: Opener())
    monkeypatch.setattr(
        module.urllib.request,
        "urlopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("environment-aware urlopen must not be used")
        ),
    )

    module.LocalProcessDriver._wait_for_http("http://127.0.0.1:3003/")

    assert opened == [("http://127.0.0.1:3003/", 1)]


def test_temporary_database_name_is_generated_internally_and_identifier_safe() -> None:
    module = load_script()

    first = module.temporary_database_name()
    second = module.temporary_database_name()

    assert first != second
    assert re.fullmatch(r"product_engine_acceptance_[0-9a-f]{32}", first)
    assert re.fullmatch(r"product_engine_acceptance_[0-9a-f]{32}", second)


def test_database_is_migrated_before_services_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load_script()
    sales_root = tmp_path / "sales"
    sales_root.mkdir()
    driver = module.LocalProcessDriver(tmp_path / "album", sales_root)
    calls: list[str] = []

    monkeypatch.setattr(driver, "_require_clean_worktrees", lambda: calls.append("clean"))
    monkeypatch.setattr(driver, "_capture_generated_files", lambda: None)
    monkeypatch.setattr(driver, "_create_temporary_database", lambda: calls.append("create"))
    monkeypatch.setattr(driver, "_migrate_temporary_database", lambda: calls.append("migrate"))
    monkeypatch.setattr(
        module.subprocess,
        "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("process-started")),
    )

    with pytest.raises(RuntimeError, match="process-started"):
        driver.start()

    assert calls == ["clean", "create", "migrate"]


def test_stop_drops_temporary_database_even_after_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load_script()
    sales_root = tmp_path / "sales"
    sales_root.mkdir()
    driver = module.LocalProcessDriver(tmp_path / "album", sales_root)
    driver.database_created = True
    dropped: list[str] = []
    monkeypatch.setattr(
        driver,
        "_drop_temporary_database",
        lambda: dropped.append(driver.database_name),
    )

    driver.stop()

    assert dropped == [driver.database_name]
    assert driver.database_removed is True
