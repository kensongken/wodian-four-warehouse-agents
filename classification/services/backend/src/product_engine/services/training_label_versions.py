import json
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from threading import RLock
from typing import Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from product_engine.services.customer_safe_content import (
    UnsafeCustomerContentError,
    assert_customer_safe_content,
)

TRAINING_STATE = "label_confirmed_model_not_trained"


class TrainingLabelFailure(ValueError):
    pass


ProductType = Literal["first_sale_bead", "legacy_material", "redesign_output"]
InventoryStatus = Literal["in_stock", "reserved", "out_of_stock"]


class PriceRangeInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    currency: Literal["CNY"] = "CNY"
    minimum: str
    maximum: str
    basis: str


class ServiceRuleInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    service_code: str
    eligibility: str
    customer_summary: str


class PriceConfirmationAudit(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    standard_code: str = Field(min_length=1)
    anchor_version: int = Field(ge=1)
    anchor_confirmed_by: str | None = Field(default=None, min_length=1)
    anchor_confirmed_at: datetime | None = None
    confirmed_by: str = Field(min_length=1)
    confirmed_at: datetime
    candidate_contract_version: Literal["0.1-candidate"] = "0.1-candidate"
    automatic_publication_allowed: Literal[False] = False

    @field_validator("standard_code", "anchor_confirmed_by", "confirmed_by", mode="before")
    @classmethod
    def strip_confirmation_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def validate_confirmation(self) -> "PriceConfirmationAudit":
        if not all((self.standard_code.strip(), self.confirmed_by.strip())):
            raise ValueError("invalid_price_confirmation_audit")
        if (self.anchor_confirmed_by is None) != (self.anchor_confirmed_at is None):
            raise ValueError("incomplete_anchor_confirmation_audit")
        if self.anchor_confirmed_by is not None and not self.anchor_confirmed_by.strip():
            raise ValueError("invalid_price_confirmation_audit")
        if (
            (
                self.anchor_confirmed_at is not None
                and (
                    self.anchor_confirmed_at.tzinfo is None
                    or self.anchor_confirmed_at.utcoffset() is None
                )
            )
            or self.confirmed_at.tzinfo is None
            or self.confirmed_at.utcoffset() is None
        ):
            raise ValueError("price_confirmation_timezone_required")
        return self


class TrainingLabelInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    sha256: str
    main_image_template_code: str | None = None
    product_type: ProductType | None = None
    inventory_status: InventoryStatus | None = None
    material_label: str
    product_form: str
    visual_observations: list[str]
    selling_points: list[str]
    specification_note: str = ""
    quality_note: str = ""
    defect_note: str = ""
    price_range: PriceRangeInput | None = None
    service_rules: list[ServiceRuleInput] = Field(default_factory=list)


class TrainingLabelVersion(TrainingLabelInput):
    version_id: UUID
    template_code: str
    version: int
    previous_version_id: UUID | None
    rollback_target_version_id: UUID | None
    change_source: Literal[
        "system_initial",
        "merchant_imported",
        "exact_match_reused",
        "trainer_confirmed",
        "trainer_rollback",
    ]
    material_evidence: Literal["system_initial", "merchant_declared", "trainer_confirmed"]
    match_method: Literal["exact_sha256"] | None = None
    matched_from_template_code: str | None = None
    authenticity_verification: Literal["not_verified"] = "not_verified"
    origin_verification: Literal["not_verified"] = "not_verified"
    quality_verification: Literal["not_verified"] = "not_verified"
    warehouse_verification: Literal["pending_warehouse_verification"] = (
        "pending_warehouse_verification"
    )
    training_state: Literal["label_confirmed_model_not_trained"] = (
        "label_confirmed_model_not_trained"
    )
    created_at: datetime
    can_rollback: bool = False
    price_confirmation: PriceConfirmationAudit | None = None


def _clean_text(value: str) -> str:
    return value.strip()


def _clean_lines(values: list[str]) -> list[str]:
    return [cleaned for item in values if (cleaned := item.strip())]


def _customer_text_fields(values: TrainingLabelInput) -> list[str]:
    parts = [
        values.material_label,
        values.product_form,
        *values.visual_observations,
        *values.selling_points,
        values.specification_note,
        values.quality_note,
        values.defect_note,
    ]
    if values.price_range is not None:
        parts.append(values.price_range.basis)
    for rule in values.service_rules:
        parts.extend([rule.service_code, rule.eligibility, rule.customer_summary])
    return parts


def _assert_customer_safe_training_label(values: TrainingLabelInput) -> None:
    try:
        assert_customer_safe_content(_customer_text_fields(values))
    except UnsafeCustomerContentError as error:
        raise TrainingLabelFailure("forbidden_unsupported_claim") from error


def _normalize_and_validate(values: TrainingLabelInput) -> TrainingLabelInput:
    price = values.price_range
    normalized_price: PriceRangeInput | None = None
    if price is not None:
        try:
            minimum = Decimal(price.minimum)
            maximum = Decimal(price.maximum)
        except InvalidOperation as error:
            raise TrainingLabelFailure("invalid_price_range") from error
        basis = price.basis.strip()
        if minimum < 0 or maximum < 0 or minimum > maximum or not basis:
            raise TrainingLabelFailure("invalid_price_range")
        normalized_price = PriceRangeInput(
            minimum=f"{minimum:.2f}",
            maximum=f"{maximum:.2f}",
            basis=basis,
        )

    normalized_service_rules: list[ServiceRuleInput] = []
    for rule in values.service_rules:
        normalized_rule = ServiceRuleInput(
            service_code=rule.service_code.strip(),
            eligibility=rule.eligibility.strip(),
            customer_summary=rule.customer_summary.strip(),
        )
        if not all(
            (
                normalized_rule.service_code,
                normalized_rule.eligibility,
                normalized_rule.customer_summary,
            )
        ):
            raise TrainingLabelFailure("invalid_service_rule")
        normalized_service_rules.append(normalized_rule)

    normalized = values.model_copy(
        update={
            "sha256": values.sha256.strip().lower(),
            "main_image_template_code": (
                values.main_image_template_code.strip() if values.main_image_template_code else None
            ),
            "material_label": _clean_text(values.material_label),
            "product_form": _clean_text(values.product_form),
            "visual_observations": _clean_lines(values.visual_observations),
            "selling_points": _clean_lines(values.selling_points),
            "specification_note": _clean_text(values.specification_note),
            "quality_note": _clean_text(values.quality_note),
            "defect_note": _clean_text(values.defect_note),
            "price_range": normalized_price,
            "service_rules": normalized_service_rules,
        }
    )
    if (
        not normalized.material_label
        or not normalized.product_form
        or not normalized.visual_observations
        or not normalized.selling_points
    ):
        raise TrainingLabelFailure("required_field_missing")
    _assert_customer_safe_training_label(normalized)
    return normalized


class TrainingLabelVersionStore:
    def __init__(self, version_path: Path, initial_path: Path) -> None:
        self.version_path = version_path
        self.initial_path = initial_path
        self._lock = RLock()

    def _read_versions(self, template_code: str | None = None) -> list[TrainingLabelVersion]:
        if not self.version_path.exists():
            return []
        versions: list[TrainingLabelVersion] = []
        for line in self.version_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                raw = json.loads(line)
                if template_code is not None and raw.get("template_code") != template_code:
                    continue
                version = TrainingLabelVersion.model_validate(raw)
                _assert_customer_safe_training_label(version)
                versions.append(version)
        return versions

    def _initial(self, template_code: str) -> TrainingLabelVersion | None:
        if not self.initial_path.exists():
            return None
        records = [
            json.loads(line)
            for line in self.initial_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        for raw in reversed(records):
            if raw.get("template_code") != template_code:
                continue
            return self._initial_from_raw(raw, template_code)
        return None

    @staticmethod
    def _initial_from_raw(raw: dict[str, Any], template_code: str) -> TrainingLabelVersion:
        if "version_id" in raw:
            stored_initial = TrainingLabelVersion.model_validate(raw)
            _assert_customer_safe_training_label(stored_initial)
            return stored_initial.model_copy(update={"can_rollback": False})
        values = TrainingLabelInput(
            sha256=str(raw.get("sha256", "")),
            material_label=str(raw.get("material_label", raw.get("category_label", "待确认"))),
            product_form=str(raw.get("product_form", "待确认")),
            visual_observations=list(raw.get("visual_observations") or ["待训练员确认图片特征"]),
            selling_points=list(raw.get("selling_points") or ["待训练员补充销售卖点"]),
            specification_note=str(raw.get("specification_note", "")),
            quality_note=str(raw.get("quality_note", "")),
            defect_note=str(raw.get("defect_note", "")),
            price_range=None,
        )
        return TrainingLabelVersion(
            **_normalize_and_validate(values).model_dump(),
            version_id=uuid5(NAMESPACE_URL, f"local-training-label:{template_code}:v1"),
            template_code=template_code,
            version=1,
            previous_version_id=None,
            rollback_target_version_id=None,
            change_source="system_initial",
            material_evidence="merchant_declared",
            created_at=datetime.now(UTC),
            can_rollback=False,
        )

    def current(self, template_code: str) -> TrainingLabelVersion | None:
        matching = self._read_versions(template_code)
        if matching:
            current = max(matching, key=lambda item: item.version)
            return current.model_copy(update={"can_rollback": current.version > 1})
        return self._initial(template_code)

    def version(self, template_code: str, version: int) -> TrainingLabelVersion | None:
        matched: TrainingLabelVersion | None = None
        if self.version_path.exists():
            for line in self.version_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                raw = json.loads(line)
                if raw.get("template_code") != template_code or raw.get("version") != version:
                    continue
                record = TrainingLabelVersion.model_validate(raw)
                _assert_customer_safe_training_label(record)
                if matched is not None:
                    raise TrainingLabelFailure("duplicate_training_version")
                matched = record.model_copy(update={"can_rollback": record.version > 1})
        if matched is not None:
            return matched
        initial = self._initial(template_code)
        if initial is not None and initial.version == version:
            return initial
        return None

    def latest(self) -> TrainingLabelVersion | None:
        versions = self._read_versions()
        if versions:
            current_by_template: dict[str, TrainingLabelVersion] = {}
            for item in versions:
                previous = current_by_template.get(item.template_code)
                if previous is None or item.version > previous.version:
                    current_by_template[item.template_code] = item
            latest = max(current_by_template.values(), key=lambda item: item.created_at)
            return latest.model_copy(update={"can_rollback": latest.version > 1})
        if not self.initial_path.exists():
            return None
        for line in reversed(self.initial_path.read_text(encoding="utf-8").splitlines()):
            if line.strip():
                raw: dict[str, Any] = json.loads(line)
                code = raw.get("template_code")
                return self._initial(str(code)) if code else None
        return None

    def _append(self, version: TrainingLabelVersion) -> None:
        self.version_path.parent.mkdir(parents=True, exist_ok=True)
        with self.version_path.open("a", encoding="utf-8") as stream:
            stream.write(version.model_dump_json() + "\n")

    @staticmethod
    def _is_reusable_confirmation(record: TrainingLabelVersion) -> bool:
        return (
            record.material_evidence in {"merchant_declared", "trainer_confirmed"}
            and record.change_source != "exact_match_reused"
        )

    def _confirmed_exact_match(
        self,
        sha256: str,
        excluded_template_code: str,
    ) -> TrainingLabelVersion | None:
        normalized_sha256 = sha256.strip().lower()
        seen_templates: set[str] = set()
        for record in reversed(self._read_versions()):
            if record.template_code in seen_templates:
                continue
            seen_templates.add(record.template_code)
            if (
                record.template_code != excluded_template_code
                and record.sha256 == normalized_sha256
                and self._is_reusable_confirmation(record)
            ):
                return record

        if not self.initial_path.exists():
            return None
        initial_records = [
            json.loads(line)
            for line in self.initial_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        for raw in reversed(initial_records):
            template_code = str(raw.get("template_code", ""))
            if not template_code or template_code in seen_templates:
                continue
            seen_templates.add(template_code)
            initial_record = self._initial(template_code)
            if (
                initial_record is not None
                and template_code != excluded_template_code
                and initial_record.sha256 == normalized_sha256
                and self._is_reusable_confirmation(initial_record)
            ):
                return initial_record
        return None

    def register_initial(
        self,
        template_code: str,
        values: TrainingLabelInput,
        *,
        source: Literal["system_initial", "merchant_imported"] = "system_initial",
    ) -> TrainingLabelVersion:
        with self._lock:
            current = self.current(template_code)
            if current is not None:
                return current
            normalized = _normalize_and_validate(values)
            exact_match = self._confirmed_exact_match(
                normalized.sha256,
                excluded_template_code=template_code,
            )
            if exact_match is not None:
                normalized = TrainingLabelInput(**exact_match.model_dump()).model_copy(
                    update={
                        "main_image_template_code": None,
                        "product_type": None,
                        "inventory_status": None,
                        "service_rules": [],
                    }
                )
            initial = TrainingLabelVersion(
                **normalized.model_dump(),
                version_id=uuid5(NAMESPACE_URL, f"local-training-label:{template_code}:v1"),
                template_code=template_code,
                version=1,
                previous_version_id=None,
                rollback_target_version_id=None,
                change_source=("exact_match_reused" if exact_match else source),
                material_evidence=(
                    exact_match.material_evidence
                    if exact_match
                    else (
                        "merchant_declared" if source == "merchant_imported" else "system_initial"
                    )
                ),
                match_method="exact_sha256" if exact_match else None,
                matched_from_template_code=(exact_match.template_code if exact_match else None),
                created_at=datetime.now(UTC),
                can_rollback=False,
            )
            self.initial_path.parent.mkdir(parents=True, exist_ok=True)
            with self.initial_path.open("a", encoding="utf-8") as stream:
                stream.write(initial.model_dump_json() + "\n")
            return initial

    def _initialize_and_append(
        self,
        initial: TrainingLabelVersion,
        saved: TrainingLabelVersion,
    ) -> None:
        self.version_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.version_path.with_name(f".{self.version_path.name}.writing")
        temporary.write_text(
            initial.model_dump_json() + "\n" + saved.model_dump_json() + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.version_path)

    def _append_with_initial_history(
        self,
        initial: TrainingLabelVersion,
        saved: TrainingLabelVersion,
    ) -> None:
        existing_versions = self._read_versions()
        if not existing_versions:
            self._initialize_and_append(initial, saved)
            return
        if not any(item.template_code == initial.template_code for item in existing_versions):
            self._append(initial)
        self._append(saved)

    def backfill_exact_matches(self) -> list[TrainingLabelVersion]:
        with self._lock:
            initial_records = (
                [
                    json.loads(line)
                    for line in self.initial_path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
                if self.initial_path.exists()
                else []
            )
            versions = self._read_versions()
            initial_by_template: dict[str, TrainingLabelVersion] = {}
            template_codes: list[str] = []
            for raw in initial_records:
                template_code = str(raw.get("template_code", ""))
                if not template_code:
                    continue
                initial_by_template[template_code] = self._initial_from_raw(raw, template_code)
                if template_code not in template_codes:
                    template_codes.append(template_code)
            current_by_template: dict[str, TrainingLabelVersion] = dict(initial_by_template)
            for record in versions:
                current = current_by_template.get(record.template_code)
                if current is None or record.version > current.version:
                    current_by_template[record.template_code] = record
                if record.template_code not in template_codes:
                    template_codes.append(record.template_code)

            latest_candidates: list[TrainingLabelVersion] = []
            seen_templates: set[str] = set()
            for record in reversed(versions):
                if record.template_code in seen_templates:
                    continue
                seen_templates.add(record.template_code)
                if self._is_reusable_confirmation(record):
                    latest_candidates.append(record)
            for raw in reversed(initial_records):
                template_code = str(raw.get("template_code", ""))
                if not template_code or template_code in seen_templates:
                    continue
                seen_templates.add(template_code)
                record = initial_by_template[template_code]
                if self._is_reusable_confirmation(record):
                    latest_candidates.append(record)

            saved_versions: list[TrainingLabelVersion] = []
            versioned_templates = {record.template_code for record in versions}
            for template_code in template_codes:
                current = current_by_template.get(template_code)
                if current is None or current.material_evidence != "system_initial":
                    continue
                exact_match = next(
                    (
                        candidate
                        for candidate in latest_candidates
                        if candidate.template_code != template_code
                        and candidate.sha256 == current.sha256.strip().lower()
                    ),
                    None,
                )
                if exact_match is None:
                    continue
                matched_values = TrainingLabelInput(**exact_match.model_dump()).model_copy(
                    update={
                        "main_image_template_code": None,
                        "product_type": None,
                        "inventory_status": None,
                        "service_rules": [],
                    }
                )
                saved = TrainingLabelVersion(
                    **matched_values.model_dump(),
                    version_id=uuid4(),
                    template_code=template_code,
                    version=current.version + 1,
                    previous_version_id=current.version_id,
                    rollback_target_version_id=None,
                    change_source="exact_match_reused",
                    material_evidence=exact_match.material_evidence,
                    match_method="exact_sha256",
                    matched_from_template_code=exact_match.template_code,
                    created_at=datetime.now(UTC),
                    can_rollback=True,
                )
                if template_code not in versioned_templates:
                    self._append(current)
                    versioned_templates.add(template_code)
                self._append(saved)
                saved_versions.append(saved)
            return saved_versions

    def save(
        self,
        template_code: str,
        base_version: int,
        values: TrainingLabelInput,
    ) -> TrainingLabelVersion:
        with self._lock:
            current = self.current(template_code)
            if current is None or current.version != base_version:
                raise TrainingLabelFailure("version_conflict")
            normalized = _normalize_and_validate(values)
            saved = TrainingLabelVersion(
                **normalized.model_dump(),
                version_id=uuid4(),
                template_code=template_code,
                version=current.version + 1,
                previous_version_id=current.version_id,
                rollback_target_version_id=None,
                change_source="trainer_confirmed",
                material_evidence="trainer_confirmed",
                created_at=datetime.now(UTC),
                can_rollback=True,
            )
            self._append_with_initial_history(current, saved)
            return saved

    def save_confirmed_price(
        self,
        template_code: str,
        base_version: int,
        values: TrainingLabelInput,
        confirmation: PriceConfirmationAudit,
        before_append: Callable[[TrainingLabelVersion], None] | None = None,
    ) -> TrainingLabelVersion:
        with self._lock:
            current = self.current(template_code)
            if current is None or current.version != base_version:
                raise TrainingLabelFailure("version_conflict")
            normalized = _normalize_and_validate(values)
            if normalized.price_range is None:
                raise TrainingLabelFailure("price_range_required")
            if confirmation.anchor_confirmed_by is None or confirmation.anchor_confirmed_at is None:
                raise TrainingLabelFailure("anchor_confirmation_required")
            saved = TrainingLabelVersion(
                **normalized.model_dump(),
                version_id=uuid4(),
                template_code=template_code,
                version=current.version + 1,
                previous_version_id=current.version_id,
                rollback_target_version_id=None,
                change_source="trainer_confirmed",
                material_evidence="trainer_confirmed",
                created_at=datetime.now(UTC),
                can_rollback=True,
                price_confirmation=confirmation,
            )
            if before_append is not None:
                before_append(saved)
            self._append_with_initial_history(current, saved)
            return saved

    def rollback(self, template_code: str, base_version: int) -> TrainingLabelVersion:
        with self._lock:
            current = self.current(template_code)
            if current is None or current.version != base_version:
                raise TrainingLabelFailure("version_conflict")
            if current.previous_version_id is None:
                raise TrainingLabelFailure("no_previous_version")
            target = next(
                (
                    item
                    for item in self._read_versions()
                    if item.version_id == current.previous_version_id
                ),
                None,
            )
            if target is None:
                raise TrainingLabelFailure("no_previous_version")
            restored_values = TrainingLabelInput(**target.model_dump())
            rolled_back = TrainingLabelVersion(
                **restored_values.model_dump(),
                version_id=uuid4(),
                template_code=template_code,
                version=current.version + 1,
                previous_version_id=current.version_id,
                rollback_target_version_id=target.version_id,
                change_source="trainer_rollback",
                material_evidence="trainer_confirmed",
                created_at=datetime.now(UTC),
                can_rollback=True,
                price_confirmation=target.price_confirmation,
            )
            self._append(rolled_back)
            return rolled_back
