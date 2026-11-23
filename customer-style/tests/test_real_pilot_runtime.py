"""Persistent real-pilot startup wiring."""

import runpy
from pathlib import Path
from types import SimpleNamespace
from typing import Any


def test_launcher_migrates_the_runtime_database_before_serving(
    monkeypatch,
) -> None:
    observed: list[tuple[str, Any]] = []

    class FakeConfig:
        def __init__(self, path: str) -> None:
            observed.append(("config", path))

        def set_main_option(self, key: str, value: str) -> None:
            observed.append((key, value))

    monkeypatch.setattr("alembic.config.Config", FakeConfig)
    monkeypatch.setattr(
        "alembic.command.upgrade",
        lambda config, revision: observed.append(("upgrade", revision)),
    )
    monkeypatch.setattr(
        "uvicorn.run",
        lambda app, **kwargs: observed.append(("serve", (app, kwargs))),
    )
    monkeypatch.setattr(
        "opc_style.config.settings",
        SimpleNamespace(database_url="sqlite:////persistent/runtime.sqlite3"),
    )

    runpy.run_path(
        str(Path("scripts/launch_real_style_pilot.py")),
        run_name="__main__",
    )

    assert observed == [
        ("config", "alembic.ini"),
        ("sqlalchemy.url", "sqlite:////persistent/runtime.sqlite3"),
        ("upgrade", "head"),
        (
            "serve",
            (
                "opc_style.main:app",
                {"host": "127.0.0.1", "port": 8000, "log_level": "info"},
            ),
        ),
    ]
