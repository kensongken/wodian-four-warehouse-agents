"""Export deterministic Stage 2 classification contract artifacts."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from photo_organizer.categorization.consumer import CategorizationConsumer
from photo_organizer.categorization.contracts import (
    CategorizationBatchResult,
    CategorizationRequest,
    ConfirmedSnapshotBatch,
    EngineCandidate,
    ReasonCode,
)

ROOT = Path(__file__).resolve().parents[1]
INPUT_FIXTURE = (
    ROOT / "contracts/fixtures/first-10-sku-categorization-events.json"
)
SCHEMA_OUTPUT = ROOT / "contracts/album-product-classification-v1.schema.json"
FIXTURE_OUTPUT = (
    ROOT / "contracts/fixtures/first-10-sku-classification-candidates.json"
)
FIXED_TIME = datetime(2026, 8, 16, 10, 30, tzinfo=UTC)


class FixtureCategorizationEngine:
    """Deterministic fixture generator; never configured as a production engine."""

    engine_name = "fixture-categorizer"
    engine_version = "1.0"

    async def classify(
        self, request: CategorizationRequest
    ) -> tuple[EngineCandidate, ...]:
        return (
            EngineCandidate(
                level_1=request.seed.level_1_candidate,
                level_2=request.seed.level_2_candidate,
                confidence=0.91,
                reason_codes=(ReasonCode.SEED_MATCH,),
            ),
        )


def build_artifacts() -> tuple[dict[str, Any], dict[str, Any]]:
    batch = ConfirmedSnapshotBatch.model_validate_json(
        INPUT_FIXTURE.read_text(encoding="utf-8")
    )
    result = asyncio.run(
        CategorizationConsumer(
            FixtureCategorizationEngine(),
            clock=lambda: FIXED_TIME,
        ).consume(batch)
    )
    schema = CategorizationBatchResult.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "urn:photo-organizer:album-product-classification:1.0"
    fixture = result.model_dump(mode="json")
    return schema, fixture


def render_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"


def export(*, check: bool) -> int:
    schema, fixture = build_artifacts()
    expected = {
        SCHEMA_OUTPUT: render_json(schema),
        FIXTURE_OUTPUT: render_json(fixture),
    }
    if check:
        stale = [
            path
            for path, content in expected.items()
            if not path.is_file() or path.read_text(encoding="utf-8") != content
        ]
        if stale:
            for path in stale:
                print(f"classification contract is stale: {path}", file=sys.stderr)
            return 1
        return 0

    for path, content in expected.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if checked-in artifacts differ without writing files",
    )
    return parser.parse_args()


def main() -> int:
    return export(check=_parse_args().check)


if __name__ == "__main__":
    raise SystemExit(main())
