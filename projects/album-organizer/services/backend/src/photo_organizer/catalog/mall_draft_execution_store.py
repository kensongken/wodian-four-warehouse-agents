from __future__ import annotations

import asyncio
import fcntl
import json
import os
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Self

from pydantic import ValidationError

from photo_organizer.catalog.mall_draft_adapter import MallDraftAdapterError
from photo_organizer.schemas.mall_draft_adapter import (
    MallDraftImportPlan,
    MallDraftImportReceipt,
    MallDraftItemResult,
)

if TYPE_CHECKING:
    from types import TracebackType


_TERMINAL_STATUSES = frozenset(
    {"created", "change_proposal_required", "permanently_blocked"}
)
_PROCESS_LOCKS: dict[Path, asyncio.Lock] = {}
_LOCK_POLL_SECONDS = 0.01


class LocalMallDraftExecutionStore:
    """A CLI-local receipt store that serializes one plan execution at a time."""

    def __init__(self, receipt_path: Path) -> None:
        self._receipt_path = receipt_path.resolve()

    @asynccontextmanager
    async def execution(
        self, plan: MallDraftImportPlan
    ) -> AsyncIterator[MallDraftExecutionTransaction]:
        transaction = MallDraftExecutionTransaction(self._receipt_path, plan)
        await transaction.__aenter__()
        try:
            yield transaction
        except BaseException as error:
            await transaction.__aexit__(type(error), error, error.__traceback__)
            raise
        else:
            await transaction.__aexit__(None, None, None)


class MallDraftExecutionTransaction:
    def __init__(self, receipt_path: Path, plan: MallDraftImportPlan) -> None:
        self._receipt_path = receipt_path
        self._plan = plan
        self._process_lock: asyncio.Lock | None = None
        self._lock_file: int | None = None
        self._current_receipt: MallDraftImportReceipt | None = None
        self._expected_generations: dict[str, int] = {}

    @property
    def current_receipt(self) -> MallDraftImportReceipt:
        if self._current_receipt is None:
            raise MallDraftAdapterError("resume_state_conflict")
        return self._current_receipt

    async def __aenter__(self) -> Self:
        self._process_lock = _PROCESS_LOCKS.setdefault(
            self._receipt_path, asyncio.Lock()
        )
        await self._process_lock.acquire()
        try:
            self._lock_file = await _acquire_cross_process_lock(
                _sibling_lock_path(self._receipt_path)
            )
            self._current_receipt = _load_or_prepare(self._receipt_path, self._plan)
            self._expected_generations = _next_generations(
                self._current_receipt, self._plan
            )
        except BaseException:
            await self._release()
            raise
        return self

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        del exception_type, exception, traceback
        await self._release()
        return None

    def next_generation(self, draft_request_id: str) -> int:
        try:
            return self._expected_generations[draft_request_id]
        except KeyError:
            raise MallDraftAdapterError("resume_state_conflict") from None

    async def settle(self, result: MallDraftItemResult) -> MallDraftImportReceipt:
        current = self.current_receipt
        try:
            expected_generation = self._expected_generations[result.draft_request_id]
        except KeyError:
            raise MallDraftAdapterError("resume_state_conflict") from None
        if result.attempt_generation != expected_generation:
            raise MallDraftAdapterError("resume_state_conflict")
        checked_result = _revalidate_result(result)
        updated = _settled_receipt(current, self._plan, checked_result)
        await asyncio.to_thread(_write_receipt, self._receipt_path, updated)
        self._current_receipt = updated
        self._expected_generations = _next_generations(updated, self._plan)
        return updated

    async def _release(self) -> None:
        lock_file, self._lock_file = self._lock_file, None
        process_lock, self._process_lock = self._process_lock, None
        try:
            if lock_file is not None:
                await _cleanup_cross_process_lock(lock_file)
        finally:
            if process_lock is not None and process_lock.locked():
                process_lock.release()


def _sibling_lock_path(receipt_path: Path) -> Path:
    return receipt_path.with_name(f".{receipt_path.name}.lock")


async def _acquire_cross_process_lock(lock_path: Path) -> int:
    while True:
        attempt = asyncio.create_task(asyncio.to_thread(_try_open_and_lock, lock_path))
        lock_file = await _finish_lock_attempt(attempt)
        if lock_file is not None:
            return lock_file
        await asyncio.sleep(_LOCK_POLL_SECONDS)


async def _finish_lock_attempt(attempt: asyncio.Task[int | None]) -> int | None:
    cancelled = False
    while True:
        try:
            lock_file = await asyncio.shield(attempt)
            break
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        if lock_file is not None:
            await _release_after_cancellation(lock_file)
        raise asyncio.CancelledError
    return lock_file


async def _release_after_cancellation(lock_file: int) -> None:
    release = asyncio.create_task(asyncio.to_thread(_unlock_and_close, lock_file))
    while True:
        try:
            await asyncio.shield(release)
            return
        except asyncio.CancelledError:
            continue
        except OSError:
            return


async def _cleanup_cross_process_lock(lock_file: int) -> None:
    cleanup = asyncio.create_task(asyncio.to_thread(_unlock_and_close, lock_file))
    cancellation: asyncio.CancelledError | None = None
    cleanup_failed = False
    while True:
        try:
            await asyncio.shield(cleanup)
            break
        except asyncio.CancelledError as error:
            cancellation = error
            continue
        except Exception:  # noqa: BLE001 - cleanup errors become one stable code.
            cleanup_failed = True
            break
    if cleanup_failed:
        raise MallDraftAdapterError("output_unavailable")
    if cancellation is not None:
        raise cancellation


def _try_open_and_lock(lock_path: Path) -> int | None:
    lock_file: int | None = None
    try:
        lock_file = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return lock_file
    except BlockingIOError:
        if lock_file is not None:
            os.close(lock_file)
        return None
    except OSError:
        if lock_file is not None:
            try:
                os.close(lock_file)
            except OSError:
                pass
        raise MallDraftAdapterError("output_unavailable") from None


def _unlock_and_close(lock_file: int) -> None:
    try:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
    finally:
        os.close(lock_file)


def _load_or_prepare(
    receipt_path: Path, plan: MallDraftImportPlan
) -> MallDraftImportReceipt:
    if not receipt_path.exists():
        return _prepared_receipt(plan)
    try:
        payload = json.loads(
            receipt_path.read_text(encoding="utf-8"),
            parse_constant=_reject_json_constant,
        )
        receipt = MallDraftImportReceipt.model_validate(payload)
    except (OSError, TypeError, ValueError, ValidationError, json.JSONDecodeError):
        raise MallDraftAdapterError("resume_state_conflict") from None
    if not _receipt_matches_plan(receipt, plan):
        raise MallDraftAdapterError("resume_state_conflict")
    return receipt


def _reject_json_constant(_: str) -> None:
    raise ValueError


def _prepared_receipt(plan: MallDraftImportPlan) -> MallDraftImportReceipt:
    return MallDraftImportReceipt(
        schema_version="opc-mall-draft-import-receipt/1.0",
        plan_id=plan.plan_id,
        opc_id=plan.opc_id,
        execution_status="prepared",
        item_count=len(plan.items),
        results=tuple(
            MallDraftItemResult(
                draft_request_id=item.draft_request_id,
                attempt_generation=1,
                status="prepared",
                mall_product_id=None,
                error_code=None,
            )
            for item in sorted(
                plan.items, key=lambda candidate: candidate.draft_request_id
            )
        ),
    )


def _receipt_matches_plan(
    receipt: MallDraftImportReceipt, plan: MallDraftImportPlan
) -> bool:
    expected_ids = tuple(sorted(item.draft_request_id for item in plan.items))
    actual_ids = tuple(result.draft_request_id for result in receipt.results)
    return (
        receipt.plan_id == plan.plan_id
        and receipt.opc_id == plan.opc_id
        and receipt.item_count == len(plan.items)
        and actual_ids == expected_ids
    )


def _next_generations(
    receipt: MallDraftImportReceipt, plan: MallDraftImportPlan
) -> dict[str, int]:
    if not _receipt_matches_plan(receipt, plan):
        raise MallDraftAdapterError("resume_state_conflict")
    generations: dict[str, int] = {}
    for result in receipt.results:
        if result.status == "prepared":
            generations[result.draft_request_id] = result.attempt_generation
        elif result.status not in _TERMINAL_STATUSES:
            generations[result.draft_request_id] = result.attempt_generation + 1
    return generations


def _revalidate_result(result: MallDraftItemResult) -> MallDraftItemResult:
    try:
        return MallDraftItemResult.model_validate(result.model_dump(mode="python"))
    except (AttributeError, TypeError, ValueError, ValidationError):
        raise MallDraftAdapterError("resume_state_conflict") from None


def _settled_receipt(
    receipt: MallDraftImportReceipt,
    plan: MallDraftImportPlan,
    result: MallDraftItemResult,
) -> MallDraftImportReceipt:
    if not _receipt_matches_plan(receipt, plan):
        raise MallDraftAdapterError("resume_state_conflict")
    results = tuple(
        result if current.draft_request_id == result.draft_request_id else current
        for current in receipt.results
    )
    execution_status: Literal["in_progress", "completed"] = (
        "completed"
        if all(current.status in _TERMINAL_STATUSES for current in results)
        else "in_progress"
    )
    return MallDraftImportReceipt(
        schema_version="opc-mall-draft-import-receipt/1.0",
        plan_id=plan.plan_id,
        opc_id=plan.opc_id,
        execution_status=execution_status,
        item_count=len(results),
        results=results,
    )


def _write_receipt(receipt_path: Path, receipt: MallDraftImportReceipt) -> None:
    temporary_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=receipt_path.parent,
            prefix=".mall-draft-",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = temporary.name
            temporary.write(
                json.dumps(
                    receipt.model_dump(mode="json"),
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, receipt_path)
        _fsync_directory(receipt_path.parent)
    except (OSError, TypeError, ValueError):
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
            except OSError:
                pass
        raise MallDraftAdapterError("output_unavailable") from None


def _fsync_directory(directory: Path) -> None:
    directory_file = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(directory_file)
    finally:
        os.close(directory_file)
