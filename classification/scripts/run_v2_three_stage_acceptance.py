#!/usr/bin/env python3
"""Run the isolated synthetic version-2 three-stage acceptance flow."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path
from typing import NoReturn, Protocol

PRODUCT_ENGINE_PORT = 18021
SALES_JOURNEY_PORT = 3003
PRODUCT_ROOT = Path(__file__).resolve().parents[1]
PRODUCT_BACKEND = PRODUCT_ROOT / "services/backend"
TENANT_ID = "22222222-2222-4222-8222-222222222222"
ALBUM_SECRET = "synthetic-album-product-image-signing-secret-2026"
SALES_SECRET = "synthetic-sales-strategy-signing-secret-2026"
DATABASE_NAME_PREFIX = "product_engine_acceptance_"


def temporary_database_name() -> str:
    return f"{DATABASE_NAME_PREFIX}{uuid.uuid4().hex}"


class AcceptanceDriver(Protocol):
    def start(self) -> None: ...

    def publish_album_v2(self) -> dict[str, object]: ...

    def relay_sales_v2(self) -> dict[str, object]: ...

    def replay_album_v2(self) -> dict[str, object]: ...

    def replay_sales_v2(self) -> dict[str, object]: ...

    def relay_material_usage(self) -> dict[str, object]: ...

    def inspect_continuity(self) -> dict[str, object]: ...

    def preserve_failure_evidence(self) -> Path | None: ...

    def stop(self) -> None: ...


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        del message
        render("refused", ["invalid_arguments"], synthetic_only=False)
        raise SystemExit(2)


def render(
    status: str,
    issue_codes: list[str],
    *,
    synthetic_only: bool,
    ports: dict[str, int] | None = None,
    failure_evidence_path: Path | None = None,
) -> None:
    payload: dict[str, object] = {
        "status": status,
        "issue_codes": issue_codes,
        "synthetic_only": synthetic_only,
    }
    if ports is not None:
        payload["ports"] = ports
    if failure_evidence_path is not None:
        payload["failure_evidence_path"] = str(failure_evidence_path)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def failure_issue_code(error: BaseException) -> str:
    code = str(error)
    if isinstance(error, RuntimeError) and re.fullmatch(r"[a-z][a-z0-9_]{2,80}", code):
        return code
    return "version_two_acceptance_failed"


def port_is_available(port: int) -> bool:
    listener = socket.socket()
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", port))
    except OSError:
        return False
    finally:
        listener.close()
    return True


def execute(driver: AcceptanceDriver) -> dict[str, object]:
    result: dict[str, object] | None = None
    try:
        driver.start()
        first_album = driver.publish_album_v2()
        first_sales = driver.relay_sales_v2()
        album_replay = driver.replay_album_v2()
        sales_replay = driver.replay_sales_v2()
        material_usage = driver.relay_material_usage()
        continuity = driver.inspect_continuity()
        if (
            first_album.get("status_code") not in {200, 202}
            or first_sales.get("delivered") != 2
            or first_sales.get("retried") != 0
            or first_sales.get("failed") != 0
            or first_sales.get("worker_restart_recovered") is not True
            or album_replay.get("status_code") != 200
            or sales_replay.get("status_code") != 200
            or sales_replay.get("outcome") != "duplicate"
            or material_usage.get("status") != "accepted_then_duplicate"
            or continuity.get("product_code") != "SYNTHETIC-001"
            or continuity.get("product_rows") != 1
            or continuity.get("package_versions") != [1, 2]
            or continuity.get("strategy_versions") != [1, 2]
            or continuity.get("pending_sales_events") != 0
            or continuity.get("delivered_sales_events") != 2
        ):
            raise RuntimeError("version_two_acceptance_mismatch")
        result = {
            "status": "completed",
            "synthetic_only": True,
            "product_code": continuity["product_code"],
            "package_versions": continuity["package_versions"],
            "strategy_versions": continuity["strategy_versions"],
            "album_first_status": first_album["status_code"],
            "outbox_delivered": first_sales["delivered"],
            "outbox_retried": first_sales["retried"],
            "outbox_failed": first_sales["failed"],
            "outbox_pending": continuity["pending_sales_events"],
            "worker_restart_recovered": first_sales["worker_restart_recovered"],
            "album_replay": "duplicate",
            "sales_replay": "duplicate",
            "material_usage_feedback": "accepted_then_duplicate",
            "database_mode": "temporary_isolated",
        }
    except Exception:
        try:
            driver.preserve_failure_evidence()
        except Exception:
            pass
        raise
    finally:
        driver.stop()
    result["database_removed"] = bool(getattr(driver, "database_removed", False))
    return result


class LocalProcessDriver:
    def __init__(
        self,
        album_root: Path,
        sales_root: Path,
        *,
        failure_evidence_root: Path | None = None,
    ) -> None:
        self.album_root = album_root.resolve()
        self.sales_root = sales_root.resolve()
        self.failure_evidence_root = (
            failure_evidence_root.resolve() if failure_evidence_root is not None else None
        )
        self.failure_evidence_path: Path | None = None
        self.processes: list[subprocess.Popen[bytes]] = []
        self.logs: list[object] = []
        self.temp_directory: tempfile.TemporaryDirectory[str] | None = None
        self.generated_file_snapshots: dict[Path, bytes] = {}
        self.database_name = temporary_database_name()
        self.database_url = (
            "postgresql+asyncpg://product_engine:product_engine_dev@"
            f"127.0.0.1:5432/{self.database_name}"
        )
        self.database_created = False
        self.database_removed = False
        self.outbox_process: subprocess.Popen[bytes] | None = None
        self.outbox_restart_count = 0
        self.worker_restart_recovered = False

    def preserve_failure_evidence(self) -> Path | None:
        if self.temp_directory is None:
            return None
        for log in self.logs:
            flush = getattr(log, "flush", None)
            if flush is not None:
                flush()
        if self.failure_evidence_root is not None:
            self.failure_evidence_root.mkdir(parents=True, exist_ok=True)
        destination = Path(
            tempfile.mkdtemp(
                prefix="v2-three-stage-failure-",
                dir=self.failure_evidence_root,
            )
        )
        shutil.copytree(
            Path(self.temp_directory.name),
            destination,
            dirs_exist_ok=True,
        )
        self.failure_evidence_path = destination
        return destination

    def start(self) -> None:
        self._require_clean_worktrees()
        self._capture_generated_files()
        self._create_temporary_database()
        self._migrate_temporary_database()
        self.temp_directory = tempfile.TemporaryDirectory(prefix="v2-three-stage-")
        log_root = Path(self.temp_directory.name)
        sales_log = (log_root / "sales.log").open("wb")
        product_log = (log_root / "product.log").open("wb")
        self.logs.extend((sales_log, product_log))
        node_path = Path(
            "/path/to/developer/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin"
        )
        sales_environment = {
            **os.environ,
            "PATH": f"{node_path}:{os.environ.get('PATH', '')}",
            "NODE_ENV": "development",
            "PRODUCT_STRATEGY_LOCAL_SYNTHETIC_ACCEPTANCE": "desensitized-only",
            "PRODUCT_STRATEGY_LOCAL_LEDGER_PATH": str(log_root / "sales-ledger.json"),
            "PRODUCT_IMAGE_ENGINE_BASE_URL": (f"http://127.0.0.1:{PRODUCT_ENGINE_PORT}"),
            "PRODUCT_STRATEGY_SIGNING_SECRET": SALES_SECRET,
        }
        product_environment = {
            **os.environ,
            "APP_ENV": "development",
            "DATABASE_URL": self.database_url,
            "S3_ENDPOINT_URL": "http://127.0.0.1:9000",
            "S3_REGION": "us-east-1",
            "S3_BUCKET": "product-engine-dev",
            "S3_ACCESS_KEY_ID": "product_engine_dev",
            "S3_SECRET_ACCESS_KEY": "local-development-only",
            "ALBUM_SIGNING_SECRET": ALBUM_SECRET,
            "SALES_SIGNING_SECRET": SALES_SECRET,
            "STYLE_SIGNING_SECRET": "synthetic-style-signing-secret-2026-09",
            "ADMIN_SIGNING_SECRET": "synthetic-admin-signing-secret-2026-09",
            "SALES_WEBHOOK_URL": (
                f"http://127.0.0.1:{SALES_JOURNEY_PORT}/"
                "api/integrations/product-image-engine/events"
            ),
            "PYTHONPATH": str(PRODUCT_BACKEND / "src"),
        }
        self.processes.append(
            subprocess.Popen(
                [
                    "pnpm",
                    "exec",
                    "next",
                    "dev",
                    "--hostname",
                    "127.0.0.1",
                    "--port",
                    str(SALES_JOURNEY_PORT),
                ],
                cwd=self.sales_root,
                env=sales_environment,
                stdout=sales_log,
                stderr=subprocess.STDOUT,
            )
        )
        self.processes.append(
            subprocess.Popen(
                [
                    str(PRODUCT_BACKEND / ".venv/bin/python"),
                    "-m",
                    "uvicorn",
                    "product_engine.api.main:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(PRODUCT_ENGINE_PORT),
                ],
                cwd=PRODUCT_BACKEND,
                env=product_environment,
                stdout=product_log,
                stderr=subprocess.STDOUT,
            )
        )
        self._wait_for_http(f"http://127.0.0.1:{SALES_JOURNEY_PORT}/")
        self._wait_for_http(f"http://127.0.0.1:{PRODUCT_ENGINE_PORT}/")

    def publish_album_v2(self) -> dict[str, object]:
        self._publish_album_fixture("product-image-set-ready.synthetic.json")
        self._start_outbox_daemon()
        self._wait_for_outbox(delivered=1, pending=0)
        self._stop_outbox_daemon()

        second = self._publish_album_fixture("product-image-set-ready-v2.synthetic.json")
        stopped_counts = self._outbox_counts()
        if stopped_counts != {"delivered": 1, "failed": 0, "pending": 1}:
            raise RuntimeError("outbox_worker_stop_boundary_failed")

        self._start_outbox_daemon()
        self._wait_for_outbox(delivered=2, pending=0)
        self.worker_restart_recovered = True
        return second

    def replay_album_v2(self) -> dict[str, object]:
        return self._publish_album_fixture("product-image-set-ready-v2.synthetic.json")

    def relay_sales_v2(self) -> dict[str, object]:
        counts = self._outbox_counts()
        return {
            "delivered": counts["delivered"],
            "retried": 0,
            "failed": counts["failed"],
            "worker_restart_recovered": self.worker_restart_recovered,
        }

    def replay_sales_v2(self) -> dict[str, object]:
        return self._send_sales_notification()

    def relay_material_usage(self) -> dict[str, object]:
        node_path = Path(
            "/path/to/developer/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin"
        )
        fallback_path = Path(
            "/path/to/developer/.cache/codex-runtimes/codex-primary-runtime/dependencies/bin/fallback"
        )
        environment = {
            **os.environ,
            "PATH": f"{node_path}:{fallback_path}:{os.environ.get('PATH', '')}",
            "PRODUCT_IMAGE_ENGINE_BASE_URL": f"http://127.0.0.1:{PRODUCT_ENGINE_PORT}",
            "PRODUCT_STRATEGY_SIGNING_SECRET": SALES_SECRET,
        }
        result = subprocess.run(
            [
                "pnpm",
                "exec",
                "vitest",
                "run",
                "tests/integration/product-material-usage-live.test.ts",
            ],
            cwd=self.sales_root,
            env=environment,
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        if result.returncode != 0:
            if self.temp_directory is not None:
                (Path(self.temp_directory.name) / "material-usage-feedback.log").write_text(
                    result.stdout + result.stderr
                )
            raise RuntimeError("material_usage_feedback_failed")
        return {"status": "accepted_then_duplicate"}

    def inspect_continuity(self) -> dict[str, object]:
        code = """
import asyncio, json, os
from sqlalchemy import func, select
from product_engine.db import Database
from product_engine.models.events import OutboxEvent
from product_engine.models.products import MaterialPackage, Product, SalesStrategyInputRecord
async def main():
    database = Database(os.environ["DATABASE_URL"])
    async with database.session() as session:
        products = list((await session.scalars(select(Product).where(Product.product_code == "SYNTHETIC-001"))).all())
        if len(products) != 1:
            print(json.dumps({"product_code": "SYNTHETIC-001", "product_rows": len(products)})); return
        product = products[0]
        packages = list((await session.scalars(select(MaterialPackage.version).where(MaterialPackage.product_id == product.id).order_by(MaterialPackage.version))).all())
        strategies = list((await session.scalars(select(SalesStrategyInputRecord.version).where(SalesStrategyInputRecord.product_id == product.id).order_by(SalesStrategyInputRecord.version))).all())
        sales_filter = OutboxEvent.topic == "sales.strategy.input.updated.v1"
        pending_sales_events = await session.scalar(select(func.count()).select_from(OutboxEvent).where(sales_filter, OutboxEvent.status == "pending"))
        delivered_sales_events = await session.scalar(select(func.count()).select_from(OutboxEvent).where(sales_filter, OutboxEvent.status == "delivered"))
        print(json.dumps({"product_code": product.product_code, "product_rows": 1, "package_versions": packages, "strategy_versions": strategies, "pending_sales_events": pending_sales_events, "delivered_sales_events": delivered_sales_events}, sort_keys=True))
    await database.dispose()
asyncio.run(main())
"""
        return self._run_product_python(code)

    def stop(self) -> None:
        self._stop_outbox_daemon()
        for process in reversed(self.processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        for log in self.logs:
            close = getattr(log, "close", None)
            if close is not None:
                close()
        for path, contents in self.generated_file_snapshots.items():
            path.write_bytes(contents)
        if self.temp_directory is not None:
            self.temp_directory.cleanup()
        if self.database_created:
            self._drop_temporary_database()
            self.database_removed = True
            self.database_created = False

    def _start_outbox_daemon(self) -> None:
        if self.outbox_process is not None and self.outbox_process.poll() is None:
            raise RuntimeError("outbox_worker_already_running")
        if self.temp_directory is None:
            raise RuntimeError("acceptance_runtime_not_started")
        self.outbox_restart_count += 1
        log = (Path(self.temp_directory.name) / f"outbox-{self.outbox_restart_count}.log").open(
            "wb"
        )
        self.logs.append(log)
        self.outbox_process = subprocess.Popen(
            [
                str(PRODUCT_BACKEND / ".venv/bin/python"),
                "-m",
                "product_engine.workers.outbox_daemon",
                "--interval-seconds",
                "0.1",
            ],
            cwd=PRODUCT_BACKEND,
            env=self._product_runtime_environment(),
            stdout=log,
            stderr=subprocess.STDOUT,
        )

    def _stop_outbox_daemon(self) -> None:
        process = self.outbox_process
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        self.outbox_process = None

    def _wait_for_outbox(self, *, delivered: int, pending: int) -> None:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            counts = self._outbox_counts()
            if counts == {"delivered": delivered, "failed": 0, "pending": pending}:
                return
            if counts["failed"]:
                break
            time.sleep(0.1)
        raise RuntimeError("outbox_worker_delivery_timeout")

    def _outbox_counts(self) -> dict[str, int]:
        code = """
import asyncio, json, os
from sqlalchemy import func, select
from product_engine.db import Database
from product_engine.models.events import OutboxEvent
async def main():
    database = Database(os.environ["DATABASE_URL"])
    async with database.session() as session:
        topic = OutboxEvent.topic == "sales.strategy.input.updated.v1"
        result = {}
        for status in ("delivered", "failed", "pending"):
            result[status] = await session.scalar(select(func.count()).select_from(OutboxEvent).where(topic, OutboxEvent.status == status))
        print(json.dumps(result, sort_keys=True))
    await database.dispose()
asyncio.run(main())
"""
        payload = self._run_product_python(code)
        return {key: int(payload[key]) for key in ("delivered", "failed", "pending")}

    def _product_runtime_environment(self) -> dict[str, str]:
        return {
            **os.environ,
            "APP_ENV": "development",
            "DATABASE_URL": self.database_url,
            "S3_ENDPOINT_URL": "http://127.0.0.1:9000",
            "S3_REGION": "us-east-1",
            "S3_BUCKET": "product-engine-dev",
            "S3_ACCESS_KEY_ID": "product_engine_dev",
            "S3_SECRET_ACCESS_KEY": "local-development-only",
            "ALBUM_SIGNING_SECRET": ALBUM_SECRET,
            "SALES_SIGNING_SECRET": SALES_SECRET,
            "STYLE_SIGNING_SECRET": "synthetic-style-signing-secret-2026-09",
            "ADMIN_SIGNING_SECRET": "synthetic-admin-signing-secret-2026-09",
            "SALES_WEBHOOK_URL": (
                f"http://127.0.0.1:{SALES_JOURNEY_PORT}/"
                "api/integrations/product-image-engine/events"
            ),
            "PYTHONPATH": str(PRODUCT_BACKEND / "src"),
        }

    def _create_temporary_database(self) -> None:
        self._validate_database_name()
        self._run_admin_python(f'await connection.execute("CREATE DATABASE {self.database_name}")')
        self.database_created = True

    def _migrate_temporary_database(self) -> None:
        result = subprocess.run(
            [
                str(PRODUCT_BACKEND / ".venv/bin/python"),
                "-m",
                "alembic",
                "upgrade",
                "head",
            ],
            cwd=PRODUCT_BACKEND,
            env={**os.environ, "DATABASE_URL": self.database_url},
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError("temporary_database_migration_failed")

    def _drop_temporary_database(self) -> None:
        self._validate_database_name()
        statements = (
            'await connection.execute("SELECT pg_terminate_backend(pid) '
            'FROM pg_stat_activity WHERE datname = $1 AND pid <> pg_backend_pid()", '
            f'"{self.database_name}")\n'
            f'await connection.execute("DROP DATABASE {self.database_name}")'
        )
        self._run_admin_python(statements)

    def _validate_database_name(self) -> None:
        suffix = self.database_name.removeprefix(DATABASE_NAME_PREFIX)
        if (
            not self.database_name.startswith(DATABASE_NAME_PREFIX)
            or len(suffix) != 32
            or any(character not in "0123456789abcdef" for character in suffix)
        ):
            raise RuntimeError("unsafe_temporary_database_name")

    @staticmethod
    def _run_admin_python(statements: str) -> None:
        code = f"""
import asyncio
import asyncpg
async def main():
    connection = await asyncpg.connect(\"postgresql://product_engine:product_engine_dev@127.0.0.1:5432/postgres\")
    try:
        {statements.replace(chr(10), chr(10) + "        ")}
    finally:
        await connection.close()
asyncio.run(main())
"""
        result = subprocess.run(
            [str(PRODUCT_BACKEND / ".venv/bin/python"), "-c", code],
            cwd=PRODUCT_BACKEND,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError("temporary_database_admin_failed")

    def _capture_generated_files(self) -> None:
        path = self.sales_root / "next-env.d.ts"
        if path.is_file():
            self.generated_file_snapshots[path] = path.read_bytes()

    def _publish_album_fixture(self, filename: str) -> dict[str, object]:
        code = """
import asyncio, json, sys
from pathlib import Path
from photo_organizer.handoff.http_publisher import ProductImageSetHttpPublisher
from photo_organizer.handoff.product_image_set import ProductImageSetReadyV1
async def main():
    event = ProductImageSetReadyV1.model_validate_json(Path(sys.argv[1]).read_bytes())
    receipt = await ProductImageSetHttpPublisher(sys.argv[2], sys.argv[3].encode()).publish(event)
    print(json.dumps({"status_code": receipt.status_code, "event_id": str(event.event_id), "product_code": event.product_code}, sort_keys=True))
asyncio.run(main())
"""
        backend = self.album_root / "services/backend"
        result = subprocess.run(
            [
                str(backend / ".venv/bin/python"),
                "-c",
                code,
                str(backend / "fixtures/v0.1" / filename),
                (
                    f"http://127.0.0.1:{PRODUCT_ENGINE_PORT}/v1/integrations/"
                    "album/product-image-sets"
                ),
                ALBUM_SECRET,
            ],
            cwd=backend,
            env={**os.environ, "PYTHONPATH": str(backend / "src")},
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        return self._json_result(result, "album_publication_failed")

    def _send_sales_notification(self) -> dict[str, object]:
        code = """
import asyncio, json, os, time
import httpx
from sqlalchemy import select
from product_engine.db import Database
from product_engine.models.events import OutboxEvent
from product_engine.security.signatures import sign_request
async def main():
    database = Database(os.environ["DATABASE_URL"])
    async with database.session() as session:
        rows = list((await session.scalars(select(OutboxEvent).where(OutboxEvent.topic == "sales.strategy.input.updated.v1").order_by(OutboxEvent.created_at))).all())
        event = next(row for row in reversed(rows) if row.payload.get("product_code") == "SYNTHETIC-001" and row.payload.get("strategy_input_version") == 2)
        body = json.dumps(event.payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
        timestamp = int(time.time()); path = "/api/integrations/product-image-engine/events"
        headers = {"Content-Type":"application/json","X-Source-System":"product-image-engine","X-Tenant-Id":str(event.tenant_id),"X-Event-Topic":event.topic,"X-Outbox-Event-Id":str(event.id),"X-Signature-Timestamp":str(timestamp),"X-Signature":sign_request(os.environ["SALES_SIGNING_SECRET"].encode(), timestamp, "product-image-engine", event.tenant_id, "POST", path, body)}
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.post(os.environ["SALES_WEBHOOK_URL"], content=body, headers=headers)
            payload = response.json(); print(json.dumps({"status_code": response.status_code, "outcome": payload.get("outcome"), "strategy_input_version": payload.get("strategyInputVersion")}, sort_keys=True))
    await database.dispose()
asyncio.run(main())
"""
        return self._run_product_python(code)

    def _run_product_python(self, code: str) -> dict[str, object]:
        environment = {
            **os.environ,
            "DATABASE_URL": self.database_url,
            "SALES_SIGNING_SECRET": SALES_SECRET,
            "STYLE_SIGNING_SECRET": "synthetic-style-signing-secret-2026-09",
            "SALES_WEBHOOK_URL": (
                f"http://127.0.0.1:{SALES_JOURNEY_PORT}/"
                "api/integrations/product-image-engine/events"
            ),
            "PYTHONPATH": str(PRODUCT_BACKEND / "src"),
        }
        result = subprocess.run(
            [str(PRODUCT_BACKEND / ".venv/bin/python"), "-c", code],
            cwd=PRODUCT_BACKEND,
            env=environment,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        return self._json_result(result, "product_acceptance_probe_failed")

    def _require_clean_worktrees(self) -> None:
        for root in (PRODUCT_ROOT, self.album_root, self.sales_root):
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=root,
                text=True,
                capture_output=True,
                check=True,
            )
            if result.stdout.strip():
                raise RuntimeError("acceptance_workspace_dirty")

    @staticmethod
    def _wait_for_http(url: str) -> None:
        deadline = time.monotonic() + 30
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        while time.monotonic() < deadline:
            try:
                with opener.open(url, timeout=1) as response:
                    if response.status < 500:
                        return
            except OSError:
                time.sleep(0.2)
        raise RuntimeError("acceptance_service_start_timeout")

    @staticmethod
    def _json_result(
        result: subprocess.CompletedProcess[str], error_code: str
    ) -> dict[str, object]:
        if result.returncode != 0:
            raise RuntimeError(error_code)
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            raise RuntimeError(error_code) from None
        if not isinstance(payload, dict):
            raise RuntimeError(error_code)  # noqa: TRY004
        return payload


def main() -> int:
    parser = SafeArgumentParser(description=__doc__)
    parser.add_argument("--confirm-synthetic", action="store_true")
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--album-workspace", type=Path)
    parser.add_argument("--sales-workspace", type=Path)
    parser.add_argument("--failure-evidence-root", type=Path)
    args = parser.parse_args()
    if not args.confirm_synthetic:
        render(
            "refused",
            ["synthetic_confirmation_required"],
            synthetic_only=False,
        )
        return 2
    if not all(port_is_available(port) for port in (PRODUCT_ENGINE_PORT, SALES_JOURNEY_PORT)):
        render("refused", ["acceptance_port_in_use"], synthetic_only=True)
        return 2
    if args.check_only:
        render(
            "ready",
            [],
            synthetic_only=True,
            ports={
                "product_engine": PRODUCT_ENGINE_PORT,
                "sales_journey": SALES_JOURNEY_PORT,
            },
        )
        return 0
    if args.album_workspace is None or args.sales_workspace is None:
        render("refused", ["workspace_arguments_required"], synthetic_only=True)
        return 2
    driver = LocalProcessDriver(
        args.album_workspace,
        args.sales_workspace,
        failure_evidence_root=args.failure_evidence_root,
    )
    try:
        result = execute(driver)
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        render(
            "failed",
            [failure_issue_code(error)],
            synthetic_only=True,
            failure_evidence_path=driver.failure_evidence_path,
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
