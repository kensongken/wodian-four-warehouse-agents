"""Prepare deterministic, offline-only OPC mall-draft import artifacts."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import stat
import sys
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from typing import NamedTuple, Never, cast

ROOT = Path(__file__).resolve().parents[1]
BACKEND_SOURCE = ROOT / "services/backend/src"
if str(BACKEND_SOURCE) not in sys.path:
    sys.path.insert(0, str(BACKEND_SOURCE))

from pydantic import ValidationError

from photo_organizer.catalog.mall_draft_adapter import (
    MallDraftAdapterError,
    build_mall_draft_import_plan,
    execute_mall_draft_import,
)
from photo_organizer.catalog.mall_draft_gateway import MallDraftGateway
from photo_organizer.catalog.package_image_streams import (
    PackageImageStreamProvider,
)
from photo_organizer.catalog.standard_product_package_acceptance import (
    StandardProductPackageAcceptanceError,
    open_verified_standard_product_package,
)
from photo_organizer.schemas.mall_draft_adapter import (
    MallDraftImportPlan,
    MallDraftImportReceipt,
)
from photo_organizer.schemas.mall_draft_export import MallProductDraftBatch
from photo_organizer.schemas.standard_product_package_acceptance import (
    StandardProductPackageAcceptanceReceipt,
)

PLAN_SCHEMA_OUTPUT = ROOT / "contracts/opc-mall-draft-import-plan-v1.schema.json"
RECEIPT_SCHEMA_OUTPUT = ROOT / "contracts/opc-mall-draft-import-receipt-v1.schema.json"
MAX_JSON_BYTES = 1_048_576
MAX_JSON_DEPTH = 64
MAX_JSON_NODES = 10_000
_FORBIDDEN_VALUE_PREFIXES = ("http://", "https://")


class ImportPreparationError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise ImportPreparationError("invalid_arguments")


def render_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def build_plan_schema() -> dict[str, object]:
    return _schema_with_semantic_check(MallDraftImportPlan.model_json_schema(), "plan")


def build_receipt_schema() -> dict[str, object]:
    return _schema_with_semantic_check(
        MallDraftImportReceipt.model_json_schema(), "receipt"
    )


def _schema_with_semantic_check(
    schema: Mapping[str, object], artifact: str
) -> dict[str, object]:
    result = dict(schema)
    result["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    result["$id"] = f"urn:photo-organizer:opc-mall-draft-import-{artifact}:1.0"
    result["x-semantic-validation"] = {
        "required": True,
        "command": (
            "python scripts/prepare_opc_mall_draft_import.py --package <package.zip> "
            "--package-acceptance <acceptance.json> --batch <batch.json> "
            "--plan-output <plan.json> --receipt-output <receipt.json> --check"
        ),
        "stable_error": "import_artifact_stale",
    }
    return result


def _parser() -> _SafeArgumentParser:
    parser = _SafeArgumentParser(add_help=False)
    parser.add_argument("--package")
    parser.add_argument("--package-acceptance")
    parser.add_argument("--batch")
    parser.add_argument("--plan-output")
    parser.add_argument("--receipt-output")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--write-artifacts", action="store_true")
    parser.add_argument("--check-artifacts", action="store_true")
    return parser


def _path(value: str | None, *, argument: bool) -> Path:
    if value is None or value.casefold().startswith(_FORBIDDEN_VALUE_PREFIXES):
        raise ImportPreparationError("invalid_arguments" if argument else "invalid_input")
    return Path(value)


def _validate_args(args: argparse.Namespace) -> tuple[Path, Path, Path, Path, Path] | None:
    artifact_mode = args.write_artifacts or args.check_artifacts
    normal_values = (
        args.package,
        args.package_acceptance,
        args.batch,
        args.plan_output,
        args.receipt_output,
    )
    if artifact_mode:
        if args.write_artifacts and args.check_artifacts or args.check or any(
            value is not None for value in normal_values
        ):
            raise ImportPreparationError("invalid_arguments")
        return None
    if any(value is None for value in normal_values):
        raise ImportPreparationError("invalid_arguments")
    paths = tuple(_path(value, argument=True) for value in normal_values)
    package, acceptance, batch, plan_output, receipt_output = cast(
        tuple[Path, Path, Path, Path, Path], paths
    )
    _reject_collisions((package, acceptance, batch, plan_output, receipt_output))
    return package, acceptance, batch, plan_output, receipt_output


def _reject_collisions(paths: tuple[Path, ...]) -> None:
    for index, path in enumerate(paths):
        for other in paths[index + 1 :]:
            if _same_location(path, other):
                raise ImportPreparationError("invalid_arguments")


def _same_location(left: Path, right: Path) -> bool:
    try:
        if left.resolve(strict=False) == right.resolve(strict=False):
            return True
        return left.exists() and right.exists() and os.path.samefile(left, right)
    except OSError:
        raise ImportPreparationError("invalid_input") from None


def _read_regular_bytes(path: Path) -> bytes:
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise ImportPreparationError("invalid_input")
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
        )
        with os.fdopen(descriptor, "rb") as source:
            opened = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)
                or opened.st_size <= 0
                or opened.st_size > MAX_JSON_BYTES
            ):
                raise ImportPreparationError("invalid_input")
            content = source.read(MAX_JSON_BYTES + 1)
            if len(content) != opened.st_size or len(content) > MAX_JSON_BYTES:
                raise ImportPreparationError("invalid_input")
            return content
    except ImportPreparationError:
        raise
    except OSError:
        raise ImportPreparationError("invalid_input") from None


def _read_json(path: Path) -> object:
    try:
        value = json.loads(_read_regular_bytes(path).decode("utf-8"))
        _validate_json_shape(value)
        return value
    except ImportPreparationError:
        raise
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise ImportPreparationError("invalid_input") from None


def _validate_json_shape(value: object, *, depth: int = 0, nodes: int = 0) -> int:
    if depth > MAX_JSON_DEPTH or nodes >= MAX_JSON_NODES:
        raise ImportPreparationError("invalid_input")
    if isinstance(value, str):
        if value.casefold().startswith(_FORBIDDEN_VALUE_PREFIXES):
            raise ImportPreparationError("invalid_input")
        return nodes + 1
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ImportPreparationError("invalid_input")
            nodes = _validate_json_shape(key, depth=depth + 1, nodes=nodes + 1)
            nodes = _validate_json_shape(child, depth=depth + 1, nodes=nodes)
        return nodes + 1
    if isinstance(value, list):
        for child in value:
            nodes = _validate_json_shape(child, depth=depth + 1, nodes=nodes)
        return nodes + 1
    if value is None or isinstance(value, bool | int | float):
        return nodes + 1
    raise ImportPreparationError("invalid_input")


def _parse_inputs(
    package: Path, acceptance_path: Path, batch_path: Path
) -> tuple[MallDraftImportPlan, MallDraftImportReceipt]:
    try:
        acceptance = StandardProductPackageAcceptanceReceipt.model_validate(
            _read_json(acceptance_path)
        )
        batch = MallProductDraftBatch.model_validate(_read_json(batch_path))
    except (ImportPreparationError, ValidationError):
        raise ImportPreparationError("invalid_input") from None
    try:
        with open_verified_standard_product_package(package) as verified:
            if acceptance != verified.receipt:
                raise ImportPreparationError("package_receipt_mismatch")
            plan = build_mall_draft_import_plan(batch, verified.manifest, acceptance)
            receipt = asyncio.run(
                execute_mall_draft_import(
                    plan,
                    cast(PackageImageStreamProvider, object()),
                    cast(MallDraftGateway, object()),
                    dry_run=True,
                )
            )
            return plan, receipt
    except ImportPreparationError:
        raise
    except MallDraftAdapterError as error:
        raise ImportPreparationError(error.code) from None
    except StandardProductPackageAcceptanceError:
        raise ImportPreparationError("invalid_input") from None
    except (OSError, RuntimeError, TypeError, ValueError, ValidationError):
        raise ImportPreparationError("invalid_input") from None


def _artifact_contents(
    plan: MallDraftImportPlan, receipt: MallDraftImportReceipt
) -> tuple[str, str]:
    return (
        render_json(plan.model_dump(mode="json")),
        render_json(receipt.model_dump(mode="json")),
    )


def _read_output(path: Path) -> bytes:
    return _read_regular_bytes(path)


def _check_pair(
    plan_output: Path, receipt_output: Path, plan_content: str, receipt_content: str
) -> bool:
    with _locked_pair(plan_output, receipt_output) as transaction:
        _recover_pair(transaction)
        try:
            return (
                _read_output(plan_output) == plan_content.encode("utf-8")
                and _read_output(receipt_output) == receipt_content.encode("utf-8")
            )
        except ImportPreparationError:
            return False


def _write_temporary(path: Path, content: str) -> Path:
    temporary_path: Path | None = None
    try:
        if not path.parent.is_dir():
            raise OSError
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise OSError
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as temporary:
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        return temporary_path
    except OSError:
        if temporary_path is not None:
            _unlink(temporary_path)
        raise ImportPreparationError("output_unavailable") from None


def _write_temporary_bytes(path: Path, content: bytes) -> Path:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.name}.backup.", delete=False
        ) as temporary:
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        return temporary_path
    except OSError:
        if temporary_path is not None:
            _unlink(temporary_path)
        raise ImportPreparationError("output_unavailable") from None


def _unlink(path: Path) -> bool:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return False
    return True


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(
        directory,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _restore_from_backup(path: Path, backup: Path) -> bool:
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_TRUNC
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb") as target, backup.open("rb") as source:
            while chunk := source.read(128 * 1024):
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
        return True
    except OSError:
        return False


class _PairTransactionPaths(NamedTuple):
    plan_output: Path
    receipt_output: Path
    lock: Path
    journal: Path
    journal_next: Path
    plan_new: Path
    receipt_new: Path
    plan_backup: Path
    receipt_backup: Path
    plan_restore: Path
    receipt_restore: Path


class _PairJournal(NamedTuple):
    plan_existed: bool
    receipt_existed: bool
    plan_backup_sha256: str | None
    receipt_backup_sha256: str | None


def _pair_transaction_paths(
    plan_output: Path, receipt_output: Path
) -> _PairTransactionPaths:
    identity = sha256(
        (
            str(plan_output.resolve(strict=False))
            + "\0"
            + str(receipt_output.resolve(strict=False))
        ).encode("utf-8")
    ).hexdigest()
    prefix = f".opc-mall-draft-pair-{identity}"
    return _PairTransactionPaths(
        plan_output=plan_output,
        receipt_output=receipt_output,
        lock=plan_output.parent / f"{prefix}.lock",
        journal=plan_output.parent / f"{prefix}.txn",
        journal_next=plan_output.parent / f"{prefix}.txn.next",
        plan_new=plan_output.parent / f"{prefix}.plan.new",
        receipt_new=receipt_output.parent / f"{prefix}.receipt.new",
        plan_backup=plan_output.parent / f"{prefix}.plan.backup",
        receipt_backup=receipt_output.parent / f"{prefix}.receipt.backup",
        plan_restore=plan_output.parent / f"{prefix}.plan.restore",
        receipt_restore=receipt_output.parent / f"{prefix}.receipt.restore",
    )


@contextmanager
def _locked_pair(
    plan_output: Path, receipt_output: Path
) -> Iterator[_PairTransactionPaths]:
    transaction: _PairTransactionPaths | None = None
    lock_file: int | None = None
    try:
        transaction = _pair_transaction_paths(plan_output, receipt_output)
        if not plan_output.parent.is_dir() or not receipt_output.parent.is_dir():
            raise OSError
        lock_file = os.open(
            transaction.lock,
            os.O_CREAT
            | os.O_RDWR
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        if not stat.S_ISREG(os.fstat(lock_file).st_mode):
            raise OSError
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        _fsync_directory(transaction.lock.parent)
        yield transaction
    except ImportPreparationError:
        raise
    except (OSError, RuntimeError):
        raise ImportPreparationError("output_unavailable") from None
    finally:
        if lock_file is not None:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(lock_file)
            except OSError:
                pass


def _stage_text(path: Path, content: str) -> None:
    temporary = _write_temporary(path, content)
    try:
        os.rename(temporary, path)
        _fsync_directory(path.parent)
    except OSError:
        _unlink(temporary)
        raise ImportPreparationError("output_unavailable") from None


def _stage_bytes(path: Path, content: bytes) -> None:
    temporary = _write_temporary_bytes(path, content)
    try:
        os.rename(temporary, path)
        _fsync_directory(path.parent)
    except OSError:
        _unlink(temporary)
        raise ImportPreparationError("output_unavailable") from None


def _write_pair_journal(path: _PairTransactionPaths, journal: _PairJournal) -> None:
    payload = {
        "schema_version": "opc-mall-draft-pair-transaction/1.0",
        "state": "prepared",
        "plan_existed": journal.plan_existed,
        "receipt_existed": journal.receipt_existed,
        "plan_backup_sha256": journal.plan_backup_sha256,
        "receipt_backup_sha256": journal.receipt_backup_sha256,
    }
    content = render_json(payload).encode("utf-8")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path.journal_next,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb") as target:
            descriptor = None
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        os.rename(path.journal_next, path.journal)
        _fsync_directory(path.journal.parent)
    except OSError:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        _unlink(path.journal_next)
        raise ImportPreparationError("output_unavailable") from None


def _read_pair_journal(path: _PairTransactionPaths) -> _PairJournal:
    try:
        payload = json.loads(_read_regular_bytes(path.journal).decode("utf-8"))
        if not isinstance(payload, dict) or set(payload) != {
            "schema_version",
            "state",
            "plan_existed",
            "receipt_existed",
            "plan_backup_sha256",
            "receipt_backup_sha256",
        }:
            raise ValueError
        if (
            payload["schema_version"] != "opc-mall-draft-pair-transaction/1.0"
            or payload["state"] != "prepared"
            or not isinstance(payload["plan_existed"], bool)
            or not isinstance(payload["receipt_existed"], bool)
        ):
            raise ValueError
        plan_digest = _validated_optional_digest(payload["plan_backup_sha256"])
        receipt_digest = _validated_optional_digest(payload["receipt_backup_sha256"])
        if (plan_digest is not None) != payload["plan_existed"] or (
            receipt_digest is not None
        ) != payload["receipt_existed"]:
            raise ValueError
        return _PairJournal(
            plan_existed=payload["plan_existed"],
            receipt_existed=payload["receipt_existed"],
            plan_backup_sha256=plan_digest,
            receipt_backup_sha256=receipt_digest,
        )
    except (ImportPreparationError, UnicodeError, ValueError, json.JSONDecodeError):
        raise ImportPreparationError("output_unavailable") from None


def _validated_optional_digest(value: object) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError
    return value


def _stage_backup(output: Path, backup: Path) -> tuple[bool, str | None]:
    try:
        metadata = output.lstat()
    except FileNotFoundError:
        return False, None
    except OSError:
        raise ImportPreparationError("output_unavailable") from None
    if not stat.S_ISREG(metadata.st_mode):
        raise ImportPreparationError("output_unavailable")
    try:
        content = _read_regular_bytes(output)
        _stage_bytes(backup, content)
        return True, sha256(content).hexdigest()
    except ImportPreparationError:
        raise ImportPreparationError("output_unavailable") from None


def _restore_original(
    output: Path,
    backup: Path,
    restore: Path,
    *,
    existed: bool,
    expected_sha256: str | None,
) -> None:
    if not existed:
        if output.exists() or output.is_symlink():
            if not _unlink(output):
                raise ImportPreparationError("output_unavailable")
            _fsync_directory(output.parent)
        return
    try:
        content = _read_regular_bytes(backup)
        if sha256(content).hexdigest() != expected_sha256:
            raise ImportPreparationError("output_unavailable")
        _stage_bytes(restore, content)
        try:
            os.replace(restore, output)
            _fsync_directory(output.parent)
        except OSError:
            _unlink(restore)
            if not _restore_from_backup(output, backup):
                raise ImportPreparationError("output_unavailable") from None
            _fsync_directory(output.parent)
    except ImportPreparationError:
        raise
    except OSError:
        raise ImportPreparationError("output_unavailable") from None


def _transaction_sidecars(path: _PairTransactionPaths) -> tuple[Path, ...]:
    return (
        path.journal_next,
        path.plan_new,
        path.receipt_new,
        path.plan_backup,
        path.receipt_backup,
        path.plan_restore,
        path.receipt_restore,
    )


def _cleanup_transaction_sidecars(path: _PairTransactionPaths) -> None:
    directories: set[Path] = set()
    for sidecar in _transaction_sidecars(path):
        if _unlink(sidecar):
            directories.add(sidecar.parent)
        try:
            temporary_paths = tuple(sidecar.parent.glob(f".{sidecar.name}.*"))
        except OSError:
            temporary_paths = ()
        for temporary in temporary_paths:
            if _unlink(temporary):
                directories.add(temporary.parent)
    for directory in directories:
        try:
            _fsync_directory(directory)
        except OSError:
            pass


def _recover_pair(path: _PairTransactionPaths) -> None:
    if not path.journal.exists():
        _cleanup_transaction_sidecars(path)
        return
    journal = _read_pair_journal(path)
    _restore_original(
        path.plan_output,
        path.plan_backup,
        path.plan_restore,
        existed=journal.plan_existed,
        expected_sha256=journal.plan_backup_sha256,
    )
    _restore_original(
        path.receipt_output,
        path.receipt_backup,
        path.receipt_restore,
        existed=journal.receipt_existed,
        expected_sha256=journal.receipt_backup_sha256,
    )
    if not _unlink(path.journal):
        raise ImportPreparationError("output_unavailable")
    try:
        _fsync_directory(path.journal.parent)
    except OSError:
        raise ImportPreparationError("output_unavailable") from None
    _cleanup_transaction_sidecars(path)


def _atomic_write_pair(
    plan_output: Path, receipt_output: Path, plan_content: str, receipt_content: str
) -> None:
    with _locked_pair(plan_output, receipt_output) as transaction:
        _recover_pair(transaction)
        try:
            _stage_text(transaction.plan_new, plan_content)
            _stage_text(transaction.receipt_new, receipt_content)
            plan_existed, plan_digest = _stage_backup(
                plan_output, transaction.plan_backup
            )
            receipt_existed, receipt_digest = _stage_backup(
                receipt_output, transaction.receipt_backup
            )
            _write_pair_journal(
                transaction,
                _PairJournal(
                    plan_existed=plan_existed,
                    receipt_existed=receipt_existed,
                    plan_backup_sha256=plan_digest,
                    receipt_backup_sha256=receipt_digest,
                ),
            )
            os.replace(transaction.plan_new, plan_output)
            _fsync_directory(plan_output.parent)
            os.replace(transaction.receipt_new, receipt_output)
            _fsync_directory(receipt_output.parent)
            if not _unlink(transaction.journal):
                raise OSError
            _fsync_directory(transaction.journal.parent)
        except (ImportPreparationError, OSError):
            _recover_pair(transaction)
            raise ImportPreparationError("output_unavailable") from None
        _cleanup_transaction_sidecars(transaction)


def _artifact_outputs() -> dict[Path, str]:
    return {
        PLAN_SCHEMA_OUTPUT: render_json(build_plan_schema()),
        RECEIPT_SCHEMA_OUTPUT: render_json(build_receipt_schema()),
    }


def _export_artifacts(*, check: bool) -> bool:
    outputs = _artifact_outputs()
    if check:
        return _check_pair(
            PLAN_SCHEMA_OUTPUT,
            RECEIPT_SCHEMA_OUTPUT,
            outputs[PLAN_SCHEMA_OUTPUT],
            outputs[RECEIPT_SCHEMA_OUTPUT],
        )
    _atomic_write_pair(
        PLAN_SCHEMA_OUTPUT,
        RECEIPT_SCHEMA_OUTPUT,
        outputs[PLAN_SCHEMA_OUTPUT],
        outputs[RECEIPT_SCHEMA_OUTPUT],
    )
    return True


def main() -> int:
    try:
        args = _parser().parse_args()
        paths = _validate_args(args)
        if args.write_artifacts:
            _export_artifacts(check=False)
            return 0
        if args.check_artifacts:
            if not _export_artifacts(check=True):
                print("import_artifact_stale", file=sys.stderr)
                return 1
            return 0
        if paths is None:
            raise ImportPreparationError("invalid_arguments")
        package, acceptance, batch, plan_output, receipt_output = paths
        plan, receipt = _parse_inputs(package, acceptance, batch)
        plan_content, receipt_content = _artifact_contents(plan, receipt)
        if args.check:
            if not _check_pair(plan_output, receipt_output, plan_content, receipt_content):
                print("import_artifact_stale", file=sys.stderr)
                return 1
            return 0
        _atomic_write_pair(plan_output, receipt_output, plan_content, receipt_content)
        return 0
    except ImportPreparationError as error:
        print(error.code, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
