"""Durable, customer-scoped storage contracts for the local style workspace."""

import json
import shutil
import stat
from hashlib import sha256
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from opc_style import settings as settings_module
from opc_style.domain.image_normalizer import NormalizedPayload
from opc_style.services.real_photos import (
    AssetContentStoreError,
    LocalSyntheticAssetStore,
)
from opc_style.settings import LocalBridgeSettings

CUSTOMER_A_ID = UUID("11111111-1111-4111-8111-111111111111")
CUSTOMER_B_ID = UUID("22222222-2222-4222-8222-222222222222")
ASSET_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
SAMPLE_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
FOREGROUND_VERSION_ID = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
STORE_IDENTITY_FILENAME = ".opc-store-identity.json"


def test_store_namespace_survives_complete_directory_copy(tmp_path: Path) -> None:
    source = tmp_path / "source"
    original = LocalSyntheticAssetStore(source)
    namespace = original.store_namespace
    original.close()

    copied_root = tmp_path / "copy"
    shutil.copytree(source, copied_root)
    copied = LocalSyntheticAssetStore(copied_root, reopen=True)

    assert copied.store_namespace == namespace


def test_store_identity_marker_is_private_and_versioned(tmp_path: Path) -> None:
    store = LocalSyntheticAssetStore(tmp_path / "media")
    marker = tmp_path / "media" / STORE_IDENTITY_FILENAME

    payload = json.loads(marker.read_text())

    assert payload == {
        "format": "opc-local-asset-store-v2",
        "store_id": str(UUID(payload["store_id"])),
    }
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600
    store.close()


def test_legacy_store_keeps_inode_namespace_until_explicit_recovery(
    tmp_path: Path,
) -> None:
    root = tmp_path / "legacy"
    root.mkdir(mode=0o700)

    store = LocalSyntheticAssetStore(root, reopen=True)

    assert store.store_namespace == store.legacy_store_namespace
    assert not (root / STORE_IDENTITY_FILENAME).exists()
    store.close()


def test_legacy_store_requires_explicit_stable_identity_initialization(
    tmp_path: Path,
) -> None:
    root = tmp_path / "legacy"
    root.mkdir(mode=0o700)
    store = LocalSyntheticAssetStore(root, reopen=True)
    legacy_namespace = store.store_namespace

    stable_namespace = store.initialize_stable_identity()

    assert stable_namespace != legacy_namespace
    assert store.store_namespace == stable_namespace
    store.close()
    assert LocalSyntheticAssetStore(root, reopen=True).store_namespace == stable_namespace


def test_default_local_asset_root_is_project_private(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("LOCAL_STYLE_ASSET_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)

    settings = LocalBridgeSettings(_env_file=None)

    assert settings_module.resolve_local_style_asset_root(settings, tmp_path) == (
        tmp_path / ".local-style-assets"
    ).resolve()


def test_configured_asset_root_must_be_absolute(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="local_style_asset_root_absolute"):
        LocalBridgeSettings(
            local_style_asset_root=tmp_path.name,
            _env_file=None,
        )


def test_original_and_style_bytes_survive_store_restart(tmp_path: Path) -> None:
    content = b"verified-local-image-bytes"
    digest = sha256(content).hexdigest()
    first = LocalSyntheticAssetStore(tmp_path / "media")
    first.persist_original(CUSTOMER_A_ID, ASSET_ID, content, digest)
    first.persist_style_sample_content(CUSTOMER_A_ID, SAMPLE_ID, content, digest)
    first.close()

    reopened = LocalSyntheticAssetStore(tmp_path / "media", reopen=True)

    assert reopened.read_original(CUSTOMER_A_ID, ASSET_ID, digest) == content
    assert (
        reopened.read_style_sample_content(CUSTOMER_A_ID, SAMPLE_ID, digest)
        == content
    )
    with pytest.raises(AssetContentStoreError):
        reopened.read_original(CUSTOMER_B_ID, ASSET_ID, digest)


def test_foreground_and_mask_bytes_survive_store_restart(tmp_path: Path) -> None:
    foreground = b"synthetic-rgba-foreground"
    mask = b"synthetic-one-channel-mask"
    foreground_digest = sha256(foreground).hexdigest()
    mask_digest = sha256(mask).hexdigest()
    first = LocalSyntheticAssetStore(tmp_path / "media")
    first.persist_product_foreground(
        CUSTOMER_A_ID, FOREGROUND_VERSION_ID, foreground, foreground_digest
    )
    first.persist_product_mask(
        CUSTOMER_A_ID, FOREGROUND_VERSION_ID, mask, mask_digest
    )
    first.close()

    reopened = LocalSyntheticAssetStore(tmp_path / "media", reopen=True)

    assert reopened.read_product_foreground(
        CUSTOMER_A_ID, FOREGROUND_VERSION_ID, foreground_digest
    ) == foreground
    assert reopened.read_product_mask(
        CUSTOMER_A_ID, FOREGROUND_VERSION_ID, mask_digest
    ) == mask
    assert reopened.verify_product_foreground(
        CUSTOMER_A_ID, FOREGROUND_VERSION_ID, foreground_digest
    )
    assert reopened.verify_product_mask(
        CUSTOMER_A_ID, FOREGROUND_VERSION_ID, mask_digest
    )
    with pytest.raises(AssetContentStoreError):
        reopened.read_product_foreground(
            CUSTOMER_B_ID, FOREGROUND_VERSION_ID, foreground_digest
        )


def test_foreground_artifact_discard_is_scoped_and_symlink_safe(tmp_path: Path) -> None:
    foreground = b"synthetic-rgba-foreground"
    digest = sha256(foreground).hexdigest()
    root = tmp_path / "media"
    store = LocalSyntheticAssetStore(root)
    store.persist_product_foreground(
        CUSTOMER_A_ID, FOREGROUND_VERSION_ID, foreground, digest
    )
    target = (
        root
        / "customers"
        / str(CUSTOMER_A_ID)
        / "product-foregrounds"
        / f"{FOREGROUND_VERSION_ID}.bin"
    )
    outside = tmp_path / "outside-foreground.bin"
    outside.write_bytes(foreground)
    target.unlink()
    target.symlink_to(outside)

    with pytest.raises(AssetContentStoreError, match="asset_delete_scope_invalid"):
        store.discard_product_foreground(
            CUSTOMER_A_ID, FOREGROUND_VERSION_ID, digest
        )

    assert outside.read_bytes() == foreground


def test_png_identity_locked_candidates_survive_store_restart(tmp_path: Path) -> None:
    root = tmp_path / "media"
    job_id = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
    content = b"sealed-png-candidate"
    first = LocalSyntheticAssetStore(root)
    target = first.reserve_derivative(CUSTOMER_A_ID, job_id, 1, extension="png")
    first.create_derivative(
        target,
        NormalizedPayload(
            content=content,
            content_sha256=sha256(content).hexdigest(),
            mime_type="image/png",
            width=1,
            height=1,
            metadata={},
        ),
    )
    first.close()

    reopened = LocalSyntheticAssetStore(root, reopen=True)

    assert reopened.recovered_job_ids == (job_id,)
    assert reopened.read_derivative(
        CUSTOMER_A_ID, job_id, 1, sha256(content).hexdigest()
    ) == content


def test_persistent_write_is_idempotent_but_rejects_conflicting_bytes(
    tmp_path: Path,
) -> None:
    content = b"verified-local-image-bytes"
    conflicting = b"different-image-bytes"
    digest = sha256(content).hexdigest()
    store = LocalSyntheticAssetStore(tmp_path / "media")

    store.persist_original(CUSTOMER_A_ID, ASSET_ID, content, digest)
    store.persist_original(CUSTOMER_A_ID, ASSET_ID, content, digest)

    with pytest.raises(AssetContentStoreError, match="asset_content_conflict"):
        store.persist_original(
            CUSTOMER_A_ID,
            ASSET_ID,
            conflicting,
            sha256(conflicting).hexdigest(),
        )


def test_persistent_read_rejects_digest_tamper(tmp_path: Path) -> None:
    content = b"verified-local-image-bytes"
    digest = sha256(content).hexdigest()
    root = tmp_path / "media"
    store = LocalSyntheticAssetStore(root)
    store.persist_original(CUSTOMER_A_ID, ASSET_ID, content, digest)
    target = root / "customers" / str(CUSTOMER_A_ID) / "originals" / f"{ASSET_ID}.bin"
    target.write_bytes(b"tampered")

    with pytest.raises(AssetContentStoreError, match="asset_content_hash_mismatch"):
        store.read_original(CUSTOMER_A_ID, ASSET_ID, digest)


def test_persistent_read_rejects_symlink_target(tmp_path: Path) -> None:
    content = b"verified-local-image-bytes"
    digest = sha256(content).hexdigest()
    root = tmp_path / "media"
    store = LocalSyntheticAssetStore(root)
    store.persist_original(CUSTOMER_A_ID, ASSET_ID, content, digest)
    target = root / "customers" / str(CUSTOMER_A_ID) / "originals" / f"{ASSET_ID}.bin"
    outside = tmp_path / "outside.bin"
    outside.write_bytes(content)
    target.unlink()
    target.symlink_to(outside)

    with pytest.raises(AssetContentStoreError, match="asset_content_unavailable"):
        store.read_original(CUSTOMER_A_ID, ASSET_ID, digest)


def test_scoped_delete_never_unlinks_a_symlink_target(tmp_path: Path) -> None:
    content = b"verified-local-image-bytes"
    digest = sha256(content).hexdigest()
    root = tmp_path / "media"
    store = LocalSyntheticAssetStore(root)
    store.persist_original(CUSTOMER_A_ID, ASSET_ID, content, digest)
    target = root / "customers" / str(CUSTOMER_A_ID) / "originals" / f"{ASSET_ID}.bin"
    outside = tmp_path / "outside.bin"
    outside.write_bytes(content)
    target.unlink()
    target.symlink_to(outside)

    with pytest.raises(AssetContentStoreError, match="asset_delete_scope_invalid"):
        store.discard_uncommitted_asset(
            CUSTOMER_A_ID, "originals", ASSET_ID, digest
        )

    assert outside.read_bytes() == content


def test_unknown_file_is_not_discovered_or_imported(tmp_path: Path) -> None:
    root = tmp_path / "media"
    first = LocalSyntheticAssetStore(root)
    first.close()
    customer_root = root / "customers"
    customer_root.mkdir(mode=0o700)
    customer_directory = customer_root / str(CUSTOMER_A_ID)
    customer_directory.mkdir(mode=0o700)
    unknown = customer_directory / "originals"
    unknown.mkdir(mode=0o700)
    (unknown / f"{ASSET_ID}.bin").write_bytes(b"unknown")

    reopened = LocalSyntheticAssetStore(root, reopen=True)

    assert reopened.recovered_job_ids == ()
