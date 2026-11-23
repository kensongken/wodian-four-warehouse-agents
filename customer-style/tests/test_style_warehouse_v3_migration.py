import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session


def test_migration_creates_isolated_style_warehouse_tables(
    migrated_session: Session,
) -> None:
    inspector = inspect(migrated_session.bind)

    assert {
        "style_warehouse_v3_receipts",
        "style_warehouse_v3_attempts",
    }.issubset(inspector.get_table_names())
    receipt_columns = {
        column["name"] for column in inspector.get_columns("style_warehouse_v3_receipts")
    }
    assert {"tenant_id", "idempotency_key", "checked_payload_sha256", "payload"}.issubset(
        receipt_columns
    )
    assert "request_body" not in receipt_columns
    assert "signing_secret" not in receipt_columns

    migrated_session.execute(
        text(
            "INSERT INTO style_warehouse_v3_receipts ("
            "id, tenant_id, handoff_id, idempotency_key, checked_payload_sha256, "
            "decision, rejection_code, generation_job_id, payload, decided_at) VALUES ("
            "'11111111-1111-4111-8111-111111111111', "
            "'22222222-2222-4222-8222-222222222222', "
            "'33333333-3333-4333-8333-333333333333', 'key', :digest, "
            "'accepted', NULL, NULL, '{}', CURRENT_TIMESTAMP)"
        ),
        {"digest": "a" * 64},
    )
    migrated_session.flush()


def test_cli_migration_honors_database_url_environment(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    database_path = tmp_path / "environment-target.db"
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=root,
        env={
            **os.environ,
            "DATABASE_URL": f"sqlite:///{database_path}",
            "PYTHONPATH": str(root / "src"),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "style_warehouse_v3_receipts" in tables
