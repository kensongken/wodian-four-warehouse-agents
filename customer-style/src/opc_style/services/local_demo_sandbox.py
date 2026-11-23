"""Physically isolated SQLite and media runtime for the local synthetic demo."""

from __future__ import annotations

import hmac
import json
import math
import os
import stat
from collections.abc import Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from secrets import token_bytes
from threading import RLock
from typing import Literal
from urllib.parse import unquote, urlsplit
from uuid import NAMESPACE_URL, UUID, uuid5

from PIL import Image, ImageDraw
from sqlalchemy import func, inspect, select, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from opc_style.db import create_database_engine
from opc_style.domain.analyzer import STYLE_ANALYSIS_LIST_FIELDS
from opc_style.models import (
    AuditEvent,
    Base,
    Customer,
    ProductAsset,
    ProductAssetPermissionEvent,
    SceneStyle,
    StyleCardVersion,
    StyleSample,
    StyleTemplateSlot,
)
from opc_style.schemas import HistoricalStyleGroupsConfirmRequest
from opc_style.services.real_photos import (
    AssetContentStoreError,
    LocalSyntheticAssetStore,
)
from opc_style.services.styles import HistoricalStyleConfirmationService

_SUBJECT_CONTEXT = b"local-demo-subject-v1:"
_CONFIRMATION_KEY = "local-demo-sandbox-v1"
_PRODUCT_ID = "local-demo-built-in-product-v1"
_PRODUCT_PHOTO_ID = "local-demo-built-in-product-photo-v1"
_PRODUCT_WIDTH = 320
_PRODUCT_HEIGHT = 320
_SCENE_STYLE_ORIGIN_INDEX = "uq_scene_styles_active_customer_origin_name"
_SCENE_STYLE_OLD_INDEX = "uq_scene_styles_active_customer_name"
_SCENE_STYLE_ORIGIN_VALID_TRIGGER = "trg_scene_styles_origin_valid"
_SCENE_STYLE_ORIGIN_IMMUTABLE_TRIGGER = "trg_scene_styles_origin_immutable"
_SCENE_STYLE_ORIGIN_VALID_TRIGGER_SQL = (
    f"CREATE TRIGGER {_SCENE_STYLE_ORIGIN_VALID_TRIGGER} "
    "BEFORE INSERT ON scene_styles "
    "WHEN NEW.style_origin NOT IN ('customer_upload', 'local_demo') "
    "BEGIN SELECT RAISE(ABORT, 'scene_style_origin_invalid'); END"
)
_SCENE_STYLE_ORIGIN_IMMUTABLE_TRIGGER_SQL = (
    f"CREATE TRIGGER {_SCENE_STYLE_ORIGIN_IMMUTABLE_TRIGGER} "
    "BEFORE UPDATE OF style_origin ON scene_styles "
    "WHEN NEW.style_origin IS NOT OLD.style_origin "
    "BEGIN SELECT RAISE(ABORT, 'scene_style_origin_immutable'); END"
)


class LocalDemoSandboxError(RuntimeError):
    """Known sandbox failure safe to expose through a stable local API code."""

    def __init__(self, code: str, *, status_code: int, retryable: bool) -> None:
        self.code = code
        self.status_code = status_code
        self.retryable = retryable
        super().__init__(code)


class LocalDemoSandboxConflictError(LocalDemoSandboxError):
    """Persisted sandbox facts conflict with the sealed demo definition."""

    def __init__(self, code: str) -> None:
        super().__init__(code, status_code=409, retryable=False)


class LocalDemoSandboxUnavailableError(LocalDemoSandboxError):
    """The sandbox database or private media store is temporarily unavailable."""

    def __init__(self, code: str) -> None:
        super().__init__(code, status_code=503, retryable=True)


@dataclass(frozen=True)
class DemoSlotDescriptor:
    """Canonical browser-facing identity for one local demo slot."""

    slot_key: str
    label: str


@dataclass(frozen=True)
class DemoStyleDescriptor:
    """Canonical browser-facing identity for one local demo style."""

    name: str
    slots: tuple[DemoSlotDescriptor, ...]


_DEMO_STYLES = (
    DemoStyleDescriptor(
        name="暖光桌面",
        slots=(
            DemoSlotDescriptor(slot_key="hero", label="主图"),
            DemoSlotDescriptor(slot_key="detail", label="细节"),
            DemoSlotDescriptor(slot_key="wear", label="佩戴"),
        ),
    ),
    DemoStyleDescriptor(
        name="清透白底",
        slots=(
            DemoSlotDescriptor(slot_key="hero", label="主图"),
            DemoSlotDescriptor(slot_key="side", label="侧面"),
        ),
    ),
)


@dataclass(frozen=True)
class DemoBootstrapSummary:
    """Stable descriptors and counts exposed by a complete demo bootstrap."""

    sample_count: int
    style_count: int
    slot_count: int
    styles: tuple[DemoStyleDescriptor, ...] = _DEMO_STYLES


@dataclass(frozen=True)
class DemoCanonicalScope:
    """Exact server-owned style revision and slot used by the demo run."""

    scene_style_version_id: UUID
    slot_key: str


_SUMMARY = DemoBootstrapSummary(
    sample_count=10,
    style_count=len(_DEMO_STYLES),
    slot_count=sum(len(style.slots) for style in _DEMO_STYLES),
)


def _upgrade_existing_demo_schema(engine: Engine) -> None:
    """Upgrade an existing local demo schema before current ORM access."""
    if not inspect(engine).has_table("scene_styles"):
        return

    with engine.connect() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        fidelity_columns = {
            item["name"]
            for item in inspect(connection).get_columns(
                "style_suite_candidate_fidelities"
            )
        }
        if "warning_acknowledged" not in fidelity_columns:
            connection.execute(
                text(
                    "ALTER TABLE style_suite_candidate_fidelities "
                    "ADD COLUMN warning_acknowledged BOOLEAN NOT NULL DEFAULT 0"
                )
            )
        columns = {
            item["name"] for item in inspect(connection).get_columns("scene_styles")
        }
        if "style_origin" not in columns:
            connection.execute(
                text(
                    "ALTER TABLE scene_styles ADD COLUMN style_origin VARCHAR(32) "
                    "NOT NULL DEFAULT 'customer_upload'"
                )
            )
            conflicting = connection.execute(
                text(
                    "SELECT s.id FROM scene_styles s "
                    "WHERE EXISTS (SELECT 1 FROM style_card_versions v "
                    "WHERE v.customer_id = s.customer_id "
                    "AND v.style_card_id = s.style_card_id "
                    "AND json_extract(v.content, '$.style_origin') = 'local_demo') "
                    "AND EXISTS (SELECT 1 FROM style_card_versions v "
                    "WHERE v.customer_id = s.customer_id "
                    "AND v.style_card_id = s.style_card_id "
                    "AND json_extract(v.content, '$.style_origin') = "
                    "'customer_upload') LIMIT 1"
                )
            ).first()
            if conflicting is not None:
                raise RuntimeError("scene style history has conflicting origins")
            connection.execute(
                text(
                    "UPDATE scene_styles SET style_origin = 'local_demo' "
                    "WHERE EXISTS (SELECT 1 FROM style_card_versions v "
                    "WHERE v.customer_id = scene_styles.customer_id "
                    "AND v.style_card_id = scene_styles.style_card_id "
                    "AND json_extract(v.content, '$.style_origin') = 'local_demo')"
                )
            )

        connection.execute(text(f"DROP INDEX IF EXISTS {_SCENE_STYLE_OLD_INDEX}"))
        connection.execute(
            text(
                f"CREATE UNIQUE INDEX IF NOT EXISTS {_SCENE_STYLE_ORIGIN_INDEX} "
                "ON scene_styles (customer_id, style_origin, name) WHERE active = 1"
            )
        )
        connection.execute(
            text(
                _SCENE_STYLE_ORIGIN_VALID_TRIGGER_SQL.replace(
                    "CREATE TRIGGER ", "CREATE TRIGGER IF NOT EXISTS ", 1
                )
            )
        )
        connection.execute(
            text(
                _SCENE_STYLE_ORIGIN_IMMUTABLE_TRIGGER_SQL.replace(
                    "CREATE TRIGGER ", "CREATE TRIGGER IF NOT EXISTS ", 1
                )
            )
        )

        indexes = {
            item["name"]: item
            for item in inspect(connection).get_indexes("scene_styles")
        }
        origin_index = indexes.get(_SCENE_STYLE_ORIGIN_INDEX)
        if (
            origin_index is None
            or origin_index["column_names"] != ["customer_id", "style_origin", "name"]
            or not origin_index["unique"]
            or _SCENE_STYLE_OLD_INDEX in indexes
        ):
            raise RuntimeError("invalid local demo scene-style origin index")
        where_clause = str(
            origin_index.get("dialect_options", {}).get("sqlite_where", "")
        )
        if " ".join(where_clause.lower().split()) not in {
            "active = 1",
            "active is 1",
        }:
            raise RuntimeError("invalid local demo scene-style origin index")

        trigger_sql = {
            row["name"]: " ".join((row["sql"] or "").lower().split())
            for row in connection.execute(
                text(
                    "SELECT name, sql FROM sqlite_master "
                    "WHERE type = 'trigger' AND tbl_name = 'scene_styles'"
                )
            ).mappings()
        }
        expected_trigger_sql = {
            _SCENE_STYLE_ORIGIN_VALID_TRIGGER: " ".join(
                _SCENE_STYLE_ORIGIN_VALID_TRIGGER_SQL.lower().split()
            ),
            _SCENE_STYLE_ORIGIN_IMMUTABLE_TRIGGER: " ".join(
                _SCENE_STYLE_ORIGIN_IMMUTABLE_TRIGGER_SQL.lower().split()
            ),
        }
        for name, expected in expected_trigger_sql.items():
            if trigger_sql.get(name) != expected:
                raise RuntimeError("invalid local demo scene-style origin guard")
        connection.commit()


def canonical_sqlite_database_path(
    database_url: str,
) -> Path | None:
    """Resolve a filesystem-backed SQLite URL to the file SQLite will open."""
    url = make_url(database_url)
    if not url.drivername.startswith("sqlite"):
        return None
    connect_args, connect_options = url.get_dialect()().create_connect_args(url)
    database = str(connect_args[0])
    if database in {"", ":memory:"}:
        return None
    if connect_options.get("uri") is True and database.startswith("file:"):
        parsed = urlsplit(database)
        if parsed.path == ":memory:" or url.query.get("mode") == "memory":
            return None
        database = unquote(parsed.path)
    path = Path(database).expanduser()
    return path.resolve(strict=False)


def validate_isolated_runtime_roots(roots: Mapping[str, Path]) -> None:
    """Reject any exact, nested, aliased, or same-inode runtime media roots."""
    normalized: list[tuple[str, Path, tuple[int, int] | None]] = []
    for name, configured in roots.items():
        try:
            path = configured.expanduser().resolve(strict=False)
            value = path.stat() if path.exists() else None
        except (OSError, RuntimeError) as error:
            raise RuntimeError("local_runtime_root_unavailable") from error
        identity = (value.st_dev, value.st_ino) if value is not None else None
        normalized.append((name, path, identity))
    for position, (_, left, left_identity) in enumerate(normalized):
        for _, right, right_identity in normalized[position + 1 :]:
            if (
                left == right
                or left in right.parents
                or right in left.parents
                or (
                    left_identity is not None
                    and right_identity is not None
                    and left_identity == right_identity
                )
            ):
                raise RuntimeError("local_runtime_roots_overlap")


class LocalDemoSandbox:
    """Own an isolated local database, media namespace, and subject secret."""

    def __init__(self, root: Path) -> None:
        configured_root = root.absolute()
        _ensure_private_directory(configured_root)
        self.sandbox_root = configured_root / "demo-sandbox"
        _ensure_private_directory(self.sandbox_root)
        self.database_path = self.sandbox_root / "demo.sqlite3"
        self.media_root = self.sandbox_root / "media"
        self.subject_key_path = self.sandbox_root / "subject.key"
        self._subject_key = _load_or_create_key(self.subject_key_path)
        self.preview_secret = token_bytes(32)
        engine: Engine | None = None
        try:
            engine = create_database_engine(f"sqlite:///{self.database_path}")
            _upgrade_existing_demo_schema(engine)
            Base.metadata.create_all(engine)
        except OperationalError as error:
            if engine is not None:
                engine.dispose()
            raise LocalDemoSandboxUnavailableError(
                "local_demo_database_unavailable"
            ) from error
        self._engine = engine
        self.database_path.chmod(0o600)
        _validate_private_file(self.database_path, expected_mode=0o600)
        self._sessions = sessionmaker(
            bind=self._engine, expire_on_commit=False, class_=Session
        )
        self._bootstrap_lock = RLock()
        self.store = LocalSyntheticAssetStore(
            self.media_root, reopen=self.media_root.exists()
        )
        self._closed = False

    def subject_id(self, real_customer_id: UUID) -> UUID:
        """Derive a stable opaque subject without persisting the real UUID."""
        digest = hmac.new(
            self._subject_key,
            _SUBJECT_CONTEXT + real_customer_id.bytes,
            sha256,
        ).digest()
        return UUID(bytes=digest[:16])

    def session(self) -> AbstractContextManager[Session]:
        """Open a session bound only to the sandbox database."""
        return self._sessions()

    def bootstrap(self, real_customer_id: UUID) -> DemoBootstrapSummary:
        """Create the complete fixed demo in one database commit."""
        with self._bootstrap_lock:
            try:
                return self._bootstrap_once(real_customer_id)
            except LocalDemoSandboxError:
                raise
            except OperationalError as error:
                raise LocalDemoSandboxUnavailableError(
                    "local_demo_database_unavailable"
                ) from error
            except AssetContentStoreError as error:
                raise LocalDemoSandboxUnavailableError(
                    "local_demo_storage_unavailable"
                ) from error

    def _bootstrap_once(self, real_customer_id: UUID) -> DemoBootstrapSummary:
        """Run one serialized bootstrap attempt after the replay re-check."""
        subject_id = self.subject_id(real_customer_id)
        sample_specs = _sample_specs(subject_id)
        confirmation = _confirmation(tuple(item[0] for item in sample_specs))
        confirmation_fingerprint = _confirmation_fingerprint(confirmation)
        product_content = _product_jpeg()
        product_hash = sha256(product_content).hexdigest()
        product_id = _product_asset_id(subject_id)
        new_media: list[tuple[Literal["style-samples", "originals"], UUID, str]] = []

        with self.session() as session:
            receipt = session.get(AuditEvent, _receipt_id(subject_id))
            if receipt is not None:
                self._verify_complete_bootstrap(
                    session,
                    subject_id,
                    sample_specs,
                    receipt,
                    confirmation_fingerprint,
                    product_id,
                    product_content,
                    product_hash,
                )
                return _SUMMARY
            try:
                session.add(Customer(id=subject_id, display_name="Local demo subject"))
                session.flush()
                for sample_id, photo_id, content, content_hash in sample_specs:
                    sample = StyleSample(
                        id=sample_id,
                        customer_id=subject_id,
                        photo_id=photo_id,
                        authorized_at=datetime.now(UTC),
                        consent_provenance="local_synthetic_acceptance",
                        consent_text_version="local-demo-v1",
                        content_sha256=content_hash,
                        mime_type="image/jpeg",
                        width=32,
                        height=32,
                        asset_store_namespace=self.store.store_namespace,
                    )
                    session.add(sample)
                    session.flush()
                    existed = self.store.verify_style_sample_content(
                        subject_id, sample_id, content_hash
                    )
                    self.store.persist_style_sample_content(
                        subject_id, sample_id, content, content_hash
                    )
                    if not existed:
                        new_media.append(("style-samples", sample_id, content_hash))

                HistoricalStyleConfirmationService(session).confirm(
                    subject_id,
                    confirmation,
                    style_origin="local_demo",
                    commit=False,
                )
                self._add_product(
                    session,
                    subject_id,
                    product_id,
                    product_content,
                    product_hash,
                    new_media,
                )
                receipt = session.get(AuditEvent, _receipt_id(subject_id))
                if receipt is None:
                    raise LocalDemoSandboxConflictError(
                        "local_demo_bootstrap_receipt_missing"
                    )
                self._verify_complete_bootstrap(
                    session,
                    subject_id,
                    sample_specs,
                    receipt,
                    confirmation_fingerprint,
                    product_id,
                    product_content,
                    product_hash,
                )
                session.commit()
                return _SUMMARY
            except BaseException:
                session.rollback()
                self._discard_uncommitted_media(subject_id, new_media)
                raise

    def ensure_product(self, real_customer_id: UUID) -> ProductAsset:
        """Return the fixed server-owned product, bootstrapping when necessary."""
        self.bootstrap(real_customer_id)
        subject_id = self.subject_id(real_customer_id)
        with self.session() as session:
            product = session.scalar(
                select(ProductAsset).where(
                    ProductAsset.customer_id == subject_id,
                    ProductAsset.id == _product_asset_id(subject_id),
                    ProductAsset.product_id == _PRODUCT_ID,
                )
            )
            if product is None:
                raise LocalDemoSandboxConflictError("local_demo_product_missing")
            return product

    def canonical_scope(self, real_customer_id: UUID) -> DemoCanonicalScope:
        """Resolve the declared first demo style and its declared first slot exactly."""
        subject_id = self.subject_id(real_customer_id)
        canonical_style = _DEMO_STYLES[0]
        canonical_slot = canonical_style.slots[0]
        with self.session() as session:
            scene = session.scalar(
                select(SceneStyle).where(
                    SceneStyle.customer_id == subject_id,
                    SceneStyle.name == canonical_style.name,
                    SceneStyle.style_origin == "local_demo",
                    SceneStyle.active.is_(True),
                )
            )
            if scene is None:
                raise LocalDemoSandboxConflictError(
                    "local_demo_canonical_style_missing"
                )
            version = session.scalar(
                select(StyleCardVersion).where(
                    StyleCardVersion.customer_id == subject_id,
                    StyleCardVersion.style_card_id == scene.style_card_id,
                    StyleCardVersion.version == 1,
                )
            )
            if version is None:
                raise LocalDemoSandboxConflictError(
                    "local_demo_canonical_version_missing"
                )
            slot = session.scalar(
                select(StyleTemplateSlot).where(
                    StyleTemplateSlot.customer_id == subject_id,
                    StyleTemplateSlot.scene_style_id == scene.id,
                    StyleTemplateSlot.style_card_version_id == version.id,
                    StyleTemplateSlot.slot_key == canonical_slot.slot_key,
                    StyleTemplateSlot.active.is_(True),
                )
            )
            if slot is None:
                raise LocalDemoSandboxConflictError("local_demo_canonical_slot_missing")
            return DemoCanonicalScope(
                scene_style_version_id=version.id,
                slot_key=slot.slot_key,
            )

    def count_all_rows(self) -> int:
        """Count application rows for fail-closed bootstrap verification."""
        with self.session() as session:
            return sum(
                session.scalar(select(func.count()).select_from(table)) or 0
                for table in Base.metadata.tables.values()
            )

    def close(self) -> None:
        """Release the media descriptor and database connection pool."""
        if self._closed:
            return
        self._closed = True
        self.store.close()
        self._engine.dispose()

    def _add_product(
        self,
        session: Session,
        subject_id: UUID,
        product_id: UUID,
        content: bytes,
        content_hash: str,
        new_media: list[tuple[Literal["style-samples", "originals"], UUID, str]],
    ) -> None:
        product = ProductAsset(
            id=product_id,
            customer_id=subject_id,
            product_id=_PRODUCT_ID,
            photo_id=_PRODUCT_PHOTO_ID,
            identity="original_real_photo",
            confirmed=True,
            source_type="synthetic_metadata",
            quality_flags=["local_synthetic_acceptance"],
            occurred_at=datetime.now(UTC),
            content_sha256=content_hash,
            mime_type="image/jpeg",
            width=_PRODUCT_WIDTH,
            height=_PRODUCT_HEIGHT,
            storage_path=None,
        )
        session.add(product)
        session.flush()
        existed = _has_original(self.store, subject_id, product_id, content_hash)
        self.store.persist_original(subject_id, product_id, content, content_hash)
        if not existed:
            new_media.append(("originals", product_id, content_hash))
        session.add(
            ProductAssetPermissionEvent(
                customer_id=subject_id,
                product_asset_id=product_id,
                transform_permission=True,
                occurred_at=datetime.now(UTC),
                provenance="local_synthetic_acceptance",
            )
        )
        session.flush()

    def _verify_complete_bootstrap(
        self,
        session: Session,
        subject_id: UUID,
        sample_specs: tuple[tuple[UUID, str, bytes, str], ...],
        receipt: AuditEvent,
        confirmation_fingerprint: str,
        product_id: UUID,
        product_content: bytes,
        product_hash: str,
    ) -> None:
        counts = _sandbox_counts(session, subject_id)
        if counts != (
            _SUMMARY.sample_count,
            _SUMMARY.style_count,
            _SUMMARY.slot_count,
        ):
            raise LocalDemoSandboxConflictError("local_demo_bootstrap_state_invalid")
        if (
            receipt.customer_id != subject_id
            or receipt.event_type != "style_card.historical.confirmed"
            or receipt.payload.get("fingerprint") != confirmation_fingerprint
            or receipt.payload.get("style_origin") != "local_demo"
            or not _receipt_versions_match(
                session, subject_id, receipt.payload.get("versions"), sample_specs
            )
        ):
            raise LocalDemoSandboxConflictError("local_demo_bootstrap_receipt_invalid")

        samples = list(
            session.scalars(
                select(StyleSample)
                .where(StyleSample.customer_id == subject_id)
                .order_by(StyleSample.photo_id)
            )
        )
        expected_by_id = {item[0]: item for item in sample_specs}
        if {sample.id for sample in samples} != set(expected_by_id):
            raise LocalDemoSandboxConflictError("local_demo_bootstrap_samples_invalid")
        for sample in samples:
            _, photo_id, _, content_hash = expected_by_id[sample.id]
            if (
                sample.photo_id != photo_id
                or sample.content_sha256 != content_hash
                or sample.asset_store_namespace != self.store.store_namespace
                or not self.store.verify_style_sample_content(
                    subject_id, sample.id, content_hash
                )
            ):
                raise LocalDemoSandboxConflictError(
                    "local_demo_bootstrap_samples_invalid"
                )

        product = session.get(ProductAsset, product_id)
        permission_count = session.scalar(
            select(func.count())
            .select_from(ProductAssetPermissionEvent)
            .where(
                ProductAssetPermissionEvent.customer_id == subject_id,
                ProductAssetPermissionEvent.product_asset_id == product_id,
                ProductAssetPermissionEvent.transform_permission.is_(True),
            )
        )
        if (
            product is None
            or product.product_id != _PRODUCT_ID
            or product.photo_id != _PRODUCT_PHOTO_ID
            or product.content_sha256 != product_hash
            or product.mime_type != "image/jpeg"
            or product.width != _PRODUCT_WIDTH
            or product.height != _PRODUCT_HEIGHT
            or permission_count != 1
            or self.store.read_original(subject_id, product_id, product_hash)
            != product_content
        ):
            raise LocalDemoSandboxConflictError("local_demo_product_invalid")

    def _discard_uncommitted_media(
        self,
        subject_id: UUID,
        new_media: list[tuple[Literal["style-samples", "originals"], UUID, str]],
    ) -> None:
        for kind, asset_id, content_hash in reversed(new_media):
            self.store.discard_uncommitted_asset(
                subject_id, kind, asset_id, content_hash
            )


def _ensure_private_directory(path: Path) -> None:
    if path.is_symlink():
        raise RuntimeError("unsafe_local_demo_directory")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    value = path.lstat()
    if (
        stat.S_ISLNK(value.st_mode)
        or not stat.S_ISDIR(value.st_mode)
        or value.st_uid != os.getuid()
        or stat.S_IMODE(value.st_mode) != 0o700
        or path.resolve(strict=True) != path
    ):
        raise RuntimeError("unsafe_local_demo_directory")


def _load_or_create_key(path: Path) -> bytes:
    if not path.exists() and not path.is_symlink():
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        descriptor = os.open(path, flags, 0o600)
        try:
            key = token_bytes(32)
            written = 0
            while written < len(key):
                written += os.write(descriptor, key[written:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    _validate_private_file(path, expected_mode=0o600)
    key = path.read_bytes()
    if len(key) != 32:
        raise RuntimeError("invalid_local_demo_subject_key")
    return key


def _validate_private_file(path: Path, *, expected_mode: int) -> None:
    value = path.lstat()
    if (
        stat.S_ISLNK(value.st_mode)
        or not stat.S_ISREG(value.st_mode)
        or value.st_uid != os.getuid()
        or stat.S_IMODE(value.st_mode) != expected_mode
    ):
        raise RuntimeError("unsafe_local_demo_file")


def _sample_specs(subject_id: UUID) -> tuple[tuple[UUID, str, bytes, str], ...]:
    specs: list[tuple[UUID, str, bytes, str]] = []
    for index in range(1, 11):
        photo_id = f"local-demo-sample-v1-{index:02d}"
        content = _sample_jpeg(index)
        specs.append(
            (
                uuid5(subject_id, photo_id),
                photo_id,
                content,
                sha256(content).hexdigest(),
            )
        )
    return tuple(specs)


def _sample_jpeg(seed: int) -> bytes:
    stream = BytesIO()
    Image.new("RGB", (32, 32), (seed * 12, 40, 80)).save(
        stream, format="JPEG", quality=90, optimize=False, progressive=False
    )
    return stream.getvalue()


def _product_jpeg() -> bytes:
    image = Image.new("RGB", (_PRODUCT_WIDTH, _PRODUCT_HEIGHT), "#f7efe3")
    draw = ImageDraw.Draw(image)
    for index in range(28):
        angle = index * math.tau / 28
        x = 160 + 92 * math.cos(angle)
        y = 160 + 92 * math.sin(angle)
        fill = "#16816f" if index % 2 else "#22a18a"
        draw.ellipse(
            (x - 11, y - 11, x + 11, y + 11), fill=fill, outline="#0c5a4e", width=2
        )
    stream = BytesIO()
    image.save(stream, format="JPEG", quality=90, optimize=False, progressive=False)
    return stream.getvalue()


def _slot(key: str, label: str) -> dict[str, object]:
    return {
        "slot_key": key,
        "label": label,
        "rules": {
            "aspect_ratio": "4:5",
            "shot_distance": "close",
            "camera_angle": "front",
            "composition": ["centered"],
            "background": ["clean"],
            "lighting": ["soft"],
            "props": ["none"],
            "negative_rules": ["no text"],
        },
    }


def _confirmation(
    sample_ids: tuple[UUID, ...],
) -> HistoricalStyleGroupsConfirmRequest:
    warm_style, clean_style = _DEMO_STYLES
    return HistoricalStyleGroupsConfirmRequest.model_validate(
        {
            "confirmation_key": _CONFIRMATION_KEY,
            "confirmed": True,
            "groups": [
                {
                    "name": warm_style.name,
                    "evidence_sample_ids": sample_ids[:5],
                    "proposal_keys": ["warm-table"],
                    "tone": ["warm"],
                    "slots": [
                        _slot(slot.slot_key, slot.label) for slot in warm_style.slots
                    ],
                },
                {
                    "name": clean_style.name,
                    "evidence_sample_ids": sample_ids[5:],
                    "proposal_keys": ["clean-white"],
                    "tone": ["clean"],
                    "slots": [
                        _slot(slot.slot_key, slot.label) for slot in clean_style.slots
                    ],
                },
            ],
        }
    )


def _receipt_id(subject_id: UUID) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        f"opc-style-confirmation:{subject_id}:{_CONFIRMATION_KEY}",
    )


def _confirmation_fingerprint(
    confirmation: HistoricalStyleGroupsConfirmRequest,
) -> str:
    return sha256(
        json.dumps(
            {
                "request": confirmation.model_dump(mode="json"),
                "style_origin": "local_demo",
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _receipt_versions_match(
    session: Session,
    subject_id: UUID,
    raw_versions: object,
    sample_specs: tuple[tuple[UUID, str, bytes, str], ...],
) -> bool:
    if not isinstance(raw_versions, list) or len(raw_versions) != _SUMMARY.style_count:
        return False
    confirmation = _confirmation(tuple(item[0] for item in sample_specs))
    expected_groups = {group.name: group for group in confirmation.groups}
    photo_by_sample_id = {item[0]: item[1] for item in sample_specs}
    referenced_scene_ids: set[UUID] = set()
    referenced_version_ids: set[UUID] = set()
    for raw_entry in raw_versions:
        if not isinstance(raw_entry, dict) or set(raw_entry) != {
            "scene_id",
            "version_id",
        }:
            return False
        try:
            scene_id = UUID(str(raw_entry["scene_id"]))
            version_id = UUID(str(raw_entry["version_id"]))
        except (TypeError, ValueError):
            return False
        if scene_id in referenced_scene_ids or version_id in referenced_version_ids:
            return False
        scene = session.scalar(
            select(SceneStyle).where(
                SceneStyle.id == scene_id,
                SceneStyle.customer_id == subject_id,
                SceneStyle.active.is_(True),
            )
        )
        version = session.scalar(
            select(StyleCardVersion).where(
                StyleCardVersion.id == version_id,
                StyleCardVersion.customer_id == subject_id,
            )
        )
        if (
            scene is None
            or scene.style_origin != "local_demo"
            or version is None
            or scene.style_card_id != scene.id
            or version.style_card_id != scene.style_card_id
            or version.version != 1
        ):
            return False
        expected_group = expected_groups.pop(scene.name, None)
        if expected_group is None:
            return False
        slots = list(
            session.scalars(
                select(StyleTemplateSlot)
                .where(
                    StyleTemplateSlot.customer_id == subject_id,
                    StyleTemplateSlot.scene_style_id == scene_id,
                    StyleTemplateSlot.style_card_version_id == version_id,
                )
                .order_by(StyleTemplateSlot.position)
            )
        )
        if len(slots) != len(expected_group.slots):
            return False
        slot_snapshots: list[dict[str, object]] = []
        for position, (slot, expected_slot) in enumerate(
            zip(slots, expected_group.slots, strict=True)
        ):
            if (
                slot.position != position
                or slot.slot_key != expected_slot.slot_key
                or slot.label != expected_slot.label
                or slot.rules != expected_slot.rules.model_dump(mode="json")
                or slot.active is not expected_slot.active
            ):
                return False
            slot_snapshots.append(
                {
                    "slot_key": slot.slot_key,
                    "label": slot.label,
                    "rules": slot.rules,
                    "active": slot.active,
                    "id": str(slot.id),
                    "customer_id": str(slot.customer_id),
                    "scene_style_id": str(slot.scene_style_id),
                    "style_card_version_id": str(slot.style_card_version_id),
                    "position": slot.position,
                }
            )
        group_payload = expected_group.model_dump(exclude_none=True)
        overrides = {
            field: value
            for field, value in group_payload.items()
            if field in STYLE_ANALYSIS_LIST_FIELDS
        }
        expected_content = {
            **{
                field: list(overrides.get(field, []))
                for field in STYLE_ANALYSIS_LIST_FIELDS
            },
            "name": expected_group.name,
            "main_style_card_version_id": None,
            "historical_style": True,
            "style_origin": "local_demo",
            "overrides": overrides,
            "source": "customer_confirmation",
            "analyzer": "customer_reviewed_proposal",
            "analysis_mode": "customer_confirmed_historical_style",
            "stability": "customer_confirmed",
            "sample_count": len(expected_group.evidence_sample_ids),
            "evidence_photo_ids": [
                photo_by_sample_id[sample_id]
                for sample_id in expected_group.evidence_sample_ids
            ],
            "proposal_keys": expected_group.proposal_keys,
            "slots": slot_snapshots,
        }
        if version.content != expected_content:
            return False
        referenced_scene_ids.add(scene_id)
        referenced_version_ids.add(version_id)
    if expected_groups:
        return False
    all_scene_ids = set(
        session.scalars(
            select(SceneStyle.id).where(SceneStyle.customer_id == subject_id)
        )
    )
    all_version_ids = set(
        session.scalars(
            select(StyleCardVersion.id).where(
                StyleCardVersion.customer_id == subject_id
            )
        )
    )
    return (
        referenced_scene_ids == all_scene_ids
        and referenced_version_ids == all_version_ids
    )


def _product_asset_id(subject_id: UUID) -> UUID:
    return uuid5(subject_id, _PRODUCT_PHOTO_ID)


def _has_original(
    store: LocalSyntheticAssetStore,
    subject_id: UUID,
    product_id: UUID,
    content_hash: str,
) -> bool:
    try:
        store.read_original(subject_id, product_id, content_hash)
    except AssetContentStoreError:
        return False
    return True


def _sandbox_counts(session: Session, subject_id: UUID) -> tuple[int, int, int]:
    return (
        session.scalar(
            select(func.count())
            .select_from(StyleSample)
            .where(StyleSample.customer_id == subject_id)
        )
        or 0,
        session.scalar(
            select(func.count())
            .select_from(SceneStyle)
            .where(SceneStyle.customer_id == subject_id)
        )
        or 0,
        session.scalar(
            select(func.count())
            .select_from(StyleTemplateSlot)
            .where(StyleTemplateSlot.customer_id == subject_id)
        )
        or 0,
    )
