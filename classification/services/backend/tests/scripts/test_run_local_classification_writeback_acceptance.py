from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from product_engine.config import Settings

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "scripts/run_local_classification_writeback_acceptance.py"


def load_script():
    spec = importlib.util.spec_from_file_location("classification_acceptance", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_git_guard_refuses_dirty_or_wrong_candidate() -> None:
    module = load_script()

    with pytest.raises(module.AcceptanceRefusal, match="worktree_not_clean"):
        module.validate_git_state("a" * 40, " M changed.py\n", "a" * 40)
    with pytest.raises(module.AcceptanceRefusal, match="candidate_sha_mismatch"):
        module.validate_git_state("a" * 40, "", "b" * 40)


def test_batch_guard_requires_exact_authorized_batch_and_hashes(tmp_path: Path) -> None:
    module = load_script()
    image = tmp_path / "image.jpg"
    image.write_bytes(b"authorized")
    manifest = {
        "batch_id": "four-warehouse-real-local-20260920-001",
        "items": [
            {
                "sanitized_file": "image.jpg",
                "sanitized_sha256": hashlib.sha256(b"authorized").hexdigest(),
            }
        ],
    }
    (tmp_path / "batch-manifest.json").write_text(json.dumps(manifest))

    verified = module.validate_batch(tmp_path)
    assert verified[0]["sanitized_sha256"] == manifest["items"][0]["sanitized_sha256"]

    manifest["batch_id"] = "another-batch"
    (tmp_path / "batch-manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(module.AcceptanceRefusal, match="unauthorized_batch"):
        module.validate_batch(tmp_path)

    manifest["batch_id"] = "four-warehouse-real-local-20260920-001"
    manifest["items"][0]["sanitized_sha256"] = "0" * 64
    (tmp_path / "batch-manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(module.AcceptanceRefusal, match="image_hash_mismatch"):
        module.validate_batch(tmp_path)


@pytest.mark.parametrize(
    "database_name",
    ["classification_production", "classification_acceptance", "acceptance_classification"],
)
def test_database_guard_requires_isolated_acceptance_marker(database_name: str) -> None:
    module = load_script()
    with pytest.raises(module.AcceptanceRefusal, match="unsafe_database_name"):
        module.validate_database_name(database_name)


def test_network_guard_allows_loopback_only() -> None:
    module = load_script()

    module.validate_loopback_urls(
        ["http://127.0.0.1:18036/local/templates", "http://localhost:9000"]
    )
    with pytest.raises(module.AcceptanceRefusal, match="external_network_forbidden"):
        module.validate_loopback_urls(["https://api.openai.com/v1/responses"])


def test_acceptance_server_environment_satisfies_runtime_settings(tmp_path: Path) -> None:
    module = load_script()

    environment = module._server_environment(
        database_url="postgresql+asyncpg://user:pass@127.0.0.1/db",
        tenant_id="22222222-2222-4222-8222-222222222222",
        data_root=tmp_path,
        python_path=tmp_path / "src",
        inherited={},
    )

    settings = Settings(**{key.lower(): value for key, value in environment.items()})
    assert str(settings.sales_webhook_url) == "http://127.0.0.1:9/disabled"
    assert settings.style_signing_secret.get_secret_value() != (
        settings.sales_signing_secret.get_secret_value()
    )
    assert environment["LOCAL_DATA_ROOT"] == str(tmp_path)


def test_object_hash_guard_checks_every_remote_byte() -> None:
    module = load_script()
    expected = hashlib.sha256(b"authorized").hexdigest()

    verified = module.verify_object_hashes(
        [{"object_key": "tenant/product/original", "sha256": expected}],
        lambda _key: b"authorized",
    )

    assert verified == [{"object_key": "tenant/product/original", "sha256": expected}]
    with pytest.raises(module.AcceptanceRefusal, match="minio_object_hash_mismatch"):
        module.verify_object_hashes(
            [{"object_key": "tenant/product/original", "sha256": expected}],
            lambda _key: b"changed",
        )


def test_minio_hashes_must_be_the_same_authorized_local_set() -> None:
    module = load_script()

    module.validate_object_set(
        [{"sanitized_sha256": "a" * 64}, {"sanitized_sha256": "b" * 64}],
        [{"object_key": "one", "sha256": "b" * 64}, {"object_key": "two", "sha256": "a" * 64}],
    )
    with pytest.raises(module.AcceptanceRefusal, match="minio_manifest_hash_mismatch"):
        module.validate_object_set(
            [{"sanitized_sha256": "a" * 64}],
            [{"object_key": "one", "sha256": "b" * 64}],
        )


def test_style_receipt_guard_requires_no_local_handoffs(tmp_path: Path) -> None:
    module = load_script()

    assert module.require_no_style_handoffs(tmp_path) == 0
    (tmp_path / "style-handoffs.json").write_text('[{"handoff_id":"existing"}]')

    with pytest.raises(module.AcceptanceRefusal, match="style_handoff_not_zero"):
        module.require_no_style_handoffs(tmp_path)
