"""Consumer-owned, local-only staging for verified classification media bytes."""

from __future__ import annotations

import fcntl
import hashlib
import io
import ipaddress
import os
import re
import sqlite3
import stat
import warnings
import zlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import RLock
from typing import BinaryIO
from uuid import UUID, uuid4

from PIL import Image, UnidentifiedImageError

from opc_style.domain.errors import DomainRuleError
from opc_style.settings import LocalBridgeSettings

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MIMES = {"JPEG": "image/jpeg", "PNG": "image/png", "HEIC": "image/heic"}
ACTIVE = "active"
PENDING = "pending_publish"
CONSUMED = "consumed"
EXPIRED = "expired"
FAILED = "failed"
CLEANUP = "cleanup_pending"
MANIFEST_VERSION = 3

type FileIdentity = tuple[int, int]


@dataclass(frozen=True)
class MediaReceipt:
    """Facts the producer must echo when referencing locally staged bytes."""

    receipt_id: UUID
    photo_id: UUID
    content_sha256: str
    mime_type: str
    width: int
    height: int
    expires_at: datetime


@dataclass
class _Publication:
    """Process-local proof of every inode created by one stage call."""

    photo_id: UUID
    pending_name: str
    pending_identity: FileIdentity
    target_name: str
    target_identity: FileIdentity | None = None


@dataclass(frozen=True)
class _Journal:
    """Durable ownership facts used by verification and exact recovery."""

    storage_identity: FileIdentity | None
    pending_owner: str | None
    pending_expires_at: datetime | None
    pending_name: str | None
    pending_identity: FileIdentity | None


class LocalMediaStage:
    """Private byte staging with a durable receipt journal; no paths enter."""

    def __init__(
        self,
        *,
        settings: LocalBridgeSettings,
        caller_host: str,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._caller_host = caller_host
        self._now = now or (lambda: datetime.now(UTC))
        self._root = settings.local_classification_bridge_stage_root.absolute()
        self._connection: sqlite3.Connection | None = None
        self._closed = False
        self._lock = RLock()
        self._root_fd = -1
        self._media_fd = -1
        self._lock_fd = -1
        self._orchestration_lock_fd = -1
        self._database_fd = -1
        self._root_identity: FileIdentity | None = None
        self._media_identity: FileIdentity | None = None
        self._lock_identity: FileIdentity | None = None
        self._orchestration_lock_identity: FileIdentity | None = None
        self._database_identity: FileIdentity | None = None
        self._owner_token = uuid4().hex
        self._publications: dict[UUID, _Publication] = {}

    def stage(
        self, *, photo_id: UUID, claimed_sha256: str, body: BinaryIO
    ) -> MediaReceipt:
        """Verify and store bytes, returning one short-lived opaque receipt."""
        with self._operation():
            if not isinstance(photo_id, UUID):
                raise DomainRuleError("local_media_photo_id_invalid")
            self._require_hash(claimed_sha256)
            content = self._read(body)
            digest = hashlib.sha256(content).hexdigest()
            if digest != claimed_sha256:
                raise DomainRuleError("local_media_hash_mismatch")
            mime_type, width, height = self._decode(content)
            now = self._now_utc()
            self._begin()
            if self._has_unsettled_receipt(photo_id):
                self._rollback()
                raise DomainRuleError("local_media_receipt_pending")
            if self._has_live_consumed_receipt(photo_id, now):
                self._rollback()
                raise DomainRuleError("local_media_receipt_conflict")
            active = self._active(photo_id)
            if active is not None and active.expires_at > now:
                expected = (digest, mime_type, width, height)
                actual = (
                    active.content_sha256,
                    active.mime_type,
                    active.width,
                    active.height,
                )
                self._rollback()
                if actual != expected:
                    raise DomainRuleError("local_media_receipt_conflict")
                self._verify(active)
                return active
            if active is not None:
                self._set_status(active.receipt_id, EXPIRED, commit=False)
            receipt = MediaReceipt(
                receipt_id=uuid4(),
                photo_id=photo_id,
                content_sha256=digest,
                mime_type=mime_type,
                width=width,
                height=height,
                expires_at=now
                + timedelta(
                    seconds=(
                        self._settings.local_classification_bridge_receipt_ttl_seconds
                    )
                ),
            )
            pending_name = f".pending-{receipt.receipt_id}-{uuid4().hex}"
            try:
                self._insert_pending(receipt, pending_name)
                self._commit()
            except sqlite3.Error as error:
                self._rollback_if_needed()
                raise DomainRuleError("local_media_receipt_record_failed") from error

            try:
                self._publish(receipt, content)
            except DomainRuleError:
                self._rollback_publish(receipt)
                raise
            except OSError as error:
                self._rollback_publish(receipt)
                raise DomainRuleError("local_media_write_failed") from error

            try:
                self._activate_receipt(receipt)
            except DomainRuleError:
                self._rollback_publish(receipt)
                raise
            except (OSError, sqlite3.Error) as error:
                self._rollback_publish(receipt)
                raise DomainRuleError("local_media_receipt_record_failed") from error
            self._publications.pop(receipt.receipt_id, None)
            return receipt

    def resolve(self, *, receipt_id: UUID, photo_id: UUID, sha256: str) -> Path:
        """Resolve only an active receipt whose echoed facts and bytes still match."""
        with self._operation():
            self._require_hash(sha256)
            self._begin()
            receipt, status_value, _ = self._load(receipt_id)
            if receipt.photo_id != photo_id or receipt.content_sha256 != sha256:
                raise DomainRuleError("local_media_receipt_mismatch")
            if status_value != ACTIVE:
                raise DomainRuleError("local_media_receipt_unavailable")
            if receipt.expires_at <= self._now_utc():
                self._set_status(receipt_id, EXPIRED, commit=False)
                self._commit()
                raise DomainRuleError("local_media_receipt_expired")
            self._verify(receipt)
            self._rollback()
            return self._path(receipt)

    def consume(self, *, receipt_id: UUID, handoff_id: UUID) -> Path:
        """Bind a verified receipt to one handoff across every stage instance."""
        with self._operation():
            if not isinstance(handoff_id, UUID):
                raise DomainRuleError("local_media_handoff_id_invalid")
            self._begin()
            receipt, status_value, bound_handoff = self._load(receipt_id)
            if receipt.expires_at <= self._now_utc():
                self._set_status(receipt_id, EXPIRED, commit=False)
                self._commit()
                raise DomainRuleError("local_media_receipt_expired")
            if status_value == CONSUMED:
                if bound_handoff != handoff_id:
                    raise DomainRuleError("local_media_receipt_replayed")
                self._verify(receipt)
                self._rollback()
                return self._path(receipt)
            if status_value != ACTIVE:
                raise DomainRuleError("local_media_receipt_unavailable")

            self._verify(receipt)
            _after_media_verification_before_consume(self._root)
            self._verify(receipt)
            changed = (
                self._db()
                .execute(
                    """
                UPDATE media_receipts
                   SET status=?, handoff_id=?
                 WHERE receipt_id=? AND status=? AND expires_at>?
                """,
                    (
                        CONSUMED,
                        str(handoff_id),
                        str(receipt_id),
                        ACTIVE,
                        self._now_utc().isoformat(),
                    ),
                )
                .rowcount
            )
            if changed != 1:
                raise DomainRuleError("local_media_receipt_unavailable")
            self._commit()
            return self._path(receipt)

    def consume_batch(
        self,
        *,
        receipts: tuple[tuple[UUID, UUID, str], ...],
        handoff_id: UUID,
    ) -> tuple[Path, ...]:
        """Atomically bind every attested receipt, or bind none of them.

        The consumer uses this as the handoff's single receipt boundary.  All
        byte/hash checks occur before any SQLite row is updated, so a malformed
        later receipt cannot leave an earlier one stranded on a half-handoff.
        """
        if not receipts or len({receipt_id for receipt_id, _, _ in receipts}) != len(
            receipts
        ):
            raise DomainRuleError("local_media_receipt_batch_invalid")
        with self._operation():
            if not isinstance(handoff_id, UUID):
                raise DomainRuleError("local_media_handoff_id_invalid")
            self._begin()
            verified: list[MediaReceipt] = []
            active_receipt_ids: list[UUID] = []
            for receipt_id, photo_id, sha256 in receipts:
                self._require_hash(sha256)
                receipt, status_value, bound_handoff = self._load(receipt_id)
                if receipt.photo_id != photo_id or receipt.content_sha256 != sha256:
                    raise DomainRuleError("local_media_receipt_mismatch")
                if status_value == CONSUMED:
                    if bound_handoff != handoff_id:
                        raise DomainRuleError("local_media_receipt_replayed")
                    if (
                        receipt.expires_at <= self._now_utc()
                        and not self._is_pinned_for_handoff(receipt_id, handoff_id)
                    ):
                        self._set_status(receipt_id, EXPIRED, commit=False)
                        self._commit()
                        raise DomainRuleError("local_media_receipt_expired")
                elif status_value == ACTIVE:
                    if receipt.expires_at <= self._now_utc():
                        self._set_status(receipt_id, EXPIRED, commit=False)
                        self._commit()
                        raise DomainRuleError("local_media_receipt_expired")
                    active_receipt_ids.append(receipt_id)
                else:
                    raise DomainRuleError("local_media_receipt_unavailable")
                self._verify(receipt)
                verified.append(receipt)
            _after_media_verification_before_consume(self._root)
            for receipt in verified:
                self._verify(receipt)
            # Recapture the clock immediately before the conditional write.  A
            # verification hook may take long enough for a receipt to expire.
            consume_now = self._now_utc()
            for receipt_id in active_receipt_ids:
                changed = (
                    self._db()
                    .execute(
                        """
                    UPDATE media_receipts
                       SET status=?, handoff_id=?
                     WHERE receipt_id=? AND status=? AND expires_at>?
                    """,
                        (
                            CONSUMED,
                            str(handoff_id),
                            str(receipt_id),
                            ACTIVE,
                            consume_now.isoformat(),
                        ),
                    )
                    .rowcount
                )
                if changed != 1:
                    raise DomainRuleError("local_media_receipt_unavailable")
            for receipt in verified:
                self._db().execute(
                    """
                    INSERT INTO handoff_receipt_pins (receipt_id, handoff_id, state)
                    VALUES (?, ?, 'active')
                    ON CONFLICT(receipt_id) DO UPDATE SET
                        handoff_id=excluded.handoff_id, state='active'
                    """,
                    (str(receipt.receipt_id), str(handoff_id)),
                )
            self._commit()
            return tuple(self._path(receipt) for receipt in verified)

    def release_handoff_receipts(self, *, handoff_id: UUID) -> None:
        """Allow terminal handoffs to return to ordinary expiry cleanup."""
        with self._operation():
            self._begin()
            self._db().execute(
                "DELETE FROM handoff_receipt_pins WHERE handoff_id=?",
                (str(handoff_id),),
            )
            self._commit()

    def resolve_for_handoff(
        self,
        *,
        receipt_id: UUID,
        photo_id: UUID,
        sha256: str,
        handoff_id: UUID,
    ) -> Path:
        """Re-attest an active or same-handoff consumed receipt for safe resume."""
        with self._operation():
            if not isinstance(handoff_id, UUID):
                raise DomainRuleError("local_media_handoff_id_invalid")
            self._require_hash(sha256)
            self._begin()
            receipt, status_value, bound_handoff = self._load(receipt_id)
            if receipt.photo_id != photo_id or receipt.content_sha256 != sha256:
                raise DomainRuleError("local_media_receipt_mismatch")
            pinned = self._is_pinned_for_handoff(receipt_id, handoff_id)
            if receipt.expires_at <= self._now_utc() and not pinned:
                self._set_status(receipt_id, EXPIRED, commit=False)
                self._commit()
                raise DomainRuleError("local_media_receipt_expired")
            if status_value == CONSUMED and bound_handoff != handoff_id:
                raise DomainRuleError("local_media_receipt_replayed")
            if status_value not in {ACTIVE, CONSUMED}:
                raise DomainRuleError("local_media_receipt_unavailable")
            self._verify(receipt)
            self._rollback()
            return self._path(receipt)

    def cleanup_expired(self, *, now: datetime) -> tuple[str, ...]:
        """Delete expired bytes and report exact receipt IDs needing retry."""
        with self._operation():
            cutoff = self._as_utc(now)
            self._begin()
            rows = (
                self._db()
                .execute(
                    """
                SELECT receipt_id
                  FROM media_receipts
                 WHERE (status=? AND expires_at<=?)
                    OR (status=? AND expires_at<=? AND NOT EXISTS (
                        SELECT 1 FROM handoff_receipt_pins pin
                         WHERE pin.receipt_id=media_receipts.receipt_id
                           AND pin.state='active'
                    ))
                    OR status=?
                    OR (
                        status IN (?, ?)
                        AND (
                            pending_expires_at IS NULL
                            OR pending_expires_at<=?
                        )
                    )
                 ORDER BY receipt_id
                """,
                    (
                        ACTIVE,
                        cutoff.isoformat(),
                        CONSUMED,
                        cutoff.isoformat(),
                        EXPIRED,
                        PENDING,
                        CLEANUP,
                        cutoff.isoformat(),
                    ),
                )
                .fetchall()
            )
            receipt_ids = tuple(_manifest_uuid(row[0]) for row in rows)
            self._rollback()

            pending: list[str] = []
            for receipt_id in receipt_ids:
                receipt, _, _ = self._load(receipt_id)
                try:
                    cleaned = self._cleanup_receipt_files(receipt)
                except (DomainRuleError, OSError):
                    cleaned = False
                self._record_cleanup_result(receipt_id, cleaned)
                if not cleaned:
                    pending.append(str(receipt_id))
            return tuple(pending)

    def close(self) -> None:
        """Release the database and every retained capability idempotently."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._connection is not None:
                self._connection.close()
            self._connection = None
            for attribute in (
                "_database_fd", "_media_fd", "_lock_fd", "_orchestration_lock_fd", "_root_fd"
            ):
                descriptor = getattr(self, attribute)
                _close_fd(descriptor)
                setattr(self, attribute, -1)

    @contextmanager
    def orchestration_lock(self) -> Iterator[None]:
        """Serialize one local consumer handoff across all stage instances."""
        with self._lock:
            self._authorize()
            if self._root_fd < 0:
                self._bootstrap()
            try:
                fcntl.flock(self._orchestration_lock_fd, fcntl.LOCK_EX)
                self._assert_namespace_identity()
                yield
            finally:
                try:
                    fcntl.flock(self._orchestration_lock_fd, fcntl.LOCK_UN)
                except OSError:
                    pass

    @contextmanager
    def _operation(self) -> Iterator[None]:
        """Hold one pinned namespace lock across validation and all mutations."""
        with self._lock:
            self._authorize()
            if self._root_fd < 0:
                self._bootstrap()
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_EX)
                self._assert_namespace_identity()
                yield
            except BaseException:
                self._rollback_if_needed()
                raise
            finally:
                try:
                    fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                except OSError:
                    pass

    def _authorize(self) -> None:
        if self._closed:
            raise DomainRuleError("local_media_stage_closed")
        if not self._settings.local_classification_bridge:
            raise DomainRuleError("local_classification_bridge_disabled")
        try:
            loopback = ipaddress.ip_address(self._caller_host).is_loopback
        except ValueError:
            loopback = self._caller_host == "localhost"
        if not loopback:
            raise DomainRuleError("local_classification_bridge_loopback_required")

    def _bootstrap(self) -> None:
        """Pin root/lock first, then initialize and recover under the file lock."""
        try:
            self._safe_ancestors(self._root)
            self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._root_fd = self._open_dir(self._root)
            os.fchmod(self._root_fd, 0o700)
            self._root_identity = _identity_from_fd(self._root_fd)
            self._lock_fd = self._open_lock(self._root_fd)
            _require_unique_regular_fd(self._lock_fd)
            os.fchmod(self._lock_fd, 0o600)
            self._lock_identity = _identity_from_fd(self._lock_fd)
            self._orchestration_lock_fd = self._open_named_lock(
                self._root_fd, ".classification-orchestration.lock"
            )
            _require_unique_regular_fd(self._orchestration_lock_fd)
            os.fchmod(self._orchestration_lock_fd, 0o600)
            self._orchestration_lock_identity = _identity_from_fd(
                self._orchestration_lock_fd
            )
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX)
            self._open_namespace_resources()
            self._initialize_manifest()
            self._recover_pending()
            self._assert_namespace_identity()
        except DomainRuleError:
            self._close_failed_bootstrap()
            raise
        except (OSError, sqlite3.Error) as error:
            self._close_failed_bootstrap()
            raise DomainRuleError("local_media_storage_unsafe") from error
        finally:
            if self._lock_fd >= 0:
                try:
                    fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                except OSError:
                    pass

    def _open_namespace_resources(self) -> None:
        try:
            os.mkdir("media", 0o700, dir_fd=self._root_fd)
        except FileExistsError:
            pass
        self._media_fd = self._open_at(self._root_fd, "media")
        os.fchmod(self._media_fd, 0o700)
        self._media_identity = _identity_from_fd(self._media_fd)

        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        self._database_fd = os.open(
            "receipts.sqlite3", flags, 0o600, dir_fd=self._root_fd
        )
        _require_unique_regular_fd(self._database_fd)
        os.fchmod(self._database_fd, 0o600)
        self._database_identity = _identity_from_fd(self._database_fd)
        self._assert_namespace_identity(require_connection=False)

        database_uri = (
            f"{(self._root / 'receipts.sqlite3').as_uri()}?mode=rw&nofollow=1"
        )
        self._connection = sqlite3.connect(
            database_uri,
            uri=True,
            timeout=5,
            check_same_thread=False,
        )
        self._assert_namespace_identity()

    def _initialize_manifest(self) -> None:
        manifest_version = int(self._db().execute("PRAGMA user_version").fetchone()[0])
        if manifest_version > MANIFEST_VERSION:
            raise DomainRuleError("local_media_manifest_version_unsupported")

        table_exists = (
            self._db()
            .execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='media_receipts'"
            )
            .fetchone()
        )
        if table_exists is None:
            self._begin()
            self._create_manifest_table("media_receipts")
            self._finish_manifest_schema()
            self._commit()
            return

        columns = {
            str(row[1])
            for row in self._db().execute("PRAGMA table_info(media_receipts)")
        }
        journal_columns = {
            "storage_dev",
            "storage_ino",
            "pending_owner",
            "pending_expires_at",
            "pending_name",
            "pending_dev",
            "pending_ino",
        }
        self._begin()
        if "storage_relpath" in columns or not journal_columns <= columns:
            self._db().execute("DROP TABLE IF EXISTS media_receipts_v3")
            self._create_manifest_table("media_receipts_v3")
            journal_select = [
                name if name in columns else "NULL"
                for name in (
                    "storage_dev",
                    "storage_ino",
                    "pending_owner",
                    "pending_expires_at",
                    "pending_name",
                    "pending_dev",
                    "pending_ino",
                )
            ]
            self._db().execute(
                f"""
                INSERT INTO media_receipts_v3 (
                    receipt_id, photo_id, content_sha256, mime_type, width,
                    height, expires_at, status, handoff_id, storage_dev,
                    storage_ino, pending_owner, pending_expires_at,
                    pending_name, pending_dev, pending_ino
                )
                SELECT receipt_id, photo_id, content_sha256, mime_type, width,
                       height, expires_at, status, handoff_id,
                       {", ".join(journal_select)}
                  FROM media_receipts
                """
            )
            self._db().execute("DROP TABLE media_receipts")
            self._db().execute("ALTER TABLE media_receipts_v3 RENAME TO media_receipts")
        self._backfill_storage_identities()
        self._finish_manifest_schema()
        self._commit()

    def _create_manifest_table(self, table_name: str) -> None:
        if table_name not in {"media_receipts", "media_receipts_v3"}:
            raise DomainRuleError("local_media_manifest_invalid")
        self._db().execute(
            f"""
            CREATE TABLE {table_name} (
                receipt_id TEXT PRIMARY KEY,
                photo_id TEXT NOT NULL,
                content_sha256 TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                width INTEGER NOT NULL,
                height INTEGER NOT NULL,
                expires_at TEXT NOT NULL,
                status TEXT NOT NULL,
                handoff_id TEXT,
                storage_dev INTEGER,
                storage_ino INTEGER,
                pending_owner TEXT,
                pending_expires_at TEXT,
                pending_name TEXT,
                pending_dev INTEGER,
                pending_ino INTEGER
            )
            """
        )

    def _finish_manifest_schema(self) -> None:
        self._db().execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_active_photo
                ON media_receipts(photo_id) WHERE status='active'
            """
        )
        self._db().execute(f"PRAGMA user_version={MANIFEST_VERSION}")
        self._db().execute(
            """
            CREATE TABLE IF NOT EXISTS handoff_receipt_pins (
                receipt_id TEXT PRIMARY KEY,
                handoff_id TEXT NOT NULL,
                state TEXT NOT NULL
            )
            """
        )

    def _is_pinned_for_handoff(self, receipt_id: UUID, handoff_id: UUID) -> bool:
        return (
            self._db()
            .execute(
                """SELECT 1 FROM handoff_receipt_pins
                   WHERE receipt_id=? AND handoff_id=? AND state='active'""",
                (str(receipt_id), str(handoff_id)),
            )
            .fetchone()
            is not None
        )

    def _backfill_storage_identities(self) -> None:
        rows = (
            self._db()
            .execute(
                """
            SELECT receipt_id, photo_id, content_sha256, mime_type, width,
                   height, expires_at, status, storage_dev, storage_ino
              FROM media_receipts
            """
            )
            .fetchall()
        )
        for row in rows:
            receipt = self._receipt(row[:7])
            status_value = str(row[7])
            stored_identity = _optional_identity(row[8], row[9])
            if status_value not in {ACTIVE, CONSUMED}:
                continue
            actual_identity = self._verified_storage_identity(
                receipt,
                expected_identity=stored_identity,
            )
            self._db().execute(
                """
                UPDATE media_receipts SET storage_dev=?, storage_ino=?
                 WHERE receipt_id=?
                """,
                (*actual_identity, str(receipt.receipt_id)),
            )

    def _close_failed_bootstrap(self) -> None:
        if self._connection is not None:
            self._connection.close()
        self._connection = None
        for attribute in (
            "_database_fd", "_media_fd", "_lock_fd", "_orchestration_lock_fd", "_root_fd"
        ):
            descriptor = getattr(self, attribute)
            _close_fd(descriptor)
            setattr(self, attribute, -1)

    @staticmethod
    def _safe_ancestors(path: Path) -> None:
        for ancestor in reversed((path, *path.parents)):
            if ancestor.exists() and ancestor.is_symlink():
                raise DomainRuleError("local_media_storage_unsafe")

    @staticmethod
    def _open_dir(path: Path) -> int:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        return os.open(path, flags)

    @staticmethod
    def _open_at(parent_fd: int, name: str) -> int:
        _require_entry_name(name)
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        return os.open(name, flags, dir_fd=parent_fd)

    @staticmethod
    def _open_lock(root_fd: int) -> int:
        return LocalMediaStage._open_named_lock(root_fd, ".namespace.lock")

    @staticmethod
    def _open_named_lock(root_fd: int, name: str) -> int:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        for _ in range(100):
            try:
                return os.open(name, flags, 0o600, dir_fd=root_fd)
            except FileNotFoundError:
                continue
        raise DomainRuleError("local_media_storage_unsafe")

    def _assert_namespace_identity(self, *, require_connection: bool = True) -> None:
        try:
            root_stat = self._root.lstat()
            lock_stat = os.stat(
                ".namespace.lock", dir_fd=self._root_fd, follow_symlinks=False
            )
            orchestration_lock_stat = os.stat(
                ".classification-orchestration.lock",
                dir_fd=self._root_fd,
                follow_symlinks=False,
            )
            media_stat = os.stat("media", dir_fd=self._root_fd, follow_symlinks=False)
            database_stat = os.stat(
                "receipts.sqlite3", dir_fd=self._root_fd, follow_symlinks=False
            )
            if (
                stat.S_ISLNK(root_stat.st_mode)
                or not stat.S_ISDIR(root_stat.st_mode)
                or _identity(root_stat) != self._root_identity
                or _identity_from_fd(self._root_fd) != self._root_identity
                or stat.S_IMODE(root_stat.st_mode) != 0o700
                or stat.S_ISLNK(lock_stat.st_mode)
                or not stat.S_ISREG(lock_stat.st_mode)
                or _identity(lock_stat) != self._lock_identity
                or _identity_from_fd(self._lock_fd) != self._lock_identity
                or stat.S_IMODE(lock_stat.st_mode) != 0o600
                or lock_stat.st_nlink != 1
                or stat.S_ISLNK(orchestration_lock_stat.st_mode)
                or not stat.S_ISREG(orchestration_lock_stat.st_mode)
                or _identity(orchestration_lock_stat)
                != self._orchestration_lock_identity
                or _identity_from_fd(self._orchestration_lock_fd)
                != self._orchestration_lock_identity
                or stat.S_IMODE(orchestration_lock_stat.st_mode) != 0o600
                or orchestration_lock_stat.st_nlink != 1
                or stat.S_ISLNK(media_stat.st_mode)
                or not stat.S_ISDIR(media_stat.st_mode)
                or _identity(media_stat) != self._media_identity
                or _identity_from_fd(self._media_fd) != self._media_identity
                or stat.S_IMODE(media_stat.st_mode) != 0o700
                or stat.S_ISLNK(database_stat.st_mode)
                or not stat.S_ISREG(database_stat.st_mode)
                or _identity(database_stat) != self._database_identity
                or _identity_from_fd(self._database_fd) != self._database_identity
                or stat.S_IMODE(database_stat.st_mode) != 0o600
                or database_stat.st_nlink != 1
                or (require_connection and self._connection is None)
            ):
                raise DomainRuleError("local_media_storage_unsafe")
        except DomainRuleError:
            raise
        except OSError as error:
            raise DomainRuleError("local_media_storage_unsafe") from error

    def _photo_fd(self, receipt: MediaReceipt, *, create: bool) -> int:
        name = str(receipt.photo_id)
        _require_entry_name(name)
        if create:
            try:
                os.mkdir(name, 0o700, dir_fd=self._media_fd)
            except FileExistsError:
                pass
        descriptor = -1
        try:
            descriptor = self._open_at(self._media_fd, name)
            os.fchmod(descriptor, 0o700)
            entry = os.stat(name, dir_fd=self._media_fd, follow_symlinks=False)
            if (
                stat.S_ISLNK(entry.st_mode)
                or not stat.S_ISDIR(entry.st_mode)
                or _identity(entry) != _identity_from_fd(descriptor)
            ):
                raise DomainRuleError("local_media_storage_unsafe")
            return descriptor
        except OSError as error:
            _close_fd(descriptor)
            raise DomainRuleError("local_media_storage_unsafe") from error
        except BaseException:
            _close_fd(descriptor)
            raise

    @staticmethod
    def _require_hash(value: str) -> None:
        if not _SHA256.fullmatch(value):
            raise DomainRuleError("local_media_hash_invalid")

    def _read(self, body: BinaryIO) -> bytes:
        chunks: list[bytes] = []
        total = 0
        try:
            while chunk := body.read(65536):
                if not isinstance(chunk, bytes):
                    raise TypeError
                total += len(chunk)
                if total > self._settings.local_classification_bridge_max_bytes:
                    raise DomainRuleError("local_media_too_large")
                chunks.append(chunk)
        except DomainRuleError:
            raise
        except (OSError, TypeError) as error:
            raise DomainRuleError("local_media_read_failed") from error
        return b"".join(chunks)

    def _decode(self, content: bytes) -> tuple[str, int, int]:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(content)) as image:
                    image_format = image.format
                    width = image.width
                    height = image.height
                    frames = getattr(image, "n_frames", 1)
                    if (
                        width > self._settings.local_classification_bridge_max_width
                        or height
                        > self._settings.local_classification_bridge_max_height
                        or width * height
                        > self._settings.local_classification_bridge_max_pixels
                        or frames
                        > self._settings.local_classification_bridge_max_frames
                    ):
                        raise DomainRuleError("local_media_decode_limits_exceeded")
                    image.verify()
                if image_format not in _MIMES:
                    raise DomainRuleError("local_media_mime_unsupported")
                if not self._canonical(image_format, content):
                    raise DomainRuleError("local_media_decode_invalid")
                with Image.open(io.BytesIO(content)) as image:
                    image.load()
        except DomainRuleError:
            raise
        except (
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
            OSError,
            UnidentifiedImageError,
        ) as error:
            raise DomainRuleError("local_media_decode_invalid") from error
        return _MIMES[image_format], width, height

    @staticmethod
    def _canonical(image_format: str | None, data: bytes) -> bool:
        if image_format == "PNG":
            return LocalMediaStage._png_end(data) == len(data)
        if image_format == "JPEG":
            return LocalMediaStage._jpeg_end(data) == len(data)
        return image_format == "HEIC"

    @staticmethod
    def _png_end(data: bytes) -> int:
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            return -1
        offset = 8
        first_chunk = True
        while offset + 12 <= len(data):
            length = int.from_bytes(data[offset : offset + 4], "big")
            kind = data[offset + 4 : offset + 8]
            payload_start = offset + 8
            payload_end = payload_start + length
            chunk_end = payload_end + 4
            if chunk_end > len(data):
                return -1
            expected_crc = int.from_bytes(data[payload_end:chunk_end], "big")
            actual_crc = zlib.crc32(kind + data[payload_start:payload_end]) & 0xFFFFFFFF
            if expected_crc != actual_crc:
                return -1
            if first_chunk and (kind != b"IHDR" or length != 13):
                return -1
            first_chunk = False
            offset = chunk_end
            if kind == b"IEND":
                return offset if length == 0 else -1
        return -1

    @staticmethod
    def _jpeg_end(data: bytes) -> int:
        if not data.startswith(b"\xff\xd8"):
            return -1
        offset = 2
        in_scan = False
        while offset < len(data):
            if in_scan:
                while offset < len(data) and data[offset] != 0xFF:
                    offset += 1
                if offset >= len(data):
                    return -1
            elif data[offset] != 0xFF:
                return -1

            while offset < len(data) and data[offset] == 0xFF:
                offset += 1
            if offset >= len(data):
                return -1
            marker = data[offset]
            offset += 1
            if in_scan and (marker == 0 or 0xD0 <= marker <= 0xD7):
                continue
            in_scan = False
            if marker == 0xD9:
                return offset
            if marker == 0xD8:
                return -1
            if marker == 1 or 0xD0 <= marker <= 0xD7:
                continue
            if offset + 2 > len(data):
                return -1
            length = int.from_bytes(data[offset : offset + 2], "big")
            if length < 2 or offset + length > len(data):
                return -1
            offset += length
            if marker == 0xDA:
                in_scan = True
        return -1

    def _insert_pending(self, receipt: MediaReceipt, pending_name: str) -> None:
        _require_entry_name(pending_name)
        self._db().execute(
            """
            INSERT INTO media_receipts (
                receipt_id, photo_id, content_sha256, mime_type, width, height,
                expires_at, status, handoff_id, storage_dev, storage_ino,
                pending_owner, pending_expires_at, pending_name,
                pending_dev, pending_ino
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?, ?, NULL, NULL)
            """,
            (
                str(receipt.receipt_id),
                str(receipt.photo_id),
                receipt.content_sha256,
                receipt.mime_type,
                receipt.width,
                receipt.height,
                receipt.expires_at.isoformat(),
                PENDING,
                self._owner_token,
                receipt.expires_at.isoformat(),
                pending_name,
            ),
        )

    def _active(self, photo_id: UUID) -> MediaReceipt | None:
        row = (
            self._db()
            .execute(
                """
            SELECT receipt_id, photo_id, content_sha256, mime_type, width,
                   height, expires_at
              FROM media_receipts
             WHERE photo_id=? AND status=?
             ORDER BY rowid DESC LIMIT 1
            """,
                (str(photo_id), ACTIVE),
            )
            .fetchone()
        )
        return self._receipt(row) if row else None

    def _has_unsettled_receipt(self, photo_id: UUID) -> bool:
        return (
            self._db()
            .execute(
                """
                SELECT 1 FROM media_receipts
                 WHERE photo_id=? AND status IN (?, ?)
                 LIMIT 1
                """,
                (str(photo_id), PENDING, CLEANUP),
            )
            .fetchone()
            is not None
        )

    def _has_live_consumed_receipt(self, photo_id: UUID, now: datetime) -> bool:
        return (
            self._db()
            .execute(
                """
                SELECT 1 FROM media_receipts
                 WHERE photo_id=? AND status=? AND expires_at>?
                 LIMIT 1
                """,
                (str(photo_id), CONSUMED, now.isoformat()),
            )
            .fetchone()
            is not None
        )

    def _load(self, receipt_id: UUID) -> tuple[MediaReceipt, str, UUID | None]:
        row = (
            self._db()
            .execute(
                """
            SELECT receipt_id, photo_id, content_sha256, mime_type, width,
                   height, expires_at, status, handoff_id
              FROM media_receipts WHERE receipt_id=?
            """,
                (str(receipt_id),),
            )
            .fetchone()
        )
        if row is None:
            raise DomainRuleError("local_media_receipt_unavailable")
        receipt = self._receipt(row[:7])
        status_value = str(row[7])
        if status_value not in {ACTIVE, PENDING, CONSUMED, EXPIRED, FAILED, CLEANUP}:
            raise DomainRuleError("local_media_manifest_invalid")
        handoff_id = _manifest_uuid(row[8]) if row[8] else None
        if (status_value == CONSUMED and handoff_id is None) or (
            status_value in {ACTIVE, PENDING} and handoff_id is not None
        ):
            raise DomainRuleError("local_media_manifest_invalid")
        return receipt, status_value, handoff_id

    @staticmethod
    def _receipt(row: tuple[object, ...]) -> MediaReceipt:
        try:
            receipt = MediaReceipt(
                receipt_id=_manifest_uuid(row[0]),
                photo_id=_manifest_uuid(row[1]),
                content_sha256=str(row[2]),
                mime_type=str(row[3]),
                width=int(str(row[4])),
                height=int(str(row[5])),
                expires_at=datetime.fromisoformat(str(row[6])),
            )
        except (IndexError, TypeError, ValueError, OverflowError) as error:
            raise DomainRuleError("local_media_manifest_invalid") from error
        if (
            not _SHA256.fullmatch(receipt.content_sha256)
            or receipt.mime_type not in _MIMES.values()
            or receipt.width <= 0
            or receipt.height <= 0
            or receipt.expires_at.tzinfo is None
        ):
            raise DomainRuleError("local_media_manifest_invalid")
        return receipt

    def _publish(self, receipt: MediaReceipt, content: bytes) -> None:
        journal = self._journal(receipt.receipt_id)
        if (
            journal.pending_owner != self._owner_token
            or journal.pending_name is None
            or journal.pending_expires_at is None
            or journal.pending_expires_at <= self._now_utc()
            or journal.pending_identity is not None
        ):
            raise DomainRuleError("local_media_manifest_invalid")
        photo_fd = self._photo_fd(receipt, create=True)
        pending_name = journal.pending_name
        target_name = self._target_name(receipt)
        pending_fd = -1
        try:
            _after_media_directory_verified_before_publish(self._root)
            self._assert_namespace_identity()
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
            pending_fd = os.open(pending_name, flags, 0o600, dir_fd=photo_fd)
            pending_identity = _identity_from_fd(pending_fd)
            publication = _Publication(
                photo_id=receipt.photo_id,
                pending_name=pending_name,
                pending_identity=pending_identity,
                target_name=target_name,
            )
            self._publications[receipt.receipt_id] = publication
            self._record_pending_identity(receipt, pending_identity)
            with os.fdopen(pending_fd, "wb", closefd=False) as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            if _identity_from_fd(pending_fd) != pending_identity:
                raise DomainRuleError("local_media_storage_unsafe")
            os.link(
                pending_name,
                target_name,
                src_dir_fd=photo_fd,
                dst_dir_fd=photo_fd,
                follow_symlinks=False,
            )
            target_identity = _identity_at(photo_fd, target_name)
            if target_identity != pending_identity:
                raise DomainRuleError("local_media_storage_unsafe")
            publication.target_identity = target_identity
            _unlink_if_identity_matches(photo_fd, pending_name, pending_identity)
            if _identity_or_none(photo_fd, pending_name) is not None:
                raise OSError("pending media cleanup interrupted")
            if _identity_at(photo_fd, target_name) != target_identity:
                raise DomainRuleError("local_media_storage_unsafe")
            self._assert_namespace_identity()
        finally:
            _close_fd(pending_fd)
            _close_fd(photo_fd)

    def _record_pending_identity(
        self, receipt: MediaReceipt, pending_identity: FileIdentity
    ) -> None:
        self._begin()
        changed = (
            self._db()
            .execute(
                """
            UPDATE media_receipts
               SET pending_dev=?, pending_ino=?
             WHERE receipt_id=? AND status=? AND pending_owner=?
                   AND pending_dev IS NULL AND pending_ino IS NULL
            """,
                (
                    *pending_identity,
                    str(receipt.receipt_id),
                    PENDING,
                    self._owner_token,
                ),
            )
            .rowcount
        )
        if changed != 1:
            raise OSError("pending media journal changed")
        self._commit()

    def _activate_receipt(self, receipt: MediaReceipt) -> None:
        publication = self._publications.get(receipt.receipt_id)
        if publication is None or publication.target_identity is None:
            raise OSError("media publication identity missing")
        actual_identity = self._verified_storage_identity(
            receipt,
            expected_identity=publication.target_identity,
        )
        _after_storage_verification_before_activation(self._root)
        actual_identity = self._verified_storage_identity(
            receipt,
            expected_identity=publication.target_identity,
        )
        photo_fd = self._photo_fd(receipt, create=False)
        try:
            if _identity_or_none(photo_fd, publication.pending_name) is not None:
                raise OSError("pending media still present")
        finally:
            _close_fd(photo_fd)
        activation_time = self._now_utc().isoformat()
        self._begin()
        changed = (
            self._db()
            .execute(
                """
            UPDATE media_receipts
               SET status=?, storage_dev=?, storage_ino=?,
                   pending_owner=NULL, pending_expires_at=NULL,
                   pending_name=NULL, pending_dev=NULL, pending_ino=NULL
             WHERE receipt_id=? AND status=? AND pending_owner=?
                   AND pending_expires_at>? AND expires_at>?
            """,
                (
                    ACTIVE,
                    *actual_identity,
                    str(receipt.receipt_id),
                    PENDING,
                    self._owner_token,
                    activation_time,
                    activation_time,
                ),
            )
            .rowcount
        )
        if changed != 1:
            raise OSError("media activation missing")
        self._commit()

    def _rollback_publish(self, receipt: MediaReceipt) -> None:
        self._rollback_if_needed()
        publication = self._publications.pop(receipt.receipt_id, None)
        cleanup_succeeded = True
        if publication is not None:
            try:
                photo_fd = self._photo_fd(receipt, create=False)
                try:
                    expected_links = int(
                        _identity_or_none(photo_fd, publication.pending_name)
                        == publication.pending_identity
                    )
                    if publication.target_identity is not None:
                        expected_links += int(
                            _identity_or_none(photo_fd, publication.target_name)
                            == publication.target_identity
                        )
                    if publication.target_identity is not None:
                        cleanup_succeeded &= _remove_owned_entry(
                            photo_fd,
                            publication.target_name,
                            publication.target_identity,
                            expected_links=expected_links,
                        )
                        if cleanup_succeeded:
                            expected_links -= 1
                    cleanup_succeeded &= _remove_owned_entry(
                        photo_fd,
                        publication.pending_name,
                        publication.pending_identity,
                        expected_links=max(expected_links, 1),
                    )
                finally:
                    _close_fd(photo_fd)
            except (DomainRuleError, OSError):
                cleanup_succeeded = False
        else:
            try:
                cleanup_succeeded = self._cleanup_receipt_files(receipt)
            except (DomainRuleError, OSError):
                cleanup_succeeded = False
        try:
            self._record_cleanup_result(receipt.receipt_id, cleanup_succeeded)
        except (DomainRuleError, sqlite3.Error):
            pass

    def _recover_pending(self) -> None:
        rows = (
            self._db()
            .execute(
                "SELECT receipt_id FROM media_receipts WHERE status IN (?, ?)",
                (PENDING, CLEANUP),
            )
            .fetchall()
        )
        for row in rows:
            receipt, _, _ = self._load(_manifest_uuid(row[0]))
            journal = self._journal(receipt.receipt_id)
            if (
                journal.pending_expires_at is not None
                and journal.pending_expires_at > self._now_utc()
            ):
                continue
            try:
                cleaned = self._cleanup_receipt_files(receipt)
            except (DomainRuleError, OSError):
                cleaned = False
            self._record_cleanup_result(receipt.receipt_id, cleaned)

    def _verify(self, receipt: MediaReceipt) -> None:
        expected_identity = self._journal(receipt.receipt_id).storage_identity
        if expected_identity is None:
            raise DomainRuleError("local_media_manifest_invalid")
        self._verified_storage_identity(
            receipt,
            expected_identity=expected_identity,
        )

    def _verified_storage_identity(
        self,
        receipt: MediaReceipt,
        *,
        expected_identity: FileIdentity | None,
    ) -> FileIdentity:
        self._assert_namespace_identity()
        photo_fd = self._photo_fd(receipt, create=False)
        handle = -1
        try:
            name = self._target_name(receipt)
            handle = os.open(
                name,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                dir_fd=photo_fd,
            )
            identity = _identity_from_fd(handle)
            file_stat = os.fstat(handle)
            if (
                not stat.S_ISREG(file_stat.st_mode)
                or stat.S_IMODE(file_stat.st_mode) != 0o600
                or file_stat.st_nlink != 1
                or (expected_identity is not None and identity != expected_identity)
            ):
                raise DomainRuleError("local_media_storage_unsafe")
            with os.fdopen(handle, "rb", closefd=False) as source:
                digest = hashlib.file_digest(source, "sha256").hexdigest()
            if (
                digest != receipt.content_sha256
                or _identity_from_fd(handle) != identity
                or _identity_at(photo_fd, name) != identity
            ):
                raise DomainRuleError("local_media_storage_unsafe")
            self._assert_namespace_identity()
            return identity
        except DomainRuleError:
            raise
        except OSError as error:
            raise DomainRuleError("local_media_storage_unsafe") from error
        finally:
            _close_fd(handle)
            _close_fd(photo_fd)

    def _unlink_verified(self, receipt: MediaReceipt) -> None:
        if not self._cleanup_receipt_files(receipt):
            raise DomainRuleError("local_media_storage_unsafe")

    def _cleanup_receipt_files(self, receipt: MediaReceipt) -> bool:
        journal = self._journal(receipt.receipt_id)
        try:
            photo_fd = self._photo_fd(receipt, create=False)
        except DomainRuleError:
            return (
                journal.storage_identity is None
                and journal.pending_identity is None
                and journal.pending_name is None
            )
        try:
            clean = True
            target_name = self._target_name(receipt)
            owned_entries: list[tuple[str, FileIdentity]] = []
            target_identity = _identity_or_none(photo_fd, target_name)
            if journal.storage_identity is None:
                if (
                    target_identity is not None
                    and journal.pending_identity is not None
                    and target_identity == journal.pending_identity
                ):
                    owned_entries.append((target_name, target_identity))
                else:
                    clean &= target_identity is None
            elif target_identity is None:
                pass
            elif target_identity == journal.storage_identity:
                owned_entries.append((target_name, target_identity))
            else:
                clean = False
            if journal.pending_name is not None:
                if journal.pending_identity is None:
                    pending_identity = _identity_or_none(photo_fd, journal.pending_name)
                    if pending_identity is not None:
                        _verify_empty_planned_pending(
                            photo_fd,
                            journal.pending_name,
                            pending_identity,
                        )
                        owned_entries.append((journal.pending_name, pending_identity))
                else:
                    pending_identity = _identity_or_none(photo_fd, journal.pending_name)
                    if pending_identity is None:
                        pass
                    elif pending_identity == journal.pending_identity:
                        owned_entries.append((journal.pending_name, pending_identity))
                    else:
                        clean = False
            recorded_identities = {
                identity
                for identity in (
                    journal.storage_identity,
                    journal.pending_identity,
                )
                if identity is not None
            }
            controlled_identities = {identity for _, identity in owned_entries}
            clean &= recorded_identities <= controlled_identities
            remaining_links = {
                identity: sum(
                    candidate_identity == identity
                    for _, candidate_identity in owned_entries
                )
                for _, identity in owned_entries
            }
            for name, identity in owned_entries:
                removed = _remove_owned_entry(
                    photo_fd,
                    name,
                    identity,
                    expected_links=remaining_links[identity],
                )
                clean &= removed
                if removed:
                    remaining_links[identity] -= 1
            return clean
        finally:
            _close_fd(photo_fd)

    def _journal(self, receipt_id: UUID) -> _Journal:
        row = (
            self._db()
            .execute(
                """
            SELECT storage_dev, storage_ino, pending_owner,
                   pending_expires_at, pending_name, pending_dev, pending_ino
              FROM media_receipts WHERE receipt_id=?
            """,
                (str(receipt_id),),
            )
            .fetchone()
        )
        if row is None:
            raise DomainRuleError("local_media_receipt_unavailable")
        storage_identity = _optional_identity(row[0], row[1])
        pending_identity = _optional_identity(row[5], row[6])
        pending_owner = str(row[2]) if row[2] is not None else None
        pending_name = str(row[4]) if row[4] is not None else None
        try:
            pending_expires_at = (
                datetime.fromisoformat(str(row[3])) if row[3] is not None else None
            )
        except ValueError as error:
            raise DomainRuleError("local_media_manifest_invalid") from error
        if pending_name is not None:
            _require_entry_name(pending_name)
        if (
            (pending_identity is not None and pending_name is None)
            or (pending_expires_at is not None and pending_expires_at.tzinfo is None)
            or (
                pending_owner is not None
                and (not pending_owner or len(pending_owner) > 128)
            )
        ):
            raise DomainRuleError("local_media_manifest_invalid")
        return _Journal(
            storage_identity=storage_identity,
            pending_owner=pending_owner,
            pending_expires_at=pending_expires_at,
            pending_name=pending_name,
            pending_identity=pending_identity,
        )

    def _record_cleanup_result(self, receipt_id: UUID, cleaned: bool) -> None:
        self._begin()
        if cleaned:
            self._db().execute(
                """
                UPDATE media_receipts
                   SET status=?, storage_dev=NULL, storage_ino=NULL,
                       pending_owner=NULL, pending_expires_at=NULL,
                       pending_name=NULL, pending_dev=NULL, pending_ino=NULL
                 WHERE receipt_id=?
                """,
                (FAILED, str(receipt_id)),
            )
        else:
            self._db().execute(
                "UPDATE media_receipts SET status=? WHERE receipt_id=?",
                (CLEANUP, str(receipt_id)),
            )
        self._commit()

    @staticmethod
    def _target_name(receipt: MediaReceipt) -> str:
        return f"{receipt.content_sha256}.media"

    def _path(self, receipt: MediaReceipt) -> Path:
        return self._root / "media" / str(receipt.photo_id) / self._target_name(receipt)

    def _set_status(
        self, receipt_id: UUID, status_value: str, *, commit: bool = True
    ) -> None:
        self._assert_namespace_identity()
        self._db().execute(
            "UPDATE media_receipts SET status=? WHERE receipt_id=?",
            (status_value, str(receipt_id)),
        )
        if commit:
            self._commit()

    def _begin(self) -> None:
        self._assert_namespace_identity()
        self._db().execute("BEGIN IMMEDIATE")

    def _commit(self) -> None:
        self._assert_namespace_identity()
        self._db().commit()

    def _rollback(self) -> None:
        self._db().rollback()

    def _rollback_if_needed(self) -> None:
        if self._connection is not None and self._connection.in_transaction:
            self._connection.rollback()

    def _db(self) -> sqlite3.Connection:
        if self._connection is None:
            raise DomainRuleError("local_media_storage_unavailable")
        return self._connection

    def _now_utc(self) -> datetime:
        return self._as_utc(self._now())

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("local media time must be timezone-aware")
        return value.astimezone(UTC)


def _identity(file_stat: os.stat_result) -> FileIdentity:
    return file_stat.st_dev, file_stat.st_ino


def _identity_from_fd(descriptor: int) -> FileIdentity:
    return _identity(os.fstat(descriptor))


def _require_unique_regular_fd(descriptor: int) -> None:
    file_stat = os.fstat(descriptor)
    if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_nlink != 1:
        raise DomainRuleError("local_media_storage_unsafe")


def _identity_at(directory_fd: int, name: str) -> FileIdentity:
    _require_entry_name(name)
    return _identity(os.stat(name, dir_fd=directory_fd, follow_symlinks=False))


def _identity_or_none(directory_fd: int, name: str) -> FileIdentity | None:
    try:
        return _identity_at(directory_fd, name)
    except FileNotFoundError:
        return None


def _optional_identity(device: object, inode: object) -> FileIdentity | None:
    if device is None and inode is None:
        return None
    if device is None or inode is None:
        raise DomainRuleError("local_media_manifest_invalid")
    try:
        identity = int(str(device)), int(str(inode))
    except (TypeError, ValueError, OverflowError) as error:
        raise DomainRuleError("local_media_manifest_invalid") from error
    if identity[0] < 0 or identity[1] <= 0:
        raise DomainRuleError("local_media_manifest_invalid")
    return identity


def _manifest_uuid(value: object) -> UUID:
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError) as error:
        raise DomainRuleError("local_media_manifest_invalid") from error


def _require_entry_name(name: str) -> None:
    if Path(name).name != name or name in {"", ".", ".."}:
        raise DomainRuleError("local_media_storage_unsafe")


def _unlink_if_identity_matches(
    directory_fd: int, name: str, expected_identity: FileIdentity
) -> None:
    try:
        if _identity_at(directory_fd, name) == expected_identity:
            os.unlink(name, dir_fd=directory_fd)
    except FileNotFoundError:
        return


def _remove_owned_entry(
    directory_fd: int,
    name: str,
    expected_identity: FileIdentity,
    *,
    expected_links: int = 1,
) -> bool:
    try:
        entry = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return True
    if (
        _identity(entry) != expected_identity
        or stat.S_ISLNK(entry.st_mode)
        or not stat.S_ISREG(entry.st_mode)
        or entry.st_nlink != expected_links
    ):
        return False
    try:
        os.unlink(name, dir_fd=directory_fd)
    except OSError:
        return False
    return _identity_or_none(directory_fd, name) is None


def _verify_empty_planned_pending(
    directory_fd: int,
    name: str,
    expected_identity: FileIdentity,
) -> None:
    descriptor = -1
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            dir_fd=directory_fd,
        )
        file_stat = os.fstat(descriptor)
        if (
            _identity(file_stat) != expected_identity
            or not stat.S_ISREG(file_stat.st_mode)
            or stat.S_IMODE(file_stat.st_mode) != 0o600
            or file_stat.st_nlink != 1
            or file_stat.st_size != 0
            or _identity_at(directory_fd, name) != expected_identity
        ):
            raise DomainRuleError("local_media_storage_unsafe")
    except DomainRuleError:
        raise
    except OSError as error:
        raise DomainRuleError("local_media_storage_unsafe") from error
    finally:
        _close_fd(descriptor)


def _close_fd(descriptor: int) -> None:
    if descriptor < 0:
        return
    try:
        os.close(descriptor)
    except OSError:
        pass


def _after_media_directory_verified_before_publish(root: Path) -> None:
    """No-op adversarial seam after descriptor pinning and before publication."""
    del root


def _after_storage_verification_before_activation(root: Path) -> None:
    """No-op adversarial seam before a verified publication becomes active."""
    del root


def _after_media_verification_before_consume(root: Path) -> None:
    """No-op adversarial seam before a verified receipt is bound."""
    del root
