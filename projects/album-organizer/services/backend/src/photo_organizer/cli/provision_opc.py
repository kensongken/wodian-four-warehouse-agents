"""Provision one OPC workspace through a private operational command."""

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import NoReturn

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from photo_organizer.admin.opc_provisioning import (
    OpcProvisioningError,
    OpcProvisioningInput,
    OpcProvisioningResult,
    OpcProvisioningService,
)

ProvisionRunner = Callable[
    [OpcProvisioningInput, str], Awaitable[OpcProvisioningResult]
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
        description="Create one OPC workspace and bind its existing first owner."
    )
    parser.add_argument("--opc-id", required=True)
    parser.add_argument("--display-name", required=True)
    parser.add_argument("--owner-user-id", required=True)
    return parser


def _database_url(environ: Mapping[str, str]) -> str:
    database_url = environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise _CliInputError("database_url_required")
    if database_url.startswith("postgresql+asyncpg://"):
        return database_url
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    raise _CliInputError("unsupported_database_url")


async def _run_provision(
    provision: OpcProvisioningInput,
    database_url: str,
) -> OpcProvisioningResult:
    engine = create_async_engine(
        database_url,
        pool_pre_ping=True,
        hide_parameters=True,
    )
    session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine,
        expire_on_commit=False,
    )
    try:
        return await OpcProvisioningService(session_factory).provision(
            opc_id=provision.opc_id,
            display_name=provision.display_name,
            owner_user_id=provision.owner_user_id,
        )
    finally:
        await engine.dispose()


async def _invoke_runner(
    runner: ProvisionRunner,
    provision: OpcProvisioningInput,
    database_url: str,
) -> OpcProvisioningResult:
    return await runner(provision, database_url)


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    runner: ProvisionRunner | None = None,
) -> int:
    environment = os.environ if environ is None else environ
    try:
        args = _parser().parse_args(argv)
        database_url = _database_url(environment)
        provision = OpcProvisioningInput.model_validate(
            {
                "opc_id": args.opc_id,
                "display_name": args.display_name,
                "owner_user_id": args.owner_user_id,
            }
        )
        result = asyncio.run(
            _invoke_runner(runner or _run_provision, provision, database_url)
        )
    except ValidationError:
        print("invalid_arguments", file=sys.stderr)
        return 2
    except _CliInputError as error:
        print(error.code, file=sys.stderr)
        return 2
    except OpcProvisioningError as error:
        print(error.code, file=sys.stderr)
        return 2
    except Exception:  # noqa: BLE001 - redact all unexpected operational failures.
        print("provisioning_failed", file=sys.stderr)
        return 1

    print(
        json.dumps(
            {
                "opc_id": result.opc_id,
                "owner_user_id": str(result.owner_user_id),
                "role": result.role,
                "status": result.status,
            },
            sort_keys=True,
        )
    )
    return 0


def entrypoint() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    entrypoint()
