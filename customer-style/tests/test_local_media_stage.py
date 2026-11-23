"""Behaviour checks for the removable, local-only media handoff store."""

from __future__ import annotations

import fcntl
import hashlib
import io
import multiprocessing
import os
import shutil
import sqlite3
import stat
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from uuid import UUID, uuid4

import pytest
from PIL import Image

from opc_style.domain.errors import DomainRuleError
from opc_style.services import local_media_stage
from opc_style.services.local_media_stage import LocalMediaStage
from opc_style.settings import LocalBridgeSettings


def image_bytes(image_format: str = "PNG", *, size: tuple[int, int] = (8, 6)) -> bytes:
    """Build a decoded-image fixture without relying on customer media."""
    data = io.BytesIO()
    Image.new("RGB", size, color=(20, 40, 60)).save(data, format=image_format)
    return data.getvalue()


def build_stage(
    tmp_path: Path,
    *,
    enabled: bool = True,
    caller_host: str = "127.0.0.1",
    max_bytes: int = 1024 * 1024,
    max_width: int = 4096,
    max_height: int = 4096,
    max_pixels: int = 16_000_000,
    max_frames: int = 1,
    ttl: timedelta = timedelta(minutes=5),
    now: datetime | Callable[[], datetime] | None = None,
) -> LocalMediaStage:
    """Create a real private namespace with a fixed clock for expiry cases."""
    current = now or datetime(2026, 8, 31, tzinfo=UTC)
    clock = current if callable(current) else lambda: current
    return LocalMediaStage(
        settings=LocalBridgeSettings(
            local_classification_bridge=enabled,
            local_classification_bridge_nonce="test-bridge-nonce" if enabled else None,
            local_classification_bridge_stage_root=tmp_path / "private-stage",
            local_classification_bridge_asset_root=(
                tmp_path / "private-assets" if enabled else None
            ),
            local_classification_bridge_max_bytes=max_bytes,
            local_classification_bridge_max_width=max_width,
            local_classification_bridge_max_height=max_height,
            local_classification_bridge_max_pixels=max_pixels,
            local_classification_bridge_max_frames=max_frames,
            local_classification_bridge_receipt_ttl_seconds=int(ttl.total_seconds()),
        ),
        caller_host=caller_host,
        now=clock,
    )


def stage_png(stage: LocalMediaStage, photo_id=None):  # type: ignore[no-untyped-def]
    """Stage one hand-checked PNG fixture."""
    body = image_bytes()
    return stage.stage(
        photo_id=photo_id or uuid4(),
        claimed_sha256=hashlib.sha256(body).hexdigest(),
        body=io.BytesIO(body),
    )


def assert_domain_error(code: str, action) -> None:  # type: ignore[no-untyped-def]
    """Assert the stable error code consumers use to stop unsafe delivery."""
    with pytest.raises(DomainRuleError) as raised:
        action()
    assert raised.value.code == code


def _attempt_orchestration_lock_worker(root: str, ready, proceed, result) -> None:  # type: ignore[no-untyped-def]
    lock_path = Path(root) / "private-stage" / ".classification-orchestration.lock"
    descriptor = os.open(lock_path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    ready.put("ready")
    proceed.get(timeout=5)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        result.put("blocked")
    else:
        result.put("acquired")
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def test_refuses_staging_when_local_bridge_is_disabled(tmp_path: Path) -> None:
    """A disabled bridge must not leave a private media namespace behind."""
    stage = build_stage(tmp_path, enabled=False)
    body = image_bytes()

    assert_domain_error(
        "local_classification_bridge_disabled",
        lambda: stage.stage(
            photo_id=uuid4(),
            claimed_sha256=hashlib.sha256(body).hexdigest(),
            body=io.BytesIO(body),
        ),
    )
    assert not (tmp_path / "private-stage").exists()


@pytest.mark.parametrize("caller_host", ["192.0.2.9", "example.invalid"])
def test_refuses_non_loopback_callers_before_creating_storage(
    tmp_path: Path, caller_host: str
) -> None:
    """Only the local test page may deliver bytes to this private store."""
    stage = build_stage(tmp_path, caller_host=caller_host)
    body = image_bytes()

    assert_domain_error(
        "local_classification_bridge_loopback_required",
        lambda: stage.stage(
            photo_id=uuid4(),
            claimed_sha256=hashlib.sha256(body).hexdigest(),
            body=io.BytesIO(body),
        ),
    )
    assert not (tmp_path / "private-stage").exists()


def test_stages_verified_bytes_in_a_private_namespace_and_returns_facts(
    tmp_path: Path,
) -> None:
    """Changing hash, decoded format, or dimensions must break this receipt."""
    stage = build_stage(tmp_path)
    photo_id = uuid4()
    body = image_bytes("JPEG", size=(12, 9))

    receipt = stage.stage(
        photo_id=photo_id,
        claimed_sha256=hashlib.sha256(body).hexdigest(),
        body=io.BytesIO(body),
    )

    assert receipt.photo_id == photo_id
    assert receipt.content_sha256 == hashlib.sha256(body).hexdigest()
    assert (receipt.mime_type, receipt.width, receipt.height) == ("image/jpeg", 12, 9)
    assert receipt.expires_at == datetime(2026, 8, 31, 0, 5, tzinfo=UTC)
    resolved = stage.resolve(
        receipt_id=receipt.receipt_id, photo_id=photo_id, sha256=receipt.content_sha256
    )
    assert resolved.read_bytes() == body
    assert stat.S_IMODE((tmp_path / "private-stage").stat().st_mode) == 0o700
    assert (
        stat.S_IMODE((tmp_path / "private-stage" / ".namespace.lock").stat().st_mode)
        == 0o600
    )
    assert (
        stat.S_IMODE((tmp_path / "private-stage" / "receipts.sqlite3").stat().st_mode)
        == 0o600
    )
    assert stat.S_IMODE(resolved.parent.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(resolved.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(resolved.stat().st_mode) == 0o600
    assert "/" not in resolved.name
    assert resolved.parent.parent == tmp_path / "private-stage" / "media"


def test_batch_consume_recaptures_expiry_after_media_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A receipt expiring inside verification must not become consumed."""
    clock = [datetime(2026, 8, 31, tzinfo=UTC)]
    stage = build_stage(tmp_path, ttl=timedelta(seconds=1), now=lambda: clock[0])
    receipt = stage_png(stage)
    monkeypatch.setattr(
        local_media_stage,
        "_after_media_verification_before_consume",
        lambda root: clock.__setitem__(0, clock[0] + timedelta(seconds=2)),
    )

    assert_domain_error(
        "local_media_receipt_unavailable",
        lambda: stage.consume_batch(
            receipts=((receipt.receipt_id, receipt.photo_id, receipt.content_sha256),),
            handoff_id=uuid4(),
        ),
    )


def test_active_handoff_pin_survives_ttl_until_terminal_release(tmp_path: Path) -> None:
    """Failed handoffs retain their attested receipt long enough to resume."""
    clock = [datetime(2026, 8, 31, tzinfo=UTC)]
    stage = build_stage(tmp_path, ttl=timedelta(seconds=1), now=lambda: clock[0])
    receipt = stage_png(stage)
    handoff_id = uuid4()
    stage.consume_batch(
        receipts=((receipt.receipt_id, receipt.photo_id, receipt.content_sha256),),
        handoff_id=handoff_id,
    )
    clock[0] += timedelta(days=1)

    assert stage.cleanup_expired(now=clock[0]) == ()
    assert stage.resolve_for_handoff(
        receipt_id=receipt.receipt_id,
        photo_id=receipt.photo_id,
        sha256=receipt.content_sha256,
        handoff_id=handoff_id,
    ).exists()
    stage.release_handoff_receipts(handoff_id=handoff_id)
    assert stage.cleanup_expired(now=clock[0]) == ()
    assert_domain_error(
        "local_media_receipt_expired",
        lambda: stage.resolve_for_handoff(
            receipt_id=receipt.receipt_id,
            photo_id=receipt.photo_id,
            sha256=receipt.content_sha256,
            handoff_id=handoff_id,
        ),
    )


def test_pinned_consumed_batch_can_retry_after_original_ttl(tmp_path: Path) -> None:
    """Same-handoff retries reuse a pin; active receipts still use fresh expiry."""
    clock = [datetime(2026, 8, 31, tzinfo=UTC)]
    stage = build_stage(tmp_path, ttl=timedelta(seconds=1), now=lambda: clock[0])
    receipt = stage_png(stage)
    handoff_id = uuid4()
    facts = ((receipt.receipt_id, receipt.photo_id, receipt.content_sha256),)
    stage.consume_batch(receipts=facts, handoff_id=handoff_id)
    clock[0] += timedelta(days=1)

    assert stage.consume_batch(receipts=facts, handoff_id=handoff_id)[0].exists()


def test_orchestration_lock_survives_nested_media_operation_across_processes(
    tmp_path: Path,
) -> None:
    """An inner resolve must not unlock the outer consumer orchestration flock."""
    stage = build_stage(tmp_path)
    receipt = stage_png(stage)
    context = multiprocessing.get_context("fork")
    ready = context.Queue()
    proceed = context.Queue()
    result = context.Queue()
    with stage.orchestration_lock():
        process = context.Process(
            target=_attempt_orchestration_lock_worker,
            args=(str(tmp_path), ready, proceed, result),
        )
        process.start()
        assert ready.get(timeout=5) == "ready"
        stage.resolve(
            receipt_id=receipt.receipt_id,
            photo_id=receipt.photo_id,
            sha256=receipt.content_sha256,
        )
        proceed.put("attempt")
        assert result.get(timeout=5) == "blocked"
    process.join(timeout=5)
    assert process.exitcode == 0


def test_orchestration_lock_replacement_fails_closed(tmp_path: Path) -> None:
    """A swapped consumer lock is never trusted for orchestration."""
    stage = build_stage(tmp_path)
    stage_png(stage)
    lock_path = tmp_path / "private-stage" / ".classification-orchestration.lock"
    lock_path.unlink()
    lock_path.touch(mode=0o600)

    assert_domain_error(
        "local_media_storage_unsafe", lambda: stage.orchestration_lock().__enter__()
    )


def test_rejects_oversized_unsupported_invalid_and_polyglot_inputs(
    tmp_path: Path,
) -> None:
    """Removing size or decoder checks would admit unsafe non-image input."""
    oversized_stage = build_stage(tmp_path / "oversized", max_bytes=10)
    body = image_bytes()
    assert_domain_error(
        "local_media_too_large",
        lambda: oversized_stage.stage(
            photo_id=uuid4(),
            claimed_sha256=hashlib.sha256(body).hexdigest(),
            body=io.BytesIO(body),
        ),
    )

    stage = build_stage(tmp_path / "content")
    unsupported = b"not-an-image"
    assert_domain_error(
        "local_media_decode_invalid",
        lambda: stage.stage(
            photo_id=uuid4(),
            claimed_sha256=hashlib.sha256(unsupported).hexdigest(),
            body=io.BytesIO(unsupported),
        ),
    )
    gif = image_bytes("GIF")
    assert_domain_error(
        "local_media_mime_unsupported",
        lambda: stage.stage(
            photo_id=uuid4(),
            claimed_sha256=hashlib.sha256(gif).hexdigest(),
            body=io.BytesIO(gif),
        ),
    )
    polyglot = image_bytes("PNG") + b"<script>not-an-image</script>"
    assert_domain_error(
        "local_media_decode_invalid",
        lambda: stage.stage(
            photo_id=uuid4(),
            claimed_sha256=hashlib.sha256(polyglot).hexdigest(),
            body=io.BytesIO(polyglot),
        ),
    )


def test_rejects_hash_mismatch_without_publishing_partial_bytes(tmp_path: Path) -> None:
    """Removing the recomputed digest check would let the receipt lie about bytes."""
    stage = build_stage(tmp_path)
    body = image_bytes()

    assert_domain_error(
        "local_media_hash_mismatch",
        lambda: stage.stage(
            photo_id=uuid4(), claimed_sha256="a" * 64, body=io.BytesIO(body)
        ),
    )
    assert not list((tmp_path / "private-stage" / "media").rglob("*"))


def test_failed_publication_removes_pending_media_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Leaving a temporary file after publication failure would retain unreceipted bytes."""
    stage = build_stage(tmp_path)
    body = image_bytes()

    def interrupt_publication(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated partial publication")

    monkeypatch.setattr(local_media_stage.os, "link", interrupt_publication)
    assert_domain_error(
        "local_media_write_failed",
        lambda: stage.stage(
            photo_id=uuid4(),
            claimed_sha256=hashlib.sha256(body).hexdigest(),
            body=io.BytesIO(body),
        ),
    )
    assert not list((tmp_path / "private-stage" / "media").rglob("*.media"))
    assert not list((tmp_path / "private-stage" / "media").rglob(".pending-*"))


def test_repeated_identical_stage_is_idempotent_but_changed_bytes_conflict(
    tmp_path: Path,
) -> None:
    """The same photo cannot mint a second receipt or silently replace bytes."""
    stage = build_stage(tmp_path)
    photo_id = uuid4()
    body = image_bytes()
    claimed_sha256 = hashlib.sha256(body).hexdigest()

    first = stage.stage(
        photo_id=photo_id, claimed_sha256=claimed_sha256, body=io.BytesIO(body)
    )
    repeated = stage.stage(
        photo_id=photo_id, claimed_sha256=claimed_sha256, body=io.BytesIO(body)
    )
    assert repeated == first
    changed = image_bytes("JPEG")
    assert_domain_error(
        "local_media_receipt_conflict",
        lambda: stage.stage(
            photo_id=photo_id,
            claimed_sha256=hashlib.sha256(changed).hexdigest(),
            body=io.BytesIO(changed),
        ),
    )


def test_expired_receipts_are_unavailable_before_cleanup(tmp_path: Path) -> None:
    """Dropping the expiry check would allow stale photos into a later handoff."""
    current = [datetime(2026, 8, 31, tzinfo=UTC)]
    stage = build_stage(tmp_path, ttl=timedelta(seconds=1), now=lambda: current[0])
    receipt = stage_png(stage)
    current[0] += timedelta(seconds=1)

    assert_domain_error(
        "local_media_receipt_expired",
        lambda: stage.resolve(
            receipt_id=receipt.receipt_id,
            photo_id=receipt.photo_id,
            sha256=receipt.content_sha256,
        ),
    )
    assert stage.cleanup_expired(now=current[0]) == ()
    assert_domain_error(
        "local_media_receipt_unavailable",
        lambda: stage.resolve(
            receipt_id=receipt.receipt_id,
            photo_id=receipt.photo_id,
            sha256=receipt.content_sha256,
        ),
    )


def test_resolve_rejects_traversal_facts_and_symlinked_media(tmp_path: Path) -> None:
    """Removing generated-path checks could expose files outside the stage root."""
    stage = build_stage(tmp_path)
    receipt = stage_png(stage)
    assert_domain_error(
        "local_media_receipt_mismatch",
        lambda: stage.resolve(
            receipt_id=receipt.receipt_id,
            photo_id=uuid4(),
            sha256="f" * 64,
        ),
    )
    path = stage.resolve(
        receipt_id=receipt.receipt_id,
        photo_id=receipt.photo_id,
        sha256=receipt.content_sha256,
    )
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"must-not-be-resolved")
    path.unlink()
    path.symlink_to(outside)
    assert_domain_error(
        "local_media_storage_unsafe",
        lambda: stage.resolve(
            receipt_id=receipt.receipt_id,
            photo_id=receipt.photo_id,
            sha256=receipt.content_sha256,
        ),
    )


def test_stage_rejects_a_runtime_non_uuid_before_path_creation(tmp_path: Path) -> None:
    """Type hints alone must not let a direct caller traverse through mkdirat."""
    stage = build_stage(tmp_path)
    body = image_bytes()

    assert_domain_error(
        "local_media_photo_id_invalid",
        lambda: stage.stage(
            photo_id="../../escape",  # type: ignore[arg-type]
            claimed_sha256=hashlib.sha256(body).hexdigest(),
            body=io.BytesIO(body),
        ),
    )
    assert not (tmp_path / "escape").exists()


def test_resolve_rejects_a_symlinked_receipt_directory(tmp_path: Path) -> None:
    """Checking only the final file would follow a receipt directory outside the store."""
    stage = build_stage(tmp_path)
    receipt = stage_png(stage)
    path = stage.resolve(
        receipt_id=receipt.receipt_id,
        photo_id=receipt.photo_id,
        sha256=receipt.content_sha256,
    )
    original_bytes = path.read_bytes()
    external_directory = tmp_path / "external-directory"
    external_directory.mkdir()
    path.unlink()
    path.parent.rmdir()
    (external_directory / path.name).write_bytes(original_bytes)
    path.parent.symlink_to(external_directory, target_is_directory=True)

    assert_domain_error(
        "local_media_storage_unsafe",
        lambda: stage.resolve(
            receipt_id=receipt.receipt_id,
            photo_id=receipt.photo_id,
            sha256=receipt.content_sha256,
        ),
    )


def test_consumption_is_single_handoff_idempotent_and_replay_closed(
    tmp_path: Path,
) -> None:
    """A second handoff ID must not reuse a receipt that was already consumed."""
    stage = build_stage(tmp_path)
    receipt = stage_png(stage)
    handoff_id = uuid4()

    first = stage.consume(receipt_id=receipt.receipt_id, handoff_id=handoff_id)
    assert stage.consume(receipt_id=receipt.receipt_id, handoff_id=handoff_id) == first
    assert_domain_error(
        "local_media_receipt_replayed",
        lambda: stage.consume(receipt_id=receipt.receipt_id, handoff_id=uuid4()),
    )
    assert_domain_error(
        "local_media_receipt_unavailable",
        lambda: stage.resolve(
            receipt_id=receipt.receipt_id,
            photo_id=receipt.photo_id,
            sha256=receipt.content_sha256,
        ),
    )


def test_consume_rejects_a_runtime_non_uuid_without_corrupting_the_manifest(
    tmp_path: Path,
) -> None:
    """A direct caller must not persist a handoff value later reads cannot parse."""
    stage = build_stage(tmp_path)
    receipt = stage_png(stage)

    assert_domain_error(
        "local_media_handoff_id_invalid",
        lambda: stage.consume(
            receipt_id=receipt.receipt_id,
            handoff_id="not-a-uuid",  # type: ignore[arg-type]
        ),
    )
    assert stage.consume(receipt_id=receipt.receipt_id, handoff_id=uuid4()).exists()


def test_failed_cleanup_remains_unavailable_and_is_reported_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claiming cleanup after unlink failure would hide retained customer bytes."""
    started = datetime(2026, 8, 31, tzinfo=UTC)
    stage = build_stage(tmp_path, ttl=timedelta(seconds=1), now=started)
    receipt = stage_png(stage)
    path = stage.resolve(
        receipt_id=receipt.receipt_id,
        photo_id=receipt.photo_id,
        sha256=receipt.content_sha256,
    )
    original_unlink = local_media_stage.os.unlink

    def refuse_target_unlink(target: str, *args: object, **kwargs: object) -> None:
        if target == path.name:
            raise OSError("simulated cleanup interruption")
        original_unlink(target, *args, **kwargs)

    monkeypatch.setattr(local_media_stage.os, "unlink", refuse_target_unlink)
    assert stage.cleanup_expired(now=started + timedelta(seconds=1)) == (
        str(receipt.receipt_id),
    )
    assert path.exists()
    assert_domain_error(
        "local_media_receipt_unavailable",
        lambda: stage.consume(receipt_id=receipt.receipt_id, handoff_id=uuid4()),
    )


def test_close_is_idempotent_and_all_later_operations_fail(tmp_path: Path) -> None:
    """A closed SQLite namespace must never be reopened implicitly by an operation."""
    stage = build_stage(tmp_path)
    stage.close()
    stage.close()
    body = image_bytes()
    assert_domain_error(
        "local_media_stage_closed",
        lambda: stage.stage(
            photo_id=uuid4(),
            claimed_sha256=hashlib.sha256(body).hexdigest(),
            body=io.BytesIO(body),
        ),
    )


def test_stage_cleanup_cycles_do_not_keep_file_descriptors_open(tmp_path: Path) -> None:
    """Forgetting to close stream or database cursors would grow descriptors per cycle."""
    fd_root = Path("/dev/fd")
    if not fd_root.exists():
        pytest.skip("descriptor accounting is unavailable on this platform")
    baseline = len(list(fd_root.iterdir()))
    now = datetime(2026, 8, 31, tzinfo=UTC)
    stage = build_stage(tmp_path, ttl=timedelta(seconds=1), now=now)
    stage_png(stage)
    assert stage.cleanup_expired(now=now + timedelta(seconds=1)) == ()
    retained_baseline = len(list(fd_root.iterdir()))
    for _ in range(99):
        stage_png(stage)
        assert stage.cleanup_expired(now=now + timedelta(seconds=1)) == ()
    after = len(list(fd_root.iterdir()))
    stage.close()
    closed = len(list(fd_root.iterdir()))
    assert after <= retained_baseline + 1
    assert closed <= baseline + 1


def test_competing_instances_consume_once_and_same_handoff_retries_safely(
    tmp_path: Path,
) -> None:
    """Process-local locks alone let two processes consume one receipt concurrently."""
    first = build_stage(tmp_path)
    receipt = stage_png(first)
    second = build_stage(tmp_path)

    def consume(stage: LocalMediaStage, handoff_id):  # type: ignore[no-untyped-def]
        try:
            return (
                "ok",
                stage.consume(receipt_id=receipt.receipt_id, handoff_id=handoff_id),
            )
        except DomainRuleError as error:
            return ("error", error.code)

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(
            executor.map(
                lambda arguments: consume(*arguments),
                ((first, uuid4()), (second, uuid4())),
            )
        )
    assert sorted(outcome[0] for outcome in outcomes) == ["error", "ok"]
    assert {outcome[1] for outcome in outcomes if outcome[0] == "error"} == {
        "local_media_receipt_replayed"
    }

    retry_first = build_stage(tmp_path / "retry")
    retry_receipt = stage_png(retry_first)
    retry_second = build_stage(tmp_path / "retry")
    retry_handoff_id = uuid4()
    with ThreadPoolExecutor(max_workers=2) as executor:
        retries = list(
            executor.map(
                lambda stage: consume_receipt(stage, retry_receipt, retry_handoff_id),
                (retry_first, retry_second),
            )
        )
    assert all(outcome == "ok" for outcome in retries)


def consume_receipt(stage: LocalMediaStage, receipt, handoff_id) -> str:  # type: ignore[no-untyped-def]
    """Return an observable result for concurrent identical-handoff retries."""
    stage.consume(receipt_id=receipt.receipt_id, handoff_id=handoff_id)
    return "ok"


def test_stage_rejects_symlinked_media_root_without_chmod_following(
    tmp_path: Path,
) -> None:
    """Following a media-root symlink would change permissions outside the store."""
    external = tmp_path / "external"
    external.mkdir(mode=0o755)
    root = tmp_path / "private-stage"
    root.mkdir(mode=0o700)
    (root / "media").symlink_to(external, target_is_directory=True)
    stage = build_stage(tmp_path)
    body = image_bytes()

    assert_domain_error(
        "local_media_storage_unsafe",
        lambda: stage.stage(
            photo_id=uuid4(),
            claimed_sha256=hashlib.sha256(body).hexdigest(),
            body=io.BytesIO(body),
        ),
    )
    assert stat.S_IMODE(external.stat().st_mode) == 0o755


def test_cleanup_does_not_follow_a_receipt_directory_symlink(tmp_path: Path) -> None:
    """Cleanup must report pending work rather than unlink an external matching file."""
    started = datetime(2026, 8, 31, tzinfo=UTC)
    stage = build_stage(tmp_path, ttl=timedelta(seconds=1), now=started)
    receipt = stage_png(stage)
    path = stage.resolve(
        receipt_id=receipt.receipt_id,
        photo_id=receipt.photo_id,
        sha256=receipt.content_sha256,
    )
    external = tmp_path / "external"
    external.mkdir()
    external_file = external / path.name
    external_file.write_bytes(path.read_bytes())
    path.unlink()
    path.parent.rmdir()
    path.parent.symlink_to(external, target_is_directory=True)

    assert stage.cleanup_expired(now=started + timedelta(seconds=1)) == (
        str(receipt.receipt_id),
    )
    assert external_file.exists()


def test_expired_and_consumed_receipts_are_cleaned_and_can_be_restaged(
    tmp_path: Path,
) -> None:
    """A historical receipt must not permanently block a fresh short-lived delivery."""
    current = [datetime(2026, 8, 31, tzinfo=UTC)]
    stage = build_stage(tmp_path, ttl=timedelta(seconds=1), now=lambda: current[0])
    photo_id = uuid4()
    receipt = stage_png(stage, photo_id)
    stage.consume(receipt_id=receipt.receipt_id, handoff_id=uuid4())
    current[0] += timedelta(seconds=1)

    assert_domain_error(
        "local_media_receipt_expired",
        lambda: stage.consume(receipt_id=receipt.receipt_id, handoff_id=uuid4()),
    )
    assert stage.cleanup_expired(now=current[0]) == ()
    replacement = stage_png(stage, photo_id)
    assert replacement.receipt_id != receipt.receipt_id


def test_rejects_dimension_pixel_and_animated_frame_bombs(tmp_path: Path) -> None:
    """Checking only byte size loads images whose decoded resource cost is unbounded."""
    stage = build_stage(tmp_path, max_width=8, max_height=8, max_pixels=64)
    oversized = image_bytes(size=(9, 8))
    assert_domain_error(
        "local_media_decode_limits_exceeded",
        lambda: stage.stage(
            photo_id=uuid4(),
            claimed_sha256=hashlib.sha256(oversized).hexdigest(),
            body=io.BytesIO(oversized),
        ),
    )
    animated = io.BytesIO()
    frame = Image.new("RGB", (4, 4), color=(1, 2, 3))
    frame.save(animated, format="PNG", save_all=True, append_images=[frame])
    data = animated.getvalue()
    assert_domain_error(
        "local_media_decode_limits_exceeded",
        lambda: stage.stage(
            photo_id=uuid4(),
            claimed_sha256=hashlib.sha256(data).hexdigest(),
            body=io.BytesIO(data),
        ),
    )


def test_rejects_jpeg_with_payload_before_a_second_end_marker(tmp_path: Path) -> None:
    """Checking only final bytes accepts a valid JPEG with an appended payload."""
    stage = build_stage(tmp_path)
    data = image_bytes("JPEG") + b"<script>payload</script>\xff\xd9"

    assert_domain_error(
        "local_media_decode_invalid",
        lambda: stage.stage(
            photo_id=uuid4(),
            claimed_sha256=hashlib.sha256(data).hexdigest(),
            body=io.BytesIO(data),
        ),
    )


def test_publish_activation_failure_is_journaled_and_leaves_no_orphan_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash after linking but before activation must remain recoverable and non-orphaned."""
    stage = build_stage(tmp_path)
    body = image_bytes()

    def interrupt_activation(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated database activation interruption")

    monkeypatch.setattr(stage, "_activate_receipt", interrupt_activation)
    assert_domain_error(
        "local_media_receipt_record_failed",
        lambda: stage.stage(
            photo_id=uuid4(),
            claimed_sha256=hashlib.sha256(body).hexdigest(),
            body=io.BytesIO(body),
        ),
    )
    media_root = tmp_path / "private-stage" / "media"
    assert not list(media_root.rglob("*.media"))
    assert not list(media_root.rglob(".pending-*"))


def test_rejects_png_payload_before_a_fake_second_iend(tmp_path: Path) -> None:
    """A suffix check alone accepts a real PNG followed by payload and fake IEND."""
    stage = build_stage(tmp_path)
    data = image_bytes() + b"payloadIEND\xaeB`\x82"

    assert_domain_error(
        "local_media_decode_invalid",
        lambda: stage.stage(
            photo_id=uuid4(),
            claimed_sha256=hashlib.sha256(data).hexdigest(),
            body=io.BytesIO(data),
        ),
    )


def test_unknown_receipt_does_not_poison_the_next_transaction(tmp_path: Path) -> None:
    """Returning from a failed lookup must roll back its SQLite transaction."""
    stage = build_stage(tmp_path)
    assert_domain_error(
        "local_media_receipt_unavailable",
        lambda: stage.consume(receipt_id=uuid4(), handoff_id=uuid4()),
    )
    assert stage_png(stage).photo_id is not None


def test_competing_instances_stage_one_receipt_for_the_same_photo(
    tmp_path: Path,
) -> None:
    """Dropping the namespace lock would let two instances mint live receipts."""
    first = build_stage(tmp_path)
    second = build_stage(tmp_path)
    photo_id = uuid4()
    body = image_bytes()
    digest = hashlib.sha256(body).hexdigest()

    with ThreadPoolExecutor(max_workers=2) as executor:
        receipts = list(
            executor.map(
                lambda stage: stage.stage(
                    photo_id=photo_id,
                    claimed_sha256=digest,
                    body=io.BytesIO(body),
                ),
                (first, second),
            )
        )

    assert receipts[0] == receipts[1]
    assert len(list((tmp_path / "private-stage" / "media").rglob("*.media"))) == 1


def test_new_instance_cannot_recover_another_instances_live_pending_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recovery before the namespace lock can erase an in-flight publication."""
    first = build_stage(tmp_path)
    second = build_stage(tmp_path)
    photo_id = uuid4()
    body = image_bytes()
    digest = hashlib.sha256(body).hexdigest()
    publish_entered = Event()
    release_publish = Event()
    second_started = Event()
    recovery_called = Event()
    original_publish = first._publish
    original_recovery = second._recover_pending

    def delayed_publish(receipt, content) -> None:  # type: ignore[no-untyped-def]
        publish_entered.set()
        assert release_publish.wait(timeout=3)
        original_publish(receipt, content)

    def observed_recovery() -> None:
        recovery_called.set()
        original_recovery()

    def stage_with_second() -> object:
        second_started.set()
        return second.stage(
            photo_id=photo_id,
            claimed_sha256=digest,
            body=io.BytesIO(body),
        )

    monkeypatch.setattr(first, "_publish", delayed_publish)
    monkeypatch.setattr(second, "_recover_pending", observed_recovery)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_result = executor.submit(
            first.stage,
            photo_id=photo_id,
            claimed_sha256=digest,
            body=io.BytesIO(body),
        )
        assert publish_entered.wait(timeout=3)
        second_result = executor.submit(stage_with_second)
        assert second_started.wait(timeout=3)
        recovered_while_live = recovery_called.wait(timeout=0.2)
        release_publish.set()
        first_receipt = first_result.result(timeout=3)
        second_receipt = second_result.result(timeout=3)

    assert not recovered_while_live
    assert second_receipt == first_receipt


def test_failed_publication_never_deletes_a_preexisting_target(tmp_path: Path) -> None:
    """Rollback must not unlink a destination inode this receipt never created."""
    photo_id = uuid4()
    body = image_bytes()
    digest = hashlib.sha256(body).hexdigest()
    photo_root = tmp_path / "private-stage" / "media" / str(photo_id)
    photo_root.mkdir(mode=0o700, parents=True)
    target = photo_root / f"{digest}.media"
    target.write_bytes(b"preexisting-unowned-sentinel")
    target.chmod(0o600)
    stage = build_stage(tmp_path)

    assert_domain_error(
        "local_media_write_failed",
        lambda: stage.stage(
            photo_id=photo_id,
            claimed_sha256=digest,
            body=io.BytesIO(body),
        ),
    )

    assert target.read_bytes() == b"preexisting-unowned-sentinel"


def test_media_directory_swap_fails_closed_without_writing_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reopening media by path after validation can publish into a swapped tree."""
    root = tmp_path / "private-stage"
    moved_media = root / "media-original"
    replacement_media = root / "media"
    replacement_marker = replacement_media / "foreign-marker"

    def swap_media_directory(_root: Path) -> None:
        replacement_media.rename(moved_media)
        replacement_media.mkdir(mode=0o700)
        replacement_marker.write_bytes(b"foreign")

    monkeypatch.setattr(
        local_media_stage,
        "_after_media_directory_verified_before_publish",
        swap_media_directory,
        raising=False,
    )
    stage = build_stage(tmp_path)
    body = image_bytes()

    assert_domain_error(
        "local_media_storage_unsafe",
        lambda: stage.stage(
            photo_id=uuid4(),
            claimed_sha256=hashlib.sha256(body).hexdigest(),
            body=io.BytesIO(body),
        ),
    )

    assert replacement_marker.read_bytes() == b"foreign"
    assert not list(replacement_media.rglob("*.media"))


def test_replacing_namespace_lock_fails_before_receipt_mutation(tmp_path: Path) -> None:
    """Reopening a replaced lock would split one namespace into two lock domains."""
    stage = build_stage(tmp_path)
    original_receipt = stage_png(stage)
    root = tmp_path / "private-stage"
    lock_path = root / ".namespace.lock"
    lock_path.rename(root / ".namespace.lock-original")
    lock_path.write_bytes(b"replacement")
    lock_path.chmod(0o600)
    body = image_bytes("JPEG")

    assert_domain_error(
        "local_media_storage_unsafe",
        lambda: stage.stage(
            photo_id=uuid4(),
            claimed_sha256=hashlib.sha256(body).hexdigest(),
            body=io.BytesIO(body),
        ),
    )

    with sqlite3.connect(root / "receipts.sqlite3") as connection:
        active_ids = {
            row[0]
            for row in connection.execute(
                "SELECT receipt_id FROM media_receipts WHERE status='active'"
            )
        }
    assert active_ids == {str(original_receipt.receipt_id)}


@pytest.mark.parametrize("entry_name", [".namespace.lock", "receipts.sqlite3"])
def test_namespace_control_hardlink_is_rejected_without_mutating_its_source(
    tmp_path: Path, entry_name: str
) -> None:
    """Identity rejection must happen before chmod can alter a foreign hardlink."""
    root = tmp_path / "private-stage"
    root.mkdir(mode=0o700)
    external = tmp_path / "foreign-control-file"
    external.write_bytes(b"foreign-control-file")
    external.chmod(0o644)
    os.link(external, root / entry_name)
    stage = build_stage(tmp_path)
    body = image_bytes()

    assert_domain_error(
        "local_media_storage_unsafe",
        lambda: stage.stage(
            photo_id=uuid4(),
            claimed_sha256=hashlib.sha256(body).hexdigest(),
            body=io.BytesIO(body),
        ),
    )

    assert external.read_bytes() == b"foreign-control-file"
    assert stat.S_IMODE(external.stat().st_mode) == 0o644


@pytest.mark.parametrize("replacement_kind", ["regular", "symlink"])
def test_replacing_database_entry_fails_before_consume_mutation(
    tmp_path: Path, replacement_kind: str
) -> None:
    """A retained SQLite connection must not hide replacement of its path entry."""
    stage = build_stage(tmp_path)
    receipt = stage_png(stage)
    root = tmp_path / "private-stage"
    database = root / "receipts.sqlite3"
    original_database = root / "receipts-original.sqlite3"
    database.rename(original_database)
    replacement_database = root / "replacement.sqlite3"
    shutil.copyfile(original_database, replacement_database)
    replacement_database.chmod(0o600)
    if replacement_kind == "regular":
        shutil.copyfile(replacement_database, database)
        database.chmod(0o600)
    else:
        database.symlink_to(replacement_database)

    assert_domain_error(
        "local_media_storage_unsafe",
        lambda: stage.consume(receipt_id=receipt.receipt_id, handoff_id=uuid4()),
    )

    with sqlite3.connect(original_database) as connection:
        status_value = connection.execute(
            "SELECT status FROM media_receipts WHERE receipt_id=?",
            (str(receipt.receipt_id),),
        ).fetchone()
    assert status_value == ("active",)


def test_failed_media_verification_does_not_consume_receipt(tmp_path: Path) -> None:
    """Binding before byte verification turns a repairable receipt into a replay."""
    stage = build_stage(tmp_path)
    receipt = stage_png(stage)
    path = stage.resolve(
        receipt_id=receipt.receipt_id,
        photo_id=receipt.photo_id,
        sha256=receipt.content_sha256,
    )
    original = path.read_bytes()
    path.write_bytes(b"corrupt")

    assert_domain_error(
        "local_media_storage_unsafe",
        lambda: stage.consume(receipt_id=receipt.receipt_id, handoff_id=uuid4()),
    )

    path.write_bytes(original)
    replacement_handoff = uuid4()
    assert (
        stage.consume(
            receipt_id=receipt.receipt_id,
            handoff_id=replacement_handoff,
        )
        == path
    )


def test_cross_instance_resolve_rejects_same_bytes_on_a_replaced_inode(
    tmp_path: Path,
) -> None:
    """Hash-only verification cannot distinguish a substituted media inode."""
    first = build_stage(tmp_path)
    receipt = stage_png(first)
    path = first.resolve(
        receipt_id=receipt.receipt_id,
        photo_id=receipt.photo_id,
        sha256=receipt.content_sha256,
    )
    original = path.read_bytes()
    path.unlink()
    path.write_bytes(original)
    path.chmod(0o600)
    second = build_stage(tmp_path)

    assert_domain_error(
        "local_media_storage_unsafe",
        lambda: second.resolve(
            receipt_id=receipt.receipt_id,
            photo_id=receipt.photo_id,
            sha256=receipt.content_sha256,
        ),
    )


def test_manifest_parse_failure_rolls_back_and_allows_a_later_repair(
    tmp_path: Path,
) -> None:
    """A malformed row must not leak ValueError or poison the next transaction."""
    stage = build_stage(tmp_path)
    receipt = stage_png(stage)
    database = tmp_path / "private-stage" / "receipts.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE media_receipts SET width='not-an-integer' WHERE receipt_id=?",
            (str(receipt.receipt_id),),
        )

    assert_domain_error(
        "local_media_manifest_invalid",
        lambda: stage.consume(receipt_id=receipt.receipt_id, handoff_id=uuid4()),
    )

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE media_receipts SET width=? WHERE receipt_id=?",
            (receipt.width, str(receipt.receipt_id)),
        )
    assert (
        stage.consume(
            receipt_id=receipt.receipt_id,
            handoff_id=uuid4(),
        ).read_bytes()
        == image_bytes()
    )


def test_cleanup_maps_a_malformed_manifest_receipt_id_to_a_domain_error(
    tmp_path: Path,
) -> None:
    """Cleanup must fail closed rather than leaking UUID parser exceptions."""
    current = datetime(2026, 8, 31, tzinfo=UTC)
    stage = build_stage(tmp_path, ttl=timedelta(seconds=1), now=current)
    receipt = stage_png(stage)
    database = tmp_path / "private-stage" / "receipts.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE media_receipts SET receipt_id='not-a-uuid' WHERE receipt_id=?",
            (str(receipt.receipt_id),),
        )

    assert_domain_error(
        "local_media_manifest_invalid",
        lambda: stage.cleanup_expired(now=current + timedelta(seconds=1)),
    )


def test_accepts_a_valid_progressive_jpeg_with_an_exact_endpoint(
    tmp_path: Path,
) -> None:
    """A parser that assumes one scan rejects a supported progressive JPEG."""
    output = io.BytesIO()
    Image.new("RGB", (32, 24), color=(20, 40, 60)).save(
        output,
        format="JPEG",
        progressive=True,
    )
    body = output.getvalue()
    stage = build_stage(tmp_path)

    receipt = stage.stage(
        photo_id=uuid4(),
        claimed_sha256=hashlib.sha256(body).hexdigest(),
        body=io.BytesIO(body),
    )

    assert (receipt.mime_type, receipt.width, receipt.height) == (
        "image/jpeg",
        32,
        24,
    )


def _create_manifest_table(connection: sqlite3.Connection, version: int) -> None:
    storage_column = ", storage_relpath TEXT NOT NULL" if version == 1 else ""
    journal_columns = (
        """
        , storage_dev INTEGER
        , storage_ino INTEGER
        , pending_owner TEXT
        , pending_expires_at TEXT
        , pending_name TEXT
        , pending_dev INTEGER
        , pending_ino INTEGER
        """
        if version == 3
        else ""
    )
    connection.execute(
        f"""
        CREATE TABLE media_receipts (
            receipt_id TEXT PRIMARY KEY,
            photo_id TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            mime_type TEXT NOT NULL,
            width INTEGER NOT NULL,
            height INTEGER NOT NULL,
            expires_at TEXT NOT NULL
            {storage_column},
            status TEXT NOT NULL,
            handoff_id TEXT
            {journal_columns}
        )
        """
    )
    connection.execute(f"PRAGMA user_version={version}")


@pytest.mark.parametrize("legacy_version", [1, 2])
def test_legacy_manifest_migration_preserves_active_receipt_and_records_identity(
    tmp_path: Path, legacy_version: int
) -> None:
    """Dropping v1/v2 rows during schema hardening would invalidate live receipts."""
    root = tmp_path / "private-stage"
    root.mkdir(mode=0o700)
    photo_id = uuid4()
    receipt_id = uuid4()
    body = image_bytes()
    digest = hashlib.sha256(body).hexdigest()
    photo_root = root / "media" / str(photo_id)
    photo_root.mkdir(mode=0o700, parents=True)
    target = photo_root / f"{digest}.media"
    target.write_bytes(body)
    target.chmod(0o600)
    database = root / "receipts.sqlite3"
    with sqlite3.connect(database) as connection:
        _create_manifest_table(connection, legacy_version)
        columns = (
            "receipt_id, photo_id, content_sha256, mime_type, width, height, "
            "expires_at, storage_relpath, status, handoff_id"
            if legacy_version == 1
            else "receipt_id, photo_id, content_sha256, mime_type, width, height, "
            "expires_at, status, handoff_id"
        )
        values: tuple[object, ...] = (
            str(receipt_id),
            str(photo_id),
            digest,
            "image/png",
            8,
            6,
            datetime(2026, 8, 31, 0, 5, tzinfo=UTC).isoformat(),
        )
        if legacy_version == 1:
            values += (f"media/{photo_id}/{digest}.media",)
        values += ("active", None)
        placeholders = ",".join("?" for _ in values)
        connection.execute(
            f"INSERT INTO media_receipts ({columns}) VALUES ({placeholders})",
            values,
        )
    database.chmod(0o600)
    stage = build_stage(tmp_path)

    resolved = stage.resolve(
        receipt_id=receipt_id,
        photo_id=photo_id,
        sha256=digest,
    )
    assert resolved.read_bytes() == body
    stage.close()

    with sqlite3.connect(database) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(media_receipts)")
        }
        stored_identity = connection.execute(
            "SELECT storage_dev, storage_ino FROM media_receipts WHERE receipt_id=?",
            (str(receipt_id),),
        ).fetchone()
    target_stat = target.stat()
    assert version == 3
    assert {
        "storage_dev",
        "storage_ino",
        "pending_owner",
        "pending_expires_at",
        "pending_name",
        "pending_dev",
        "pending_ino",
    } <= columns
    assert stored_identity == (target_stat.st_dev, target_stat.st_ino)


def test_future_manifest_version_fails_closed_without_downgrade(tmp_path: Path) -> None:
    """An older process must never reinterpret or overwrite a future manifest."""
    root = tmp_path / "private-stage"
    (root / "media").mkdir(mode=0o700, parents=True)
    database = root / "receipts.sqlite3"
    with sqlite3.connect(database) as connection:
        _create_manifest_table(connection, 3)
        connection.execute("PRAGMA user_version=99")
    database.chmod(0o600)
    stage = build_stage(tmp_path)
    body = image_bytes()

    assert_domain_error(
        "local_media_manifest_version_unsupported",
        lambda: stage.stage(
            photo_id=uuid4(),
            claimed_sha256=hashlib.sha256(body).hexdigest(),
            body=io.BytesIO(body),
        ),
    )

    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (99,)
        assert connection.execute("SELECT COUNT(*) FROM media_receipts").fetchone() == (
            0,
        )


def _create_pending_v3_receipt(
    tmp_path: Path,
    *,
    lease_expires_at: datetime,
) -> tuple[Path, Path, UUID, datetime]:
    root = tmp_path / "private-stage"
    root.mkdir(mode=0o700)
    photo_id = uuid4()
    receipt_id = uuid4()
    body = image_bytes()
    digest = hashlib.sha256(body).hexdigest()
    photo_root = root / "media" / str(photo_id)
    photo_root.mkdir(mode=0o700, parents=True)
    pending_name = f".pending-{receipt_id}-journaled"
    pending = photo_root / pending_name
    pending.write_bytes(body)
    pending.chmod(0o600)
    pending_stat = pending.stat()
    database = root / "receipts.sqlite3"
    with sqlite3.connect(database) as connection:
        _create_manifest_table(connection, 3)
        connection.execute(
            """
            INSERT INTO media_receipts (
                receipt_id, photo_id, content_sha256, mime_type, width, height,
                expires_at, status, handoff_id, storage_dev, storage_ino,
                pending_owner, pending_expires_at, pending_name,
                pending_dev, pending_ino
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?, ?, ?, ?)
            """,
            (
                str(receipt_id),
                str(photo_id),
                digest,
                "image/png",
                8,
                6,
                (lease_expires_at + timedelta(minutes=5)).isoformat(),
                "pending_publish",
                "foreign-live-owner",
                lease_expires_at.isoformat(),
                pending_name,
                pending_stat.st_dev,
                pending_stat.st_ino,
            ),
        )
    database.chmod(0o600)
    return database, pending, receipt_id, lease_expires_at


def test_foreign_pending_lease_is_not_recovered_until_it_expires(
    tmp_path: Path,
) -> None:
    """A live pending owner must not lose its exact journaled temporary file."""
    now = datetime(2026, 8, 31, tzinfo=UTC)
    current = [now]
    database, pending, receipt_id, lease_end = _create_pending_v3_receipt(
        tmp_path,
        lease_expires_at=now + timedelta(seconds=30),
    )
    stage = build_stage(tmp_path, now=lambda: current[0])

    assert stage.cleanup_expired(now=current[0]) == ()
    assert pending.exists()
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT status FROM media_receipts WHERE receipt_id=?",
            (str(receipt_id),),
        ).fetchone() == ("pending_publish",)

    current[0] = lease_end + timedelta(microseconds=1)
    assert stage.cleanup_expired(now=current[0]) == ()
    assert not pending.exists()
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT status FROM media_receipts WHERE receipt_id=?",
            (str(receipt_id),),
        ).fetchone() == ("failed",)


def test_live_pending_receipt_blocks_a_second_stage_for_the_same_photo(
    tmp_path: Path,
) -> None:
    """Respecting a lease must also prevent a second receipt for its photo."""
    now = datetime(2026, 8, 31, tzinfo=UTC)
    database, pending, receipt_id, _ = _create_pending_v3_receipt(
        tmp_path,
        lease_expires_at=now + timedelta(seconds=30),
    )
    with sqlite3.connect(database) as connection:
        photo_id = UUID(
            connection.execute(
                "SELECT photo_id FROM media_receipts WHERE receipt_id=?",
                (str(receipt_id),),
            ).fetchone()[0]
        )
    stage = build_stage(tmp_path, now=now)
    body = image_bytes()

    assert_domain_error(
        "local_media_receipt_pending",
        lambda: stage.stage(
            photo_id=photo_id,
            claimed_sha256=hashlib.sha256(body).hexdigest(),
            body=io.BytesIO(body),
        ),
    )

    assert pending.exists()
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM media_receipts WHERE photo_id=?",
            (str(photo_id),),
        ).fetchone() == (1,)


def test_unexpired_consumed_receipt_blocks_changed_bytes_for_the_same_photo(
    tmp_path: Path,
) -> None:
    """Restaging changed bytes must not bypass one-handoff receipt consumption."""
    stage = build_stage(tmp_path)
    photo_id = uuid4()
    receipt = stage_png(stage, photo_id)
    stage.consume(receipt_id=receipt.receipt_id, handoff_id=uuid4())
    changed = image_bytes("JPEG")

    assert_domain_error(
        "local_media_receipt_conflict",
        lambda: stage.stage(
            photo_id=photo_id,
            claimed_sha256=hashlib.sha256(changed).hexdigest(),
            body=io.BytesIO(changed),
        ),
    )

    with sqlite3.connect(tmp_path / "private-stage" / "receipts.sqlite3") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM media_receipts WHERE photo_id=?",
            (str(photo_id),),
        ).fetchone() == (1,)


def test_pending_recovery_cleans_only_the_exact_journaled_temporary(
    tmp_path: Path,
) -> None:
    """Prefix scans can delete a foreign file or leave the owned temp orphaned."""
    now = datetime(2026, 8, 31, tzinfo=UTC)
    _, pending, _, _ = _create_pending_v3_receipt(
        tmp_path,
        lease_expires_at=now - timedelta(seconds=1),
    )
    unrelated = pending.parent / ".pending-unrelated"
    unrelated.write_bytes(b"must-remain")
    unrelated.chmod(0o600)
    stage = build_stage(tmp_path, now=now)

    assert stage.cleanup_expired(now=now) == ()
    assert not pending.exists()
    assert unrelated.read_bytes() == b"must-remain"


def test_recovery_preserves_nonempty_pending_when_inode_was_never_journaled(
    tmp_path: Path,
) -> None:
    """The empty-temp crash rule must not authorize deleting substituted bytes."""
    now = datetime(2026, 8, 31, tzinfo=UTC)
    database, pending, receipt_id, _ = _create_pending_v3_receipt(
        tmp_path,
        lease_expires_at=now - timedelta(seconds=1),
    )
    original = pending.read_bytes()
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE media_receipts SET pending_dev=NULL, pending_ino=NULL"
        )
    stage = build_stage(tmp_path, now=now)

    assert stage.cleanup_expired(now=now) == (str(receipt_id),)
    assert pending.read_bytes() == original


def test_interrupted_temp_cleanup_is_recoverable_without_an_orphan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The journal must retain the exact temp identity when unlink is interrupted."""
    current = [datetime(2026, 8, 31, tzinfo=UTC)]
    stage = build_stage(tmp_path, now=lambda: current[0])
    body = image_bytes()
    original_unlink = os.unlink

    def refuse_pending(name, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        if Path(os.fspath(name)).name.startswith(".pending-"):
            raise OSError("simulated pending unlink interruption")
        original_unlink(name, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(local_media_stage.os, "unlink", refuse_pending)
        assert_domain_error(
            "local_media_write_failed",
            lambda: stage.stage(
                photo_id=uuid4(),
                claimed_sha256=hashlib.sha256(body).hexdigest(),
                body=io.BytesIO(body),
            ),
        )
    media_root = tmp_path / "private-stage" / "media"
    owned_pending = list(media_root.rglob(".pending-*"))
    assert len(owned_pending) == 1
    unrelated = owned_pending[0].parent / ".pending-unrelated"
    unrelated.write_bytes(b"foreign")
    unrelated.chmod(0o600)
    stage.close()
    current[0] += timedelta(minutes=6)
    replacement = build_stage(tmp_path, now=lambda: current[0])

    assert replacement.cleanup_expired(now=current[0]) == ()
    assert not owned_pending[0].exists()
    assert unrelated.read_bytes() == b"foreign"
    assert not list(media_root.rglob("*.media"))


def test_process_death_after_link_recovers_both_journaled_inode_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recovery must remove both names when a crash leaves one owned inode linked twice."""
    current = [datetime(2026, 8, 31, tzinfo=UTC)]
    stage = build_stage(tmp_path, now=lambda: current[0])
    body = image_bytes()

    class SimulatedProcessDeath(BaseException):
        pass

    def die_after_target_link(*_args: object, **_kwargs: object) -> None:
        raise SimulatedProcessDeath

    with monkeypatch.context() as patch:
        patch.setattr(
            local_media_stage,
            "_unlink_if_identity_matches",
            die_after_target_link,
        )
        with pytest.raises(SimulatedProcessDeath):
            stage.stage(
                photo_id=uuid4(),
                claimed_sha256=hashlib.sha256(body).hexdigest(),
                body=io.BytesIO(body),
            )

    media_root = tmp_path / "private-stage" / "media"
    pending = next(media_root.rglob(".pending-*"))
    target = next(media_root.rglob("*.media"))
    assert pending.stat().st_ino == target.stat().st_ino
    stage.close()
    current[0] += timedelta(minutes=6)
    replacement = build_stage(tmp_path, now=lambda: current[0])

    assert replacement.cleanup_expired(now=current[0]) == ()
    assert not pending.exists()
    assert not target.exists()


def test_process_death_after_pending_unlink_recovers_lone_owned_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash before activation must not count an already-unlinked temp as a link."""
    current = [datetime(2026, 8, 31, tzinfo=UTC)]
    stage = build_stage(tmp_path, now=lambda: current[0])
    body = image_bytes()

    class SimulatedProcessDeath(BaseException):
        pass

    def die_before_activation(*_args: object, **_kwargs: object) -> None:
        raise SimulatedProcessDeath

    monkeypatch.setattr(stage, "_activate_receipt", die_before_activation)
    with pytest.raises(SimulatedProcessDeath):
        stage.stage(
            photo_id=uuid4(),
            claimed_sha256=hashlib.sha256(body).hexdigest(),
            body=io.BytesIO(body),
        )

    media_root = tmp_path / "private-stage" / "media"
    target = next(media_root.rglob("*.media"))
    assert not list(media_root.rglob(".pending-*"))
    stage.close()
    current[0] += timedelta(minutes=6)
    replacement = build_stage(tmp_path, now=lambda: current[0])

    assert replacement.cleanup_expired(now=current[0]) == ()
    assert not target.exists()


def test_process_death_before_inode_journal_recovers_exact_empty_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pre-journal crash window must not strand its exact empty temp forever."""
    current = [datetime(2026, 8, 31, tzinfo=UTC)]
    stage = build_stage(tmp_path, now=lambda: current[0])
    body = image_bytes()

    class SimulatedProcessDeath(BaseException):
        pass

    def die_before_identity_record(*_args: object, **_kwargs: object) -> None:
        raise SimulatedProcessDeath

    monkeypatch.setattr(stage, "_record_pending_identity", die_before_identity_record)
    with pytest.raises(SimulatedProcessDeath):
        stage.stage(
            photo_id=uuid4(),
            claimed_sha256=hashlib.sha256(body).hexdigest(),
            body=io.BytesIO(body),
        )

    pending = next((tmp_path / "private-stage" / "media").rglob(".pending-*"))
    assert pending.stat().st_size == 0
    stage.close()
    current[0] += timedelta(minutes=6)
    replacement = build_stage(tmp_path, now=lambda: current[0])

    assert replacement.cleanup_expired(now=current[0]) == ()
    assert not pending.exists()


def test_activation_rechecks_media_identity_and_preserves_a_swapped_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A target swapped after verification must not be activated or deleted."""
    root = tmp_path / "private-stage"
    swapped_path: list[Path] = []

    def replace_target(_root: Path) -> None:
        target = next((root / "media").rglob("*.media"))
        target.unlink()
        target.write_bytes(b"foreign-after-verification")
        target.chmod(0o600)
        swapped_path.append(target)

    monkeypatch.setattr(
        local_media_stage,
        "_after_storage_verification_before_activation",
        replace_target,
        raising=False,
    )
    stage = build_stage(tmp_path)
    body = image_bytes()

    assert_domain_error(
        "local_media_storage_unsafe",
        lambda: stage.stage(
            photo_id=uuid4(),
            claimed_sha256=hashlib.sha256(body).hexdigest(),
            body=io.BytesIO(body),
        ),
    )

    assert swapped_path[0].read_bytes() == b"foreign-after-verification"
    with sqlite3.connect(root / "receipts.sqlite3") as connection:
        assert connection.execute("SELECT status FROM media_receipts").fetchone() == (
            "cleanup_pending",
        )


def test_stage_rolls_back_when_its_pending_lease_expires_before_activation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A publication must not activate after its durable owner lease has expired."""
    current = [datetime(2026, 8, 31, tzinfo=UTC)]
    stage = build_stage(tmp_path, now=lambda: current[0])
    body = image_bytes()

    def expire_lease(_root: Path) -> None:
        current[0] += timedelta(minutes=6)

    monkeypatch.setattr(
        local_media_stage,
        "_after_storage_verification_before_activation",
        expire_lease,
    )

    assert_domain_error(
        "local_media_receipt_record_failed",
        lambda: stage.stage(
            photo_id=uuid4(),
            claimed_sha256=hashlib.sha256(body).hexdigest(),
            body=io.BytesIO(body),
        ),
    )
    media_root = tmp_path / "private-stage" / "media"
    assert not list(media_root.rglob("*.media"))
    assert not list(media_root.rglob(".pending-*"))


def test_consume_rechecks_media_identity_before_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A same-content inode swap after verification must not bind the receipt."""
    stage = build_stage(tmp_path)
    receipt = stage_png(stage)
    path = stage.resolve(
        receipt_id=receipt.receipt_id,
        photo_id=receipt.photo_id,
        sha256=receipt.content_sha256,
    )
    original = path.read_bytes()

    def replace_target(_root: Path) -> None:
        path.unlink()
        path.write_bytes(original)
        path.chmod(0o600)

    monkeypatch.setattr(
        local_media_stage,
        "_after_media_verification_before_consume",
        replace_target,
        raising=False,
    )

    assert_domain_error(
        "local_media_storage_unsafe",
        lambda: stage.consume(receipt_id=receipt.receipt_id, handoff_id=uuid4()),
    )
    with sqlite3.connect(tmp_path / "private-stage" / "receipts.sqlite3") as connection:
        assert connection.execute(
            "SELECT status FROM media_receipts WHERE receipt_id=?",
            (str(receipt.receipt_id),),
        ).fetchone() == ("active",)


def test_cleanup_reports_an_untracked_hardlink_without_deleting_owned_media(
    tmp_path: Path,
) -> None:
    """Unlinking one name must not claim byte cleanup while another link exists."""
    started = datetime(2026, 8, 31, tzinfo=UTC)
    stage = build_stage(tmp_path, ttl=timedelta(seconds=1), now=started)
    receipt = stage_png(stage)
    path = stage.resolve(
        receipt_id=receipt.receipt_id,
        photo_id=receipt.photo_id,
        sha256=receipt.content_sha256,
    )
    outside = tmp_path / "untracked-hardlink.media"
    os.link(path, outside)

    assert stage.cleanup_expired(now=started + timedelta(seconds=1)) == (
        str(receipt.receipt_id),
    )
    assert path.exists()
    assert outside.exists()
    assert_domain_error(
        "local_media_receipt_unavailable",
        lambda: stage.resolve(
            receipt_id=receipt.receipt_id,
            photo_id=receipt.photo_id,
            sha256=receipt.content_sha256,
        ),
    )


def test_cleanup_retains_target_identity_when_only_external_hardlink_remains(
    tmp_path: Path,
) -> None:
    """A missing controlled target cannot prove its recorded inode has no links."""
    started = datetime(2026, 8, 31, tzinfo=UTC)
    stage = build_stage(tmp_path, ttl=timedelta(seconds=1), now=started)
    receipt = stage_png(stage)
    target = stage.resolve(
        receipt_id=receipt.receipt_id,
        photo_id=receipt.photo_id,
        sha256=receipt.content_sha256,
    )
    identity = (target.stat().st_dev, target.stat().st_ino)
    outside = tmp_path / "only-external-target-link.media"
    os.link(target, outside)
    target.unlink()

    assert stage.cleanup_expired(now=started + timedelta(seconds=1)) == (
        str(receipt.receipt_id),
    )
    assert not target.exists()
    assert outside.exists()
    with sqlite3.connect(tmp_path / "private-stage" / "receipts.sqlite3") as connection:
        assert connection.execute(
            """
            SELECT status, storage_dev, storage_ino
              FROM media_receipts WHERE receipt_id=?
            """,
            (str(receipt.receipt_id),),
        ).fetchone() == ("cleanup_pending", *identity)


def test_cleanup_retains_pending_identity_when_only_external_hardlink_remains(
    tmp_path: Path,
) -> None:
    """A missing controlled temp cannot prove its recorded inode has no links."""
    now = datetime(2026, 8, 31, tzinfo=UTC)
    database, pending, receipt_id, _ = _create_pending_v3_receipt(
        tmp_path,
        lease_expires_at=now - timedelta(seconds=1),
    )
    identity = (pending.stat().st_dev, pending.stat().st_ino)
    outside = tmp_path / "only-external-pending-link.media"
    os.link(pending, outside)
    pending.unlink()
    stage = build_stage(tmp_path, now=now)

    assert stage.cleanup_expired(now=now) == (str(receipt_id),)
    assert not pending.exists()
    assert outside.exists()
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            """
            SELECT status, pending_dev, pending_ino
              FROM media_receipts WHERE receipt_id=?
            """,
            (str(receipt_id),),
        ).fetchone() == ("cleanup_pending", *identity)


def test_batch_consume_is_atomic_when_one_receipt_is_not_attestable(
    tmp_path: Path,
) -> None:
    """An invalid second receipt must not consume the first receipt."""
    stage = build_stage(tmp_path)
    first = stage_png(stage)
    second = stage_png(stage)
    handoff_id = uuid4()

    assert_domain_error(
        "local_media_receipt_mismatch",
        lambda: stage.consume_batch(
            receipts=(
                (first.receipt_id, first.photo_id, first.content_sha256),
                (second.receipt_id, second.photo_id, "0" * 64),
            ),
            handoff_id=handoff_id,
        ),
    )

    assert stage.resolve(
        receipt_id=first.receipt_id,
        photo_id=first.photo_id,
        sha256=first.content_sha256,
    ).is_file()


def test_bound_receipt_can_be_reattested_only_by_its_same_handoff(
    tmp_path: Path,
) -> None:
    stage = build_stage(tmp_path)
    receipt = stage_png(stage)
    handoff_id = uuid4()
    stage.consume_batch(
        receipts=((receipt.receipt_id, receipt.photo_id, receipt.content_sha256),),
        handoff_id=handoff_id,
    )

    assert stage.resolve_for_handoff(
        receipt_id=receipt.receipt_id,
        photo_id=receipt.photo_id,
        sha256=receipt.content_sha256,
        handoff_id=handoff_id,
    ).is_file()
    assert_domain_error(
        "local_media_receipt_replayed",
        lambda: stage.resolve_for_handoff(
            receipt_id=receipt.receipt_id,
            photo_id=receipt.photo_id,
            sha256=receipt.content_sha256,
            handoff_id=uuid4(),
        ),
    )
