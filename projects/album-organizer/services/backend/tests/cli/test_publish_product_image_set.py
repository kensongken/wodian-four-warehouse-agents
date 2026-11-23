import json
from collections.abc import Awaitable, Callable
from pathlib import Path

from photo_organizer.handoff.http_publisher import (
    PublicationDisposition,
    PublicationReceipt,
)
from photo_organizer.handoff.product_image_set import ProductImageSetReadyV1

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "fixtures/v0.1/product-image-set-ready.synthetic.json"
)
Runner = Callable[
    [ProductImageSetReadyV1, str, bytes, str, int], Awaitable[PublicationReceipt]
]


def _arguments(fixture: Path = FIXTURE, *, confirm: bool = True) -> list[str]:
    arguments = [
        "--fixture",
        str(fixture),
        "--endpoint",
        "http://127.0.0.1:18020/v1/integrations/album/product-image-sets",
    ]
    if confirm:
        arguments.append("--confirm-synthetic")
    return arguments


def _environment() -> dict[str, str]:
    return {
        "DATABASE_URL": "postgresql://photo_organizer:synthetic-db-secret@127.0.0.1/db",
        "PRODUCT_IMAGE_SET_SIGNING_SECRET": "synthetic-signing-secret-with-at-least-32-bytes",
    }


def test_requires_explicit_synthetic_confirmation(capsys) -> None:
    from photo_organizer.cli.publish_product_image_set import main

    called = False

    async def runner(*args: object) -> PublicationReceipt:
        nonlocal called
        del args
        called = True
        raise AssertionError("runner must not be called")

    assert main(_arguments(confirm=False), environ=_environment(), runner=runner) == 2
    console = capsys.readouterr()
    assert console.err == "synthetic_confirmation_required\n"
    assert called is False


def test_rejects_fixture_that_is_not_the_frozen_synthetic_sample(
    tmp_path: Path, capsys
) -> None:
    from photo_organizer.cli.publish_product_image_set import main

    changed = tmp_path / "product-image-set-ready.synthetic.json"
    changed.write_text('{"synthetic_only": true}')

    async def runner(*args: object) -> PublicationReceipt:
        del args
        raise AssertionError("runner must not be called")

    assert main(_arguments(changed), environ=_environment(), runner=runner) == 2
    assert capsys.readouterr().err == "frozen_synthetic_fixture_required\n"


def test_delivered_fixture_prints_only_safe_receipt(capsys) -> None:
    from photo_organizer.cli.publish_product_image_set import main

    captured: list[tuple[str, str, str, int]] = []

    async def runner(
        event: ProductImageSetReadyV1,
        endpoint: str,
        secret: bytes,
        database_url: str,
        attempt: int,
    ) -> PublicationReceipt:
        captured.append((event.product_code, endpoint, database_url, attempt))
        assert secret == _environment()["PRODUCT_IMAGE_SET_SIGNING_SECRET"].encode()
        return PublicationReceipt(PublicationDisposition.DELIVERED, 202, None)

    assert main(_arguments(), environ=_environment(), runner=runner) == 0
    console = capsys.readouterr()
    assert json.loads(console.out) == {
        "disposition": "delivered",
        "retry_after_seconds": None,
        "status_code": 202,
    }
    assert console.err == ""
    assert captured == [
        (
            "SYNTHETIC-001",
            "http://127.0.0.1:18020/v1/integrations/album/product-image-sets",
            "postgresql+asyncpg://photo_organizer:synthetic-db-secret@127.0.0.1/db",
            1,
        )
    ]
    assert "synthetic-db-secret" not in console.out
    assert "synthetic-signing-secret" not in console.out


def test_missing_operational_secrets_fails_closed(capsys) -> None:
    from photo_organizer.cli.publish_product_image_set import main

    async def runner(*args: object) -> PublicationReceipt:
        del args
        raise AssertionError("runner must not be called")

    assert main(_arguments(), environ={}, runner=runner) == 2
    assert capsys.readouterr().err == "database_url_required\n"
