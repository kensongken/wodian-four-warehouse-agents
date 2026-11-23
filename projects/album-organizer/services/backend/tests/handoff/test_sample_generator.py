from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from photo_organizer.handoff.synthetic_sample import generate_synthetic_sample

SCHEMA_SHA256 = "0503f10d7b0bbd5b4d25bcc12bc9fe5990ddd6da8f3f8fb7709bc77ab2b1f732"


def test_generator_writes_only_the_closed_synthetic_contract(tmp_path: Path) -> None:
    output = generate_synthetic_sample(tmp_path / "sample.json")
    payload = json.loads(output.read_text(encoding="utf-8"))

    assert set(payload) == {
        "schema_version",
        "event_id",
        "occurred_at",
        "tenant_id",
        "source_task_id",
        "product_code",
        "category",
        "form",
        "classification_confidence",
        "images",
    }
    assert payload["product_code"] == "SYNTHETIC-001"
    assert payload["images"][0]["object_key"].startswith(
        f"tenants/{payload['tenant_id']}/"
    )


def test_checked_in_sample_matches_generator_and_frozen_schema(tmp_path: Path) -> None:
    repository = Path(__file__).resolve().parents[4]
    schema_path = repository / "contracts/product-image-set-ready-v1.schema.json"
    checked_in = (
        repository
        / "services/backend/fixtures/v0.1/product-image-set-ready.synthetic.json"
    )
    regenerated = generate_synthetic_sample(tmp_path / "regenerated.json")

    schema_bytes = schema_path.read_bytes()
    assert sha256(schema_bytes).hexdigest() == SCHEMA_SHA256
    assert regenerated.read_bytes() == checked_in.read_bytes()
    validator = Draft202012Validator(
        json.loads(schema_bytes),
        format_checker=FormatChecker(),
    )
    assert list(validator.iter_errors(json.loads(checked_in.read_text()))) == []


def test_v2_batch_fixture_keeps_product_identity_and_changes_release_identity() -> None:
    repository = Path(__file__).resolve().parents[4]
    schema = json.loads(
        (repository / "contracts/product-image-set-ready-v1.schema.json").read_text()
    )
    fixture_root = repository / "services/backend/fixtures/v0.1"
    first = json.loads(
        (fixture_root / "product-image-set-ready.synthetic.json").read_text()
    )
    second = json.loads(
        (fixture_root / "product-image-set-ready-v2.synthetic.json").read_text()
    )
    validator = Draft202012Validator(schema, format_checker=FormatChecker())

    assert list(validator.iter_errors(second)) == []
    assert second["tenant_id"] == first["tenant_id"]
    assert second["product_code"] == first["product_code"] == "SYNTHETIC-001"
    assert second["event_id"] != first["event_id"]
    assert second["source_task_id"] != first["source_task_id"]
    assert second["images"][0]["asset_id"] != first["images"][0]["asset_id"]
    assert second["images"][0]["sha256"] != first["images"][0]["sha256"]
