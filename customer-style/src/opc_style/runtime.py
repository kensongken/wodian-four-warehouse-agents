"""One immutable image supports API, migrations, grants and isolated executor."""

import argparse
import os
import re
import time
from decimal import Decimal
from pathlib import Path
from types import FrameType

import httpx
import uvicorn
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from opc_style.config import Settings
from opc_style.executor_runtime import (
    ExecutorRuntime,
    create_database_engine,
    create_executor_app,
)
from opc_style.production_runtime import build_production_store, readiness
from opc_style.production_settings import ProductionSettings
from opc_style.providers.identity_openai_backgrounds import OpenAIBackgroundGenerator
from opc_style.providers.openai_images import RealImageBudget
from opc_style.services.mall_style_executor import MallStyleExecutor
from opc_style.settings import RealImageProviderSettings


def _url(name: str) -> str:
    value = os.environ.get(name, "")
    if not value.startswith(("postgresql://", "postgresql+psycopg://")):
        raise RuntimeError("production_postgresql_required")
    return value


def migrate() -> None:
    _url("DATABASE_MIGRATION_URL")
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic-postgresql.ini"))
    config.set_main_option("script_location", str(root / "alembic_postgresql"))
    command.upgrade(config, "head")


def grant() -> None:
    engine = create_database_engine(_url("DATABASE_GRANT_URL"))
    roles = [
        os.environ.get("STYLE_API_DB_ROLE", ""),
        os.environ.get("STYLE_EXECUTOR_DB_ROLE", ""),
    ]
    if any(not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", role) for role in roles):
        raise RuntimeError("explicit_database_roles_required")
    with engine.begin() as connection:
        for role in set(roles):
            # Identifiers validated above, values never include a password.
            connection.execute(text(f'GRANT USAGE ON SCHEMA public TO "{role}"'))
            connection.execute(
                text(
                    f'GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO "{role}"'
                )
            )
            connection.execute(
                text(f'REVOKE ALL ON style_pg_alembic_version FROM "{role}"')
            )
            connection.execute(
                text(f'GRANT SELECT ON style_pg_alembic_version TO "{role}"')
            )
            connection.execute(text(f'REVOKE CREATE ON SCHEMA public FROM "{role}"'))
        connection.execute(text("REVOKE CREATE ON SCHEMA public FROM PUBLIC"))
    engine.dispose()


def executor(*, once: bool) -> None:
    if not once:
        controller = ExecutorRuntime()

        class ExecutorServer(uvicorn.Server):
            def handle_exit(self, sig: int, frame: FrameType | None) -> None:
                controller.stop()
                super().handle_exit(sig, frame)

        ExecutorServer(
            uvicorn.Config(
                create_executor_app(controller),
                host="0.0.0.0",
                port=int(os.environ.get("PORT", "8080")),
                # Wait for the provider timeout and durable completion fence.
                timeout_graceful_shutdown=360,
            )
        ).run()
        return
    settings = ProductionSettings()
    if not settings.executor_enabled:
        print("provider_disabled: no paid calls")
        return
    store = build_production_store(settings)
    if store is None:
        raise RuntimeError("private_storage_required")
    engine = create_database_engine(_url("DATABASE_URL"))
    ok, _ = readiness(engine, store, settings, integration_enabled=True)
    if not ok:
        raise RuntimeError("production_readiness_required")
    real = RealImageProviderSettings()
    if real.model != settings.model or real.max_run_usd < settings.estimated_cost_usd:
        raise RuntimeError("provider_budget_configuration_mismatch")
    with httpx.Client() as transport:
        generator = OpenAIBackgroundGenerator(
            real,
            transport=transport,
            budget=RealImageBudget(limit_usd=real.max_run_usd),
            estimated_request_usd=Decimal(settings.estimated_cost_usd),
        )
        worker = MallStyleExecutor(
            sessionmaker(bind=engine, expire_on_commit=False, class_=Session),
            store,
            settings,
            generator=generator,
        )
        while True:
            worker.reconcile_expired_reservations()
            print(worker.run_once(), flush=True)
            if once:
                break
            time.sleep(5)
    store.close()
    engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "mode", choices=("api", "migration", "grant", "executor", "storage-check")
    )
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.mode == "api":
        if Settings().app_env != "production":
            raise RuntimeError("production_environment_required")
        uvicorn.run(
            "opc_style.main:app",
            host="0.0.0.0",
            port=int(os.environ.get("PORT", "8080")),
        )
    elif args.mode == "migration":
        migrate()
    elif args.mode == "grant":
        grant()
    elif args.mode == "executor":
        executor(once=args.once)
    else:
        store = build_production_store(ProductionSettings())
        if store is None:
            raise RuntimeError("private_storage_required")
        store.validate_backend()
        store.probe()
        print(
            "storage_contract_verified: backup restore and private access evidence still required"
        )
        store.close()


if __name__ == "__main__":
    main()
