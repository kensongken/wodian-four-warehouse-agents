"""Cloud executor supervision with health HTTP, safe disable and graceful drain."""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from threading import Event, Lock, Thread
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from opc_style.production_runtime import build_production_store, readiness
from opc_style.production_settings import ProductionSettings
from opc_style.providers.identity_openai_backgrounds import OpenAIBackgroundGenerator
from opc_style.providers.openai_images import RealImageBudget
from opc_style.services.mall_style_executor import MallStyleExecutor
from opc_style.services.s3_asset_store import S3AssetStore
from opc_style.settings import RealImageProviderSettings, StyleWarehouseV3Settings


def create_database_engine(database_url: str) -> Engine:
    """Load the legacy DB module only after enabled authority checks."""
    from opc_style.db import create_database_engine as factory

    return factory(database_url)


class ExecutorRuntime:
    """Only the worker thread accesses DB/storage; HTTP uses a safe snapshot."""

    def __init__(self) -> None:
        self.stop_event = Event()
        self._lock = Lock()
        self._thread: Thread | None = None
        source_sha = os.environ.get("SOURCE_SHA", "")
        self._source_sha = (
            source_sha if re.fullmatch(r"[a-f0-9]{40}", source_sha) else "unconfigured"
        )
        self._enabled = False
        self._body: dict[str, Any] = {
            "status": "not_ready",
            "probes": {"config": "starting"},
        }

    def _set(self, body: dict[str, Any]) -> None:
        with self._lock:
            self._body = body

    def start(self) -> None:
        self._thread = Thread(target=self._run, name="style-executor", daemon=False)
        self._thread.start()

    def stop(self) -> None:
        # Also called on the first SIGTERM, before Uvicorn drains HTTP.
        self.stop_event.set()

    def join(self) -> None:
        if self._thread is not None:
            self._thread.join()

    def health(self) -> dict[str, Any]:
        with self._lock:
            return {
                "status": "ok",
                "service": "opc-style-agent",
                "mode": "executor",
                "sourceSha": self._source_sha,
                "enabled": self._enabled,
                "config": self._body.get("probes", {}).get("config", "ok"),
                "stopping": self.stop_event.is_set(),
            }

    def ready(self) -> tuple[bool, dict[str, Any]]:
        with self._lock:
            body = {
                **self._body,
                "mode": "executor",
                "sourceSha": self._source_sha,
                "enabled": self._enabled,
                "paidProviderEnabled": self._enabled,
            }
        if self.stop_event.is_set():
            return False, {**body, "status": "not_ready", "reason": "stopping"}
        if self._thread is None or not self._thread.is_alive():
            return False, {
                **body,
                "status": "not_ready",
                "reason": "worker_unavailable",
            }
        return body["status"] == "ready", body

    def _run(self) -> None:
        engine: Engine | None = None
        store: S3AssetStore | None = None
        transport: httpx.Client | None = None
        worker: MallStyleExecutor | None = None
        try:
            settings = ProductionSettings()
            with self._lock:
                self._source_sha = settings.source_sha
                self._enabled = settings.executor_enabled
            if not re.fullmatch(r"[a-f0-9]{40}", settings.source_sha):
                raise RuntimeError("source_configuration_invalid")
            if not settings.executor_enabled:
                self._set(
                    {
                        "status": "ready",
                        "execution": "disabled_safe",
                        "probes": {"config": "ok", "provider": "disabled"},
                    }
                )
                self.stop_event.wait()
                return
            real = RealImageProviderSettings()
            if (
                not real.enabled
                or real.api_key is None
                or real.model != settings.model
                or real.max_run_usd < settings.estimated_cost_usd
            ):
                raise RuntimeError("provider_configuration_invalid")
            integration = StyleWarehouseV3Settings()
            # Never allow local SQLite fallback for production execution.
            url = os.environ.get("DATABASE_URL", "")
            if not url.startswith(("postgresql://", "postgresql+psycopg://")):
                raise RuntimeError("database_configuration_invalid")
            engine = create_database_engine(url)
            store = build_production_store(settings)
            transport = httpx.Client()
            generator = OpenAIBackgroundGenerator(
                real,
                transport=transport,
                budget=RealImageBudget(limit_usd=real.max_run_usd),
                estimated_request_usd=Decimal(settings.estimated_cost_usd),
            )
            if store is None:
                raise RuntimeError("storage_configuration_invalid")
            worker = MallStyleExecutor(
                sessionmaker(bind=engine, expire_on_commit=False, class_=Session),
                store,
                settings,
                generator=generator,
                stop_event=self.stop_event,
            )
            while not self.stop_event.is_set():
                ok, body = readiness(
                    engine, store, settings, integration_enabled=integration.enabled
                )
                body["probes"].update({"config": "ok", "provider": "ok"})
                self._set(body)
                if ok and not self.stop_event.is_set():
                    worker.reconcile_expired_reservations()
                    worker.run_once()
                self.stop_event.wait(5)
        except Exception:  # noqa: BLE001 - fail closed without leaking secrets
            # Never serialize config validation/driver/provider errors or secrets.
            self._set(
                {
                    "status": "not_ready",
                    "probes": {"config": "configuration_or_worker_failed"},
                }
            )
            self.stop_event.wait()
        finally:
            for close in (
                worker.release_unreserved_leases if worker is not None else None,
                transport.close if transport is not None else None,
                store.close if store is not None else None,
                engine.dispose if engine is not None else None,
            ):
                if close is not None:
                    try:
                        close()
                    except Exception:  # noqa: BLE001 - driver errors can contain secrets
                        self._set(
                            {
                                "status": "not_ready",
                                "probes": {"config": "shutdown_cleanup_failed"},
                            }
                        )


def create_executor_app(runtime: ExecutorRuntime | None = None) -> FastAPI:
    controller = runtime or ExecutorRuntime()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        controller.start()
        try:
            yield
        finally:
            controller.stop()
            # Complete the in-flight request before releasing unused claims.
            await asyncio.to_thread(controller.join)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.executor_runtime = controller

    @app.get("/health")
    def health() -> dict[str, Any]:
        return controller.health()

    @app.get("/ready")
    def ready() -> JSONResponse:
        ok, body = controller.ready()
        return JSONResponse(body, status_code=200 if ok else 503)

    return app
