"""Migrate the configured persistent database, then start the local pilot."""

import os

import uvicorn
from alembic.config import Config

from alembic import command
from opc_style.config import settings


def main() -> None:
    """Apply every migration to the same database the app will open."""
    configuration = Config("alembic.ini")
    configuration.set_main_option("sqlalchemy.url", settings.database_url)
    command.upgrade(configuration, "head")
    uvicorn.run(
        "opc_style.main:app",
        host="127.0.0.1",
        port=int(os.environ.get("LOCAL_STYLE_PORT", "8000")),
        log_level="info",
    )


if __name__ == "__main__":
    main()
