from importlib import import_module


class FakeConnection:
    def __init__(self) -> None:
        self.statements = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        return None

    async def execute(self, statement) -> None:
        self.statements.append(statement)


class FakeEngine:
    def __init__(self) -> None:
        self.connection = FakeConnection()

    def connect(self):
        return self.connection


async def test_database_ping_executes_a_minimal_query() -> None:
    database_module = import_module("product_engine.db")
    engine = FakeEngine()
    database = database_module.Database("postgresql+asyncpg://unused", engine=engine)

    await database.ping()

    assert len(engine.connection.statements) == 1
    assert str(engine.connection.statements[0]) == "SELECT 1"
