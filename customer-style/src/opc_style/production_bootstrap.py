"""Bind the production health endpoint before importing the full API graph."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from inspect import isawaitable
from threading import Lock, Thread
from typing import Any

import uvicorn
from starlette.types import ASGIApp, Receive, Scope, Send


def _load_full_app() -> ASGIApp:
    # Importing the full route graph is intentionally kept off the server's
    # critical path. The module owns the single configured FastAPI instance.
    from opc_style.main import app

    return app


class ProductionBootstrap:
    """Small ASGI gate that serves liveness while the full API initializes."""

    def __init__(self, loader: Callable[[], ASGIApp] = _load_full_app) -> None:
        self._loader = loader
        self._inner: ASGIApp | None = None
        self._failed = False
        self._started = False
        self._lock = Lock()

    def _load(self) -> None:
        try:
            inner = self._loader()
        except Exception:  # noqa: BLE001 - never expose boot diagnostics over HTTP
            with self._lock:
                self._failed = True
            return
        with self._lock:
            self._inner = inner

    def _start_once(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
        Thread(target=self._load, name="opc-style-api-loader", daemon=True).start()

    def _snapshot(self) -> tuple[ASGIApp | None, bool]:
        with self._lock:
            return self._inner, self._failed

    @staticmethod
    async def _json(
        send: Send,
        status: int,
        body: dict[str, Any],
    ) -> None:
        content = json.dumps(body, separators=(",", ":")).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(content)).encode()),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": content})

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] == "lifespan":
            message = await receive()
            if message["type"] != "lifespan.startup":
                raise RuntimeError("unexpected_lifespan_message")
            self._start_once()
            await send({"type": "lifespan.startup.complete"})
            message = await receive()
            if message["type"] != "lifespan.shutdown":
                raise RuntimeError("unexpected_lifespan_message")
            inner, _failed = self._snapshot()
            router = getattr(inner, "router", None)
            if router is not None:
                for handler in router.on_shutdown:
                    result = handler()
                    if isawaitable(result):
                        await result
            await send({"type": "lifespan.shutdown.complete"})
            return

        if scope["type"] != "http":
            await send({"type": "websocket.close", "code": 1013})
            return

        path = scope.get("path", "")
        if path == "/health":
            await self._json(
                send,
                200,
                {
                    "status": "ok",
                    "service": "opc-style-agent",
                    "sourceSha": os.environ.get("SOURCE_SHA", "development"),
                },
            )
            return

        inner, failed = self._snapshot()
        if inner is None:
            await self._json(
                send,
                503,
                {
                    "status": "initialization_failed" if failed else "initializing",
                    "service": "opc-style-agent",
                    "sourceSha": os.environ.get("SOURCE_SHA", "development"),
                },
            )
            return
        await inner(scope, receive, send)


app = ProductionBootstrap()


def main() -> None:
    uvicorn.run(
        "opc_style.production_bootstrap:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
    )


if __name__ == "__main__":
    main()
