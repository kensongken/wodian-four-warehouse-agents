from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import zipfile
from hashlib import sha256
from pathlib import Path
from types import ModuleType
from typing import Any

from jsonschema import Draft202012Validator

from photo_organizer.catalog.standard_product_package import (
    build_standard_product_package,
)
from photo_organizer.schemas.mall_draft_export import MallProductDraftBatch

ROOT = Path(__file__).resolve().parents[4]
SCRIPT_PATH = ROOT / "scripts/export_standard_product_package.py"
SOURCE_BATCH = ROOT / "contracts/fixtures/opc-mall-product-draft-batch-synthetic.json"
SCHEMA_PATH = ROOT / "contracts/opc-standard-product-package-v1.schema.json"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "export_standard_product_package",
        SCRIPT_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load exporter")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _canonical_batch_id(batch: dict[str, Any]) -> str:
    canonical = json.dumps(
        {
            "opc_id": batch["opc_id"],
            "data_classification": batch["data_classification"],
            "export_version": 1,
            "operation": "create_draft_only",
            "existing_product_policy": ("emit_change_proposal_do_not_overwrite"),
            "auto_publish": False,
            "shared_market_opt_in": False,
            "items": batch["items"],
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"mall-draft-batch:{sha256(canonical.encode()).hexdigest()}"


def _input(tmp_path: Path) -> tuple[Path, list[tuple[str, Path]]]:
    batch = json.loads(SOURCE_BATCH.read_text(encoding="utf-8"))
    batch["items"] = batch["items"][:1]
    contents = (
        b"\xff\xd8\xff\xe0JFIF\x00" + b"\x11" * 32,
        b"\xff\xd8\xff\xe1Exif\x00" + b"\x22" * 32,
    )
    assets: list[tuple[str, Path]] = []
    for position, (image, content) in enumerate(
        zip(batch["items"][0]["images"], contents, strict=True),
        start=1,
    ):
        image["sha256"] = sha256(content).hexdigest()
        image["byte_size"] = len(content)
        path = tmp_path / f"private-source-{position}.bin"
        path.write_bytes(content)
        assets.append((image["asset_id"], path))
    batch["batch_id"] = _canonical_batch_id(batch)
    batch_path = tmp_path / "batch.json"
    batch_path.write_text(json.dumps(batch), encoding="utf-8")
    return batch_path, assets


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _asset_args(assets: list[tuple[str, Path]]) -> list[str]:
    arguments: list[str] = []
    for asset_id, path in assets:
        arguments.extend(("--asset", f"{asset_id}={path}"))
    return arguments


def test_cli_writes_a_deterministic_offline_package(tmp_path: Path) -> None:
    batch, assets = _input(tmp_path)
    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"

    first_result = _run(
        "--batch",
        str(batch),
        *_asset_args(assets),
        "--output",
        str(first),
    )
    second_result = _run(
        "--batch",
        str(batch),
        *_asset_args(list(reversed(assets))),
        "--output",
        str(second),
    )

    assert first_result.returncode == second_result.returncode == 0
    assert first_result.stdout == first_result.stderr == ""
    assert second_result.stdout == second_result.stderr == ""
    assert first.read_bytes() == second.read_bytes()
    with zipfile.ZipFile(first) as archive:
        assert len(archive.namelist()) == 4
        assert archive.namelist()[-1].endswith("/manifest.json")
        manifest = json.loads(archive.read(archive.namelist()[-1]))
    assert manifest["operation"] == "create_draft_only"
    assert manifest["auto_publish"] is False
    assert manifest["shared_market_opt_in"] is False
    assert manifest["items"][0]["mall_product_id"] is None
    assert manifest["items"][0]["bead_id"] is None


def test_cli_argument_errors_never_echo_unknown_secret_values() -> None:
    secret = "https://operator:secret@private.invalid"

    result = _run("--database-url", secret)

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == "invalid_arguments\n"
    assert secret not in result.stderr


def test_cli_rejects_a_signed_url_asset_without_echoing_it(tmp_path: Path) -> None:
    batch, assets = _input(tmp_path)
    secret = "HTTPS://store.invalid/a?X-Amz-Signature=secret"

    result = _run(
        "--batch",
        str(batch),
        "--asset",
        f"{assets[0][0]}={secret}",
        "--asset",
        f"{assets[1][0]}={assets[1][1]}",
        "--output",
        str(tmp_path / "package.zip"),
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == "invalid_arguments\n"
    assert secret not in result.stderr


def test_checked_in_manifest_schema_is_current() -> None:
    exporter = _load_script()

    expected = exporter.build_schema()
    actual = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    assert actual == expected
    Draft202012Validator.check_schema(actual)
    assert actual["x-semantic-validation"] == {
        "required": True,
        "command": (
            "python scripts/export_standard_product_package.py "
            "--verify-manifest <manifest.json>"
        ),
        "stable_error": "unsafe_manifest",
    }


def test_cli_semantically_verifies_manifest_without_leaking_its_path(
    tmp_path: Path,
) -> None:
    batch = MallProductDraftBatch.model_validate_json(
        SOURCE_BATCH.read_text(encoding="utf-8")
    )
    manifest = build_standard_product_package(batch).manifest.model_dump(mode="json")
    manifest["root_directory"] = "export-aaaaaaaaaaaaaaaa"
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert list(Draft202012Validator(schema).iter_errors(manifest)) == []
    secret_path = tmp_path / "private-customer-secret-manifest.json"
    secret_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = _run("--verify-manifest", str(secret_path))

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == "unsafe_manifest\n"
    assert str(secret_path) not in result.stderr


def test_manifest_verifier_uses_one_safe_error_for_forbidden_values(
    tmp_path: Path,
) -> None:
    batch = MallProductDraftBatch.model_validate_json(
        SOURCE_BATCH.read_text(encoding="utf-8")
    )
    manifest = build_standard_product_package(batch).manifest.model_dump(mode="json")
    secret = "https://operator:secret@private.invalid"
    manifest["download_url"] = secret
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    result = _run("--verify-manifest", str(path))

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == "unsafe_manifest\n"
    assert secret not in result.stderr


def test_deep_json_never_leaks_a_recursion_traceback(tmp_path: Path) -> None:
    deep = tmp_path / "deep-private-input.json"
    deep.write_text("[" * 2000 + "0" + "]" * 2000, encoding="utf-8")

    batch_result = _run(
        "--batch",
        str(deep),
        "--asset",
        "30000000-0000-4000-8000-000000000011=unused.bin",
        "--output",
        str(tmp_path / "package.zip"),
    )
    manifest_result = _run("--verify-manifest", str(deep))

    assert batch_result.returncode == manifest_result.returncode == 2
    assert batch_result.stdout == manifest_result.stdout == ""
    assert batch_result.stderr == "invalid_input\n"
    assert manifest_result.stderr == "unsafe_manifest\n"
    assert "Traceback" not in batch_result.stderr + manifest_result.stderr
    assert str(deep) not in batch_result.stderr + manifest_result.stderr


def test_cli_check_is_read_only_and_duplicate_asset_is_rejected(
    tmp_path: Path,
) -> None:
    batch, assets = _input(tmp_path)
    output = tmp_path / "package.zip"
    written = _run(
        "--batch",
        str(batch),
        *_asset_args(assets),
        "--output",
        str(output),
    )
    assert written.returncode == 0
    original = output.read_bytes()
    checked = _run(
        "--batch",
        str(batch),
        *_asset_args(assets),
        "--output",
        str(output),
        "--check",
    )
    duplicate = _run(
        "--batch",
        str(batch),
        *_asset_args(assets),
        "--asset",
        f"{assets[0][0]}={assets[0][1]}",
        "--output",
        str(output),
    )

    assert checked.returncode == 0
    assert checked.stdout == checked.stderr == ""
    assert output.read_bytes() == original
    assert duplicate.returncode == 2
    assert duplicate.stdout == ""
    assert duplicate.stderr == "invalid_arguments\n"


def test_cli_rejects_unsafe_json_without_echoing_the_value(
    tmp_path: Path,
) -> None:
    batch, assets = _input(tmp_path)
    value = json.loads(batch.read_text(encoding="utf-8"))
    secret = "https://operator:secret@private.invalid"
    value["download_url"] = secret
    batch.write_text(json.dumps(value), encoding="utf-8")

    result = _run(
        "--batch",
        str(batch),
        *_asset_args(assets),
        "--output",
        str(tmp_path / "package.zip"),
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == "unsafe_batch\n"
    assert secret not in result.stderr
