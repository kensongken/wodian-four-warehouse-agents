from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
BACKEND = ROOT / "services/backend"


def test_backend_build_context_excludes_local_secrets_and_state() -> None:
    ignored = set((BACKEND / ".dockerignore").read_text(encoding="utf-8").splitlines())

    assert {".env", ".venv", ".pytest_cache", ".mypy_cache", ".ruff_cache"} <= ignored
    assert ".local-data" in ignored


def test_backend_image_runs_as_non_root_without_embedded_secrets() -> None:
    dockerfile = (BACKEND / "Dockerfile").read_text(encoding="utf-8")

    assert "USER app" in dockerfile
    assert "COPY .env" not in dockerfile
    assert "SIGNING_SECRET=" not in dockerfile
    assert 'CMD [".venv/bin/uvicorn"' in dockerfile


def test_compose_separates_migration_api_and_restartable_worker() -> None:
    compose = (ROOT / "infra/compose.yaml").read_text(encoding="utf-8")

    assert "  migrate:" in compose
    assert "  api:" in compose
    assert "  outbox-worker:" in compose
    assert "condition: service_completed_successfully" in compose
    worker = compose.split("  outbox-worker:", 1)[1].split("volumes:", 1)[0]
    assert "product_engine.workers.outbox_daemon" in worker
    assert 'restart: "unless-stopped"' in worker


def test_compose_requires_runtime_signing_secrets_without_defaults() -> None:
    compose = (ROOT / "infra/compose.yaml").read_text(encoding="utf-8")

    for name in (
        "ALBUM_SIGNING_SECRET",
        "SALES_SIGNING_SECRET",
        "STYLE_SIGNING_SECRET",
        "ADMIN_SIGNING_SECRET",
        "SALES_WEBHOOK_URL",
    ):
        assert f"${{{name}:?" in compose
