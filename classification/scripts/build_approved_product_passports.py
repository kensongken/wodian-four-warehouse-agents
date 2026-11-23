#!/usr/bin/env python3
"""Build deterministic human-confirmed ProductPassport regression fixtures."""

import json
from pathlib import Path
from uuid import UUID, uuid5

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures" / "v0.1"
SOURCE = FIXTURES / "mall-import-10-sku.json"
OUTPUT = FIXTURES / "approved-product-passports-10-sku.json"
SAMPLE = FIXTURES / "mall-approved-product-passport-sample.json"
EVENTS = FIXTURES / "product-passport-review-events.json"
NAMESPACE = UUID("85c93c40-d046-44d4-8d81-0f73f0655853")
REVIEWER_ID = "TRAINER-SYNTHETIC-001"
REVIEWED_AT = "2026-08-17T10:00:00Z"


def stable_uuid(kind: str, value: str) -> str:
    return str(uuid5(NAMESPACE, f"{kind}:{value}"))


def build_record(source: dict[str, object]) -> dict[str, object]:
    sku = str(source["sku"])
    passport_id = stable_uuid("product-passport", sku)
    return {
        "product_passport_id": passport_id,
        "sku_id": sku,
        "approved_attributes": {
            "product_type": source["type"],
            "title": source["title"],
            "category": source["category"],
            "form": source["form"],
            "bead_id": source["bead_id"],
            "source_bead_id": source["source_bead_id"],
            "source_ticket_id": source["source_ticket_id"],
            "material": {
                "value": source["category"],
                "source": "merchant_declared",
                "review_status": "human_confirmed",
            },
            "quality": {
                "value": None,
                "source": "not_assessed",
                "review_status": "not_claimed",
            },
            "authenticity": {
                "value": None,
                "source": "not_assessed",
                "review_status": "not_claimed",
            },
            "price_advisory": {
                "value": f"{source['price_min']}-{source['price_max']}",
                "currency": source["price_currency"],
                "minimum": source["price_min"],
                "maximum": source["price_max"],
                "basis": source["price_basis"],
                "source": "human_confirmed",
                "review_status": "human_confirmed",
            },
            "selling_points": source["selling_points"],
            "description": source["description"],
            "primary_image_url": source["primary_image_url"],
            "service_summary": source["service_summary"],
            "lineage_evidence_available": source["lineage_evidence_available"],
        },
        "evidence_refs": [
            {
                "evidence_id": stable_uuid("public-product-photo", sku),
                "evidence_type": "public_product_photo",
                "status": "approved",
                "visibility": "public",
                "ref": source["primary_image_url"],
            }
        ],
        "taxonomy_version": "product-taxonomy-v0.1",
        "availability_status": "available",
        "reviewer": {
            "reviewer_type": "human",
            "reviewer_id": REVIEWER_ID,
            "reviewed_at": REVIEWED_AT,
        },
        "version": 1,
    }


def build_events(records: list[dict[str, object]]) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for record in records:
        sku = str(record["sku_id"])
        events.extend(
            [
                {
                    "event_id": stable_uuid("candidate", sku),
                    "sku_id": sku,
                    "event_type": "candidate_created",
                    "candidate_source": "ai_candidate",
                    "candidate_attributes": {
                        "title": record["approved_attributes"]["title"],
                        "category": record["approved_attributes"]["category"],
                        "form": record["approved_attributes"]["form"],
                    },
                    "created_at": "2026-08-17T09:55:00Z",
                },
                {
                    "event_id": stable_uuid("confirmed", sku),
                    "sku_id": sku,
                    "event_type": "human_confirmed",
                    "product_passport_id": record["product_passport_id"],
                    "reviewer_type": "human",
                    "reviewer_id": REVIEWER_ID,
                    "version": 1,
                    "created_at": REVIEWED_AT,
                },
            ]
        )

    withdrawn_sku = "OPC-WITHDRAWN-DEMO-001"
    withdrawn_passport_id = stable_uuid("product-passport", withdrawn_sku)
    events.extend(
        [
            {
                "event_id": stable_uuid("candidate", withdrawn_sku),
                "sku_id": withdrawn_sku,
                "event_type": "candidate_created",
                "candidate_source": "ai_candidate",
                "candidate_attributes": {"title": "撤回流程演示商品"},
                "created_at": "2026-08-17T10:05:00Z",
            },
            {
                "event_id": stable_uuid("confirmed", withdrawn_sku),
                "sku_id": withdrawn_sku,
                "event_type": "human_confirmed",
                "product_passport_id": withdrawn_passport_id,
                "reviewer_type": "human",
                "reviewer_id": REVIEWER_ID,
                "version": 1,
                "created_at": "2026-08-17T10:06:00Z",
            },
            {
                "event_id": stable_uuid("withdrawn", withdrawn_sku),
                "sku_id": withdrawn_sku,
                "event_type": "withdrawn",
                "product_passport_id": withdrawn_passport_id,
                "reviewer_type": "human",
                "reviewer_id": REVIEWER_ID,
                "version": 2,
                "reason": "回归演示：人工撤回后不得进入商城资料包",
                "created_at": "2026-08-17T10:07:00Z",
            },
        ]
    )
    return events


def write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    sources = json.loads(SOURCE.read_text(encoding="utf-8"))
    records = [build_record(source) for source in sources]
    write_json(OUTPUT, records)
    write_json(SAMPLE, records[0])
    write_json(EVENTS, build_events(records))
    print(f"BUILT approved_product_passports={len(records)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
