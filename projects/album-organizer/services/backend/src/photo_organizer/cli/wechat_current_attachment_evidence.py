"""Emit evidence for one current attachment without scanning other sources."""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import NoReturn

from pydantic import ValidationError

from photo_organizer.handoff.wechat_current_attachment_evidence import (
    AuthorizedProductPackage,
    CurrentAttachmentEvidenceError,
    analyze_current_attachment,
)

MAX_AUTHORIZED_PACKAGE_BYTES = 1024 * 1024


class _CliInputError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        del message
        raise _CliInputError("invalid_arguments")


def _parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(
        description="Match one current attachment against one authorized package."
    )
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--attachment", type=Path)
    parser.add_argument("--authorized-package", type=Path)
    return parser


def _load_authorized_package(path: Path) -> AuthorizedProductPackage:
    try:
        path_stat = path.lstat()
    except OSError as error:
        raise _CliInputError("authorized_package_unavailable") from error
    if stat.S_ISLNK(path_stat.st_mode):
        raise _CliInputError("authorized_package_symlink_not_allowed")
    if not stat.S_ISREG(path_stat.st_mode):
        raise _CliInputError("authorized_package_not_regular_file")
    if path_stat.st_size == 0:
        raise _CliInputError("authorized_package_invalid")
    if path_stat.st_size > MAX_AUTHORIZED_PACKAGE_BYTES:
        raise _CliInputError("authorized_package_too_large")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise _CliInputError("authorized_package_unavailable") from error

    chunks: list[bytes] = []
    total_bytes = 0
    try:
        opened_stat = os.fstat(descriptor)
        if not stat.S_ISREG(opened_stat.st_mode):
            raise _CliInputError("authorized_package_not_regular_file")
        if _stable_file_identity(opened_stat) != _stable_file_identity(path_stat):
            raise _CliInputError("authorized_package_changed_during_read")
        if opened_stat.st_size == 0:
            raise _CliInputError("authorized_package_invalid")
        if opened_stat.st_size > MAX_AUTHORIZED_PACKAGE_BYTES:
            raise _CliInputError("authorized_package_too_large")
        while chunk := _read_chunk(descriptor, 64 * 1024):
            total_bytes += len(chunk)
            if total_bytes > MAX_AUTHORIZED_PACKAGE_BYTES:
                raise _CliInputError("authorized_package_too_large")
            chunks.append(chunk)
        final_stat = os.fstat(descriptor)
        if (
            _stable_file_identity(final_stat) != _stable_file_identity(opened_stat)
            or total_bytes != opened_stat.st_size
        ):
            raise _CliInputError("authorized_package_changed_during_read")
        try:
            final_path_stat = path.lstat()
        except OSError as error:
            raise _CliInputError(
                "authorized_package_changed_during_read"
            ) from error
        if _stable_file_identity(final_path_stat) != _stable_file_identity(final_stat):
            raise _CliInputError("authorized_package_changed_during_read")
    except OSError as error:
        raise _CliInputError("authorized_package_unavailable") from error
    finally:
        os.close(descriptor)

    try:
        return AuthorizedProductPackage.model_validate_json(b"".join(chunks))
    except (ValidationError, ValueError) as error:
        raise _CliInputError("authorized_package_invalid") from error


def _stable_file_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _read_chunk(descriptor: int, size: int) -> bytes:
    return os.read(descriptor, size)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        package = None
        if args.attachment is not None and args.authorized_package is not None:
            package = _load_authorized_package(args.authorized_package)
        result = analyze_current_attachment(
            request_id=args.request_id,
            attachment_path=args.attachment,
            package=package,
        )
    except (_CliInputError, CurrentAttachmentEvidenceError) as error:
        print(error.code, file=sys.stderr)
        return 2
    except ValidationError:
        print("invalid_arguments", file=sys.stderr)
        return 2
    except Exception:  # noqa: BLE001 - never expose local paths or package data.
        print("current_attachment_evidence_failed", file=sys.stderr)
        return 1

    print(
        json.dumps(
            result.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


def entrypoint() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    entrypoint()
