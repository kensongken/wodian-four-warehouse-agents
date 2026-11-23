from __future__ import annotations

import copy
import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from photo_organizer.categorization.contracts import (
    ClassificationCandidateEvent,
    ConfirmedClassificationEvent,
)
from photo_organizer.schemas.classification_reviews import (
    ClassificationReviewResponse,
)

ROOT = Path(__file__).resolve().parents[4]
SCRIPT_PATH = ROOT / "scripts/export_wo_tr_008_evidence.py"
SCHEMA_PATH = ROOT / "contracts/wo-tr-008-controlled-import-evidence-v1.schema.json"
FIXTURE_PATH = ROOT / "contracts/fixtures/wo-tr-008-first-10-sku-evidence.json"
SOURCE_FIXTURE_PATH = (
    ROOT / "contracts/fixtures/first-10-sku-categorization-events.json"
)


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "export_wo_tr_008_evidence", SCRIPT_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


def _keys(value: Any) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            keys.add(str(key).lower())
            keys.update(_keys(child))
    elif isinstance(value, list):
        for child in value:
            keys.update(_keys(child))
    return keys


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


def test_schema_accepts_exactly_ten_traceable_controlled_skus() -> None:
    exporter = _load_script()
    schema, fixture = exporter.build_artifacts()

    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(fixture)

    assert (
        schema["$id"] == "urn:photo-organizer:wo-tr-008-controlled-import-evidence:1.0"
    )
    assert fixture["data_classification"] == "synthetic_no_customer_data"
    assert len(fixture["skus"]) == 10
    assert len({item["sku_id"] for item in fixture["skus"]}) == 10
    assert len({item["asset_id"] for item in fixture["skus"]}) == 10

    source = exporter.load_json(SOURCE_FIXTURE_PATH)
    source_events = {event["album_product_id"]: event for event in source["events"]}
    for item in fixture["skus"]:
        source_event = source_events[item["candidate_event"]["album_product_id"]]
        original = next(
            image for image in source_event["images"] if image["role"] == "original"
        )
        assert item["asset_id"] == original["asset_id"]
        assert item["source_type"] == source_event["source_type"]
        assert item["opc_workspace_id"] == source_event["opc_id"]
        assert item["import_version"] == source_event["snapshot_version"]
        assert item["image_evidence"] == original
        assert len(item["evidence_refs"]) == 4


def test_candidate_review_and_confirmation_samples_use_production_contracts() -> None:
    exporter = _load_script()
    _, fixture = exporter.build_artifacts()

    for item in fixture["skus"]:
        candidate = ClassificationCandidateEvent.model_validate(item["candidate_event"])
        review = ClassificationReviewResponse.model_validate(item["review_record"])
        confirmation = ConfirmedClassificationEvent.model_validate(
            item["confirmation_event"]
        )

        assert candidate.input_event_id == confirmation.input_event_id
        assert candidate.opc_id == review.opc_id == confirmation.opc_id
        assert (
            candidate.album_product_id
            == review.album_product_id
            == confirmation.album_product_id
        )
        assert candidate.source_type == review.source_type == confirmation.source_type
        assert review.id == confirmation.review_id
        assert review.classification_id == confirmation.classification_id
        assert review.status == "resolved"
        assert review.mall_product_id is confirmation.mall_product_id is None
        assert review.bead_id is confirmation.bead_id is None
        assert confirmation.claims_not_evaluated == (
            "authenticity",
            "origin",
            "quality",
            "warehouse_acceptance",
        )


def test_fixture_has_no_private_or_overreaching_context() -> None:
    exporter = _load_script()
    _, fixture = exporter.build_artifacts()
    strings = [value for value in _walk(fixture) if isinstance(value, str)]
    forbidden_keys = {
        "customer_name",
        "customer_id",
        "phone",
        "mobile",
        "wechat",
        "address",
        "chat_text",
        "authenticity",
        "origin_claim",
        "quality_claim",
        "warehouse_verification",
        "auto_list",
    }

    assert not (_keys(fixture) & forbidden_keys)
    assert not any(
        value.startswith(("http://", "https://", "file:", "content:", "/"))
        or "X-Amz-" in value
        for value in strings
    )


def test_export_rejects_a_source_that_is_not_explicitly_synthetic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exporter = _load_script()
    source = copy.deepcopy(exporter.load_json(SOURCE_FIXTURE_PATH))
    source["data_classification"] = "opc_owned_product_material_no_customer_data"
    original_load_json = exporter.load_json

    def load_json(path: Path) -> dict[str, object]:
        if path == exporter.SOURCE_FIXTURE:
            return source
        return original_load_json(path)

    monkeypatch.setattr(exporter, "load_json", load_json)

    with pytest.raises(ValueError, match="WO-TR-008 requires synthetic source data"):
        exporter.build_artifacts()


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
