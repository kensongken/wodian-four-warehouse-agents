import pytest

from photo_organizer.cli.cleanup import main


@pytest.mark.asyncio
async def test_cleanup_cli_runs_one_explicit_bounded_drain() -> None:
    """The operational entrypoint delegates one bounded pass without app wiring."""
    calls: list[tuple[object, int]] = []

    class Service:
        async def process_cleanup_jobs(self, user_id=None, *, limit: int) -> None:
            calls.append((user_id, limit))

    await main(Service())  # type: ignore[arg-type]

    assert calls == [(None, 100)]
