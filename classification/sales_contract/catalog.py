"""Tenant-bound, fail-closed sales views over validated ProductPassport records."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Dict, List

from scripts.validate_product_passports import ValidationFailure, validate_dataset


SELLABLE_TYPES = {"first_sale_bead", "redesign_output"}
CODE_TYPES = {"sku", "bead_id", "product_id"}


class SalesCatalog:
    """Resolves product codes and creates sales-safe packages for one tenant."""

    def __init__(self, records: List[Dict[str, object]], tenant_id: str) -> None:
        try:
            validate_dataset(records)
        except ValidationFailure as error:
            raise ValueError("ProductPassport records must be validated") from error
        self._tenant_id = tenant_id
        self._records = deepcopy(records)
        self._by_code = {code_type: {} for code_type in CODE_TYPES}
        self._historical_bead_ids = set()
        for record in self._records:
            self._add_code("sku", record.get("sku"), record)
            self._add_code("product_id", record.get("product_id"), record)
            self._add_code("bead_id", record.get("bead_id"), record)
            source_bead_id = record.get("source_bead_id")
            if isinstance(source_bead_id, str):
                self._historical_bead_ids.add(source_bead_id)

    @classmethod
    def from_records(
        cls, records: List[Dict[str, object]], tenant_id: str
    ) -> "SalesCatalog":
        """Create a catalog from ProductPassport records already validated upstream."""
        return cls(records, tenant_id)

    def resolve(
        self, tenant_id: str, code_type: str, code: str
    ) -> Dict[str, object]:
        """Resolve an exact upstream code without selecting a potentially ambiguous record."""
        if tenant_id != self._tenant_id:
            return {"resolution_status": "tenant_mismatch"}
        if code_type not in CODE_TYPES:
            return {"resolution_status": "not_found"}
        if code_type == "bead_id" and code in self._historical_bead_ids:
            return {"resolution_status": "ambiguous"}

        matches = self._by_code[code_type].get(code, [])
        if not matches:
            return {"resolution_status": "not_found"}
        if len(matches) != 1:
            return {"resolution_status": "ambiguous"}

        record = matches[0]
        sellability = self._sellability_status(record)
        if sellability != "resolved":
            return {"resolution_status": sellability}
        return {
            "contract_version": "0.1",
            "resolution_status": "resolved",
            "product_id": record["product_id"],
            "package_version": record["package_version"],
            "product_status": record["status"],
            "resolved_from": code_type,
        }

    def sales_package(self, tenant_id: str, product_id: str) -> Dict[str, object]:
        """Return only the sales_journey projection for a sellable ProductPassport."""
        if tenant_id != self._tenant_id:
            return {"status": "tenant_mismatch"}

        matches = self._by_code["product_id"].get(product_id, [])
        if not matches:
            return {"status": "not_found"}
        if len(matches) != 1:
            return {"status": "ambiguous"}

        record = matches[0]
        sellability = self._sellability_status(record)
        if sellability != "resolved":
            return {"status": sellability}
        projection = record["sales_journey_projection"]
        return {
            "contract_version": "0.1",
            "audience": "sales_journey",
            "product_id": projection["product_id"],
            "package_version": projection["package_version"],
            "evidence_level": projection["evidence_level"],
            "evidence_ids": list(projection["evidence_ids"]),
            "validity": deepcopy(projection["validity"]),
            "forbidden_claims": self._approved_texts(
                projection["forbidden_claims"]
            ),
            "product_passport": {
                "sku": projection["sku"]["text"],
                "category": projection["category"]["text"],
                "form": projection["form"]["text"],
                "title": projection["title"]["text"],
                "description": projection["description"]["text"],
                "selling_points": self._approved_texts(projection["selling_points"]),
                "service_summary": self._approved_texts(projection["service_summary"]),
                "conservative_price_range": self._sales_price_range(
                    projection["conservative_price_range"]
                ),
                "public_media": self._public_media(projection["public_media"]),
            },
        }

    def _add_code(
        self, code_type: str, value: object, record: Dict[str, object]
    ) -> None:
        if isinstance(value, str):
            self._by_code[code_type].setdefault(value, []).append(record)

    @staticmethod
    def _sellability_status(record: Dict[str, object]) -> str:
        if record.get("status") == "retired":
            return "retired"
        if (
            record.get("status") != "published"
            or record.get("type") not in SELLABLE_TYPES
        ):
            return "not_sellable"
        validity = record["sales_journey_projection"]["validity"]
        valid_from = datetime.fromisoformat(
            validity["valid_from"].replace("Z", "+00:00")
        )
        valid_until = datetime.fromisoformat(
            validity["valid_until"].replace("Z", "+00:00")
        )
        now = datetime.now(timezone.utc)
        if now < valid_from or now >= valid_until:
            return "not_sellable"
        return "resolved"

    @staticmethod
    def _public_media(approved_media: object) -> List[Dict[str, object]]:
        return [
            {
                "media_id": item["media_id"],
                "public_url": item["public_url"]["text"],
            }
            for item in approved_media
        ]

    @staticmethod
    def _sales_price_range(approved_price: object) -> Dict[str, object]:
        return {
            "currency": approved_price["currency"],
            "minimum": approved_price["minimum"],
            "maximum": approved_price["maximum"],
            "basis": approved_price["basis"]["text"],
            "evidence_level": approved_price["evidence_level"],
            "evidence_ids": list(approved_price["evidence_ids"]),
        }

    @staticmethod
    def _approved_texts(values: object) -> List[str]:
        return [value["text"] for value in values]
