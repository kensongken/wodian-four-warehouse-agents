"""Physical isolation and atomicity checks for the local demo sandbox."""

from __future__ import annotations

import stat
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from threading import Barrier, BrokenBarrierError, Lock
from unittest.mock import patch
from urllib.parse import quote
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import func, inspect, select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

import opc_style.services.local_demo_sandbox as local_demo_sandbox_module
from opc_style.api.dependencies import get_session
from opc_style.api.routes_real_photos import get_asset_content_store
from opc_style.db import create_database_engine
from opc_style.main import create_app
from opc_style.models import (
    AuditEvent,
    Base,
    Customer,
    ProductAsset,
    ProductAssetPermissionEvent,
    SceneStyle,
    StyleSample,
    StyleSuiteCandidateFidelity,
    StyleTemplateSlot,
)
from opc_style.schemas import HistoricalStyleGroupsConfirmRequest
from opc_style.services.local_demo_sandbox import (
    DemoBootstrapSummary,
    LocalDemoSandbox,
    LocalDemoSandboxUnavailableError,
)
from opc_style.services.real_photos import (
    AssetContentStoreError,
    LocalSyntheticAssetStore,
)
from opc_style.services.styles import HistoricalStyleConfirmationService

CUSTOMER_A = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
CUSTOMER_B = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
KNOWN_DEMO_SUBJECT_KEY = b"known-local-demo-subject-key-v1!"


def table_counts(session: Session) -> dict[str, int]:
    """Count every application table so isolation checks cannot omit a table."""
    return {
        table.name: session.scalar(select(func.count()).select_from(table)) or 0
        for table in sorted(Base.metadata.tables.values(), key=lambda item: item.name)
    }


def sandbox_counts(
    sandbox: LocalDemoSandbox, real_customer_id: UUID
) -> tuple[int, int, int]:
    """Return the three bootstrap facts for one opaque sandbox subject."""
    subject_id = sandbox.subject_id(real_customer_id)
    with sandbox.session() as session:
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


def _content_manifest(root: Path) -> tuple[tuple[str, str, int], ...]:
    """Bind every regular media file to its relative path, digest, and size."""
    return tuple(
        (
            path.relative_to(root).as_posix(),
            sha256(path.read_bytes()).hexdigest(),
            path.stat().st_size,
        )
        for path in sorted(root.rglob("*"))
        if path.is_file()
    )


def _build_pre_origin_demo(root: Path) -> tuple[Path, tuple[str, ...]]:
    """Create a complete demo, then restore only scene_styles to its 0023 shape."""
    sandbox_root = root / "demo-sandbox"
    sandbox_root.mkdir(mode=0o700, parents=True)
    subject_key_path = sandbox_root / "subject.key"
    subject_key_path.write_bytes(KNOWN_DEMO_SUBJECT_KEY)
    subject_key_path.chmod(0o600)

    initial = LocalDemoSandbox(root)
    try:
        initial.bootstrap(CUSTOMER_A)
    finally:
        initial.close()

    database_path = sandbox_root / "demo.sqlite3"
    engine = create_database_engine(f"sqlite:///{database_path}")
    try:
        with engine.begin() as connection:
            version_origins = tuple(
                connection.execute(
                    text(
                        "SELECT DISTINCT json_extract(content, '$.style_origin') "
                        "FROM style_card_versions "
                        "WHERE json_extract(content, '$.style_origin') IS NOT NULL"
                    )
                ).scalars()
            )
            scene_ids = tuple(
                connection.execute(
                    text("SELECT id FROM scene_styles ORDER BY id")
                ).scalars()
            )
            connection.execute(
                text("DROP TRIGGER IF EXISTS trg_scene_styles_origin_immutable")
            )
            connection.execute(
                text("DROP TRIGGER IF EXISTS trg_scene_styles_origin_valid")
            )
            connection.execute(
                text("DROP INDEX uq_scene_styles_active_customer_origin_name")
            )
            connection.execute(
                text("ALTER TABLE scene_styles DROP COLUMN style_origin")
            )
            connection.execute(
                text(
                    "CREATE UNIQUE INDEX uq_scene_styles_active_customer_name "
                    "ON scene_styles (customer_id, name) WHERE active = 1"
                )
            )
    finally:
        engine.dispose()

    assert version_origins == ("local_demo",)
    return database_path, scene_ids


def _database_manifest(
    database_path: Path,
) -> tuple[tuple[tuple[object, ...], ...], ...]:
    """Capture schema and persisted rows without relying on the current ORM shape."""
    engine = create_database_engine(f"sqlite:///{database_path}")
    try:
        with engine.connect() as connection:
            return (
                tuple(
                    tuple(row)
                    for row in connection.execute(
                        text("PRAGMA table_info(scene_styles)")
                    )
                ),
                tuple(
                    tuple(row)
                    for row in connection.execute(
                        text(
                            "SELECT type, name, tbl_name, sql FROM sqlite_master "
                            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
                        )
                    )
                ),
                tuple(
                    tuple(row)
                    for row in connection.execute(
                        text("SELECT * FROM scene_styles ORDER BY id")
                    )
                ),
                tuple(
                    tuple(row)
                    for row in connection.execute(
                        text("SELECT * FROM style_card_versions ORDER BY id")
                    )
                ),
            )
    finally:
        engine.dispose()


def _add_conflicting_origin_version(database_path: Path) -> None:
    """Give one legacy demo scene both local-demo and customer-upload evidence."""
    engine = create_database_engine(f"sqlite:///{database_path}")
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO style_card_versions "
                    "(id, customer_id, style_card_id, version, content, created_at) "
                    "SELECT :id, customer_id, style_card_id, 2, :content, created_at "
                    "FROM scene_styles ORDER BY id LIMIT 1"
                ),
                {
                    "id": str(uuid4()),
                    "content": '{"style_origin":"customer_upload"}',
                },
            )
    finally:
        engine.dispose()


def _jpeg(seed: int) -> bytes:
    stream = BytesIO()
    Image.new("RGB", (32, 32), (seed, 40, 80)).save(stream, format="JPEG")
    return stream.getvalue()


@contextmanager
def _confirmation_database(
    tmp_path: Path,
) -> Iterator[tuple[sessionmaker[Session], UUID]]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'atomic.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    sample_id = uuid4()
    with sessions() as session:
        content = _jpeg(7)
        session.add(Customer(id=CUSTOMER_A, display_name="Atomic test"))
        session.add(
            StyleSample(
                id=sample_id,
                customer_id=CUSTOMER_A,
                photo_id="atomic-sample-v1",
                authorized_at=datetime.now(UTC),
                consent_provenance="local_synthetic_acceptance",
                consent_text_version="local-demo-v1",
                content_sha256=sha256(content).hexdigest(),
                mime_type="image/jpeg",
                width=32,
                height=32,
                asset_store_namespace="atomic-test",
            )
        )
        session.commit()
    try:
        yield sessions, sample_id
    finally:
        engine.dispose()


def _confirmation(sample_id: UUID, key: str) -> HistoricalStyleGroupsConfirmRequest:
    return HistoricalStyleGroupsConfirmRequest.model_validate(
        {
            "confirmation_key": key,
            "confirmed": True,
            "groups": [
                {
                    "name": "原子事务测试风格",
                    "evidence_sample_ids": [sample_id],
                    "tone": ["warm"],
                    "slots": [
                        {
                            "slot_key": "hero",
                            "label": "主图",
                            "rules": {
                                "aspect_ratio": "4:5",
                                "shot_distance": "close",
                                "camera_angle": "front",
                            },
                        }
                    ],
                }
            ],
        }
    )


def create_five_real_styles(
    client: TestClient,
    customer_id: UUID,
    *,
    first_name: str = "真实风格-1",
) -> None:
    """Create five customer-owned styles through the real HTTP boundary."""
    evidence_ids: list[str] = []
    for index in range(5):
        content = _jpeg(30 + index)
        uploaded = client.post(
            "/v1/style-samples/content",
            headers={
                "X-Customer-Id": str(customer_id),
                "X-Photo-Id": f"real-style-{customer_id.hex}-{index}",
                "X-Content-Sha256": sha256(content).hexdigest(),
                "X-Mime-Type": "image/jpeg",
                "X-Consent-Granted": "true",
                "X-Consent-Provenance": "local-explicit-style-upload",
                "X-Consent-Text-Version": "local-style-upload-v1",
            },
            content=content,
        )
        assert uploaded.status_code == 201, uploaded.text
        evidence_ids.append(uploaded.json()["id"])
    confirmed = client.post(
        "/v1/style-cards/scenes/confirm",
        headers={"X-Customer-Id": str(customer_id)},
        json={
            "confirmation_key": f"five-real-styles-{customer_id.hex}",
            "confirmed": True,
            "groups": [
                {
                    "name": first_name if index == 0 else f"真实风格-{index + 1}",
                    "evidence_sample_ids": [sample_id],
                    "slots": [
                        {
                            "slot_key": "hero",
                            "label": "主图",
                            "rules": {
                                "aspect_ratio": "4:5",
                                "shot_distance": "close",
                                "camera_angle": "front",
                            },
                        }
                    ],
                }
                for index, sample_id in enumerate(evidence_ids)
            ],
        },
    )
    assert confirmed.status_code == 201, confirmed.text


def real_style_count(session: Session, customer_id: UUID) -> int:
    """Count customer styles in the real runtime."""
    return (
        session.scalar(
            select(func.count())
            .select_from(SceneStyle)
            .where(SceneStyle.customer_id == customer_id)
        )
        or 0
    )


def demo_style_count(sandbox: LocalDemoSandbox, real_customer_id: UUID) -> int:
    """Count only the opaque subject's styles inside the demo runtime."""
    subject_id = sandbox.subject_id(real_customer_id)
    with sandbox.session() as session:
        return (
            session.scalar(
                select(func.count())
                .select_from(SceneStyle)
                .where(SceneStyle.customer_id == subject_id)
            )
            or 0
        )


@contextmanager
def _isolated_api_client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    name: str,
    raise_server_exceptions: bool = True,
) -> Iterator[tuple[TestClient, sessionmaker[Session]]]:
    """Run one app with physically separate real and demo roots."""
    real_database = tmp_path / f"{name}-real.sqlite3"
    real_media = tmp_path / f"{name}-real-media"
    demo_root = (tmp_path / f"{name}-demo-root").resolve()
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{real_database}")
    monkeypatch.setenv("LOCAL_STYLE_ASSET_ROOT", str(demo_root))
    engine = create_database_engine(f"sqlite:///{real_database}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    real_store = LocalSyntheticAssetStore(real_media)
    app = create_app()
    app.state.classification_bridge_store = real_store

    def session_override() -> Iterator[Session]:
        with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_asset_content_store] = lambda: real_store
    try:
        with TestClient(app, raise_server_exceptions=raise_server_exceptions) as client:
            yield client, sessions
    finally:
        engine.dispose()


def _headers(customer_id: UUID = CUSTOMER_A) -> dict[str, str]:
    return {"X-Customer-Id": str(customer_id)}


def _real_counts(sessions: sessionmaker[Session]) -> dict[str, int]:
    with sessions() as session:
        return table_counts(session)


def _without_real_mutation(sessions: sessionmaker[Session], request: object) -> object:
    before = _real_counts(sessions)
    response = request()
    assert _real_counts(sessions) == before
    return response


def test_bootstrap_changes_only_the_physical_sandbox(tmp_path: Path) -> None:
    """Using the real DB or media root would mutate either before-snapshot."""
    real_database_path = tmp_path / "real.sqlite3"
    real_media_root = tmp_path / "real-media"
    real_engine = create_database_engine(f"sqlite:///{real_database_path}")
    Base.metadata.create_all(real_engine)
    real_sessions = sessionmaker(
        bind=real_engine, expire_on_commit=False, class_=Session
    )
    real_store = LocalSyntheticAssetStore(real_media_root)
    sandbox = LocalDemoSandbox(tmp_path / "sandbox-root")
    try:
        with real_sessions() as real_session:
            before_counts = table_counts(real_session)
            before_media = sorted(real_media_root.rglob("*"))

            summary = sandbox.bootstrap(CUSTOMER_A)

            assert summary == DemoBootstrapSummary(10, 2, 5)
            assert table_counts(real_session) == before_counts
            assert sorted(real_media_root.rglob("*")) == before_media
            assert sandbox.database_path != real_database_path
            assert sandbox.media_root != real_media_root
    finally:
        sandbox.close()
        real_store.close()
        real_engine.dispose()


def test_subject_mapping_is_stable_opaque_and_owner_only(tmp_path: Path) -> None:
    """Changing the key or leaking the real UUID breaks stable private identity."""
    root = tmp_path / "stable-root"
    first = LocalDemoSandbox(root)
    first_subject = first.subject_id(CUSTOMER_A)
    first_preview_secret = first.preview_secret
    first.bootstrap(CUSTOMER_A)
    first.close()

    reopened = LocalDemoSandbox(root)
    try:
        assert reopened.subject_id(CUSTOMER_A) == first_subject
        assert reopened.subject_id(CUSTOMER_B) != first_subject
        assert reopened.preview_secret != first_preview_secret
        assert stat.S_IMODE(reopened.subject_key_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(reopened.sandbox_root.stat().st_mode) == 0o700
        assert str(CUSTOMER_A).encode() not in reopened.database_path.read_bytes()
        assert CUSTOMER_A.hex.encode() not in reopened.database_path.read_bytes()
    finally:
        reopened.close()


def test_bootstrap_is_idempotent_and_valid_after_restart(tmp_path: Path) -> None:
    """A replay must not duplicate rows or silently change the built-in product."""
    root = tmp_path / "replay-root"
    sandbox = LocalDemoSandbox(root)
    try:
        sandbox.bootstrap(CUSTOMER_A)
        first_product = sandbox.ensure_product(CUSTOMER_A)
        sandbox.bootstrap(CUSTOMER_A)

        assert sandbox_counts(sandbox, CUSTOMER_A) == (10, 2, 5)
        assert sandbox.store.read_original(
            sandbox.subject_id(CUSTOMER_A),
            first_product.id,
            first_product.content_sha256,
        )
    finally:
        sandbox.close()

    reopened = LocalDemoSandbox(root)
    try:
        assert reopened.bootstrap(CUSTOMER_A) == DemoBootstrapSummary(10, 2, 5)
        reopened_product = reopened.ensure_product(CUSTOMER_A)
        assert sandbox_counts(reopened, CUSTOMER_A) == (10, 2, 5)
        assert reopened_product.id == first_product.id
        assert reopened_product.content_sha256 == first_product.content_sha256
        assert reopened.store.read_original(
            reopened.subject_id(CUSTOMER_A),
            reopened_product.id,
            reopened_product.content_sha256,
        )
    finally:
        reopened.close()


def test_existing_demo_schema_adds_missing_fidelity_acknowledgement_column(
    tmp_path: Path,
) -> None:
    """A pre-0027 demo database must remain readable after an app restart."""
    root = tmp_path / "pre-yellow-fidelity-root"
    initial = LocalDemoSandbox(root)
    database_path = initial.database_path
    initial.close()

    engine = create_database_engine(f"sqlite:///{database_path}")
    try:
        with engine.begin() as connection:
            trigger_names = tuple(
                connection.execute(
                    text(
                        "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                        "AND tbl_name = 'style_suite_candidate_fidelities'"
                    )
                ).scalars()
            )
            for trigger_name in trigger_names:
                connection.execute(text(f'DROP TRIGGER "{trigger_name}"'))
            connection.execute(
                text(
                    "ALTER TABLE style_suite_candidate_fidelities "
                    "DROP COLUMN warning_acknowledged"
                )
            )
    finally:
        engine.dispose()

    reopened = LocalDemoSandbox(root)
    try:
        with reopened.session() as session:
            assert list(session.scalars(select(StyleSuiteCandidateFidelity))) == []
    finally:
        reopened.close()


def test_old_schema_upgrade_preserves_demo_identity_history_media_and_real_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Dropping the sandbox-only 0024 upgrade would break this old-schema restart."""
    root = tmp_path / "legacy-demo-root"
    database_path, scene_ids_before = _build_pre_origin_demo(root)
    subject_key_path = root / "demo-sandbox" / "subject.key"
    media_root = root / "demo-sandbox" / "media"
    subject_key_before = subject_key_path.read_bytes()
    media_manifest_before = _content_manifest(media_root)

    real_database = tmp_path / "instrumented-real.sqlite3"
    real_engine = create_database_engine(f"sqlite:///{real_database}")
    try:
        with real_engine.begin() as connection:
            connection.execute(text("CREATE TABLE sentinel (value TEXT NOT NULL)"))
            connection.execute(text("INSERT INTO sentinel VALUES ('untouched')"))
    finally:
        real_engine.dispose()
    real_database_before = sha256(real_database.read_bytes()).hexdigest()

    real_connection_attempts: list[str | None] = []
    original_engine_factory = local_demo_sandbox_module.create_database_engine

    def isolated_engine_factory(database_url: str | None = None) -> Engine:
        if database_url is None or database_url == f"sqlite:///{real_database}":
            real_connection_attempts.append(database_url)
            raise AssertionError(
                "demo schema upgrade attempted to open the real database"
            )
        return original_engine_factory(database_url)

    monkeypatch.setattr(
        local_demo_sandbox_module,
        "create_database_engine",
        isolated_engine_factory,
    )

    upgraded = LocalDemoSandbox(root)
    try:
        first_summary = upgraded.bootstrap(CUSTOMER_A)
        second_summary = upgraded.bootstrap(CUSTOMER_A)
    finally:
        upgraded.close()

    reopened = LocalDemoSandbox(root)
    try:
        restart_summary = reopened.bootstrap(CUSTOMER_A)
    finally:
        reopened.close()

    inspection_engine = create_database_engine(f"sqlite:///{database_path}")
    try:
        with inspection_engine.connect() as connection:
            scene_style_columns = {
                item["name"] for item in inspect(connection).get_columns("scene_styles")
            }
            migrated_origins = tuple(
                connection.execute(
                    text("SELECT DISTINCT style_origin FROM scene_styles")
                ).scalars()
            )
            scene_ids_after = tuple(
                connection.execute(
                    text("SELECT id FROM scene_styles ORDER BY id")
                ).scalars()
            )
            indexes = {
                item["name"]: item
                for item in inspect(connection).get_indexes("scene_styles")
            }
            triggers = {
                row[0]
                for row in connection.execute(
                    text(
                        "SELECT name FROM sqlite_master "
                        "WHERE type = 'trigger' AND tbl_name = 'scene_styles'"
                    )
                )
            }

        with (
            inspection_engine.begin() as connection,
            pytest.raises(IntegrityError, match="scene_style_origin_invalid"),
        ):
            connection.execute(
                text(
                    "INSERT INTO scene_styles "
                    "(id, customer_id, style_card_id, name, style_origin, "
                    "active, rules, created_at) "
                    "SELECT :id, customer_id, NULL, 'invalid-origin', 'other', "
                    "1, '{}', created_at FROM scene_styles LIMIT 1"
                ),
                {"id": str(uuid4())},
            )

        with (
            inspection_engine.begin() as connection,
            pytest.raises(IntegrityError, match="scene_style_origin_immutable"),
        ):
            connection.execute(
                text(
                    "UPDATE scene_styles SET style_origin = 'customer_upload' "
                    "WHERE id = :id"
                ),
                {"id": scene_ids_before[0]},
            )
    finally:
        inspection_engine.dispose()

    expected_index = indexes["uq_scene_styles_active_customer_origin_name"]
    assert "style_origin" in scene_style_columns
    assert migrated_origins == ("local_demo",)
    assert scene_ids_after == scene_ids_before
    assert expected_index["column_names"] == [
        "customer_id",
        "style_origin",
        "name",
    ]
    assert expected_index["unique"] == 1
    assert "uq_scene_styles_active_customer_name" not in indexes
    assert triggers == {
        "trg_scene_styles_origin_valid",
        "trg_scene_styles_origin_immutable",
    }
    assert subject_key_path.read_bytes() == subject_key_before == KNOWN_DEMO_SUBJECT_KEY
    assert _content_manifest(media_root) == media_manifest_before
    assert first_summary == second_summary == restart_summary
    assert real_connection_attempts == []
    assert sha256(real_database.read_bytes()).hexdigest() == real_database_before


def test_schema_upgrade_operational_failure_uses_stable_unavailable_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A SQLite upgrade outage must keep the existing stable local API error."""

    def unavailable_create_all(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise OperationalError("schema upgrade", {}, RuntimeError("disk unavailable"))

    monkeypatch.setattr(Base.metadata, "create_all", unavailable_create_all)

    with pytest.raises(LocalDemoSandboxUnavailableError) as captured:
        LocalDemoSandbox(tmp_path / "unavailable-upgrade-root")

    assert captured.value.code == "local_demo_database_unavailable"
    assert captured.value.status_code == 503
    assert captured.value.retryable is True


def test_schema_upgrade_conflict_rolls_back_every_ddl_change_on_each_retry(
    tmp_path: Path,
) -> None:
    """A conflict must leave the exact pre-0024 schema retryable, not half-upgraded."""
    root = tmp_path / "conflicting-legacy-root"
    database_path, _ = _build_pre_origin_demo(root)
    _add_conflicting_origin_version(database_path)
    subject_key_path = root / "demo-sandbox" / "subject.key"
    media_root = root / "demo-sandbox" / "media"
    subject_key_before = subject_key_path.read_bytes()
    media_before = _content_manifest(media_root)
    database_before = _database_manifest(database_path)

    for _ in range(2):
        with pytest.raises(
            RuntimeError, match="scene style history has conflicting origins"
        ):
            LocalDemoSandbox(root)

        database_after = _database_manifest(database_path)
        assert database_after == database_before
        assert subject_key_path.read_bytes() == subject_key_before
        assert _content_manifest(media_root) == media_before
        columns = {str(row[1]) for row in database_after[0]}
        schema_names = {str(row[1]) for row in database_after[1]}
        assert "style_origin" not in columns
        assert "uq_scene_styles_active_customer_name" in schema_names
        assert "uq_scene_styles_active_customer_origin_name" not in schema_names
        assert "trg_scene_styles_origin_valid" not in schema_names
        assert "trg_scene_styles_origin_immutable" not in schema_names


def test_schema_upgrade_engine_factory_failure_uses_stable_unavailable_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Opening the isolated SQLite engine is part of the stable unavailable fence."""

    def unavailable_engine(database_url: str | None = None) -> Engine:
        del database_url
        raise OperationalError("connect", {}, RuntimeError("disk unavailable"))

    monkeypatch.setattr(
        local_demo_sandbox_module, "create_database_engine", unavailable_engine
    )

    with pytest.raises(LocalDemoSandboxUnavailableError) as captured:
        LocalDemoSandbox(tmp_path / "unavailable-engine-root")

    assert captured.value.code == "local_demo_database_unavailable"
    assert captured.value.status_code == 503
    assert captured.value.retryable is True


def test_schema_upgrade_engine_factory_programmer_fault_remains_visible(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The availability fence must not relabel an unrelated factory defect."""
    programmer_fault = RuntimeError("programmer_fault")

    def broken_engine(database_url: str | None = None) -> Engine:
        del database_url
        raise programmer_fault

    monkeypatch.setattr(
        local_demo_sandbox_module, "create_database_engine", broken_engine
    )

    with pytest.raises(RuntimeError) as captured:
        LocalDemoSandbox(tmp_path / "programmer-engine-root")

    assert captured.value is programmer_fault


def test_schema_upgrade_rejects_noncanonical_named_origin_guards_without_writes(
    tmp_path: Path,
) -> None:
    """Trigger names and keywords cannot disguise guards that never abort writes."""
    root = tmp_path / "false-guard-root"
    sandbox = LocalDemoSandbox(root)
    try:
        sandbox.bootstrap(CUSTOMER_A)
    finally:
        sandbox.close()
    database_path = root / "demo-sandbox" / "demo.sqlite3"
    engine = create_database_engine(f"sqlite:///{database_path}")
    try:
        with engine.begin() as connection:
            connection.execute(
                text("DROP TRIGGER IF EXISTS trg_scene_styles_origin_valid")
            )
            connection.execute(
                text("DROP TRIGGER IF EXISTS trg_scene_styles_origin_immutable")
            )
            connection.execute(
                text(
                    "CREATE TRIGGER trg_scene_styles_origin_valid "
                    "BEFORE INSERT ON scene_styles "
                    "WHEN 0 AND NEW.style_origin NOT IN "
                    "('customer_upload', 'local_demo') "
                    "BEGIN SELECT 'scene_style_origin_invalid'; END"
                )
            )
            connection.execute(
                text(
                    "CREATE TRIGGER trg_scene_styles_origin_immutable "
                    "BEFORE UPDATE OF style_origin ON scene_styles "
                    "WHEN 0 AND NEW.style_origin IS NOT OLD.style_origin "
                    "BEGIN SELECT 'scene_style_origin_immutable'; END"
                )
            )
    finally:
        engine.dispose()
    database_before = _database_manifest(database_path)

    with pytest.raises(
        RuntimeError, match="invalid local demo scene-style origin guard"
    ):
        LocalDemoSandbox(root)

    assert _database_manifest(database_path) == database_before


def test_concurrent_http_bootstrap_returns_the_same_complete_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Two first-use requests must converge instead of racing to a database error."""
    barrier = Barrier(2)
    guard = Lock()
    missing_receipt_reads = 0
    original_get = Session.get

    def synchronize_first_receipt_reads(
        session: Session, entity: object, ident: object, *args: object, **kwargs: object
    ) -> object:
        nonlocal missing_receipt_reads
        result = original_get(session, entity, ident, *args, **kwargs)
        should_wait = False
        if entity is AuditEvent and result is None:
            with guard:
                if missing_receipt_reads < 2:
                    missing_receipt_reads += 1
                    should_wait = True
        if should_wait:
            try:
                barrier.wait(timeout=1)
            except BrokenBarrierError:
                pass
        return result

    monkeypatch.setattr(Session, "get", synchronize_first_receipt_reads)
    with _isolated_api_client(
        monkeypatch,
        tmp_path,
        name="concurrent-bootstrap",
        raise_server_exceptions=False,
    ) as (client, sessions):
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(
                    client.post,
                    "/local/style-suite/api/demo/bootstrap",
                    headers=_headers(),
                )
                for _ in range(2)
            ]
            responses = [future.result(timeout=10) for future in futures]

        assert [response.status_code for response in responses] == [201, 201]
        assert responses[0].json() == responses[1].json()
        assert responses[0].json()["sample_count"] == 10
        assert all(count == 0 for count in _real_counts(sessions).values())
        assert sandbox_counts(client.app.state.local_demo_sandbox, CUSTOMER_A) == (
            10,
            2,
            5,
        )


@pytest.mark.parametrize(
    "path",
    [
        "/local/style-suite/api/demo/bootstrap",
        "/local/style-suite/api/synthetic-history",
    ],
)
def test_corrupt_bootstrap_receipt_has_stable_http_conflict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, path: str
) -> None:
    with _isolated_api_client(
        monkeypatch,
        tmp_path,
        name=f"corrupt-receipt-{path.rsplit('/', 1)[-1]}",
        raise_server_exceptions=False,
    ) as (client, _):
        assert (
            client.post(
                "/local/style-suite/api/demo/bootstrap", headers=_headers()
            ).status_code
            == 201
        )
        sandbox = client.app.state.local_demo_sandbox
        subject_id = sandbox.subject_id(CUSTOMER_A)
        with sandbox.session() as session:
            receipt = session.scalar(
                select(AuditEvent).where(
                    AuditEvent.customer_id == subject_id,
                    AuditEvent.event_type == "style_card.historical.confirmed",
                )
            )
            assert receipt is not None
            session.execute(
                update(AuditEvent)
                .where(AuditEvent.id == receipt.id)
                .values(payload={**receipt.payload, "fingerprint": "0" * 64})
            )
            session.commit()

        response = client.post(path, headers=_headers())

        assert response.status_code == 409
        assert response.json() == {
            "error": {
                "code": "local_demo_bootstrap_receipt_invalid",
                "retryable": False,
            }
        }


def test_demo_missing_candidate_media_has_stable_unavailable_response(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with _isolated_api_client(
        monkeypatch,
        tmp_path,
        name="missing-demo-media",
        raise_server_exceptions=False,
    ) as (client, _):
        run = client.post(
            "/local/style-suite/api/demo/runs",
            headers=_headers(),
            json={"idempotency_key": "missing-demo-media-v1"},
        ).json()
        state = client.get(
            f"/local/style-suite/api/demo/runs/{run['id']}", headers=_headers()
        ).json()
        sandbox = client.app.state.local_demo_sandbox

        def unavailable(*args: object, **kwargs: object) -> bytes:
            raise AssetContentStoreError("private-path-must-not-leak")

        monkeypatch.setattr(sandbox.store, "read_derivative", unavailable)
        response = client.get(state["review_cards"][0]["preview_url"])

        assert response.status_code == 503
        assert response.json() == {
            "error": {"code": "local_demo_media_unavailable", "retryable": True}
        }
        assert b"private-path-must-not-leak" not in response.content


def test_demo_sqlite_and_store_failures_have_stable_unavailable_codes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with _isolated_api_client(
        monkeypatch,
        tmp_path,
        name="demo-operational-errors",
        raise_server_exceptions=False,
    ) as (client, _):
        sandbox = client.app.state.local_demo_sandbox

        @contextmanager
        def unavailable_session() -> Iterator[Session]:
            raise OperationalError("select 1", {}, RuntimeError("disk unavailable"))
            yield  # pragma: no cover

        monkeypatch.setattr(sandbox, "session", unavailable_session)
        database_response = client.post(
            "/local/style-suite/api/demo/bootstrap", headers=_headers()
        )
        assert database_response.status_code == 503
        assert database_response.json() == {
            "error": {
                "code": "local_demo_database_unavailable",
                "retryable": True,
            }
        }

    with _isolated_api_client(
        monkeypatch,
        tmp_path,
        name="demo-store-error",
        raise_server_exceptions=False,
    ) as (client, _):
        sandbox = client.app.state.local_demo_sandbox
        sandbox.store.close()
        store_response = client.post(
            "/local/style-suite/api/demo/bootstrap", headers=_headers()
        )
        assert store_response.status_code == 503
        assert store_response.json() == {
            "error": {
                "code": "local_demo_storage_unavailable",
                "retryable": True,
            }
        }


def test_unknown_demo_programmer_fault_is_not_relabelled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with _isolated_api_client(monkeypatch, tmp_path, name="programmer-fault") as (
        client,
        _,
    ):
        sandbox = client.app.state.local_demo_sandbox

        def programmer_fault(customer_id: UUID) -> DemoBootstrapSummary:
            del customer_id
            raise RuntimeError("programmer_fault")

        monkeypatch.setattr(sandbox, "bootstrap", programmer_fault)
        with pytest.raises(RuntimeError, match="programmer_fault"):
            client.post("/local/style-suite/api/demo/bootstrap", headers=_headers())


def test_bootstrap_replay_rejects_a_changed_version_receipt(tmp_path: Path) -> None:
    """A fixed receipt ID must not make altered bootstrap evidence trustworthy."""
    sandbox = LocalDemoSandbox(tmp_path / "receipt-root")
    try:
        sandbox.bootstrap(CUSTOMER_A)
        subject_id = sandbox.subject_id(CUSTOMER_A)
        with sandbox.session() as session:
            receipt = session.scalar(
                select(AuditEvent).where(
                    AuditEvent.customer_id == subject_id,
                    AuditEvent.event_type == "style_card.historical.confirmed",
                )
            )
            assert receipt is not None
            changed_payload = dict(receipt.payload)
            changed_payload["fingerprint"] = "0" * 64
            session.execute(
                update(AuditEvent)
                .where(AuditEvent.id == receipt.id)
                .values(payload=changed_payload)
            )
            session.commit()

        with pytest.raises(RuntimeError, match="local_demo_bootstrap_receipt_invalid"):
            sandbox.bootstrap(CUSTOMER_A)
    finally:
        sandbox.close()


def test_bootstrap_replay_rejects_changed_version_references(
    tmp_path: Path,
) -> None:
    """Receipt references must identify the exact persisted scenes and versions."""
    sandbox = LocalDemoSandbox(tmp_path / "version-reference-root")
    try:
        sandbox.bootstrap(CUSTOMER_A)
        subject_id = sandbox.subject_id(CUSTOMER_A)
        with sandbox.session() as session:
            receipt = session.scalar(
                select(AuditEvent).where(
                    AuditEvent.customer_id == subject_id,
                    AuditEvent.event_type == "style_card.historical.confirmed",
                )
            )
            assert receipt is not None
            changed_payload = dict(receipt.payload)
            changed_versions = [dict(item) for item in changed_payload["versions"]]
            changed_versions[0]["version_id"] = str(uuid4())
            changed_payload["versions"] = changed_versions
            session.execute(
                update(AuditEvent)
                .where(AuditEvent.id == receipt.id)
                .values(payload=changed_payload)
            )
            session.commit()

        with pytest.raises(RuntimeError, match="local_demo_bootstrap_receipt_invalid"):
            sandbox.bootstrap(CUSTOMER_A)
    finally:
        sandbox.close()


def test_bootstrap_failure_rolls_back_database_and_media(tmp_path: Path) -> None:
    """A confirmation failure must not leave a half-created demo behind."""
    failing = LocalDemoSandbox(tmp_path / "failure-root")
    try:
        with (
            patch.object(
                HistoricalStyleConfirmationService,
                "confirm",
                side_effect=RuntimeError("boom"),
            ),
            pytest.raises(RuntimeError, match="boom"),
        ):
            failing.bootstrap(CUSTOMER_A)
        assert failing.count_all_rows() == 0
        assert list(failing.media_root.rglob("*.jpg")) == []
        assert list(failing.media_root.rglob("*.bin")) == []
    finally:
        failing.close()


def test_commit_false_visibility_obeys_owner_commit_and_rollback(
    tmp_path: Path,
) -> None:
    """A second session must see only committed historical-style work."""
    with _confirmation_database(tmp_path) as (sessions, sample_id):
        request = _confirmation(sample_id, "atomic-confirmation-v1")
        with sessions() as owner:
            HistoricalStyleConfirmationService(owner).confirm(
                CUSTOMER_A, request, style_origin="local_demo", commit=False
            )
            with sessions() as observer:
                assert (
                    observer.scalar(select(func.count()).select_from(SceneStyle)) == 0
                )
            owner.rollback()
            with sessions() as observer:
                assert (
                    observer.scalar(select(func.count()).select_from(SceneStyle)) == 0
                )

        with sessions() as owner:
            HistoricalStyleConfirmationService(owner).confirm(
                CUSTOMER_A, request, style_origin="local_demo", commit=False
            )
            owner.commit()
        with sessions() as observer:
            assert observer.scalar(select(func.count()).select_from(SceneStyle)) == 1
            assert (
                observer.scalar(select(func.count()).select_from(StyleTemplateSlot))
                == 1
            )


def test_local_app_owns_and_closes_demo_sandbox(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The local app must attach and close its sandbox-owned media descriptor."""
    asset_root = (tmp_path / "app-assets").resolve()
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("LOCAL_STYLE_ASSET_ROOT", str(asset_root))

    app = create_app()
    sandbox = app.state.local_demo_sandbox
    with TestClient(app):
        assert isinstance(sandbox, LocalDemoSandbox)

    with pytest.raises(AssetContentStoreError, match="asset_content_store_closed"):
        _ = sandbox.store.sample_content_count


def test_production_app_does_not_create_a_demo_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production must not expose or initialize the local-only sandbox."""
    monkeypatch.setenv("APP_ENV", "production")

    app = create_app()

    assert getattr(app.state, "local_demo_sandbox", None) is None


def test_local_app_rejects_real_database_collision_with_demo_sandbox(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Canonical path aliases must not let real and demo SQLite share one file."""
    asset_root = (tmp_path / "collision-assets").resolve()
    sandbox_database = asset_root / "demo-sandbox" / "demo.sqlite3"
    aliased_database = sandbox_database.parent / ".." / "demo-sandbox" / "demo.sqlite3"
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("LOCAL_STYLE_ASSET_ROOT", str(asset_root))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{aliased_database}")

    with pytest.raises(
        RuntimeError, match="local_demo_database_conflicts_with_runtime_database"
    ):
        create_app()

    assert not sandbox_database.exists()


def test_local_app_rejects_percent_encoded_uri_database_collision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """SQLite URI decoding must not hide a space-containing collision path."""
    asset_root = (tmp_path / "collision assets").resolve()
    sandbox_database = asset_root / "demo-sandbox" / "demo.sqlite3"
    encoded_database = quote(str(sandbox_database), safe="/")
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("LOCAL_STYLE_ASSET_ROOT", str(asset_root))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///file:{encoded_database}?uri=true")
    created_app = None

    try:
        with pytest.raises(
            RuntimeError, match="local_demo_database_conflicts_with_runtime_database"
        ):
            created_app = create_app()
    finally:
        if created_app is not None:
            created_app.state.classification_bridge_store.close()
            created_app.state.local_demo_sandbox.close()

    assert not sandbox_database.exists()


def test_local_app_rejects_non_uri_literal_file_prefix_collision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without URI mode, a leading file: remains part of the SQLite filename."""
    monkeypatch.chdir(tmp_path)
    asset_root = (tmp_path / "file:literal-assets").resolve()
    sandbox_database = asset_root / "demo-sandbox" / "demo.sqlite3"
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("LOCAL_STYLE_ASSET_ROOT", str(asset_root))
    monkeypatch.setenv(
        "DATABASE_URL",
        "sqlite:///file:literal-assets/demo-sandbox/demo.sqlite3",
    )
    created_app = None

    try:
        with pytest.raises(
            RuntimeError, match="local_demo_database_conflicts_with_runtime_database"
        ):
            created_app = create_app()
    finally:
        if created_app is not None:
            created_app.state.classification_bridge_store.close()
            created_app.state.local_demo_sandbox.close()

    assert not sandbox_database.exists()


@pytest.mark.parametrize(
    "collision_kind",
    ["exact_bridge", "nested_bridge", "ancestor_bridge", "exact_stage", "alias"],
)
def test_local_app_rejects_demo_and_real_media_root_collisions_before_store_creation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    collision_kind: str,
) -> None:
    local_root = (tmp_path / f"local-{collision_kind}").resolve()
    demo_media = local_root / "demo-sandbox" / "media"
    bridge_asset = (tmp_path / f"bridge-assets-{collision_kind}").resolve()
    stage_root = (tmp_path / f"bridge-stage-{collision_kind}").resolve()
    if collision_kind == "exact_bridge":
        bridge_asset = demo_media
    elif collision_kind == "nested_bridge":
        bridge_asset = demo_media / "real-bridge"
    elif collision_kind == "ancestor_bridge":
        bridge_asset = demo_media.parent
    elif collision_kind == "exact_stage":
        stage_root = demo_media
    else:
        bridge_asset.mkdir(mode=0o700)
        (local_root / "demo-sandbox").mkdir(mode=0o700, parents=True)
        demo_media.symlink_to(bridge_asset, target_is_directory=True)

    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'real.sqlite3'}")
    monkeypatch.setenv("LOCAL_STYLE_ASSET_ROOT", str(local_root))
    monkeypatch.setenv("LOCAL_CLASSIFICATION_BRIDGE", "true")
    monkeypatch.setenv("LOCAL_CLASSIFICATION_BRIDGE_NONCE", "test-startup-nonce")
    monkeypatch.setenv("LOCAL_CLASSIFICATION_BRIDGE_STAGE_ROOT", str(stage_root))
    monkeypatch.setenv("LOCAL_CLASSIFICATION_BRIDGE_ASSET_ROOT", str(bridge_asset))

    with pytest.raises(RuntimeError, match="local_runtime_roots_overlap"):
        create_app()

    assert not (local_root / "demo-sandbox" / "demo.sqlite3").exists()


def test_local_app_accepts_disjoint_sibling_runtime_roots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    local_root = (tmp_path / "local-root").resolve()
    bridge_asset = (tmp_path / "bridge-assets").resolve()
    stage_root = (tmp_path / "bridge-stage").resolve()
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'real.sqlite3'}")
    monkeypatch.setenv("LOCAL_STYLE_ASSET_ROOT", str(local_root))
    monkeypatch.setenv("LOCAL_CLASSIFICATION_BRIDGE", "true")
    monkeypatch.setenv("LOCAL_CLASSIFICATION_BRIDGE_NONCE", "test-startup-nonce")
    monkeypatch.setenv("LOCAL_CLASSIFICATION_BRIDGE_STAGE_ROOT", str(stage_root))
    monkeypatch.setenv("LOCAL_CLASSIFICATION_BRIDGE_ASSET_ROOT", str(bridge_asset))

    app = create_app()
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert app.state.local_demo_sandbox.media_root == (
            local_root / "demo-sandbox" / "media"
        )
        assert app.state.classification_bridge_store.store_identity == (
            "local-synthetic-asset-store-v1"
        )


def test_product_permission_is_created_inside_the_bootstrap_commit(
    tmp_path: Path,
) -> None:
    """The server-owned product must be ready without a later permission write."""
    sandbox = LocalDemoSandbox(tmp_path / "product-root")
    try:
        sandbox.ensure_product(CUSTOMER_B)
        subject_id = sandbox.subject_id(CUSTOMER_B)
        with sandbox.session() as session:
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(ProductAsset)
                    .where(ProductAsset.customer_id == subject_id)
                )
                == 1
            )
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(ProductAssetPermissionEvent)
                    .where(ProductAssetPermissionEvent.customer_id == subject_id)
                )
                == 1
            )
    finally:
        sandbox.close()


def test_demo_api_bootstrap_run_and_state_never_mutate_real_data(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Any demo write through the app's real dependencies breaks isolation."""
    with _isolated_api_client(monkeypatch, tmp_path, name="api-isolation") as (
        client,
        sessions,
    ):
        readiness_before = client.get(
            "/v1/style-samples/readiness", headers=_headers()
        ).content
        bootstrap = _without_real_mutation(
            sessions,
            lambda: client.post(
                "/local/style-suite/api/demo/bootstrap", headers=_headers()
            ),
        )
        assert bootstrap.status_code == 201
        assert bootstrap.json() == {
            "sample_count": 10,
            "style_count": 2,
            "slot_count": 5,
            "analysis_adapter": "customer_confirmed_synthetic_history",
            "publishable": False,
            "styles": [
                {
                    "name": "暖光桌面",
                    "slots": [
                        {"slot_key": "hero", "label": "主图"},
                        {"slot_key": "detail", "label": "细节"},
                        {"slot_key": "wear", "label": "佩戴"},
                    ],
                },
                {
                    "name": "清透白底",
                    "slots": [
                        {"slot_key": "hero", "label": "主图"},
                        {"slot_key": "side", "label": "侧面"},
                    ],
                },
            ],
        }

        run = _without_real_mutation(
            sessions,
            lambda: client.post(
                "/local/style-suite/api/demo/runs",
                headers=_headers(),
                json={"idempotency_key": "browser-demo-v1"},
            ),
        )
        assert run.status_code == 201, run.text
        assert run.json()["style_count"] == 1
        assert run.json()["slot_count"] == 1
        assert run.json()["candidate_count"] == 3
        assert run.json()["items"][0]["style_name"] == "暖光桌面"
        assert run.json()["items"][0]["slot_key"] == "hero"

        state = _without_real_mutation(
            sessions,
            lambda: client.get(
                f"/local/style-suite/api/demo/runs/{run.json()['id']}",
                headers=_headers(),
            ),
        )
        assert state.status_code == 200, state.text
        assert len(state.json()["review_cards"]) == 3
        assert {
            (card["style_name"], card["slot_key"])
            for card in state.json()["review_cards"]
        } == {("暖光桌面", "hero")}
        assert (
            client.get("/v1/style-samples/readiness", headers=_headers()).content
            == readiness_before
        )


def test_demo_api_replay_content_security_and_forbidden_inputs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Replay, tenant checks, and the closed request shape protect demo access."""
    with _isolated_api_client(monkeypatch, tmp_path, name="api-security") as (
        client,
        sessions,
    ):
        readiness_before = client.get(
            "/v1/style-samples/readiness", headers=_headers()
        ).content
        first = _without_real_mutation(
            sessions,
            lambda: client.post(
                "/local/style-suite/api/demo/runs",
                headers=_headers(),
                json={"idempotency_key": "browser-demo-v1"},
            ),
        )
        assert first.status_code == 201, first.text
        replay = _without_real_mutation(
            sessions,
            lambda: client.post(
                "/local/style-suite/api/demo/runs",
                headers=_headers(),
                json={"idempotency_key": "browser-demo-v1"},
            ),
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["id"] == first.json()["id"]

        state = _without_real_mutation(
            sessions,
            lambda: client.get(
                f"/local/style-suite/api/demo/runs/{first.json()['id']}",
                headers=_headers(),
            ),
        )
        assert state.status_code == 200, state.text
        preview_url = state.json()["review_cards"][0]["preview_url"]
        image = _without_real_mutation(
            sessions,
            lambda: client.get(preview_url, headers=_headers()),
        )
        assert image.status_code == 200, image.text
        assert image.headers["x-synthetic-artifact"] == "true"
        assert image.headers["cache-control"] == "private, no-store"
        assert image.content

        foreign_state = _without_real_mutation(
            sessions,
            lambda: client.get(
                f"/local/style-suite/api/demo/runs/{first.json()['id']}",
                headers=_headers(CUSTOMER_B),
            ),
        )
        assert foreign_state.status_code == 404
        foreign_content = _without_real_mutation(
            sessions,
            lambda: client.get(preview_url, headers=_headers(CUSTOMER_B)),
        )
        assert foreign_content.status_code == 404

        for payload in (
            {"idempotency_key": "x", "model": "gpt-image-2"},
            {"idempotency_key": "x", "confirmed_max_usd": "3.00"},
        ):
            denied = _without_real_mutation(
                sessions,
                lambda payload=payload: client.post(
                    "/local/style-suite/api/demo/runs",
                    headers=_headers(),
                    json=payload,
                ),
            )
            assert denied.status_code == 422
        assert (
            client.get("/v1/style-samples/readiness", headers=_headers()).content
            == readiness_before
        )


def test_demo_bootstrap_is_order_independent_from_real_styles(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Real-first and demo-first orders must preserve independent style sets."""
    with _isolated_api_client(monkeypatch, tmp_path, name="real-first") as (
        real_client,
        real_sessions,
    ):
        create_five_real_styles(real_client, CUSTOMER_A)
        readiness_before = real_client.get(
            "/v1/style-samples/readiness", headers=_headers()
        ).content
        prepared = _without_real_mutation(
            real_sessions,
            lambda: real_client.post(
                "/local/style-suite/api/demo/bootstrap", headers=_headers()
            ),
        )
        assert prepared.status_code == 201, prepared.text
        with real_sessions() as real_session:
            assert real_style_count(real_session, CUSTOMER_A) == 5
        assert (
            demo_style_count(real_client.app.state.local_demo_sandbox, CUSTOMER_A) == 2
        )
        assert (
            real_client.get("/v1/style-samples/readiness", headers=_headers()).content
            == readiness_before
        )

    with _isolated_api_client(monkeypatch, tmp_path, name="demo-first") as (
        second_client,
        second_sessions,
    ):
        readiness_before = second_client.get(
            "/v1/style-samples/readiness", headers=_headers(CUSTOMER_B)
        ).content
        prepared = _without_real_mutation(
            second_sessions,
            lambda: second_client.post(
                "/local/style-suite/api/demo/bootstrap",
                headers=_headers(CUSTOMER_B),
            ),
        )
        assert prepared.status_code == 201, prepared.text
        assert (
            second_client.get(
                "/v1/style-samples/readiness", headers=_headers(CUSTOMER_B)
            ).content
            == readiness_before
        )
        create_five_real_styles(second_client, CUSTOMER_B, first_name="暖光桌面")
        with second_sessions() as second_real_session:
            assert real_style_count(second_real_session, CUSTOMER_B) == 5
        assert (
            demo_style_count(second_client.app.state.local_demo_sandbox, CUSTOMER_B)
            == 2
        )


def test_synthetic_history_is_a_sandbox_only_compatibility_proxy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The old bootstrap URL must not retain a real session or store side effect."""
    with _isolated_api_client(monkeypatch, tmp_path, name="compatibility") as (
        client,
        sessions,
    ):
        readiness_before = client.get(
            "/v1/style-samples/readiness", headers=_headers()
        ).content
        response = _without_real_mutation(
            sessions,
            lambda: client.post(
                "/local/style-suite/api/synthetic-history", headers=_headers()
            ),
        )

        assert response.status_code == 201, response.text
        assert response.json()["style_count"] == 2
        assert demo_style_count(client.app.state.local_demo_sandbox, CUSTOMER_A) == 2
        assert (
            client.get("/v1/style-samples/readiness", headers=_headers()).content
            == readiness_before
        )


def test_production_hides_all_demo_and_compatibility_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production must not mount any local demo bootstrap, run, or content route."""
    monkeypatch.setenv("APP_ENV", "production")
    run_id = uuid4()
    candidate_id = uuid4()
    with TestClient(create_app()) as client:
        responses = (
            client.post("/local/style-suite/api/demo/bootstrap", headers=_headers()),
            client.post(
                "/local/style-suite/api/demo/runs",
                headers=_headers(),
                json={"idempotency_key": "production-denied"},
            ),
            client.get(
                f"/local/style-suite/api/demo/runs/{run_id}", headers=_headers()
            ),
            client.get(
                "/local/style-suite/api/demo/runs/"
                f"{run_id}/candidates/{candidate_id}/content",
                headers=_headers(),
            ),
            client.post("/local/style-suite/api/synthetic-history", headers=_headers()),
        )
    assert [response.status_code for response in responses] == [404] * 5
