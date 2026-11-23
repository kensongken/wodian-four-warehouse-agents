import json
from collections.abc import Awaitable, Callable
from uuid import UUID

import pytest

from photo_organizer.admin.opc_provisioning import (
    OpcProvisioningError,
    OpcProvisioningInput,
    OpcProvisioningResult,
)
from photo_organizer.cli.provision_opc import main

OWNER_ID = UUID("00000000-0000-0000-0000-000000000201")
DATABASE_URL = "postgresql://operator:synthetic-secret@localhost/photo_organizer"
Runner = Callable[[OpcProvisioningInput, str], Awaitable[OpcProvisioningResult]]


def _arguments(*, owner_user_id: str = str(OWNER_ID)) -> list[str]:
    return [
        "--opc-id",
        "opc-demo",
        "--display-name",
        "  Demo OPC  ",
        "--owner-user-id",
        owner_user_id,
    ]


def test_success_prints_only_stable_identifiers(capsys) -> None:
    captured: list[tuple[OpcProvisioningInput, str]] = []

    async def runner(
        provision: OpcProvisioningInput,
        database_url: str,
    ) -> OpcProvisioningResult:
        captured.append((provision, database_url))
        return OpcProvisioningResult(
            status="created",
            opc_id=provision.opc_id,
            owner_user_id=provision.owner_user_id,
        )

    exit_code = main(
        _arguments(),
        environ={"DATABASE_URL": DATABASE_URL},
        runner=runner,
    )

    console = capsys.readouterr()
    assert exit_code == 0
    assert json.loads(console.out) == {
        "opc_id": "opc-demo",
        "owner_user_id": str(OWNER_ID),
        "role": "owner",
        "status": "created",
    }
    assert console.err == ""
    assert captured == [
        (
            OpcProvisioningInput(
                opc_id="opc-demo",
                display_name="Demo OPC",
                owner_user_id=OWNER_ID,
            ),
            "postgresql+asyncpg://operator:synthetic-secret@localhost/photo_organizer",
        )
    ]
    assert "synthetic-secret" not in console.out


def test_domain_conflict_returns_safe_stable_error(capsys) -> None:
    async def runner(
        provision: OpcProvisioningInput,
        database_url: str,
    ) -> OpcProvisioningResult:
        del provision, database_url
        raise OpcProvisioningError("opc_owner_conflict")

    exit_code = main(
        _arguments(),
        environ={"DATABASE_URL": DATABASE_URL},
        runner=runner,
    )

    console = capsys.readouterr()
    assert exit_code == 2
    assert console.out == ""
    assert console.err == "opc_owner_conflict\n"
    assert "synthetic-secret" not in console.err
    assert "Traceback" not in console.err


def test_missing_database_url_does_not_call_runner(capsys) -> None:
    called = False

    async def runner(
        provision: OpcProvisioningInput,
        database_url: str,
    ) -> OpcProvisioningResult:
        nonlocal called
        del provision, database_url
        called = True
        raise AssertionError("runner must not be called")

    exit_code = main(_arguments(), environ={}, runner=runner)

    console = capsys.readouterr()
    assert exit_code == 2
    assert console.out == ""
    assert console.err == "database_url_required\n"
    assert called is False


def test_unsupported_database_url_is_not_echoed(capsys) -> None:
    async def runner(
        provision: OpcProvisioningInput,
        database_url: str,
    ) -> OpcProvisioningResult:
        del provision, database_url
        raise AssertionError("runner must not be called")

    exit_code = main(
        _arguments(),
        environ={"DATABASE_URL": "sqlite:///private-customer.db"},
        runner=runner,
    )

    console = capsys.readouterr()
    assert exit_code == 2
    assert console.out == ""
    assert console.err == "unsupported_database_url\n"
    assert "private-customer" not in console.err


def test_malformed_owner_uuid_returns_safe_validation_error(capsys) -> None:
    async def runner(
        provision: OpcProvisioningInput,
        database_url: str,
    ) -> OpcProvisioningResult:
        del provision, database_url
        raise AssertionError("runner must not be called")

    exit_code = main(
        _arguments(owner_user_id="not-a-user-id"),
        environ={"DATABASE_URL": DATABASE_URL},
        runner=runner,
    )

    console = capsys.readouterr()
    assert exit_code == 2
    assert console.out == ""
    assert console.err == "invalid_arguments\n"


def test_unexpected_runner_failure_is_redacted(capsys) -> None:
    async def runner(
        provision: OpcProvisioningInput,
        database_url: str,
    ) -> OpcProvisioningResult:
        del provision, database_url
        raise RuntimeError("postgresql://operator:leaked-secret@localhost/database")

    exit_code = main(
        _arguments(),
        environ={"DATABASE_URL": DATABASE_URL},
        runner=runner,
    )

    console = capsys.readouterr()
    assert exit_code == 1
    assert console.out == ""
    assert console.err == "provisioning_failed\n"
    assert "leaked-secret" not in console.err


@pytest.mark.parametrize(
    ("unknown_flag", "sensitive_value"),
    [
        (
            "--database-url",
            "postgresql://operator:argument-secret@localhost/database",
        ),
        ("--phone-e164", "+8613800098999"),
    ],
)
def test_unknown_arguments_are_rejected_without_echoing_values(
    unknown_flag: str,
    sensitive_value: str,
    capsys,
) -> None:
    async def runner(
        provision: OpcProvisioningInput,
        database_url: str,
    ) -> OpcProvisioningResult:
        del provision, database_url
        raise AssertionError("runner must not be called")

    exit_code = main(
        [*_arguments(), unknown_flag, sensitive_value],
        environ={"DATABASE_URL": DATABASE_URL},
        runner=runner,
    )

    console = capsys.readouterr()
    assert exit_code == 2
    assert console.out == ""
    assert console.err == "invalid_arguments\n"
    assert sensitive_value not in console.err


def test_missing_required_arguments_return_redacted_error(capsys) -> None:
    exit_code = main([], environ={"DATABASE_URL": DATABASE_URL})

    console = capsys.readouterr()
    assert exit_code == 2
    assert console.out == ""
    assert console.err == "invalid_arguments\n"
