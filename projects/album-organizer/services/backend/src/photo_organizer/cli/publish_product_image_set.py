"""Publish the frozen synthetic ProductImageSetReadyV1 through the durable ledger."""

import argparse
import asyncio
import hashlib
import json
import os
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import NoReturn

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from photo_organizer.handoff.delivery_ledger import (
    ProductImageSetDeliveryLedger,
    PublicationReceiptState,
)
from photo_organizer.handoff.http_publisher import (
    ProductImageSetHttpPublisher,
    PublicationDisposition,
    PublicationReceipt,
)
from photo_organizer.handoff.product_image_set import (
    ProductImageSetReadyV1,
    canonical_body_sha256,
)

FROZEN_SYNTHETIC_SHA256 = (
    "4f6e785b9c35d288c50dd13a3bb5612aaac698d738ab3dae6e34ce935d05d3b1"
)
PublicationRunner = Callable[
    [ProductImageSetReadyV1, str, bytes, str, int], Awaitable[PublicationReceipt]
]


class _CliInputError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        del message
        raise _CliInputError("invalid_arguments")


def _parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(
        description="Publish only the byte-frozen synthetic image-set handoff."
    )
    parser.add_argument("--fixture", required=True, type=Path)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--attempt", type=int, default=1)
    parser.add_argument("--confirm-synthetic", action="store_true")
    return parser


def _database_url(environ: Mapping[str, str]) -> str:
    value = environ.get("DATABASE_URL", "").strip()
    if not value:
        raise _CliInputError("database_url_required")
    if value.startswith("postgresql+asyncpg://"):
        return value
    if value.startswith("postgresql://"):
        return value.replace("postgresql://", "postgresql+asyncpg://", 1)
    raise _CliInputError("unsupported_database_url")


def _signing_secret(environ: Mapping[str, str]) -> bytes:
    value = environ.get("PRODUCT_IMAGE_SET_SIGNING_SECRET", "").encode()
    if len(value) < 32:
        raise _CliInputError("product_image_set_signing_secret_required")
    return value


def _load_frozen_event(path: Path) -> ProductImageSetReadyV1:
    try:
        body = path.read_bytes()
    except OSError as error:
        raise _CliInputError("frozen_synthetic_fixture_required") from error
    if hashlib.sha256(body).hexdigest() != FROZEN_SYNTHETIC_SHA256:
        raise _CliInputError("frozen_synthetic_fixture_required")
    try:
        return ProductImageSetReadyV1.model_validate_json(body)
    except ValidationError as error:
        raise _CliInputError("frozen_synthetic_fixture_required") from error


async def _run_publication(
    event: ProductImageSetReadyV1,
    endpoint: str,
    signing_secret: bytes,
    database_url: str,
    attempt: int,
) -> PublicationReceipt:
    engine = create_async_engine(
        database_url,
        pool_pre_ping=True,
        hide_parameters=True,
    )
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine,
        expire_on_commit=False,
    )
    try:
        receipt = await ProductImageSetHttpPublisher(
            endpoint,
            signing_secret,
        ).publish(event, attempt=attempt)
        await ProductImageSetDeliveryLedger(factory).record_publication_receipt(
            event_id=event.event_id,
            canonical_sha256=canonical_body_sha256(event),
            state=PublicationReceiptState(
                status=receipt.disposition.value,
                retry_after_seconds=receipt.retry_after_seconds,
                error_code=receipt.error_code,
            ),
        )
        return receipt
    finally:
        await engine.dispose()


async def _invoke_runner(
    runner: PublicationRunner,
    event: ProductImageSetReadyV1,
    endpoint: str,
    signing_secret: bytes,
    database_url: str,
    attempt: int,
) -> PublicationReceipt:
    return await runner(event, endpoint, signing_secret, database_url, attempt)


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    runner: PublicationRunner | None = None,
) -> int:
    environment = os.environ if environ is None else environ
    try:
        args = _parser().parse_args(argv)
        if not args.confirm_synthetic:
            raise _CliInputError("synthetic_confirmation_required")
        if args.attempt < 1:
            raise _CliInputError("invalid_arguments")
        event = _load_frozen_event(args.fixture)
        database_url = _database_url(environment)
        signing_secret = _signing_secret(environment)
        receipt: PublicationReceipt = asyncio.run(
            _invoke_runner(
                runner or _run_publication,
                event,
                args.endpoint,
                signing_secret,
                database_url,
                args.attempt,
            )
        )
    except _CliInputError as error:
        print(error.code, file=sys.stderr)
        return 2
    except Exception:  # noqa: BLE001 - redact operational details and secrets.
        print("product_image_set_publication_failed", file=sys.stderr)
        return 1

    print(
        json.dumps(
            {
                "disposition": receipt.disposition.value,
                "retry_after_seconds": receipt.retry_after_seconds,
                "status_code": receipt.status_code,
            },
            sort_keys=True,
        )
    )
    return 0 if receipt.disposition is PublicationDisposition.DELIVERED else 1


def entrypoint() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    entrypoint()
