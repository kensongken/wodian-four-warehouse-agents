"""Verify all Alembic migrations against a disposable empty SQLite database."""

import sys
import tempfile
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text

from alembic import command

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _migration_config(database_url: str) -> Config:
    """Build Alembic configuration bound only to the supplied database URL."""
    config = Config(str(REPOSITORY_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPOSITORY_ROOT / "alembic"))
    config.set_main_option("prepend_sys_path", str(REPOSITORY_ROOT / "src"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def main() -> None:
    """Migrate a new database and assert that its recorded revision is Alembic head."""
    sys.path.insert(0, str(REPOSITORY_ROOT / "src"))
    with tempfile.TemporaryDirectory(prefix="opc-style-clean-migration-") as directory:
        database_path = Path(directory) / "clean-migration.db"
        database_url = f"sqlite:///{database_path}"
        config = _migration_config(database_url)
        expected_revision = ScriptDirectory.from_config(config).get_current_head()

        command.upgrade(config, "head")

        engine = create_engine(database_url)
        with engine.connect() as connection:
            actual_revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
        engine.dispose()

        if actual_revision != expected_revision:
            raise SystemExit(
                "clean_migration_verifier=FAIL "
                f"expected_revision={expected_revision} "
                f"actual_revision={actual_revision}"
            )

    print("clean_migration_verifier=PASS")
    print("temporary_database=true")
    print(f"verified_revision={actual_revision}")


if __name__ == "__main__":
    main()
