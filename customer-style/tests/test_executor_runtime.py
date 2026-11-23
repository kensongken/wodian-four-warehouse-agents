"""Real process HTTP/TERM checks and zero-network executor supervision tests."""

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from threading import Event
from typing import Any

import pytest
from fastapi.testclient import TestClient

from opc_style import executor_runtime
from opc_style.executor_runtime import create_executor_app

ROOT = Path(__file__).resolve().parents[1]


def wait_ready(client: TestClient, expected: int) -> dict[str, Any]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = client.get("/ready")
        body = response.json()
        if (
            response.status_code == expected
            and body.get("probes", {}).get("config") != "starting"
        ):
            return body
        time.sleep(0.01)
    raise AssertionError("executor readiness did not settle")


def test_disabled_serves_http_without_any_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOURCE_SHA", "a" * 40)
    monkeypatch.setenv("STYLE_EXECUTOR_ENABLED", "false")
    monkeypatch.setenv("STYLE_ASSET_BACKEND", "disabled")

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("disabled executor accessed a dependency")

    for name in (
        "build_production_store",
        "create_database_engine",
        "RealImageProviderSettings",
        "MallStyleExecutor",
        "OpenAIBackgroundGenerator",
    ):
        monkeypatch.setattr(executor_runtime, name, forbidden)
    with TestClient(create_executor_app()) as client:
        assert wait_ready(client, 200)["execution"] == "disabled_safe"
        health = client.get("/health").json()
        assert health["mode"] == "executor" and health["enabled"] is False
        assert health["sourceSha"] == "a" * 40
        assert health["config"] == "ok"
        for path in (
            "/docs",
            "/openapi.json",
            "/v1/assets",
            "/internal/mall-style/v1/style-selections",
        ):
            assert client.get(path).status_code == 404
    assert not client.app.state.executor_runtime._thread.is_alive()


@pytest.mark.parametrize("invalid", ["source", "provider", "database"])
def test_invalid_config_stays_live_but_not_ready(
    monkeypatch: pytest.MonkeyPatch, invalid: str
) -> None:
    monkeypatch.setenv("SOURCE_SHA", "a" * 40 if invalid != "source" else "development")
    monkeypatch.setenv("STYLE_ASSET_BACKEND", "disabled")
    monkeypatch.setenv(
        "STYLE_EXECUTOR_ENABLED", "true" if invalid != "source" else "false"
    )
    monkeypatch.setenv("STYLE_EXECUTOR_PROVIDER", "openai")
    monkeypatch.setenv("STYLE_EXECUTOR_MODEL", "gpt-image-2")
    monkeypatch.setenv("STYLE_EXECUTOR_MAXIMUM_COST_USD", "0.36")
    monkeypatch.setenv("STYLE_EXECUTOR_ESTIMATED_COST_USD", "0.30")
    monkeypatch.setenv(
        "REAL_IMAGE_ENABLED", "true" if invalid == "database" else "false"
    )
    monkeypatch.setenv("REAL_IMAGE_API_KEY", "private-test-value-do-not-echo")
    monkeypatch.setenv("REAL_IMAGE_MAX_RUN_USD", "0.36")
    monkeypatch.setenv("STYLE_WAREHOUSE_V3_ENABLED", "false")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    with TestClient(create_executor_app()) as client:
        body = wait_ready(client, 503)
        assert body["status"] == "not_ready"
        assert "private-test-value" not in json.dumps(body)
        assert "sqlite" not in json.dumps(body)
        assert client.get("/health").status_code == 200


def configure_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {
        "SOURCE_SHA": "a" * 40,
        "STYLE_EXECUTOR_ENABLED": "true",
        "STYLE_EXECUTOR_PROVIDER": "openai",
        "STYLE_EXECUTOR_MODEL": "gpt-image-2",
        "STYLE_EXECUTOR_MAXIMUM_COST_USD": "0.36",
        "STYLE_EXECUTOR_ESTIMATED_COST_USD": "0.30",
        "STYLE_ASSET_BACKEND": "disabled",
        "REAL_IMAGE_ENABLED": "true",
        "REAL_IMAGE_API_KEY": "private-test-value",
        "REAL_IMAGE_MAX_RUN_USD": "0.36",
        "STYLE_WAREHOUSE_V3_ENABLED": "true",
        "STYLE_WAREHOUSE_SIGNING_SECRET": "test-signing-only",
        "STYLE_WAREHOUSE_VERIFY_SOURCE_IMAGES": "false",
        "DATABASE_URL": "postgresql://invalid/unused",
    }.items():
        monkeypatch.setenv(name, value)


@pytest.mark.parametrize("failed_probe", ["database", "schema", "storage", None])
def test_enabled_probes_gate_queue_and_shutdown_drains(
    monkeypatch: pytest.MonkeyPatch, failed_probe: str | None
) -> None:
    probe_ok = failed_probe is None
    configure_enabled(monkeypatch)
    events: list[str] = []
    in_flight, complete = Event(), Event()

    class Resource:
        def close(self) -> None:
            events.append("closed")

        def dispose(self) -> None:
            events.append("disposed")

    class Worker:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.stop = kwargs["stop_event"]

        def reconcile_expired_reservations(self) -> None:
            events.append("reconcile")

        def run_once(self) -> str:
            events.append("claim")
            in_flight.set()
            assert self.stop.wait(5)
            events.append("in_flight_finished")
            complete.set()
            return "completed"

        def release_unreserved_leases(self) -> None:
            if probe_ok:
                assert complete.is_set()
            events.append("release")

    monkeypatch.setattr(
        executor_runtime, "create_database_engine", lambda _: Resource()
    )
    monkeypatch.setattr(
        executor_runtime, "build_production_store", lambda _: Resource()
    )
    monkeypatch.setattr(
        executor_runtime, "OpenAIBackgroundGenerator", lambda *args, **kwargs: object()
    )
    monkeypatch.setattr(executor_runtime, "MallStyleExecutor", Worker)
    monkeypatch.setattr(
        executor_runtime,
        "readiness",
        lambda *args, **kwargs: (
            probe_ok,
            {
                "status": "ready" if probe_ok else "not_ready",
                "probes": {
                    "database": "database_unavailable"
                    if failed_probe == "database"
                    else "ok",
                    "schema": "schema_unavailable"
                    if failed_probe == "schema"
                    else "ok",
                    "storage": "private_storage_unavailable"
                    if failed_probe == "storage"
                    else "ok",
                },
            },
        ),
    )
    with TestClient(create_executor_app()) as client:
        body = wait_ready(client, 200 if probe_ok else 503)
        assert body["probes"]["provider"] == "ok"
        if probe_ok:
            assert in_flight.wait(5)
        else:
            assert "claim" not in events and "reconcile" not in events
        controller = client.app.state.executor_runtime
        controller.stop()
        assert client.get("/ready").status_code == 503
    assert events.count("claim") == (1 if probe_ok else 0)
    assert events.index("release") < events.index("disposed")
    if probe_ok:
        assert events.index("in_flight_finished") < events.index("release")


def test_container_entrypoint_http_on_port_and_sigterm() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {
        **os.environ,
        "PORT": str(port),
        "SOURCE_SHA": "b" * 40,
        "STYLE_EXECUTOR_ENABLED": "false",
        "STYLE_ASSET_BACKEND": "disabled",
        "PYTHONPATH": str(ROOT / "src"),
        # A disabled worker must not even load the legacy global DB engine.
        "DATABASE_URL": "not-a-database-url",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "opc_style.runtime", "executor"],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        body = None
        while time.monotonic() < deadline:
            assert proc.poll() is None, "executor exited instead of serving HTTP"
            try:
                with opener.open(
                    f"http://127.0.0.1:{port}/ready", timeout=1
                ) as response:
                    body = json.load(response)
                break
            except (urllib.error.URLError, TimeoutError):
                time.sleep(0.05)
        assert body and body["execution"] == "disabled_safe"
        assert body["sourceSha"] == "b" * 40
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=10) in (0, -signal.SIGTERM)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
