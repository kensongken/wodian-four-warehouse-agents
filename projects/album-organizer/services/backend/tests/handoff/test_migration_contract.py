from __future__ import annotations

import os
import subprocess
from pathlib import Path


def test_0018_revision_fits_postgres_and_offline_sql_contains_authority_fks() -> None:
    backend = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [str(backend / ".venv/bin/alembic"), "upgrade", "head", "--sql"],
        cwd=backend,
        env={
            **os.environ,
            "DATABASE_URL": "postgresql://synthetic:synthetic@127.0.0.1/synthetic",
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "0018_image_set_delivery" in result.stdout
    assert len("0018_image_set_delivery") <= 32
    assert "CREATE TABLE opc_external_tenant_bindings" in result.stdout
    assert "CREATE TABLE product_code_bindings" in result.stdout
    assert "CREATE TABLE prepared_image_handoffs" in result.stdout
    assert "CREATE TABLE product_image_set_deliveries" in result.stdout
    assert "CREATE TABLE product_image_set_delivery_images" in result.stdout
    assert "REFERENCES opc_external_tenant_bindings" in result.stdout
    assert "REFERENCES product_classifications" in result.stdout
    assert "REFERENCES album_product_snapshots" in result.stdout
    assert "CREATE FUNCTION reject_handoff_authority_update" in result.stdout
    assert result.stdout.count("BEFORE UPDATE ON") == 3
