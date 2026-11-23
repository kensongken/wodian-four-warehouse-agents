import asyncio
import fcntl
import io
import json
import multiprocessing
import os
import threading
from pathlib import Path

import pytest

from photo_organizer.catalog.mall_draft_adapter import (
    MallDraftAdapterError,
    execute_mall_draft_import_with_store,
)
from photo_organizer.catalog.mall_draft_execution_store import (
    LocalMallDraftExecutionStore,
)
from photo_organizer.catalog.mall_draft_gateway import (
    DraftImageSource,
    MallDraftCreateResult,
    MallDraftLookupResult,
)
from photo_organizer.schemas.mall_draft_adapter import (
    MallDraftImportItem,
    MallDraftImportPlan,
    MallDraftItemResult,
    plan_id_for_body,
)
from photo_organizer.schemas.mall_draft_export import MallProductDraftBatch

ROOT = Path(__file__).resolve().parents[4]
TEN_ITEM_BATCH = ROOT / "contracts/fixtures/opc-mall-product-draft-batch-synthetic.json"


class _MemoryStreams:
    def open_verified_image(self, **_: object):
        from contextlib import nullcontext

        return nullcontext(io.BytesIO(b"synthetic-image"))


class _CrashAfterCreateGateway:
    def __init__(self, ledger_path: Path) -> None:
        self._ledger_path = ledger_path

    async def lookup_by_source_album_product(
        self, **_: object
    ) -> MallDraftLookupResult:
        return MallDraftLookupResult(status="not_found")

    async def create_draft(
        self,
        *,
        idempotency_key: str,
        command: MallDraftImportItem,
        image_sources: tuple[DraftImageSource, ...],
    ) -> MallDraftCreateResult:
        for source in image_sources:
            with source.open() as image:
                image.read()
        self._ledger_path.write_text(
            json.dumps(
                {
                    "idempotency_key": idempotency_key,
                    "mall_product_id": "mall-draft-replayed",
                }
            )
        )
        os._exit(0)


class _IdempotentGateway:
    def __init__(self, ledger_path: Path, opc_id: str) -> None:
        self._ledger_path = ledger_path
        self._opc_id = opc_id
        self.create_call_count = 0

    async def lookup_by_source_album_product(
        self, **_: object
    ) -> MallDraftLookupResult:
        return MallDraftLookupResult(status="not_found")

    async def create_draft(
        self,
        *,
        idempotency_key: str,
        command: MallDraftImportItem,
        image_sources: tuple[DraftImageSource, ...],
    ) -> MallDraftCreateResult:
        self.create_call_count += 1
        for source in image_sources:
            with source.open() as image:
                image.read()
        ledger = json.loads(self._ledger_path.read_text())
        if ledger["idempotency_key"] != idempotency_key:
            raise AssertionError
        product_id = ledger["mall_product_id"]
        return MallDraftCreateResult(
            opc_id=self._opc_id,
            draft_request_id=idempotency_key,
            source_album_product_id=command.source_album_product_id,
            mall_product_id=product_id,
            publication_state="draft",
        )


def _run_crashing_execution(
    receipt_path: str, plan: MallDraftImportPlan, ledger_path: str
) -> None:
    from photo_organizer.catalog.mall_draft_adapter import (
        execute_mall_draft_import_with_store,
    )

    asyncio.run(
        execute_mall_draft_import_with_store(
            plan,
            _MemoryStreams(),
            _CrashAfterCreateGateway(Path(ledger_path)),
            LocalMallDraftExecutionStore(Path(receipt_path)),
        )
    )


def _hold_sibling_lock(lock_path: str, acquired: object, release: object) -> None:
    lock_file = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        acquired.set()
        release.wait(5)
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        os.close(lock_file)


def _plan() -> MallDraftImportPlan:
    batch = MallProductDraftBatch.model_validate_json(TEN_ITEM_BATCH.read_text())
    source = batch.items[0]
    item = MallDraftImportItem(
        draft_request_id=source.draft_request_id,
        source_acceptance_id=source.source_acceptance_id,
        source_group_confirmation_event_id=source.source_group_confirmation_event_id,
        source_album_product_id=source.source_album_product_id,
        source_snapshot_version=source.source_snapshot_version,
        source_type=source.source_type,
        classification=source.classification,
        images=source.images,
        required_business_fields=source.required_business_fields,
        material_authorization="opc_human_confirmed_for_draft_material_use",
        claims_not_evaluated=source.claims_not_evaluated,
        mall_product_id=None,
        bead_id=None,
        publication_state="draft",
        auto_publish=False,
        shared_market_opt_in=False,
    )
    body: dict[str, object] = {
        "schema_version": "opc-mall-draft-import-plan/1.0",
        "opc_id": batch.opc_id,
        "source_batch_id": batch.batch_id,
        "package_id": "standard-product-package:" + "a" * 64,
        "package_acceptance_id": "standard-package-acceptance:" + "b" * 64,
        "package_sha256": "c" * 64,
        "data_classification": batch.data_classification,
        "operation": "create_draft_only",
        "existing_product_policy": "change_proposal_required",
        "auto_publish": False,
        "shared_market_opt_in": False,
        "items": [item.model_dump(mode="json")],
    }
    return MallDraftImportPlan.model_validate(
        {"plan_id": plan_id_for_body(body), **body}
    )


def _retryable_result(
    plan: MallDraftImportPlan, generation: int
) -> MallDraftItemResult:
    return MallDraftItemResult(
        draft_request_id=plan.items[0].draft_request_id,
        attempt_generation=generation,
        status="retryable_failed",
        mall_product_id=None,
        error_code="gateway_unavailable",
    )


@pytest.mark.asyncio
async def test_rejects_a_stale_settle_after_a_new_generation_is_claimed(
    tmp_path: Path,
) -> None:
    """Catches a CAS bug that lets a prior runner overwrite a newer attempt."""
    plan = _plan()
    store = LocalMallDraftExecutionStore(tmp_path / "receipt.json")

    async with store.execution(plan) as first:
        assert first.next_generation(plan.items[0].draft_request_id) == 1
        await first.settle(_retryable_result(plan, 1))

    async with store.execution(plan) as retry:
        assert retry.next_generation(plan.items[0].draft_request_id) == 2
        with pytest.raises(MallDraftAdapterError) as raised:
            await retry.settle(_retryable_result(plan, 1))

    assert raised.value.code == "resume_state_conflict"


@pytest.mark.asyncio
async def test_malformed_receipt_fails_closed_before_a_transaction_is_opened(
    tmp_path: Path,
) -> None:
    """Catches accepting a corrupt on-disk receipt as a fresh execution."""
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text("{not-json")

    with pytest.raises(MallDraftAdapterError) as raised:
        async with LocalMallDraftExecutionStore(receipt_path).execution(_plan()):
            pass

    assert raised.value.code == "resume_state_conflict"


@pytest.mark.asyncio
async def test_semantically_corrupt_terminal_receipt_fails_closed_before_skip(
    tmp_path: Path,
) -> None:
    """Catches a noncompleted terminal receipt suppressing required execution."""
    plan = _plan()
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(
        json.dumps(
            {
                "schema_version": "opc-mall-draft-import-receipt/1.0",
                "plan_id": plan.plan_id,
                "opc_id": plan.opc_id,
                "execution_status": "in_progress",
                "item_count": 1,
                "results": [
                    {
                        "draft_request_id": plan.items[0].draft_request_id,
                        "attempt_generation": 1,
                        "status": "created",
                        "mall_product_id": "mall-draft-corrupt",
                        "error_code": None,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(MallDraftAdapterError) as raised:
        await execute_mall_draft_import_with_store(
            plan,
            _MemoryStreams(),
            object(),  # type: ignore[arg-type]
            LocalMallDraftExecutionStore(receipt_path),
        )

    assert raised.value.code == "resume_state_conflict"


@pytest.mark.asyncio
async def test_receipt_for_a_different_plan_fails_closed(tmp_path: Path) -> None:
    """Catches resuming a receipt whose plan binding does not match this run."""
    plan = _plan()
    store = LocalMallDraftExecutionStore(tmp_path / "receipt.json")
    async with store.execution(plan) as transaction:
        await transaction.settle(_retryable_result(plan, 1))
    different_plan = plan.model_copy(update={"opc_id": "opc-other"})

    with pytest.raises(MallDraftAdapterError) as raised:
        async with store.execution(different_plan):
            pass

    assert raised.value.code == "resume_state_conflict"


@pytest.mark.asyncio
async def test_output_failure_keeps_the_last_complete_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches replacing a valid receipt with a partial write when publishing fails."""
    plan = _plan()
    receipt_path = tmp_path / "receipt.json"
    store = LocalMallDraftExecutionStore(receipt_path)
    async with store.execution(plan) as transaction:
        await transaction.settle(_retryable_result(plan, 1))
    before = receipt_path.read_bytes()

    import photo_organizer.catalog.mall_draft_execution_store as store_module

    def fail_replace(source: object, destination: object) -> None:
        raise OSError("private local failure")

    monkeypatch.setattr(store_module.os, "replace", fail_replace)
    async with store.execution(plan) as transaction:
        with pytest.raises(MallDraftAdapterError) as raised:
            await transaction.settle(_retryable_result(plan, 2))

    assert raised.value.code == "output_unavailable"
    assert receipt_path.read_bytes() == before
    assert json.loads(before)["results"][0]["attempt_generation"] == 1
    assert not any(path.name.startswith(".mall-draft-") for path in tmp_path.iterdir())


@pytest.mark.asyncio
async def test_process_crash_after_creation_replays_the_same_idempotency_key(
    tmp_path: Path,
) -> None:
    """Catches an OS lock that survives a crash or causes a second external draft."""
    from photo_organizer.catalog.mall_draft_adapter import (
        execute_mall_draft_import_with_store,
    )

    plan = _plan()
    receipt_path = tmp_path / "receipt.json"
    ledger_path = tmp_path / "idempotency-ledger.json"
    context = multiprocessing.get_context("fork")
    crashed = context.Process(
        target=_run_crashing_execution,
        args=(str(receipt_path), plan, str(ledger_path)),
    )
    crashed.start()
    await asyncio.to_thread(crashed.join, 5)

    assert crashed.exitcode == 0
    assert (
        json.loads(ledger_path.read_text())["idempotency_key"]
        == plan.items[0].draft_request_id
    )

    gateway = _IdempotentGateway(ledger_path, plan.opc_id)
    receipt = await asyncio.wait_for(
        execute_mall_draft_import_with_store(
            plan,
            _MemoryStreams(),
            gateway,
            LocalMallDraftExecutionStore(receipt_path),
        ),
        timeout=2,
    )

    assert gateway.create_call_count == 1
    assert receipt.results[0].mall_product_id == "mall-draft-replayed"


@pytest.mark.asyncio
async def test_cancelling_a_cross_process_lock_waiter_closes_any_late_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a cancelled flock worker retaining its FD after a late acquisition."""
    import photo_organizer.catalog.mall_draft_execution_store as store_module

    plan = _plan()
    receipt_path = tmp_path / "receipt.json"
    lock_path = receipt_path.with_name(f".{receipt_path.name}.lock")
    context = multiprocessing.get_context("fork")
    holder_ready = context.Event()
    holder_release = context.Event()
    holder = context.Process(
        target=_hold_sibling_lock,
        args=(str(lock_path), holder_ready, holder_release),
    )
    holder.start()
    assert await asyncio.to_thread(holder_ready.wait, 1)

    attempt_acquired = threading.Event()
    allow_attempt_return = threading.Event()
    attempt_name = (
        "_try_open_and_lock"
        if hasattr(store_module, "_try_open_and_lock")
        else "_open_and_lock"
    )
    original_attempt = getattr(store_module, attempt_name)

    def gate_after_lock_acquisition(lock_path: Path) -> int | None:
        lock_file = original_attempt(lock_path)
        if lock_file is not None:
            attempt_acquired.set()
            allow_attempt_return.wait(1)
        return lock_file

    monkeypatch.setattr(store_module, attempt_name, gate_after_lock_acquisition)
    store = LocalMallDraftExecutionStore(receipt_path)

    async def open_transaction() -> int:
        async with store.execution(plan) as transaction:
            return transaction.next_generation(plan.items[0].draft_request_id)

    waiting = asyncio.create_task(open_transaction())
    holder_release.set()
    await asyncio.to_thread(holder.join, 1)
    assert holder.exitcode == 0
    assert await asyncio.to_thread(attempt_acquired.wait, 1)
    waiting.cancel()
    await asyncio.sleep(0)
    allow_attempt_return.set()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert await asyncio.wait_for(open_transaction(), timeout=1) == 1


@pytest.mark.asyncio
async def test_repeated_cancellation_during_unlock_releases_the_process_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches repeated cancellation stranding the cached same-process lock."""
    import photo_organizer.catalog.mall_draft_execution_store as store_module

    plan = _plan()
    store = LocalMallDraftExecutionStore(tmp_path / "receipt.json")
    original_unlock = store_module._unlock_and_close
    unlock_started = threading.Event()
    allow_unlock = threading.Event()

    def gated_unlock(lock_file: int) -> None:
        unlock_started.set()
        allow_unlock.wait(2)
        original_unlock(lock_file)

    monkeypatch.setattr(store_module, "_unlock_and_close", gated_unlock)
    entered = asyncio.Event()

    async def open_then_exit() -> None:
        async with store.execution(plan):
            entered.set()

    closing = asyncio.create_task(open_then_exit())
    await asyncio.wait_for(entered.wait(), timeout=1)
    assert await asyncio.to_thread(unlock_started.wait, 1)
    closing.cancel()
    await asyncio.sleep(0)
    closing.cancel()
    allow_unlock.set()
    with pytest.raises(asyncio.CancelledError):
        await closing

    async def reopen() -> int:
        async with store.execution(plan) as transaction:
            return transaction.next_generation(plan.items[0].draft_request_id)

    assert await asyncio.wait_for(reopen(), timeout=1) == 1


@pytest.mark.asyncio
async def test_unlock_failure_is_redacted_after_releasing_the_process_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches an OS cleanup error permanently blocking later local execution."""
    import photo_organizer.catalog.mall_draft_execution_store as store_module

    plan = _plan()
    store = LocalMallDraftExecutionStore(tmp_path / "receipt.json")
    original_unlock = store_module._unlock_and_close
    calls = 0

    def fail_after_unlock(lock_file: int) -> None:
        nonlocal calls
        calls += 1
        original_unlock(lock_file)
        if calls == 1:
            raise OSError("private unlock detail")

    monkeypatch.setattr(store_module, "_unlock_and_close", fail_after_unlock)

    with pytest.raises(MallDraftAdapterError) as raised:
        async with store.execution(plan):
            pass

    assert raised.value.code == "output_unavailable"
    assert str(raised.value) == "output_unavailable"

    async with asyncio.timeout(1):
        async with store.execution(plan) as transaction:
            assert transaction.next_generation(plan.items[0].draft_request_id) == 1
