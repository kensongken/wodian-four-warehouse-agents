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
SCRIPT_PATH = ROOT / "scripts/export_opc_mall_draft_batch.py"
SCHEMA_PATH = ROOT / "contracts/opc-mall-product-draft-batch-v1.schema.json"
FIXTURE_PATH = ROOT / "contracts/fixtures/opc-mall-product-draft-batch-synthetic.json"
ACCEPTANCE_FIXTURE = (
    ROOT / "contracts/fixtures/opc-sku-material-acceptance-v2-synthetic.json"
)
GENERATED_AT = "2026-08-17T23:30:00Z"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "export_opc_mall_draft_batch", SCRIPT_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load exporter")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _receipt_dict(index: int) -> dict[str, Any]:
    value = json.loads(ACCEPTANCE_FIXTURE.read_text(encoding="utf-8"))
    source_event_id = f"00000000-0000-4000-8000-{index:012x}"
    value["acceptance_id"] = f"sku-material-acceptance:{source_event_id}"
    value["album_product_id"] = f"album-product-{index:04d}"
    value["source_event_id"] = source_event_id
    value["download_confirmation_event_id"] = f"50000000-0000-4000-8000-{index:012x}"
    value["classification_id"] = f"10000000-0000-4000-8000-{index:012x}"
    value["review_id"] = f"20000000-0000-4000-8000-{index:012x}"
    asset_ids: list[str] = []
    for offset, image in enumerate(value["images"], start=1):
        asset_id = f"30000000-0000-4000-8000-{index * 10 + offset:012x}"
        evidence_id = f"40000000-0000-4000-8000-{index * 10 + offset:012x}"
        asset_ids.append(asset_id)
        image["asset_id"] = asset_id
        image["evidence_asset_id"] = evidence_id
        image["sha256"] = f"{index * 10 + offset:064x}"
        image["storage_ref"] = f"evidence/{evidence_id}/original"
    value["download_verified_asset_ids"] = asset_ids
    return value


def _write_receipts(tmp_path: Path, indexes: tuple[int, ...]) -> list[str]:
    args: list[str] = []
    for position, index in enumerate(indexes):
        path = tmp_path / f"receipt-{position}.json"
        path.write_text(json.dumps(_receipt_dict(index)), encoding="utf-8")
        args.extend(("--receipt", str(path)))
    return args


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


def test_cli_writes_a_sorted_redacted_batch(tmp_path: Path) -> None:
    args = _write_receipts(tmp_path, (2, 1))
    output = tmp_path / "batch.json"

    result = _run(
        *args,
        "--generated-at",
        GENERATED_AT,
        "--output",
        str(output),
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == result.stderr == ""
    batch = json.loads(output.read_text(encoding="utf-8"))
    assert batch["schema_version"] == "opc-mall-product-draft-batch/1.0"
    assert batch["operation"] == "create_draft_only"
    assert batch["auto_publish"] is False
    assert batch["shared_market_opt_in"] is False
    assert [item["source_album_product_id"] for item in batch["items"]] == [
        "album-product-0001",
        "album-product-0002",
    ]
    serialized = json.dumps(batch)
    assert "http://" not in serialized
    assert "https://" not in serialized
    assert "download_url" not in serialized
    assert "token" not in serialized


def test_check_mode_is_read_only_and_detects_drift(tmp_path: Path) -> None:
    args = _write_receipts(tmp_path, (1, 2))
    output = tmp_path / "batch.json"
    written = _run(
        *args,
        "--generated-at",
        GENERATED_AT,
        "--output",
        str(output),
    )
    assert written.returncode == 0, written.stderr

    current = _run(
        *args,
        "--generated-at",
        GENERATED_AT,
        "--output",
        str(output),
        "--check",
    )
    output.write_text("{}\n", encoding="utf-8")
    stale = _run(
        *args,
        "--generated-at",
        GENERATED_AT,
        "--output",
        str(output),
        "--check",
    )

    assert current.returncode == 0
    assert current.stdout == current.stderr == ""
    assert stale.returncode == 1
    assert stale.stdout == ""
    assert stale.stderr == "mall_draft_batch_stale\n"


def test_checked_in_schema_and_ten_item_fixture_are_current_and_safe() -> None:
    exporter = _load_script()
    expected_schema, expected_fixture = exporter.build_artifacts()
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    assert schema == expected_schema
    assert fixture == expected_fixture
    Draft202012Validator(
        schema,
        format_checker=FormatChecker(),
    ).validate(fixture)
    assert fixture["data_classification"] == "synthetic_no_customer_data"
    assert len(fixture["items"]) == 10
    assert len({item["draft_request_id"] for item in fixture["items"]}) == 10
    assert [item["source_type"] for item in fixture["items"]] == [
        "first_sale_bead",
        "first_sale_bead",
        "first_sale_bead",
        "first_sale_bead",
        "first_sale_bead",
        "first_sale_bead",
        "legacy_material",
        "legacy_material",
        "redesign_output",
        "redesign_output",
    ]
    assert fixture["auto_publish"] is False
    assert fixture["shared_market_opt_in"] is False
    flattened = _walk(fixture)
    assert not any(
        isinstance(value, str) and value.startswith(("http://", "https://"))
        for value in flattened
    )
    assert not {
        "access_token",
        "refresh_token",
        "download_url",
        "source_object_key",
        "customer_id",
        "order_id",
        "chat_text",
        "support_context",
    }.intersection(value for value in flattened if isinstance(value, str))


@pytest.mark.parametrize(
    "mutation",
    ("auto_publish", "mall_product_id", "storage_ref", "download_url"),
)
def test_schema_rejects_unsafe_or_publishable_output(mutation: str) -> None:
    exporter = _load_script()
    schema, fixture = exporter.build_artifacts()
    unsafe = copy.deepcopy(fixture)
    if mutation == "auto_publish":
        unsafe["items"][0]["auto_publish"] = True
    elif mutation == "mall_product_id":
        unsafe["items"][0]["mall_product_id"] = "mall-1"
    elif mutation == "storage_ref":
        unsafe["items"][0]["images"][0]["storage_ref"] = "evidence/../original"
    else:
        unsafe["items"][0]["download_lookup"][0]["download_url"] = (
            "https://private.invalid"
        )

    with pytest.raises(ValidationError):
        Draft202012Validator(
            schema,
            format_checker=FormatChecker(),
        ).validate(unsafe)


def test_parser_errors_do_not_echo_unknown_secret_values() -> None:
    secret = "https://operator:secret@private.invalid"

    result = _run("--database-url", secret)

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == "invalid_arguments\n"
    assert secret not in result.stderr


def test_unreadable_input_is_stable_and_does_not_echo_the_path(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "private-opc-receipt.json"
    output = tmp_path / "batch.json"

    result = _run(
        "--receipt",
        str(missing),
        "--generated-at",
        GENERATED_AT,
        "--output",
        str(output),
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == "invalid_input\n"
    assert str(missing) not in result.stderr


def test_output_failure_is_stable_and_does_not_echo_the_path(tmp_path: Path) -> None:
    args = _write_receipts(tmp_path, (1,))
    non_directory = tmp_path / "blocked-output-parent"
    non_directory.write_text("not a directory", encoding="utf-8")
    output = non_directory / "batch.json"

    result = _run(
        *args,
        "--generated-at",
        GENERATED_AT,
        "--output",
        str(output),
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == "output_unavailable\n"
    assert str(output) not in result.stderr


def test_artifact_check_is_read_only_and_detects_drift(tmp_path: Path) -> None:
    exporter = _load_script()
    schema_path = tmp_path / "schema.json"
    fixture_path = tmp_path / "fixture.json"
    exporter.SCHEMA_OUTPUT = schema_path
    exporter.FIXTURE_OUTPUT = fixture_path

    assert exporter._export_artifacts(check=False) == 0
    before = (schema_path.read_bytes(), fixture_path.read_bytes())
    assert exporter._export_artifacts(check=True) == 0
    assert before == (schema_path.read_bytes(), fixture_path.read_bytes())

    fixture_path.write_text("{}\n", encoding="utf-8")
    assert exporter._export_artifacts(check=True) == 1
    assert fixture_path.read_text(encoding="utf-8") == "{}\n"
