"""Run the phase-two image-output acceptance with synthetic local data only."""

from __future__ import annotations

import os
import sys

os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.dont_write_bytecode = True

import argparse
import fcntl
import json
import re
import shlex
import socket
import stat
import subprocess
import tempfile
from collections.abc import Callable, Generator
from contextlib import ExitStack, contextmanager, nullcontext
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from threading import local
from typing import Any, ClassVar, Never, Protocol, Self, cast
from unittest.mock import patch
from urllib.parse import unquote, urlsplit
from uuid import UUID, uuid5


class OutboundBlockedError(RuntimeError):
    """Raised at the first attempted external channel operation."""


class OutboundGuard:
    """Observe and fail closed on network, process, and shell attempts."""

    _AUDIT_EVENTS: ClassVar[dict[str, str]] = {
        "socket.connect": "socket.connect",
        "socket.connect_ex": "socket.connect_ex",
        "socket.sendto": "socket.sendto",
        "subprocess.Popen": "subprocess.Popen",
        "os.system": "os.system",
        "os.posix_spawn": "os.posix_spawn",
        "os.spawn": "os.spawn",
        "os.exec": "os.exec",
    }

    def __init__(self) -> None:
        self.attempts: list[dict[str, object]] = []
        self._installed = False

    def _block(self, channel: str) -> Never:
        self.attempts.append(
            {"sequence": len(self.attempts) + 1, "channel": channel, "blocked": True}
        )
        raise OutboundBlockedError(f"outbound channel blocked: {channel}")

    def _audit(self, event: str, _args: tuple[object, ...]) -> None:
        channel = self._AUDIT_EVENTS.get(event)
        if channel is not None:
            self._block(channel)

    def install(self) -> None:
        if self._installed:
            return
        self._installed = True
        sys.addaudithook(self._audit)

        guard = self

        def block_connect(_socket: socket.socket, _address: object) -> None:
            guard._block("socket.connect")

        def block_connect_ex(_socket: socket.socket, _address: object) -> int:
            guard._block("socket.connect_ex")

        def block_sendto(_socket: socket.socket, _data: bytes, *_args: object) -> int:
            guard._block("socket.sendto")

        def block_create_connection(*_args: object, **_kwargs: object) -> socket.socket:
            guard._block("socket.create_connection")

        def block_popen(*_args: object, **_kwargs: object) -> subprocess.Popen[bytes]:
            guard._block("subprocess.Popen")

        def block_system(_command: object) -> int:
            guard._block("os.system")

        socket.socket.connect = block_connect  # type: ignore[assignment]
        socket.socket.connect_ex = block_connect_ex  # type: ignore[assignment]
        socket.socket.sendto = block_sendto  # type: ignore[assignment]
        socket.create_connection = block_create_connection
        subprocess.Popen = block_popen  # type: ignore[misc,assignment]
        os.system = block_system  # type: ignore[assignment]

    def snapshot(self) -> dict[str, object]:
        return {
            "installed_at": "program_entry_before_third_party_imports",
            "attempt_count": len(self.attempts),
            "attempts": list(self.attempts),
            "covered_channels": [
                "socket.connect",
                "socket.connect_ex",
                "socket.create_connection",
                "socket.sendto",
                "subprocess.Popen",
                "os.system",
                "os.posix_spawn/os.spawn/os.exec via Python audit hook",
            ],
        }

    def assert_clean(self) -> None:
        if self.attempts:
            raise OutboundBlockedError(
                f"{len(self.attempts)} outbound attempt(s) were observed"
            )


class FileSideEffectBlockedError(RuntimeError):
    """Raised before a local mutation outside the explicit demo allowlist."""


class FileSideEffectGuard:
    """Fail closed on Python-visible local file mutations outside demo scope."""

    _WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC

    def __init__(self) -> None:
        self._active = False
        self._installed = False
        self._allowed_roots: dict[Path, str] = {}
        self._allowed_files: dict[Path, str] = {}
        self._thread_state = local()
        self.blocked_attempts: list[dict[str, object]] = []

    @staticmethod
    def _path_from_fd(file_descriptor: int) -> Path | None:
        if sys.platform == "darwin":
            try:
                raw_path = fcntl.fcntl(file_descriptor, 50, b"\0" * 1024)
            except (OSError, ValueError):
                return None
            if isinstance(raw_path, bytes):
                decoded = os.fsdecode(raw_path.split(b"\0", 1)[0])
                return Path(decoded) if decoded else None
            return None
        try:
            return Path(os.readlink(f"/proc/self/fd/{file_descriptor}"))
        except OSError:
            return None

    @classmethod
    def _absolute_path(cls, raw_path: object, dir_fd: int | None = None) -> Path | None:
        if isinstance(raw_path, int):
            return cls._path_from_fd(raw_path)
        if not isinstance(raw_path, (str, bytes, os.PathLike)):
            return None
        decoded = os.fsdecode(raw_path)
        if decoded.startswith("file:"):
            parsed = urlsplit(decoded)
            if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
                return None
            path = Path(unquote(parsed.path))
        else:
            path = Path(decoded)
        if not path.is_absolute() and dir_fd is not None:
            descriptor_path = cls._path_from_fd(dir_fd)
            if descriptor_path is None:
                return None
            path = descriptor_path / path
        elif not path.is_absolute():
            path = Path.cwd() / path
        return path.resolve(strict=False)

    @classmethod
    def _is_write_open(cls, mode: object, flags: object) -> bool:
        if isinstance(mode, str) and any(token in mode for token in "wax+"):
            return True
        return isinstance(flags, int) and bool(flags & cls._WRITE_FLAGS)

    def allow_root(self, path: Path, label: str) -> None:
        self._allowed_roots[path.resolve(strict=False)] = label

    def allow_file(self, path: Path, label: str) -> None:
        self._allowed_files[path.resolve(strict=False)] = label

    def activate(self) -> None:
        self._active = True

    def deactivate(self) -> None:
        self._active = False

    def _is_allowed(self, target: Path) -> bool:
        if target in self._allowed_files:
            return True
        return any(
            target == root or root in target.parents for root in self._allowed_roots
        )

    def _block(self, operation: str, normalized_target: str) -> Never:
        self.blocked_attempts.append(
            {
                "sequence": len(self.blocked_attempts) + 1,
                "operation": operation,
                "target": normalized_target,
                "blocked": True,
            }
        )
        raise FileSideEffectBlockedError(
            f"file side effect blocked: {operation}:{normalized_target}"
        )

    @staticmethod
    def _unsupported_fd_kind(mode: int) -> str:
        if stat.S_ISFIFO(mode):
            return "pipe"
        if stat.S_ISSOCK(mode):
            return "socket"
        if stat.S_ISCHR(mode):
            return "character_device"
        if stat.S_ISDIR(mode):
            return "directory"
        return "non_regular"

    def _validated_fd_duplicate(self, operation: str, file_descriptor: int) -> int:
        try:
            duplicate = os.dup(file_descriptor)
        except OSError:
            self._block(operation, "unresolved_fd/closed")
        except (OverflowError, TypeError, ValueError):
            self._block(operation, "unresolved_fd/unresolvable")
        try:
            before = os.fstat(duplicate)
            if not stat.S_ISREG(before.st_mode):
                self._block(
                    operation,
                    f"unsupported_fd/{self._unsupported_fd_kind(before.st_mode)}",
                )
            raw_target = self._path_from_fd(duplicate)
            if raw_target is None:
                self._block(operation, "unresolved_fd/unresolvable")
            target = raw_target.resolve(strict=True)
            target_stat = target.stat()
            after = os.fstat(duplicate)
            before_identity = (
                before.st_dev,
                before.st_ino,
                stat.S_IFMT(before.st_mode),
            )
            target_identity = (
                target_stat.st_dev,
                target_stat.st_ino,
                stat.S_IFMT(target_stat.st_mode),
            )
            after_identity = (after.st_dev, after.st_ino, stat.S_IFMT(after.st_mode))
            if before_identity != target_identity or before_identity != after_identity:
                self._block(operation, "unresolved_fd/identity_changed")
            if not self._is_allowed(target):
                self._block(operation, f"outside_allowlist/{target.name or 'unnamed'}")
            return duplicate
        except FileSideEffectBlockedError:
            os.close(duplicate)
            raise
        except (OSError, RuntimeError):
            os.close(duplicate)
            self._block(operation, "unresolved_fd/unresolvable")

    def _check(
        self, operation: str, raw_path: object, dir_fd: int | None = None
    ) -> None:
        if not self._active:
            return
        if isinstance(raw_path, int):
            duplicate = self._validated_fd_duplicate(operation, raw_path)
            os.close(duplicate)
            return
        target = self._absolute_path(raw_path, dir_fd)
        if target is None:
            self._block(operation, "unresolved_path/unresolvable")
        if self._is_allowed(target):
            return
        self._block(operation, f"outside_allowlist/{target.name or 'unnamed'}")

    def _audit(self, event: str, args: tuple[object, ...]) -> None:
        if not self._active:
            return
        if event == "open" and not getattr(self._thread_state, "open_bypass", False):
            path = args[0] if args else None
            mode = args[1] if len(args) > 1 else None
            flags = args[2] if len(args) > 2 else None
            if self._is_write_open(mode, flags):
                self._check("open", path)
        elif event == "sqlite3.connect" and args:
            self._check("sqlite3.connect", args[0])
        elif event == "os.rename" and len(args) >= 2:
            destination_fd = (
                args[3] if len(args) > 3 and isinstance(args[3], int) else None
            )
            self._check("os.rename", args[1], destination_fd)

    def install(self) -> None:
        if self._installed:
            return
        self._installed = True
        sys.addaudithook(self._audit)
        original_os_open = os.open
        original_os_write = os.write
        original_os_pwrite = os.pwrite
        original_os_writev = os.writev
        original_os_pwritev = os.pwritev
        guard = self

        def guarded_os_open(
            path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
            flags: int,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> int:
            if guard._active and guard._is_write_open(None, flags):
                guard._check("open", path, dir_fd)
                guard._thread_state.open_bypass = True
                try:
                    return original_os_open(path, flags, mode, dir_fd=dir_fd)
                finally:
                    guard._thread_state.open_bypass = False
            return original_os_open(path, flags, mode, dir_fd=dir_fd)

        os.open = guarded_os_open

        def guarded_os_write(file_descriptor: int, data: Any) -> int:
            if not guard._active:
                return original_os_write(file_descriptor, data)
            duplicate = guard._validated_fd_duplicate("os.write", file_descriptor)
            try:
                return original_os_write(duplicate, data)
            finally:
                os.close(duplicate)

        def guarded_os_pwrite(file_descriptor: int, data: Any, offset: int) -> int:
            if not guard._active:
                return original_os_pwrite(file_descriptor, data, offset)
            duplicate = guard._validated_fd_duplicate("os.pwrite", file_descriptor)
            try:
                return original_os_pwrite(duplicate, data, offset)
            finally:
                os.close(duplicate)

        def guarded_os_writev(file_descriptor: int, buffers: Any) -> int:
            if not guard._active:
                return original_os_writev(file_descriptor, buffers)
            duplicate = guard._validated_fd_duplicate("os.writev", file_descriptor)
            try:
                return original_os_writev(duplicate, buffers)
            finally:
                os.close(duplicate)

        def guarded_os_pwritev(
            file_descriptor: int,
            buffers: Any,
            offset: int,
            flags: int = 0,
        ) -> int:
            if not guard._active:
                return original_os_pwritev(file_descriptor, buffers, offset, flags)
            duplicate = guard._validated_fd_duplicate("os.pwritev", file_descriptor)
            try:
                return original_os_pwritev(duplicate, buffers, offset, flags)
            finally:
                os.close(duplicate)

        os.write = guarded_os_write
        os.pwrite = guarded_os_pwrite
        os.writev = guarded_os_writev
        os.pwritev = guarded_os_pwritev

    def snapshot(self) -> dict[str, object]:
        return {
            "installed_at": "program_entry_before_third_party_imports",
            "policy": "deny_writes_outside_explicit_allowlist",
            "allowed_root_labels": sorted(self._allowed_roots.values()),
            "allowed_file_labels": sorted(self._allowed_files.values()),
            "declared_system_temp_roots": [],
            "blocked_attempt_count": len(self.blocked_attempts),
            "blocked_attempts": list(self.blocked_attempts),
            "covered_operations": [
                "open/write including integer fd",
                "os.open/write",
                "os.write",
                "os.pwrite",
                "os.writev",
                "os.pwritev",
                "sqlite3.connect",
                "os.rename",
            ],
            "fd_policy": (
                "duplicate_then_resolve_and_match_dev_inode_type_on_every_write"
            ),
        }


OUTBOUND_GUARD = OutboundGuard()
OUTBOUND_GUARD.install()
FILE_SIDE_EFFECT_GUARD = FileSideEffectGuard()
FILE_SIDE_EFFECT_GUARD.install()

from alembic.config import Config
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import event, inspect, text
from sqlalchemy.orm import Session, sessionmaker

import opc_style.services.assets as assets_service_module
import opc_style.services.authenticity as authenticity_service_module
import opc_style.services.image_jobs as image_jobs_service_module
import opc_style.services.image_reviews as image_reviews_service_module
import opc_style.services.real_photos as real_photos_service_module
import opc_style.services.samples as samples_service_module
import opc_style.services.styles as styles_service_module
from alembic import command
from opc_style import models as models_module
from opc_style import repositories as repositories_module
from opc_style.api.dependencies import get_session
from opc_style.api.routes_image_jobs import get_image_generator
from opc_style.api.routes_real_photos import get_asset_content_store
from opc_style.db import create_database_engine
from opc_style.domain.image_generator import (
    DeterministicLocalImageGenerator,
    GeneratedCandidate,
    GenerationRequest,
)
from opc_style.models import AuditEvent, ProductAsset
from opc_style.services.real_photos import LocalSyntheticAssetStore

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCAN_COMMAND = shlex.join(
    [sys.executable, "scripts/verify_image_output_demo.py", "--scan-only"]
)
DOCUMENTED_ACCEPTANCE_MEDIA = {
    "docs/verification/assets/automatic-product-identity-card-cost-gate.jpg": (
        "bca4bf11171e1ed144806a794bdbaf15cb32c299e2d5e99eaca7274cfe0f6fd3"
    ),
    "docs/verification/assets/automatic-product-identity-card-refresh.jpg": (
        "540e6ddd17d63427d9ea128f8ee86f573f1ec05ffc91c0361deddc92c86742eb"
    ),
    "tests/fixtures/foreground/simple-product.png": (
        "59ebff6e9adb1f02d0ab130593952443234fe9efa16c4e97a599ec79b3b6554f"
    ),
}
CUSTOMER_A_ID = UUID("11111111-1111-1111-1111-111111111111")
CUSTOMER_B_ID = UUID("22222222-2222-2222-2222-222222222222")
NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
DETERMINISTIC_ID_NAMESPACE = UUID("70000000-0000-0000-0000-000000000001")
CUSTOMER_A = {"X-Customer-Id": str(CUSTOMER_A_ID)}
CUSTOMER_B = {"X-Customer-Id": str(CUSTOMER_B_ID)}
ASSET_SPECS = (
    (
        UUID("40000000-0000-0000-0000-000000000001"),
        "product-scene",
        "photo-scene",
        True,
        "original_real_photo",
        (26, 82, 126),
    ),
    (
        UUID("40000000-0000-0000-0000-000000000002"),
        "product-multi-a",
        "photo-multi-a",
        True,
        "original_real_photo",
        (86, 46, 122),
    ),
    (
        UUID("40000000-0000-0000-0000-000000000003"),
        "product-multi-b",
        "photo-multi-b",
        True,
        "original_real_photo",
        (116, 68, 36),
    ),
    (
        UUID("40000000-0000-0000-0000-000000000004"),
        "product-denied",
        "photo-denied",
        False,
        "original_real_photo",
        (110, 32, 42),
    ),
    (
        UUID("40000000-0000-0000-0000-000000000005"),
        "product-quality-fail",
        "photo-quality-fail",
        True,
        "original_real_photo",
        (24, 118, 74),
    ),
    (
        UUID("40000000-0000-0000-0000-000000000006"),
        "product-ai-only",
        "photo-ai-only",
        True,
        "ai_style_generated",
        (78, 78, 78),
    ),
)


class _FixedDemoDateTime(datetime):
    """Return one declared instant inside the synthetic acceptance runtime."""

    @classmethod
    def now(cls, tz: object = None) -> Self:
        if tz is None:
            return cls(2026, 8, 31, 12, 0)
        return cls.fromtimestamp(NOW.timestamp(), tz=cast(Any, tz))


class _DeterministicDemoIdentityFactory:
    """Allocate stable UUIDv5 identities in deterministic insertion order."""

    version = "uuid5-sequence-v1"

    def __init__(self) -> None:
        self._sequence = 0

    def next_uuid(self) -> UUID:
        self._sequence += 1
        return uuid5(DETERMINISTIC_ID_NAMESPACE, f"identity-{self._sequence:04d}")

    def assign_missing_ids(
        self,
        session: Session,
        _flush_context: object,
        _instances: object,
    ) -> None:
        for value in session.new:
            if hasattr(value, "id") and value.id is None:
                value.id = self.next_uuid()


@contextmanager
def _deterministic_demo_runtime(
    identity_factory: _DeterministicDemoIdentityFactory,
) -> Generator[None, None, None]:
    """Inject deterministic UUID and time only into this safe local demo."""
    datetime_modules = (
        models_module,
        repositories_module,
        assets_service_module,
        authenticity_service_module,
        image_jobs_service_module,
        image_reviews_service_module,
        samples_service_module,
        styles_service_module,
    )
    with ExitStack() as stack:
        for module in datetime_modules:
            stack.enter_context(patch.object(module, "datetime", _FixedDemoDateTime))
        for module in (
            image_jobs_service_module,
            real_photos_service_module,
            styles_service_module,
        ):
            stack.enter_context(
                patch.object(module, "uuid4", identity_factory.next_uuid)
            )
        yield


def _migration_config(database_url: str) -> Config:
    config = Config(str(REPOSITORY_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPOSITORY_ROOT / "alembic"))
    config.set_main_option("prepend_sys_path", str(REPOSITORY_ROOT / "src"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


class _Response(Protocol):
    @property
    def status_code(self) -> int: ...

    @property
    def text(self) -> str: ...

    def json(self) -> object: ...


def _expect(response: _Response, status: int, label: str) -> dict[str, Any]:
    if response.status_code != status:
        raise AssertionError(
            f"{label}: expected HTTP {status}, got {response.status_code}: "
            f"{response.text}"
        )
    body = response.json()
    if not isinstance(body, dict):
        raise TypeError(f"{label}: response must be an object")
    return cast(dict[str, Any], body)


def _generation_request(
    product_assets: list[tuple[str, UUID]],
    key: str,
    *,
    scene_style_id: str,
    scene_style_card_version_id: str,
) -> dict[str, object]:
    return {
        "sources": [
            {"product_id": product_id, "asset_id": str(asset_id)}
            for product_id, asset_id in product_assets
        ],
        "scene_style_id": scene_style_id,
        "style_card_version_id": scene_style_card_version_id,
        "output_ratio": "1:1",
        "idempotency_key": key,
    }


def _authorize_style_prerequisites(client: TestClient) -> dict[str, str]:
    for index in range(10):
        output = BytesIO()
        Image.new("RGB", (48, 32), (index * 12, 80, 140)).save(output, format="JPEG")
        content = output.getvalue()
        _expect(
            client.post(
                "/v1/style-samples/content",
                content=content,
                headers={
                    **CUSTOMER_A,
                    "X-Photo-Id": f"synthetic-style-sample-{index:02d}",
                    "X-Content-Sha256": sha256(content).hexdigest(),
                    "X-Mime-Type": "image/jpeg",
                    "X-Consent-Granted": "true",
                    "X-Consent-Provenance": "customer_style_history_opt_in",
                    "X-Consent-Text-Version": "synthetic-consent-v1",
                },
            ),
            201,
            "style sample authorization",
        )
    main = _expect(
        client.post("/v1/style-cards/main/generate", headers=CUSTOMER_A),
        201,
        "main style generation",
    )
    scene = _expect(
        client.post(
            "/v1/style-cards/scenes",
            headers=CUSTOMER_A,
            json={
                "name": "Synthetic acceptance scene",
                "aspect_ratio": "1:1",
                "backgrounds": ["synthetic studio"],
            },
        ),
        201,
        "scene style creation",
    )
    return {
        "main_style_card_version_id": str(main["version_id"]),
        "scene_style_id": str(scene["id"]),
        "scene_style_card_version_id": str(scene["version_id"]),
    }


def _create_source_files(directory: Path) -> dict[str, Path]:
    directory.mkdir(parents=True, exist_ok=False)
    sources: dict[str, Path] = {}
    for _asset_id, product_id, photo_id, _permission, identity, color in ASSET_SPECS:
        if identity != "original_real_photo":
            continue
        source = directory / f"{photo_id}.jpg"
        Image.new("RGB", (640, 480), color).save(
            source,
            format="JPEG",
            quality=85,
            optimize=False,
            progressive=False,
            subsampling=0,
        )
        sources[product_id] = source
    return sources


def _intake_assets_over_http(
    client: TestClient,
    store: LocalSyntheticAssetStore,
    sources: dict[str, Path],
) -> dict[str, UUID]:
    assets: dict[str, UUID] = {}
    permissions = {
        product_id: permission
        for _asset_id, product_id, _photo_id, permission, identity, _color in ASSET_SPECS
        if identity == "original_real_photo"
    }
    for product_id, source in sorted(sources.items()):
        payload = source.read_bytes()
        body = _expect(
            client.post(
                "/v1/product-assets",
                headers=CUSTOMER_A,
                json={
                    "product_id": product_id,
                    "photo_id": f"http-{product_id}",
                    "confirmed": True,
                    "source_type": "synthetic_metadata",
                    "quality_flags": ["synthetic_fixture"],
                    "occurred_at": "2026-08-31T12:00:00Z",
                    "transform_permission": permissions[product_id],
                    "content_sha256": sha256(payload).hexdigest(),
                    "mime_type": "image/jpeg",
                    "width": 640,
                    "height": 480,
                },
            ),
            201,
            f"asset intake {product_id}",
        )
        asset_id = UUID(str(body["id"]))
        assets[product_id] = asset_id
        store.register_original(CUSTOMER_A_ID, asset_id, source)
    return assets


def _seed_ai_only_negative_fixture(factory: Callable[[], Session]) -> None:
    with factory() as session:
        session.add(
            ProductAsset(
                customer_id=CUSTOMER_A_ID,
                product_id="product-ai-only",
                photo_id="photo-ai-only",
                identity="ai_style_generated",
                confirmed=True,
                source_type="synthetic_metadata",
                quality_flags=["synthetic_fixture"],
                occurred_at=NOW,
                content_sha256="f" * 64,
                mime_type="image/jpeg",
                width=640,
                height=480,
                storage_path=None,
            )
        )
        session.commit()


def _openapi_inventory(
    app_schema: dict[str, Any],
) -> tuple[list[dict[str, object]], list[str]]:
    operations: list[dict[str, object]] = []
    publish_capable: list[str] = []
    methods = {"get", "post", "put", "patch", "delete"}
    schemas = app_schema.get("components", {}).get("schemas", {})

    def request_fields(operation: dict[str, Any]) -> list[str]:
        schema = (
            operation.get("requestBody", {})
            .get("content", {})
            .get("application/json", {})
            .get("schema", {})
        )
        if "$ref" in schema:
            schema = schemas.get(str(schema["$ref"]).rsplit("/", 1)[-1], {})
        return sorted(str(value) for value in schema.get("properties", {}))

    for path, path_item in sorted(app_schema["paths"].items()):
        for method, operation_value in sorted(path_item.items()):
            if method not in methods or not isinstance(operation_value, dict):
                continue
            operation = cast(dict[str, Any], operation_value)
            fields = request_fields(operation)
            operation_id = str(operation.get("operationId", ""))
            tags = sorted(str(tag) for tag in operation.get("tags", []))
            inventory_id = f"{method.upper()} {path}::{operation_id}"
            operations.append(
                {
                    "method": method.upper(),
                    "path": path,
                    "operation_id": operation_id,
                    "tags": tags,
                    "request_fields": fields,
                }
            )
            action_tokens = {operation_id.lower(), *(tag.lower() for tag in tags)}
            field_tokens = {field.lower() for field in fields}
            if any(
                token.startswith(("publish", "dispatch", "external_send"))
                for token in action_tokens
            ) or field_tokens.intersection(
                {"publish", "publishable", "dispatch", "external_send"}
            ):
                publish_capable.append(inventory_id)
    return operations, publish_capable


def _repository_scans() -> dict[str, dict[str, object]]:
    runtime_excluded_directories = {
        ".git",
        ".worktrees",
        ".venv",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".local-style-assets",
        "local-backups",
        "__pycache__",
        ".superpowers",
        ".local-style-runtime",
    }
    media_excluded_directories = runtime_excluded_directories | {
        "交付文档",
        "待验收素材",
    }
    media_extensions = {
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".webp",
        ".heic",
        ".tif",
        ".tiff",
        ".bmp",
        ".mp4",
        ".mov",
        ".avi",
        ".mkv",
        ".wav",
        ".mp3",
        ".m4a",
        ".flac",
    }
    secret_rules = [
        "PEM private-key header shapes",
        "AWS access-key identifier shape",
        "provider secret-token shape",
    ]
    secret_patterns = (
        re.compile("BEGIN " + "(?:RSA |EC |OPENSSH )?PRIVATE KEY"),
        re.compile("AK" + "IA[0-9A-Z]{16}"),
        re.compile("s" + "k-[A-Za-z0-9_-]{20,}"),
    )
    media_matches: list[str] = []
    allowed_media_matches: list[dict[str, str]] = []
    secret_matches: list[str] = []
    repository_files: list[Path] = []
    for root, directory_names, file_names in os.walk(REPOSITORY_ROOT):
        directory_names[:] = sorted(
            name
            for name in directory_names
            if name not in runtime_excluded_directories
        )
        repository_files.extend(Path(root) / name for name in sorted(file_names))
    for path in repository_files:
        relative_path = path.relative_to(REPOSITORY_ROOT)
        relative = str(relative_path)
        is_verification_evidence = (
            relative_path.is_relative_to(Path("docs/verification"))
            and not relative_path.is_relative_to(Path("docs/verification/assets"))
        )
        if (
            path.suffix.lower() in media_extensions
            and not is_verification_evidence
            and not any(part in media_excluded_directories for part in path.parts)
        ):
            allowed_sha256 = DOCUMENTED_ACCEPTANCE_MEDIA.get(relative)
            try:
                actual_sha256 = sha256(path.read_bytes()).hexdigest()
            except OSError:
                actual_sha256 = ""
            if allowed_sha256 is not None and actual_sha256 == allowed_sha256:
                allowed_media_matches.append(
                    {"path": relative, "sha256": actual_sha256}
                )
            else:
                media_matches.append(relative)
        if path.name == "uv.lock":
            continue
        if path.suffix.lower() in media_extensions:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if any(pattern.search(content) for pattern in secret_patterns):
            secret_matches.append(relative)
    exclusions = sorted(runtime_excluded_directories) + ["uv.lock for secret scan"]
    verified_allowed_paths = {item["path"] for item in allowed_media_matches}
    media_matches.extend(
        path
        for path in DOCUMENTED_ACCEPTANCE_MEDIA
        if path not in verified_allowed_paths and path not in media_matches
    )
    return {
        "secret_scan": {
            "command": SCAN_COMMAND,
            "rules": secret_rules,
            "match_count": len(secret_matches),
            "matches": sorted(secret_matches),
            "exclusions": exclusions,
        },
        "real_media_scan": {
            "command": SCAN_COMMAND,
            "rules": sorted(media_extensions),
            "match_count": len(media_matches),
            "matches": sorted(media_matches),
            "allowed_exact_matches": sorted(
                allowed_media_matches, key=lambda item: item["path"]
            ),
            "exclusions": sorted(media_excluded_directories),
        },
    }


def _artifact_manifest(generated: dict[str, Any]) -> list[dict[str, object]]:
    artifacts: list[dict[str, object]] = []
    for candidate in generated["candidates"]:
        artifacts.append(
            {
                "sequence_number": candidate["sequence_number"],
                "content_sha256": candidate["content_sha256"],
                "adapter_version": candidate["adapter_version"],
                "generation_mode": candidate["generation_mode"],
                "synthetic_test_only": candidate["synthetic_test_only"],
                "publishable": candidate["publishable"],
            }
        )
    return artifacts


def _failing_quality_generator() -> DeterministicLocalImageGenerator:
    generator = DeterministicLocalImageGenerator()
    original_generate = generator.generate

    def generate(request: GenerationRequest) -> list[GeneratedCandidate]:
        candidates = original_generate(request)
        metadata = dict(candidates[1].metadata)
        fixture = dict(cast(dict[str, object], metadata["local_authenticity_fixture"]))
        fixture["material_preserved"] = False
        metadata["local_authenticity_fixture"] = fixture
        candidates[1] = replace(candidates[1], metadata=metadata)
        return candidates

    generator.generate = generate  # type: ignore[method-assign]
    return generator


def run_demo(
    configured_temp_root: Path | None = None,
    preopened_outside_fd: int | None = None,
) -> dict[str, Any]:
    """Exercise both successful flows and every fail-closed acceptance gate."""
    temp_context = (
        tempfile.TemporaryDirectory(prefix="opc-image-output-demo-")
        if configured_temp_root is None
        else nullcontext(str(configured_temp_root))
    )
    with temp_context as temp_name:
        temp_root = Path(temp_name)
        FILE_SIDE_EFFECT_GUARD.allow_root(temp_root, "acceptance_temp_root")
        database_url = f"sqlite:///{temp_root / 'acceptance.db'}"
        command.upgrade(_migration_config(database_url), "head")
        engine = create_database_engine(database_url)
        identity_factory = _DeterministicDemoIdentityFactory()

        class DeterministicDemoSession(Session):
            pass

        event.listen(
            DeterministicDemoSession,
            "before_flush",
            identity_factory.assign_missing_ids,
        )
        factory = sessionmaker(
            bind=engine,
            expire_on_commit=False,
            class_=DeterministicDemoSession,
        )
        generator_state = {"active": DeterministicLocalImageGenerator()}

        with (
            _deterministic_demo_runtime(identity_factory),
            LocalSyntheticAssetStore(temp_root / "synthetic-artifacts") as store,
        ):
            sources = _create_source_files(temp_root / "sources")
            with patch.dict(
                os.environ,
                {"LOCAL_STYLE_ASSET_ROOT": str(temp_root / "local-style-runtime")},
            ):
                from opc_style.main import create_app

                app = create_app()
            app.state.classification_bridge_store = store

            def override_session() -> Generator[Session, None, None]:
                with factory() as session:
                    yield session

            app.dependency_overrides[get_session] = override_session
            app.dependency_overrides[get_asset_content_store] = lambda: store
            app.dependency_overrides[get_image_generator] = lambda: generator_state[
                "active"
            ]

            with TestClient(app) as client:
                pins = _authorize_style_prerequisites(client)
                assets = _intake_assets_over_http(client, store, sources)
                _seed_ai_only_negative_fixture(factory)
                with nullcontext():
                    unconfirmed = client.post(
                        "/v1/product-assets",
                        headers=CUSTOMER_A,
                        json={
                            "product_id": "product-unconfirmed",
                            "photo_id": "photo-unconfirmed",
                            "confirmed": False,
                            "source_type": "synthetic_metadata",
                            "quality_flags": ["synthetic_fixture"],
                            "occurred_at": "2026-08-31T12:00:00Z",
                            "transform_permission": True,
                            "content_sha256": "a" * 64,
                            "mime_type": "image/jpeg",
                            "width": 640,
                            "height": 480,
                        },
                    )
                    if unconfirmed.status_code != 422:
                        raise AssertionError("unconfirmed asset was not rejected")

                    denied = client.post(
                        "/v1/image-outputs/scene",
                        headers=CUSTOMER_A,
                        json=_generation_request(
                            [("product-denied", assets["product-denied"])],
                            "acceptance-denied",
                            scene_style_id=pins["scene_style_id"],
                            scene_style_card_version_id=pins[
                                "scene_style_card_version_id"
                            ],
                        ),
                    )
                    denied_body = _expect(denied, 409, "permission gate")
                    if denied_body["error"]["code"] != "transform_permission_denied":
                        raise AssertionError("permission gate returned the wrong code")

                    scene = _expect(
                        client.post(
                            "/v1/image-outputs/scene",
                            headers=CUSTOMER_A,
                            json=_generation_request(
                                [("product-scene", assets["product-scene"])],
                                "acceptance-scene",
                                scene_style_id=pins["scene_style_id"],
                                scene_style_card_version_id=pins[
                                    "scene_style_card_version_id"
                                ],
                            ),
                        ),
                        201,
                        "scene generation",
                    )
                    if len(scene["candidates"]) != 3:
                        raise AssertionError(
                            "scene must return exactly three candidates"
                        )
                    scene_replay = _expect(
                        client.post(
                            "/v1/image-outputs/scene",
                            headers=CUSTOMER_A,
                            json=_generation_request(
                                [("product-scene", assets["product-scene"])],
                                "acceptance-scene",
                                scene_style_id=pins["scene_style_id"],
                                scene_style_card_version_id=pins[
                                    "scene_style_card_version_id"
                                ],
                            ),
                        ),
                        200,
                        "scene replay",
                    )
                    scene_checked = _expect(
                        client.post(
                            f"/v1/image-outputs/{scene['id']}/authenticity-checks",
                            headers=CUSTOMER_A,
                        ),
                        201,
                        "scene authenticity",
                    )
                    if scene_checked["status"] != "awaiting_customer_review":
                        raise AssertionError("scene did not reach customer review")
                    unapproved_view = _expect(
                        client.get(
                            "/v1/products/product-scene/images", headers=CUSTOMER_A
                        ),
                        200,
                        "unapproved product view",
                    )
                    if unapproved_view["resolved_view"] != "real_photo" or any(
                        item["image_kind"] == "style_image"
                        for item in unapproved_view["images"]
                    ):
                        raise AssertionError("unapproved style image became visible")

                    foreign_job = client.get(
                        f"/v1/image-outputs/{scene['id']}", headers=CUSTOMER_B
                    )
                    if foreign_job.status_code != 404:
                        raise AssertionError("cross-customer job was not hidden")

                    multi = _expect(
                        client.post(
                            "/v1/image-outputs/multi-product",
                            headers=CUSTOMER_A,
                            json=_generation_request(
                                [
                                    (
                                        "product-multi-a",
                                        assets["product-multi-a"],
                                    ),
                                    (
                                        "product-multi-b",
                                        assets["product-multi-b"],
                                    ),
                                ],
                                "acceptance-multi",
                                scene_style_id=pins["scene_style_id"],
                                scene_style_card_version_id=pins[
                                    "scene_style_card_version_id"
                                ],
                            ),
                        ),
                        201,
                        "multi-product generation",
                    )
                    if len(multi["candidates"]) != 3:
                        raise AssertionError(
                            "multi-product must return exactly three candidates"
                        )
                    multi_replay = _expect(
                        client.post(
                            "/v1/image-outputs/multi-product",
                            headers=CUSTOMER_A,
                            json=_generation_request(
                                [
                                    (
                                        "product-multi-a",
                                        assets["product-multi-a"],
                                    ),
                                    (
                                        "product-multi-b",
                                        assets["product-multi-b"],
                                    ),
                                ],
                                "acceptance-multi",
                                scene_style_id=pins["scene_style_id"],
                                scene_style_card_version_id=pins[
                                    "scene_style_card_version_id"
                                ],
                            ),
                        ),
                        200,
                        "multi-product replay",
                    )
                    multi_checked = _expect(
                        client.post(
                            f"/v1/image-outputs/{multi['id']}/authenticity-checks",
                            headers=CUSTOMER_A,
                        ),
                        201,
                        "multi-product authenticity",
                    )
                    selected = multi_checked["candidates"][1]["id"]
                    reviewed = _expect(
                        client.post(
                            f"/v1/image-outputs/{multi['id']}/candidates/"
                            f"{selected}/review",
                            headers=CUSTOMER_A,
                            json={
                                "decision": "approved",
                                "reason": "synthetic acceptance selection",
                                "idempotency_key": "acceptance-review",
                            },
                        ),
                        201,
                        "customer approval",
                    )
                    if reviewed["publishable"] is not False:
                        raise AssertionError("approval must remain non-publishable")
                    if len(reviewed["product_associations"]) != 2:
                        raise AssertionError("multi approval needs two source links")
                    injected_publish_audit = os.environ.get(
                        "OPC_DEMO_INJECT_PUBLISH_AUDIT"
                    )
                    if injected_publish_audit:
                        with factory() as injection_session:
                            injection_session.add(
                                AuditEvent(
                                    customer_id=CUSTOMER_A_ID,
                                    event_type=injected_publish_audit,
                                    payload={"synthetic_test_injection": True},
                                    occurred_at=NOW,
                                )
                            )
                            injection_session.commit()
                    injected_file_name = os.environ.get(
                        "OPC_DEMO_INJECT_CAUGHT_FILE_WRITE"
                    )
                    if injected_file_name:
                        outside_target = Path.cwd() / Path(injected_file_name).name
                        try:
                            outside_target.write_text(
                                "synthetic publish attempt\n", encoding="utf-8"
                            )
                        except FileSideEffectBlockedError:
                            # Model business code swallowing the immediate block.
                            pass
                    if preopened_outside_fd is not None:
                        try:
                            preopened_method = os.environ.get(
                                "OPC_DEMO_INJECT_PREOPENED_FD_METHOD", "write"
                            )
                            payload = b"synthetic approved storefront queue write\n"
                            if preopened_method == "write":
                                os.write(preopened_outside_fd, payload)
                            elif preopened_method == "pwrite":
                                os.pwrite(preopened_outside_fd, payload, 0)
                            elif preopened_method == "writev":
                                os.writev(preopened_outside_fd, [payload])
                            elif preopened_method == "pwritev":
                                os.pwritev(preopened_outside_fd, [payload], 0)
                            else:
                                raise ValueError("unsupported preopened-fd method")
                        except FileSideEffectBlockedError:
                            # The durable ledger must survive business-level catch.
                            pass
                    default_view = _expect(
                        client.get(
                            "/v1/products/product-multi-a/images",
                            headers=CUSTOMER_A,
                        ),
                        200,
                        "default style view",
                    )
                    real_view = _expect(
                        client.get(
                            "/v1/products/product-multi-a/images?view=real",
                            headers=CUSTOMER_A,
                        ),
                        200,
                        "explicit real view",
                    )
                    if default_view["resolved_view"] != "style_image":
                        raise AssertionError("approved style was not the default")
                    if real_view["resolved_view"] != "real_photo" or any(
                        item["image_kind"] != "real_photo"
                        for item in real_view["images"]
                    ):
                        raise AssertionError(
                            "explicit real view masqueraded a derivative"
                        )

                    generator_state["active"] = _failing_quality_generator()
                    quality_job = _expect(
                        client.post(
                            "/v1/image-outputs/scene",
                            headers=CUSTOMER_A,
                            json=_generation_request(
                                [
                                    (
                                        "product-quality-fail",
                                        assets["product-quality-fail"],
                                    )
                                ],
                                "acceptance-quality-fail",
                                scene_style_id=pins["scene_style_id"],
                                scene_style_card_version_id=pins[
                                    "scene_style_card_version_id"
                                ],
                            ),
                        ),
                        201,
                        "quality fixture generation",
                    )
                    quality_checked = _expect(
                        client.post(
                            f"/v1/image-outputs/{quality_job['id']}"
                            "/authenticity-checks",
                            headers=CUSTOMER_A,
                        ),
                        201,
                        "quality failure",
                    )
                    if quality_checked["status"] != "authenticity_failed":
                        raise AssertionError("failed quality fixture reached review")
                    quality_review = client.post(
                        f"/v1/image-outputs/{quality_job['id']}/candidates/"
                        f"{quality_checked['candidates'][0]['id']}/review",
                        headers=CUSTOMER_A,
                        json={
                            "decision": "approved",
                            "reason": None,
                            "idempotency_key": "acceptance-quality-review",
                        },
                    )
                    quality_review_body = _expect(
                        quality_review, 409, "quality review block"
                    )
                    if quality_review_body["error"]["code"] != (
                        "review_job_not_awaiting_customer"
                    ):
                        raise AssertionError("quality review gate returned wrong code")

                    no_real = client.get(
                        "/v1/products/product-ai-only/images?view=real",
                        headers=CUSTOMER_A,
                    )
                    if no_real.status_code != 404:
                        raise AssertionError("AI-only asset masqueraded as real")

                    operations, publish_operations = _openapi_inventory(app.openapi())
                    required_operation_ids = {
                        "authorize_style_sample_v1_style_samples_post",
                        "create_scene_style_card_v1_style_cards_scenes_post",
                        "generate_main_style_card_v1_style_cards_main_generate_post",
                        "intake_product_asset_v1_product_assets_post",
                    }
                    actual_operation_ids = {
                        str(item["operation_id"]) for item in operations
                    }
                    required_operations_present = required_operation_ids.issubset(
                        actual_operation_ids
                    )
                    if publish_operations or not required_operations_present:
                        raise AssertionError("API action inventory failed closed")

                OUTBOUND_GUARD.assert_clean()
                with factory() as evidence_session:
                    database_tables = sorted(inspect(engine).get_table_names())
                    audit_event_types = sorted(
                        {
                            str(value)
                            for value in evidence_session.execute(
                                text("SELECT event_type FROM audit_events")
                            ).scalars()
                        }
                    )
                    association_rows = [
                        {
                            "product_id": str(row.product_id),
                            "approved_candidate_id": str(row.image_candidate_id),
                            "source_asset_id": str(row.source_asset_id),
                        }
                        for row in evidence_session.execute(
                            text(
                                "SELECT product_id, image_candidate_id, "
                                "source_asset_id FROM approved_image_associations "
                                "ORDER BY product_id"
                            )
                        )
                    ]
            engine.dispose()

        selected_candidate = next(
            item for item in multi["candidates"] if item["id"] == selected
        )
        original_by_product = {
            item["product_id"]: item["real_photo"]
            for item in reviewed["product_associations"]
        }
        exported_associations = [
            {
                **row,
                "approved_style_candidate": {
                    "candidate_id": row["approved_candidate_id"],
                    "content_sha256": selected_candidate["content_sha256"],
                },
                "confirmed_original_asset": {
                    "asset_id": row["source_asset_id"],
                    "content_sha256": original_by_product[row["product_id"]][
                        "content_sha256"
                    ],
                    "identity": original_by_product[row["product_id"]]["identity"],
                },
            }
            for row in association_rows
        ]
        publish_or_outbox_tables = [
            table
            for table in database_tables
            if any(
                token in table.lower() for token in ("publish", "outbox", "dispatch")
            )
        ]
        publish_audit_events = [
            event
            for event in audit_event_types
            if any(
                token in event.lower()
                for token in ("publish", "dispatch", "external_send")
            )
        ]
        observed_side_effect_actions = sorted(
            {
                *(f"api:{value}" for value in publish_operations),
                *(f"table:{value}" for value in publish_or_outbox_tables),
                *(f"audit:{value}" for value in publish_audit_events),
                *(f"outbound:{item['channel']}" for item in OUTBOUND_GUARD.attempts),
                *(
                    f"file_write:{item['operation']}:{item['target']}"
                    for item in FILE_SIDE_EFFECT_GUARD.blocked_attempts
                ),
            }
        )
        external_publish_actions = list(observed_side_effect_actions)
        no_publish_passed = not external_publish_actions
        repository_scans = _repository_scans()
        if any(scan["match_count"] != 0 for scan in repository_scans.values()):
            raise AssertionError("repository secret/media scan found matches")
        manifest: dict[str, Any] = {
            "schema_version": "image-output-acceptance-manifest-v1",
            "result": "PASS" if no_publish_passed else "FAIL",
            "classification": "synthetic_test_only",
            "publishable": False,
            "adapter": "deterministic-local-image-v1",
            "authenticity_checker": "deterministic-local-authenticity-rules-v1",
            "outbound_guard": OUTBOUND_GUARD.snapshot(),
            "file_side_effect_guard": FILE_SIDE_EFFECT_GUARD.snapshot(),
            "external_publish_actions": external_publish_actions,
            "no_publish_evidence": {
                "api_operations": operations,
                "publish_capable_api_operations": publish_operations,
                "database_tables": database_tables,
                "publish_or_outbox_tables": publish_or_outbox_tables,
                "audit_event_types": audit_event_types,
                "publish_audit_events": publish_audit_events,
                "observed_outbound_attempts": list(OUTBOUND_GUARD.attempts),
                "observed_blocked_file_writes": list(
                    FILE_SIDE_EFFECT_GUARD.blocked_attempts
                ),
                "observed_side_effect_actions": observed_side_effect_actions,
            },
            "repository_scans": repository_scans,
            "runner": {
                "supported_platforms": ["linux", "macos"],
                "portability": "posix_only",
                "reason": (
                    "outbound interception and local artifact controls rely on POSIX"
                ),
            },
            "deterministic_runtime": {
                "identity_factory": identity_factory.version,
                "clock": NOW.isoformat(),
                "scope": "synthetic_demo_only_production_defaults_remain_random",
            },
            "prerequisite_contract": {
                "http_completed": sorted(
                    [
                        "asset_intake_confirmation_permission",
                        "main_style_generation",
                        "scene_style_creation",
                        "style_sample_authorization",
                    ]
                ),
                "required_api_operations_present": required_operations_present,
                "seeded_preconditions": [
                    {
                        "name": "ai_only_negative_fixture",
                        "reason": (
                            "no public API may create an AI asset that masquerades as real"
                        ),
                    },
                    {
                        "name": "synthetic_original_store_registration",
                        "reason": (
                            "the ordinary app uses UnconfiguredAssetContentStore and "
                            "has no media-upload API; acceptance registers temporary "
                            "synthetic bytes"
                        ),
                    },
                ],
                "ordinary_app_store": "UnconfiguredAssetContentStore",
                "claim": "acceptance_workflow_with_seeded_storage_precondition_not_full_e2e",
            },
            "inputs": {
                "customer_id": str(CUSTOMER_A_ID),
                "products": [
                    "product-scene",
                    "product-multi-a",
                    "product-multi-b",
                ],
                "asset_confirmation": "confirmed",
                "transform_permission": "granted",
                "main_style_card_version_id": pins["main_style_card_version_id"],
                "scene_style_card_version_id": pins["scene_style_card_version_id"],
                "scene_style_id": pins["scene_style_id"],
            },
            "flows": {
                "scene": {
                    "job_id": scene["id"],
                    "candidate_count": len(scene["candidates"]),
                    "status_after_authenticity": scene_checked["status"],
                    "checker_version": scene_checked["checker_version"],
                    "adapter_version": scene["adapter_version"],
                    "source_asset_ids": scene["source_asset_ids"],
                    "main_style_card_version_id": scene["main_style_card_version_id"],
                    "scene_style_card_version_id": scene["style_card_version_id"],
                    "replay_matches_original": scene_replay == scene,
                    "artifacts": _artifact_manifest(scene),
                },
                "multi_product": {
                    "job_id": multi["id"],
                    "candidate_count": len(multi["candidates"]),
                    "status_after_authenticity": multi_checked["status"],
                    "status_after_review": reviewed["job_status"],
                    "checker_version": multi_checked["checker_version"],
                    "adapter_version": multi["adapter_version"],
                    "source_asset_ids": multi["source_asset_ids"],
                    "main_style_card_version_id": multi["main_style_card_version_id"],
                    "scene_style_card_version_id": multi["style_card_version_id"],
                    "association_count": len(reviewed["product_associations"]),
                    "product_associations": exported_associations,
                    "customer_selection": {
                        "candidate_id": selected,
                        "content_sha256": selected_candidate["content_sha256"],
                        "review_id": reviewed["review_id"],
                    },
                    "default_view": default_view["resolved_view"],
                    "explicit_real_view": real_view["resolved_view"],
                    "replay_matches_original": multi_replay == multi,
                    "artifacts": _artifact_manifest(multi),
                },
            },
            "gates": {
                "unconfirmed_asset_rejected": {
                    "result": "PASS",
                    "http_status": unconfirmed.status_code,
                },
                "transform_permission_required": {
                    "result": "PASS",
                    "http_status": denied.status_code,
                    "error_code": denied_body["error"]["code"],
                },
                "quality_failure_blocks_review": {
                    "result": "PASS",
                    "http_status": quality_review.status_code,
                    "job_status": quality_checked["status"],
                },
                "not_approved_style_hidden": {
                    "result": "PASS",
                    "http_status": 200,
                    "resolved_view": unapproved_view["resolved_view"],
                },
                "cross_customer_hidden": {
                    "result": "PASS",
                    "http_status": foreign_job.status_code,
                },
                "no_real_photo_no_masquerade": {
                    "result": "PASS",
                    "http_status": no_real.status_code,
                },
                "no_publish_api_or_action": {
                    "result": "PASS" if no_publish_passed else "FAIL",
                    "http_status": None,
                },
            },
            "storage": {
                "database": "temporary_sqlite",
                "artifacts": "temporary_local_synthetic_store",
                "retained_media_files": 0,
            },
            "non_claims": [
                "not_production_ai",
                "not_deployed",
                "not_a_customer_pilot",
                "not_60_percent_online",
            ],
        }
    return manifest


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scan-only",
        action="store_true",
        help="Run repository secret/media scans only and emit structured counts.",
    )
    parser.add_argument(
        "--output",
        "--manifest",
        dest="output",
        type=Path,
        help="Optional user-selected JSON output path; stdout is always emitted.",
    )
    return parser.parse_args()


def _inject_caught_outbound_attempts() -> None:
    attempts = (
        lambda: socket.socket().connect(("127.0.0.1", 9)),
        lambda: socket.socket().connect_ex(("127.0.0.1", 9)),
        lambda: socket.create_connection(("127.0.0.1", 9)),
        lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(
            b"synthetic", ("127.0.0.1", 9)
        ),
        lambda: subprocess.Popen(["true"]),
        lambda: os.system("true"),
    )
    for attempt in attempts:
        try:
            attempt()
        except OutboundBlockedError:
            # Deliberately model business code swallowing the immediate block.
            pass


def _inject_caught_integer_fd_write(fixture: str) -> None:
    descriptors: list[int] = []
    sockets: list[socket.socket] = []
    if fixture == "closed":
        read_fd, write_fd = os.pipe()
        descriptors.append(read_fd)
        os.close(write_fd)
        target_fd = write_fd
    elif fixture == "pipe":
        read_fd, write_fd = os.pipe()
        descriptors.extend([read_fd, write_fd])
        target_fd = write_fd
    elif fixture == "socket":
        first, second = socket.socketpair()
        sockets.extend([first, second])
        target_fd = first.fileno()
    else:
        raise ValueError("unsupported integer-fd injection fixture")
    try:
        try:
            os.write(target_fd, b"synthetic blocked fd write\n")
        except FileSideEffectBlockedError:
            pass
    finally:
        for descriptor in descriptors:
            try:
                os.close(descriptor)
            except OSError:
                pass
        for opened_socket in sockets:
            opened_socket.close()


def _inject_caught_closed_dir_fd_write() -> None:
    directory_fd = os.open(".", os.O_RDONLY)
    os.close(directory_fd)
    try:
        os.open(
            "must-not-fall-back.queue",
            os.O_WRONLY | os.O_CREAT,
            0o600,
            dir_fd=directory_fd,
        )
    except FileSideEffectBlockedError:
        pass


def _failure_manifest(error: Exception) -> dict[str, object]:
    external_actions = sorted(
        {
            *(f"outbound:{item['channel']}" for item in OUTBOUND_GUARD.attempts),
            *(
                f"file_write:{item['operation']}:{item['target']}"
                for item in FILE_SIDE_EFFECT_GUARD.blocked_attempts
            ),
        }
    )
    return {
        "schema_version": "image-output-acceptance-manifest-v1",
        "result": "FAIL",
        "classification": "synthetic_test_only",
        "publishable": False,
        "error_type": type(error).__name__,
        "outbound_guard": OUTBOUND_GUARD.snapshot(),
        "file_side_effect_guard": FILE_SIDE_EFFECT_GUARD.snapshot(),
        "external_publish_actions": external_actions,
        "gates": {
            "no_publish_api_or_action": {
                "result": "FAIL",
                "http_status": None,
            }
        },
        "runner": {
            "supported_platforms": ["linux", "macos"],
            "portability": "posix_only",
            "reason": "outbound interception and local artifact controls rely on POSIX",
        },
    }


def _scan_only_manifest() -> dict[str, object]:
    scans = _repository_scans()
    passed = all(scan["match_count"] == 0 for scan in scans.values())
    return {
        "schema_version": "repository-scan-v1",
        "result": "PASS" if passed else "FAIL",
        "classification": "synthetic_test_only",
        "publishable": False,
        "command": SCAN_COMMAND,
        "repository_scans": scans,
        "outbound_guard": OUTBOUND_GUARD.snapshot(),
        "file_side_effect_guard": FILE_SIDE_EFFECT_GUARD.snapshot(),
    }


def _enforce_external_action_gate(manifest: dict[str, Any]) -> None:
    """Make a nonempty external-action inventory impossible to report as PASS."""
    if not manifest.get("external_publish_actions"):
        return
    manifest["result"] = "FAIL"
    gates = cast(dict[str, Any], manifest.setdefault("gates", {}))
    gates["no_publish_api_or_action"] = {
        "result": "FAIL",
        "http_status": None,
    }


def _emit(manifest: dict[str, object] | dict[str, Any], output: Path | None) -> None:
    serialized = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
    try:
        if output is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(f"{serialized}\n", encoding="utf-8")
    finally:
        # stdout/stderr are non-file descriptors; emit only after enforcement ends.
        FILE_SIDE_EFFECT_GUARD.deactivate()
    print(serialized)


def main() -> int:
    args = _parse_args()
    if args.output is not None:
        FILE_SIDE_EFFECT_GUARD.allow_file(args.output, "explicit_output")
    preopened_outside_fd: int | None = None
    try:
        if os.name != "posix" or sys.platform not in {"darwin", "linux"}:
            raise RuntimeError("acceptance runner supports macOS/Linux POSIX only")
        if args.scan_only:
            FILE_SIDE_EFFECT_GUARD.activate()
            manifest = _scan_only_manifest()
        else:
            preopened_path = os.environ.get("OPC_DEMO_INJECT_PREOPENED_FD_WRITE")
            if preopened_path:
                preopened_outside_fd = os.open(
                    preopened_path,
                    os.O_WRONLY | os.O_APPEND,
                )
            with tempfile.TemporaryDirectory(
                prefix="opc-image-output-demo-"
            ) as temp_name:
                temp_root = Path(temp_name)
                FILE_SIDE_EFFECT_GUARD.allow_root(temp_root, "acceptance_temp_root")
                FILE_SIDE_EFFECT_GUARD.activate()
                if os.environ.get("OPC_DEMO_INJECT_CAUGHT_OUTBOUND") == "all":
                    _inject_caught_outbound_attempts()
                    OUTBOUND_GUARD.assert_clean()
                integer_fd_fixture = os.environ.get(
                    "OPC_DEMO_INJECT_CAUGHT_INTEGER_FD_WRITE"
                )
                if integer_fd_fixture:
                    _inject_caught_integer_fd_write(integer_fd_fixture)
                if os.environ.get("OPC_DEMO_INJECT_CAUGHT_CLOSED_DIR_FD_WRITE"):
                    _inject_caught_closed_dir_fd_write()
                manifest = run_demo(temp_root, preopened_outside_fd)
        OUTBOUND_GUARD.assert_clean()
    # Any acceptance failure must produce a structured FAIL manifest and nonzero exit.
    except Exception as error:  # noqa: BLE001
        manifest = _failure_manifest(error)
        exit_code = 1
    else:
        _enforce_external_action_gate(manifest)
        exit_code = 0 if manifest["result"] == "PASS" else 1
    finally:
        if preopened_outside_fd is not None:
            os.close(preopened_outside_fd)
    _emit(manifest, args.output)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
