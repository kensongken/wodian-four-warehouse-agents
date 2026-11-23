from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

ROOT = Path(__file__).resolve().parents[4]
SCRIPT_PATH = ROOT / "scripts/verify_first_sku_material.py"
SCHEMA_PATH = ROOT / "contracts/opc-sku-material-acceptance-v2.schema.json"
FIXTURE_PATH = ROOT / "contracts/fixtures/opc-sku-material-acceptance-v2-synthetic.json"
LEGACY_SCHEMA_PATH = ROOT / "contracts/opc-sku-material-acceptance-v1.schema.json"
LEGACY_FIXTURE_PATH = (
    ROOT / "contracts/fixtures/opc-sku-material-acceptance-synthetic.json"
)
SIGNED_URL = "https://signed.invalid/private?X-Amz-Signature=do-not-persist"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "verify_first_sku_material", SCRIPT_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load verifier")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_inputs(tmp_path: Path) -> tuple[list[str], dict[str, Path]]:
    verifier = _load_script()
    bundle = verifier.synthetic_source_bundle(download_url=SIGNED_URL)
    paths: dict[str, Path] = {}
    for name in ("group", "review", "manifest", "downloads"):
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(bundle[name]), encoding="utf-8")
        paths[name] = path
    args = [
        "--data-classification",
        "synthetic_no_customer_data",
        "--group",
        str(paths["group"]),
        "--review",
        str(paths["review"]),
        "--manifest",
        str(paths["manifest"]),
        "--downloads",
        str(paths["downloads"]),
    ]
    return args, paths


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _walk(value: Any) -> list[Any]:
    values = [value]
    if isinstance(value, dict):
        for key, child in value.items():
            values.extend(_walk(key))
            values.extend(_walk(child))
    elif isinstance(value, list):
        for child in value:
            values.extend(_walk(child))
    return values


def test_cli_writes_a_redacted_receipt_atomically(tmp_path: Path) -> None:
    args, _ = _write_inputs(tmp_path)
    output = tmp_path / "receipt.json"

    result = _run(*args, "--output", str(output))

    assert result.returncode == 0, result.stderr
    assert result.stdout == result.stderr == ""
    receipt = json.loads(output.read_text(encoding="utf-8"))
    assert receipt["schema_version"] == "opc-sku-material-acceptance/2.0"
    assert receipt["data_classification"] == "synthetic_no_customer_data"
    assert receipt["acceptance_status"] == ("material_ready_for_mall_draft_not_listed")
    assert receipt["download_ttl_seconds"] == 300
    assert len(receipt["images"]) == 2
    serialized = json.dumps(receipt)
    assert SIGNED_URL not in serialized
    assert "download_url" not in serialized
    assert "X-Amz-Signature" not in serialized


def test_cli_failure_prints_only_a_stable_code_and_preserves_output(
    tmp_path: Path,
) -> None:
    args, paths = _write_inputs(tmp_path)
    output = tmp_path / "receipt.json"
    output.write_text('{"existing":"safe"}\n', encoding="utf-8")
    review = json.loads(paths["review"].read_text(encoding="utf-8"))
    review["opc_id"] = "opc-mismatch"
    review["unsafe_token"] = "bearer-private-value"
    paths["review"].write_text(json.dumps(review), encoding="utf-8")

    result = _run(*args, "--output", str(output))

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == "unsafe_material_data\n"
    assert SIGNED_URL not in result.stderr
    assert "bearer-private-value" not in result.stderr
    assert str(paths["review"]) not in result.stderr
    assert output.read_text(encoding="utf-8") == '{"existing":"safe"}\n'


def test_checked_in_schema_and_synthetic_fixture_are_current_and_safe() -> None:
    verifier = _load_script()
    expected_schema, expected_fixture = verifier.build_artifacts()

    assert SCHEMA_PATH.read_text(encoding="utf-8") == verifier.render_json(
        expected_schema
    )
    assert FIXTURE_PATH.read_text(encoding="utf-8") == verifier.render_json(
        expected_fixture
    )
    Draft202012Validator.check_schema(expected_schema)
    Draft202012Validator(expected_schema, format_checker=FormatChecker()).validate(
        expected_fixture
    )
    assert expected_fixture["data_classification"] == "synthetic_no_customer_data"
    assert expected_fixture["mall_product_id"] is None
    assert expected_fixture["bead_id"] is None
    assert expected_fixture["auto_publish"] is False
    strings = [value for value in _walk(expected_fixture) if isinstance(value, str)]
    assert not any(
        value.startswith(("http://", "https://", "file:", "content:", "/"))
        or "X-Amz-" in value
        for value in strings
    )


def test_published_v1_artifacts_remain_readable_without_download_event() -> None:
    from photo_organizer.schemas.acceptance_receipt import (
        SkuMaterialAcceptanceReceiptV1,
    )

    schema = json.loads(LEGACY_SCHEMA_PATH.read_text(encoding="utf-8"))
    fixture = json.loads(LEGACY_FIXTURE_PATH.read_text(encoding="utf-8"))

    assert fixture["schema_version"] == "opc-sku-material-acceptance/1.0"
    assert "download_confirmation_event_id" not in fixture
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(fixture)
    parsed = SkuMaterialAcceptanceReceiptV1.model_validate(fixture)
    assert str(parsed.source_event_id) == fixture["source_event_id"]


def test_check_mode_reports_published_v1_receipt_as_stale_not_invalid(
    tmp_path: Path,
) -> None:
    args, _ = _write_inputs(tmp_path)
    output = tmp_path / "receipt.json"
    legacy = LEGACY_FIXTURE_PATH.read_bytes()
    output.write_bytes(legacy)

    result = _run(*args, "--output", str(output), "--check")

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "acceptance_receipt_stale\n"
    assert output.read_bytes() == legacy


def test_schema_rejects_a_noncanonical_evidence_reference() -> None:
    verifier = _load_script()
    schema, fixture = verifier.build_artifacts()
    unsafe = copy.deepcopy(fixture)
    unsafe["images"][0]["storage_ref"] = (
        "evidence/00000000000040008000000000000301/original"
    )

    with pytest.raises(ValidationError):
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(unsafe)


def test_check_mode_is_read_only_and_detects_drift(tmp_path: Path) -> None:
    args, _ = _write_inputs(tmp_path)
    output = tmp_path / "receipt.json"
    created = _run(*args, "--output", str(output))
    assert created.returncode == 0
    before = output.read_bytes()

    current = _run(*args, "--output", str(output), "--check")
    assert current.returncode == 0
    assert output.read_bytes() == before

    value = json.loads(output.read_text(encoding="utf-8"))
    value["album_product_id"] = "album-product-drifted"
    output.write_text(json.dumps(value), encoding="utf-8")
    drifted = output.read_bytes()

    stale = _run(*args, "--output", str(output), "--check")
    assert stale.returncode == 1
    assert stale.stderr == "acceptance_receipt_stale\n"
    assert output.read_bytes() == drifted


def test_parser_errors_do_not_echo_unknown_values() -> None:
    secret = "postgresql://operator:secret@localhost/private"

    result = _run("--database-url", secret)

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == "invalid_arguments\n"
    assert secret not in result.stderr


def test_output_failure_is_stable_and_does_not_echo_the_path(tmp_path: Path) -> None:
    args, _ = _write_inputs(tmp_path)
    non_directory = tmp_path / "blocked-output-parent"
    non_directory.write_text("not a directory", encoding="utf-8")
    output = non_directory / "receipt.json"

    result = _run(*args, "--output", str(output))

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == "output_unavailable\n"
    assert str(output) not in result.stderr


def test_naive_chain_timestamp_is_rejected_without_a_traceback(
    tmp_path: Path,
) -> None:
    args, paths = _write_inputs(tmp_path)
    review = json.loads(paths["review"].read_text(encoding="utf-8"))
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    review["decided_at"] = "2026-08-17T22:00:00"
    manifest["items"][0]["classification"]["confirmed_at"] = "2026-08-17T22:00:00"
    paths["review"].write_text(json.dumps(review), encoding="utf-8")
    paths["manifest"].write_text(json.dumps(manifest), encoding="utf-8")
    output = tmp_path / "receipt.json"

    result = _run(*args, "--output", str(output))

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == "invalid_input\n"
    assert SIGNED_URL not in result.stderr
    assert str(paths["review"]) not in result.stderr
    assert "Traceback" not in result.stderr
    assert not output.exists()
