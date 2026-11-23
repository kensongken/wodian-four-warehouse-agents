import pytest

from photo_organizer.repositories.unit_of_work import UnitOfWork


class SessionFailure(Exception):
    pass


class FakeAsyncSession:
    def __init__(
        self,
        *,
        commit_error: BaseException | None = None,
        rollback_error: BaseException | None = None,
    ) -> None:
        self.commit_error = commit_error
        self.rollback_error = rollback_error
        self.commits = 0
        self.rollbacks = 0
        self.closes = 0

    async def commit(self) -> None:
        self.commits += 1
        if self.commit_error is not None:
            raise self.commit_error

    async def rollback(self) -> None:
        self.rollbacks += 1
        if self.rollback_error is not None:
            raise self.rollback_error

    async def close(self) -> None:
        self.closes += 1


@pytest.mark.asyncio
async def test_successful_context_commits_and_closes_session() -> None:
    session = FakeAsyncSession()
    async with UnitOfWork(lambda: session) as unit_of_work:  # type: ignore[arg-type]
        assert unit_of_work.session is session

    assert (session.commits, session.rollbacks, session.closes) == (1, 0, 1)


@pytest.mark.asyncio
async def test_exception_context_rolls_back_and_closes_session() -> None:
    session = FakeAsyncSession()

    with pytest.raises(SessionFailure):
        async with UnitOfWork(lambda: session):  # type: ignore[arg-type]
            raise SessionFailure()

    assert (session.commits, session.rollbacks, session.closes) == (0, 1, 1)


@pytest.mark.asyncio
async def test_commit_failure_rolls_back_and_closes_session() -> None:
    session = FakeAsyncSession(commit_error=SessionFailure())

    with pytest.raises(SessionFailure):
        async with UnitOfWork(lambda: session):  # type: ignore[arg-type]
            pass

    assert (session.commits, session.rollbacks, session.closes) == (1, 1, 1)


@pytest.mark.asyncio
async def test_rollback_failure_still_closes_session() -> None:
    session = FakeAsyncSession(rollback_error=SessionFailure())

    with pytest.raises(SessionFailure):
        async with UnitOfWork(lambda: session):  # type: ignore[arg-type]
            raise RuntimeError("application failure")

    assert (session.commits, session.rollbacks, session.closes) == (0, 1, 1)
