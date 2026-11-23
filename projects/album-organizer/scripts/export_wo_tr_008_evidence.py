"""Export the deterministic WO-TR-008 controlled 10-SKU evidence register."""

import argparse
import json
import sys
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Literal, Self
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field, model_validator

from photo_organizer.categorization.contracts import (
    CategorizationBatchResult,
    ClassificationCandidateEvent,
    ConfirmedClassificationEvent,
    ConfirmedSnapshotBatch,
    FrozenModel,
    ImageEvidence,
    ProductSourceType,
    StableId,
)
from photo_organizer.schemas.classification_reviews import (
    ClassificationReviewResponse,
)

ROOT = Path(__file__).resolve().parents[1]
SOURCE_FIXTURE = ROOT / "contracts/fixtures/first-10-sku-categorization-events.json"
CANDIDATE_FIXTURE = (
    ROOT / "contracts/fixtures/first-10-sku-classification-candidates.json"
)
SCHEMA_OUTPUT = ROOT / "contracts/wo-tr-008-controlled-import-evidence-v1.schema.json"
FIXTURE_OUTPUT = ROOT / "contracts/fixtures/wo-tr-008-first-10-sku-evidence.json"
CONFIRMED_BY = "user:00000000-0000-4000-8000-000000000001"


class ControlledSkuEvidence(FrozenModel):
    sku_id: StableId
    asset_id: UUID
    source_type: ProductSourceType
    opc_workspace_id: StableId
    evidence_refs: tuple[StableId, StableId, StableId, StableId]
    import_version: Annotated[int, Field(ge=1)]
    image_evidence: ImageEvidence
    candidate_event: ClassificationCandidateEvent
    review_record: ClassificationReviewResponse
    confirmation_event: ConfirmedClassificationEvent

    @model_validator(mode="after")
    def evidence_chain_must_match(self) -> Self:
        candidate = self.candidate_event
        review = self.review_record
        confirmation = self.confirmation_event
        if self.asset_id != self.image_evidence.asset_id:
            raise ValueError("asset_id must identify image_evidence")
        if self.image_evidence.role != "original":
            raise ValueError("the registered evidence image must be an original")
        if self.source_type != candidate.source_type:
            raise ValueError("source_type must match the candidate event")
        if self.opc_workspace_id != candidate.opc_id:
            raise ValueError("opc_workspace_id must match the candidate event")
        if self.import_version != candidate.input_snapshot_version:
            raise ValueError("import_version must match the input snapshot")
        if not (
            candidate.opc_id == review.opc_id == confirmation.opc_id
            and candidate.album_product_id
            == review.album_product_id
            == confirmation.album_product_id
            and candidate.source_type == review.source_type == confirmation.source_type
            and candidate.input_event_id == confirmation.input_event_id
            and candidate.input_snapshot_version
            == review.input_snapshot_version
            == confirmation.input_snapshot_version
            and candidate.classification_version
            == review.classification_version
            == confirmation.classification_version
            and review.id == confirmation.review_id
            and review.classification_id == confirmation.classification_id
        ):
            raise ValueError("candidate, review, and confirmation identities must match")
        if not (
            review.status == "resolved"
            and review.confirmed_level_1 == confirmation.confirmed_level_1
            and review.confirmed_level_2 == confirmation.confirmed_level_2
            and review.confirmed_by == confirmation.confirmed_by
            and review.decided_at == confirmation.confirmed_at
        ):
            raise ValueError("resolved review must match the confirmation event")
        expected_refs = (
            f"asset:{self.asset_id}",
            f"candidate:{candidate.event_id}",
            f"review:{review.id}",
            f"confirmation:{confirmation.event_id}",
        )
        if self.evidence_refs != expected_refs:
            raise ValueError("evidence_refs must name the complete evidence chain")
        return self


class ControlledImportEvidenceBatch(FrozenModel):
    schema_version: Literal["wo-tr-008-controlled-import-evidence/1.0"]
    evidence_id: Literal["WO-TR-008-EV-001"]
    data_classification: Literal["synthetic_no_customer_data"]
    delivery_boundary: Literal["classification_review_only_no_auto_listing"]
    skus: Annotated[tuple[ControlledSkuEvidence, ...], Field(min_length=10, max_length=10)]

    @model_validator(mode="after")
    def identities_must_be_unique(self) -> Self:
        for field_name in ("sku_id", "asset_id"):
            values = [getattr(item, field_name) for item in self.skus]
            if len(set(values)) != len(values):
                raise ValueError(f"{field_name} must be unique")
        if len({item.candidate_event.event_id for item in self.skus}) != 10:
            raise ValueError("candidate event IDs must be unique")
        if len({item.review_record.id for item in self.skus}) != 10:
            raise ValueError("review IDs must be unique")
        if len({item.confirmation_event.event_id for item in self.skus}) != 10:
            raise ValueError("confirmation event IDs must be unique")
        return self


def load_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"expected a JSON object in {path}")
    return value


def _stable_uuid(kind: str, candidate_id: UUID) -> UUID:
    return uuid5(NAMESPACE_URL, f"wo-tr-008:{kind}:{candidate_id}")


def build_artifacts() -> tuple[dict[str, object], dict[str, object]]:
    snapshots = ConfirmedSnapshotBatch.model_validate(load_json(SOURCE_FIXTURE))
    if snapshots.data_classification != "synthetic_no_customer_data":
        raise ValueError("WO-TR-008 requires synthetic source data")
    candidates = CategorizationBatchResult.model_validate(load_json(CANDIDATE_FIXTURE))
    snapshots_by_event = {event.event_id: event for event in snapshots.events}
    evidence: list[ControlledSkuEvidence] = []

    for index, candidate in enumerate(candidates.events, start=1):
        snapshot = snapshots_by_event[candidate.input_event_id]
        original = next(image for image in snapshot.images if image.role == "original")
        classification_id = _stable_uuid("classification", candidate.event_id)
        review_id = _stable_uuid("review", candidate.event_id)
        confirmation_id = _stable_uuid("confirmation", candidate.event_id)
        reviewed_at = candidate.occurred_at + timedelta(minutes=30 + index)
        selected = candidate.candidates[0]
        review = ClassificationReviewResponse(
            id=review_id,
            opc_id=candidate.opc_id,
            classification_id=classification_id,
            album_product_id=candidate.album_product_id,
            source_type=candidate.source_type,
            input_snapshot_version=candidate.input_snapshot_version,
            classification_version=candidate.classification_version,
            candidates=candidate.candidates,
            status="resolved",
            confirmed_level_1=selected.level_1,
            confirmed_level_2=selected.level_2,
            confirmed_by=CONFIRMED_BY,
            decided_at=reviewed_at,
            created_at=candidate.occurred_at + timedelta(minutes=index),
            updated_at=reviewed_at,
            claims_not_evaluated=candidate.claims_not_evaluated,
            mall_product_id=None,
            bead_id=None,
        )
        confirmation = ConfirmedClassificationEvent(
            schema_version="album-product-classification-confirmed/1.0",
            event_id=confirmation_id,
            event_type="album.product.classification.confirmed.v1",
            occurred_at=reviewed_at,
            opc_id=candidate.opc_id,
            album_product_id=candidate.album_product_id,
            source_type=candidate.source_type,
            classification_id=classification_id,
            review_id=review_id,
            input_event_id=candidate.input_event_id,
            input_snapshot_version=candidate.input_snapshot_version,
            classification_version=candidate.classification_version,
            confirmed_level_1=selected.level_1,
            confirmed_level_2=selected.level_2,
            confirmed_by=CONFIRMED_BY,
            confirmed_at=reviewed_at,
            claims_not_evaluated=candidate.claims_not_evaluated,
            mall_product_id=None,
            bead_id=None,
        )
        evidence.append(
            ControlledSkuEvidence(
                sku_id=f"synthetic-sku-{index:03d}",
                asset_id=original.asset_id,
                source_type=candidate.source_type,
                opc_workspace_id=candidate.opc_id,
                evidence_refs=(
                    f"asset:{original.asset_id}",
                    f"candidate:{candidate.event_id}",
                    f"review:{review_id}",
                    f"confirmation:{confirmation_id}",
                ),
                import_version=snapshot.snapshot_version,
                image_evidence=original,
                candidate_event=candidate,
                review_record=review,
                confirmation_event=confirmation,
            )
        )

    batch = ControlledImportEvidenceBatch(
        schema_version="wo-tr-008-controlled-import-evidence/1.0",
        evidence_id="WO-TR-008-EV-001",
        data_classification="synthetic_no_customer_data",
        delivery_boundary="classification_review_only_no_auto_listing",
        skus=tuple(evidence),
    )
    schema = ControlledImportEvidenceBatch.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "urn:photo-organizer:wo-tr-008-controlled-import-evidence:1.0"
    return schema, batch.model_dump(mode="json")


def render_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


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
        for path in stale:
            print(f"WO-TR-008 evidence artifact is stale: {path}", file=sys.stderr)
        return 1 if stale else 0
    for path, content in expected.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    return parser.parse_args()


def main() -> int:
    return export(check=_parse_args().check)


if __name__ == "__main__":
    raise SystemExit(main())
