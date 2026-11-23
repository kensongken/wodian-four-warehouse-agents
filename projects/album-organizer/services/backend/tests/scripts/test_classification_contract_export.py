from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[4]
SCRIPT_PATH = ROOT / "scripts/export_classification_contract.py"
SCHEMA_PATH = ROOT / "contracts/album-product-classification-v1.schema.json"
FIXTURE_PATH = ROOT / "contracts/fixtures/first-10-sku-classification-candidates.json"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "export_classification_contract",
        SCRIPT_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _walk(value: Any) -> list[Any]:
    result = [value]
    if isinstance(value, dict):
        for key, child in value.items():
            result.extend(_walk(key))
            result.extend(_walk(child))
    elif isinstance(value, list):
        for child in value:
            result.extend(_walk(child))
    return result


def test_export_is_deterministic_and_matches_checked_in_artifacts() -> None:
    exporter = _load_script()

    first_schema, first_fixture = exporter.build_artifacts()
    second_schema, second_fixture = exporter.build_artifacts()

    assert exporter.render_json(first_schema) == exporter.render_json(second_schema)
    assert exporter.render_json(first_fixture) == exporter.render_json(second_fixture)
    assert SCHEMA_PATH.read_text(encoding="utf-8") == exporter.render_json(first_schema)
    assert FIXTURE_PATH.read_text(encoding="utf-8") == exporter.render_json(
        first_fixture
    )


def test_schema_is_valid_and_accepts_the_ten_sku_output_fixture() -> None:
    exporter = _load_script()
    schema, fixture = exporter.build_artifacts()

    Draft202012Validator.check_schema(schema)
    Draft202012Validator(
        schema,
        format_checker=FormatChecker(),
    ).validate(fixture)

    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"] == "urn:photo-organizer:album-product-classification:1.0"
    assert len(fixture["events"]) == 10
    assert fixture["failures"] == []
    assert len({event["event_id"] for event in fixture["events"]}) == 10


def test_output_fixture_is_review_only_and_contains_no_sensitive_context() -> None:
    exporter = _load_script()
    _, fixture = exporter.build_artifacts()

    for event in fixture["events"]:
        assert event["review_status"] == "requires_review"
        assert event["mall_product_id"] is None
        assert event["bead_id"] is None
        assert event["engine"] == {"name": "fixture-categorizer", "version": "1.0"}
        assert event["claims_not_evaluated"] == [
            "authenticity",
            "origin",
            "quality",
            "warehouse_acceptance",
        ]

    strings = [value for value in _walk(fixture) if isinstance(value, str)]
    forbidden = {
        "storage_ref",
        "customer_name",
        "customer_id",
        "phone",
        "mobile",
        "wechat",
        "address",
        "authenticity_assertion",
        "origin_claim",
        "quality_claim",
        "warehouse_verification",
    }
    assert not ({value.lower() for value in strings} & forbidden)
    assert not any(
        value.startswith(("http://", "https://", "file:", "content:", "/"))
        or "X-Amz-" in value
        for value in strings
    )


def test_check_mode_is_read_only_and_current() -> None:
    before_schema = SCHEMA_PATH.read_bytes()
    before_fixture = FIXTURE_PATH.read_bytes()

    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert SCHEMA_PATH.read_bytes() == before_schema
    assert FIXTURE_PATH.read_bytes() == before_fixture
