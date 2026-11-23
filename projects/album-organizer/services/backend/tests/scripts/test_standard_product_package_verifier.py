from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from hashlib import sha256
from pathlib import Path
from types import ModuleType
from uuid import UUID

import pytest
from jsonschema import Draft202012Validator

from photo_organizer.catalog.standard_product_package import (
    write_standard_product_package,
)
from photo_organizer.schemas.mall_draft_export import MallProductDraftBatch

ROOT = Path(__file__).resolve().parents[4]
SCRIPT_PATH = ROOT / "scripts/verify_standard_product_package.py"
SOURCE_BATCH = ROOT / "contracts/fixtures/opc-mall-product-draft-batch-synthetic.json"
SCHEMA_PATH = ROOT / "contracts/opc-standard-product-package-acceptance-v1.schema.json"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "verify_standard_product_package",
        SCRIPT_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load verifier")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _canonical_batch_id(batch: MallProductDraftBatch) -> str:
    canonical = json.dumps(
        {
            "opc_id": batch.opc_id,
            "data_classification": batch.data_classification,
            "export_version": 1,
            "operation": "create_draft_only",
            "existing_product_policy": ("emit_change_proposal_do_not_overwrite"),
            "auto_publish": False,
            "shared_market_opt_in": False,
            "items": [item.model_dump(mode="json") for item in batch.items],
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"mall-draft-batch:{sha256(canonical.encode()).hexdigest()}"


def _package(tmp_path: Path, *, salt: bytes = b"first") -> Path:
    source = MallProductDraftBatch.model_validate_json(SOURCE_BATCH.read_text())
    item = source.items[0]
    paths: dict[UUID, Path] = {}
    images = []
    for image in item.images:
        content = b"\xff\xd8\xff" + image.asset_id.bytes + salt
        path = tmp_path / f"{image.asset_id}-{salt.hex()}.bin"
        path.write_bytes(content)
        paths[image.asset_id] = path
        images.append(
            image.model_copy(
                update={
                    "sha256": sha256(content).hexdigest(),
                    "byte_size": len(content),
                    "media_type": "image/jpeg",
                }
            )
        )
    batch = source.model_copy(
        update={"items": (item.model_copy(update={"images": tuple(images)}),)}
    )
    batch = batch.model_copy(update={"batch_id": _canonical_batch_id(batch)})
    output = tmp_path / f"package-{salt.hex()}.zip"
    write_standard_product_package(batch, paths, output)
    return output


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_cli_writes_a_deterministic_redacted_acceptance_receipt(
    tmp_path: Path,
) -> None:
    package = _package(tmp_path)
    copied = tmp_path / "other" / "private-name.zip"
    copied.parent.mkdir()
    copied.write_bytes(package.read_bytes())
    first = tmp_path / "first.json"
    second = tmp_path / "other" / "second.json"

    first_result = _run("--package", str(package), "--output", str(first))
    second_result = _run("--package", str(copied), "--output", str(second))

    assert first_result.returncode == second_result.returncode == 0
    assert first_result.stdout == first_result.stderr == ""
    assert second_result.stdout == second_result.stderr == ""
    assert first.read_bytes() == second.read_bytes()
    receipt = json.loads(first.read_text())
    assert receipt["operation"] == "validate_only"
    assert receipt["material_authorization"] == (
        "opc_human_confirmed_for_draft_material_use"
    )
    assert receipt["product_sources"][0]["source_type"] in {
        "first_sale_bead",
        "legacy_material",
        "redesign_output",
    }
    assert receipt["publication_state"] == "draft"
    assert receipt["auto_publish"] is False
    assert receipt["shared_market_opt_in"] is False
    assert receipt["mall_product_ids"] == []
    assert receipt["bead_ids"] == []
    serialized = first.read_text().lower()
    assert "http://" not in serialized
    assert "https://" not in serialized
    assert str(tmp_path).lower() not in serialized


def test_cli_check_is_read_only_and_detects_a_stale_receipt(tmp_path: Path) -> None:
    first_package = _package(tmp_path, salt=b"first")
    second_package = _package(tmp_path, salt=b"second")
    receipt = tmp_path / "receipt.json"
    assert (
        _run("--package", str(first_package), "--output", str(receipt)).returncode == 0
    )
    original = receipt.read_bytes()
    inode = receipt.stat().st_ino

    current = _run(
        "--package",
        str(first_package),
        "--output",
        str(receipt),
        "--check",
    )
    stale = _run(
        "--package",
        str(second_package),
        "--output",
        str(receipt),
        "--check",
    )

    assert current.returncode == 0
    assert stale.returncode == 1
    assert stale.stdout == ""
    assert stale.stderr == "receipt_stale\n"
    assert receipt.read_bytes() == original
    assert receipt.stat().st_ino == inode


def test_cli_rejects_unknown_secrets_urls_and_colliding_paths(
    tmp_path: Path,
) -> None:
    secret = "https://operator:secret@private.invalid/a.zip?token=secret"
    unknown = _run("--database-url", secret)
    package_url = _run(
        "--package",
        secret,
        "--output",
        str(tmp_path / "receipt.json"),
    )
    package = _package(tmp_path)
    collision = _run("--package", str(package), "--output", str(package))
    hardlink = tmp_path / "package-hardlink.zip"
    os.link(package, hardlink)
    linked = _run("--package", str(package), "--output", str(hardlink))

    for result in (unknown, package_url, collision, linked):
        assert result.returncode == 2
        assert result.stdout == ""
        assert result.stderr == "invalid_arguments\n"
        assert secret not in result.stderr


def test_checked_in_acceptance_schema_is_current_and_strict() -> None:
    verifier = _load_script()

    expected = verifier.build_schema()
    actual = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    assert actual == expected
    Draft202012Validator.check_schema(actual)
    assert actual["additionalProperties"] is False
    properties = actual["properties"]
    assert properties["schema_version"]["const"] == (
        "opc-standard-product-package-acceptance/1.0"
    )
    assert properties["auto_publish"]["const"] is False
    assert properties["shared_market_opt_in"]["const"] is False
    assert properties["mall_product_ids"]["maxItems"] == 0
    assert properties["bead_ids"]["maxItems"] == 0
    assert actual["x-semantic-validation"] == {
        "required": True,
        "command": (
            "python scripts/verify_standard_product_package.py "
            "--package <package.zip> --output <receipt.json> --check"
        ),
        "stable_error": "receipt_stale",
    }


def test_schema_valid_mutation_is_rejected_by_required_package_check(
    tmp_path: Path,
) -> None:
    package = _package(tmp_path)
    receipt_path = tmp_path / "receipt.json"
    assert (
        _run("--package", str(package), "--output", str(receipt_path)).returncode == 0
    )
    receipt = json.loads(receipt_path.read_text())
    receipt["acceptance_id"] = f"standard-package-acceptance:{'cd' * 32}"
    receipt["product_sources"][0]["snapshot_version"] += 1
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert list(Draft202012Validator(schema).iter_errors(receipt)) == []
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    result = _run(
        "--package",
        str(package),
        "--output",
        str(receipt_path),
        "--check",
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "receipt_stale\n"


def test_atomic_output_failure_preserves_existing_receipt_and_cleans_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verifier = _load_script()
    output = tmp_path / "receipt.json"
    output.write_text("existing", encoding="utf-8")

    def fail_replace(_source: Path, _target: Path) -> None:
        raise OSError("private path must not escape")

    monkeypatch.setattr(verifier.os, "replace", fail_replace)

    with pytest.raises(verifier.StandardProductPackageAcceptanceError) as caught:
        verifier._atomic_write(output, "replacement")

    assert caught.value.code == "output_unavailable"
    assert output.read_text(encoding="utf-8") == "existing"
    assert list(tmp_path.glob(".receipt.json.*")) == []


def test_artifact_modes_are_deterministic_and_exclusive(tmp_path: Path) -> None:
    checked = _run("--check-artifacts")
    mixed = _run(
        "--check-artifacts",
        "--package",
        str(tmp_path / "private.zip"),
    )

    assert checked.returncode == 0
    assert checked.stdout == checked.stderr == ""
    assert mixed.returncode == 2
    assert mixed.stdout == ""
    assert mixed.stderr == "invalid_arguments\n"
