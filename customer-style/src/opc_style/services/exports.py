"""Deterministic, customer-scoped style evidence exports."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from opc_style.domain.analyzer import (
    STYLE_ANALYSIS_LIST_FIELDS,
    STYLE_FIELD_ENTRY_MAX_LENGTH,
    STYLE_FIELD_MAX_ITEMS,
)
from opc_style.domain.style_rules import SceneAspectRatio
from opc_style.models import (
    AuditEvent,
    SceneStyle,
    StyleCard,
    StyleCardSampleLink,
    StyleCardVersion,
    StyleSample,
    StyleTemplateSlot,
)
from opc_style.schemas import StyleTemplateSlotResponse

EXPORT_SCHEMA = "opc.style-export.v1"

_AUDIT_DETAIL_FIELDS = (
    "active_sample_count",
    "analyzer",
    "analysis_mode",
    "changed_fields",
    "consent_text_version",
    "main_style_card_version_id",
    "photo_id",
    "previous_style_card_version_id",
    "sample_id",
    "scene_id",
    "source",
    "style_card_id",
    "style_card_version_id",
    "version",
    "version_id",
)
_MAIN_VERSION_FIELDS = (
    "analysis_mode",
    "analyzer",
    "evidence_photo_ids",
    "sample_count",
    "source",
    "stability",
)
_SCENE_VERSION_FIELDS = (
    "aspect_ratio",
    "main_style_card_version_id",
    "name",
    "overrides",
    "purpose",
    "source",
    "usage_rules",
)
_AUDIT_REFERENCE_FIELDS = {
    "main_style_card_version_id": "main_style_version_ids",
    "photo_id": "photo_ids",
    "previous_style_card_version_id": "style_version_ids",
    "sample_id": "sample_ids",
    "scene_id": "scene_ids",
    "style_card_id": "style_card_ids",
    "style_card_version_id": "style_version_ids",
    "version_id": "style_version_ids",
}


class _Omit:
    """Internal marker for an untrusted value that must not be exported."""


_OMIT = _Omit()


@dataclass(frozen=True)
class _OwnedReferenceSets:
    """Opaque reference values that are safe to repeat in one export."""

    sample_ids: frozenset[str]
    photo_ids: frozenset[str]
    style_card_ids: frozenset[str]
    style_version_ids: frozenset[str]
    main_style_version_ids: frozenset[str]
    scene_ids: frozenset[str]


class StyleExportService:
    """Build one safe, read-only export from the request customer's records."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def build(self, customer_id: UUID) -> dict[str, Any]:
        """Return a stably ordered export without cross-customer data or secrets."""
        samples = self._samples(customer_id)
        cards = self._cards(customer_id)
        versions_by_card = self._versions_by_card(customer_id)
        evidence_by_version = self._evidence_by_version(customer_id)
        scenes_by_card = self._scenes_by_card(customer_id)
        references = _OwnedReferenceSets(
            sample_ids=frozenset(str(sample.id) for sample in samples),
            photo_ids=frozenset(sample.photo_id for sample in samples),
            style_card_ids=frozenset(str(card.id) for card in cards),
            style_version_ids=frozenset(
                str(version.id)
                for versions in versions_by_card.values()
                for version in versions
            ),
            main_style_version_ids=frozenset(
                str(version.id)
                for card in cards
                if card.kind == "main"
                for version in versions_by_card.get(card.id, [])
            ),
            scene_ids=frozenset(str(scene.id) for scene in scenes_by_card.values()),
        )

        return {
            "schema": EXPORT_SCHEMA,
            "customer_id": str(customer_id),
            "samples": [self._sample_export(sample) for sample in samples],
            "main_style_cards": [
                self._card_export(
                    card, versions_by_card, evidence_by_version, references, None
                )
                for card in cards
                if card.kind == "main"
            ],
            "scene_style_cards": [
                self._card_export(
                    card,
                    versions_by_card,
                    evidence_by_version,
                    references,
                    scenes_by_card.get(card.id),
                )
                for card in cards
                if card.kind == "scene"
            ],
            "audit_actions": [
                self._audit_export(event, references)
                for event in self._audit_events(customer_id)
            ],
        }

    def _samples(self, customer_id: UUID) -> list[StyleSample]:
        return list(
            self._session.scalars(
                select(StyleSample)
                .where(StyleSample.customer_id == customer_id)
                .order_by(StyleSample.authorized_at, StyleSample.id)
            )
        )

    def _cards(self, customer_id: UUID) -> list[StyleCard]:
        return list(
            self._session.scalars(
                select(StyleCard)
                .where(StyleCard.customer_id == customer_id)
                .order_by(StyleCard.created_at, StyleCard.id)
            )
        )

    def _versions_by_card(
        self, customer_id: UUID
    ) -> dict[UUID, list[StyleCardVersion]]:
        versions = self._session.scalars(
            select(StyleCardVersion)
            .where(StyleCardVersion.customer_id == customer_id)
            .order_by(
                StyleCardVersion.style_card_id,
                StyleCardVersion.version,
                StyleCardVersion.created_at,
                StyleCardVersion.id,
            )
        )
        grouped: dict[UUID, list[StyleCardVersion]] = defaultdict(list)
        for version in versions:
            grouped[version.style_card_id].append(version)
        return grouped

    def _evidence_by_version(self, customer_id: UUID) -> dict[UUID, list[UUID]]:
        links = self._session.scalars(
            select(StyleCardSampleLink)
            .where(StyleCardSampleLink.customer_id == customer_id)
            .order_by(
                StyleCardSampleLink.style_card_version_id,
                StyleCardSampleLink.created_at,
                StyleCardSampleLink.id,
            )
        )
        grouped: dict[UUID, list[UUID]] = defaultdict(list)
        for link in links:
            grouped[link.style_card_version_id].append(link.style_sample_id)
        return grouped

    def _scenes_by_card(self, customer_id: UUID) -> dict[UUID, SceneStyle]:
        scenes = self._session.scalars(
            select(SceneStyle)
            .where(SceneStyle.customer_id == customer_id)
            .order_by(SceneStyle.created_at, SceneStyle.id)
        )
        return {
            scene.style_card_id: scene
            for scene in scenes
            if scene.style_card_id is not None
        }

    def _audit_events(self, customer_id: UUID) -> list[AuditEvent]:
        return list(
            self._session.scalars(
                select(AuditEvent)
                .where(AuditEvent.customer_id == customer_id)
                .order_by(AuditEvent.occurred_at, AuditEvent.id)
            )
        )

    def _sample_export(self, sample: StyleSample) -> dict[str, Any]:
        history: list[dict[str, str]] = [
            {
                "action": "authorized",
                "occurred_at": _timestamp(sample.authorized_at),
                "provenance": sample.consent_provenance,
            }
        ]
        if sample.withdrawn_at is not None:
            history.append(
                {"action": "withdrawn", "occurred_at": _timestamp(sample.withdrawn_at)}
            )
        return {
            "id": str(sample.id),
            "photo_id": sample.photo_id,
            "status": "withdrawn" if sample.withdrawn_at is not None else "active",
            "consent_history": history,
        }

    def _card_export(
        self,
        card: StyleCard,
        versions_by_card: dict[UUID, list[StyleCardVersion]],
        evidence_by_version: dict[UUID, list[UUID]],
        references: _OwnedReferenceSets,
        scene: SceneStyle | None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": str(card.id),
            "active": card.active,
            "created_at": _timestamp(card.created_at),
            "versions": [
                self._version_export(
                    version, card.kind, evidence_by_version, references
                )
                for version in versions_by_card.get(card.id, [])
            ],
        }
        if scene is not None:
            result["scene"] = {
                "id": str(scene.id),
                "name": scene.name,
                "active": scene.active,
            }
        return result

    def _version_export(
        self,
        version: StyleCardVersion,
        kind: str,
        evidence_by_version: dict[UUID, list[UUID]],
        references: _OwnedReferenceSets,
    ) -> dict[str, Any]:
        content = version.content
        exported_content: dict[str, Any] = {
            "source": _string(content.get("source")),
        }
        if kind == "main":
            exported_content.update(
                {
                    field: _safe_list(content.get(field))
                    for field in STYLE_ANALYSIS_LIST_FIELDS
                }
            )
            exported_content.update(
                {
                    field: _safe_reference_list(
                        content.get(field), references.photo_ids
                    )
                    if field == "evidence_photo_ids"
                    else _safe_value(content.get(field))
                    for field in _MAIN_VERSION_FIELDS
                    if field != "source"
                    if field in content
                }
            )
        elif kind == "scene":
            for field in _SCENE_VERSION_FIELDS:
                if field == "source" or field not in content:
                    continue
                value = _safe_scene_value(field, content[field], references)
                if not isinstance(value, _Omit):
                    exported_content[field] = value

        result = {
            "id": str(version.id),
            "version": version.version,
            "created_at": _timestamp(version.created_at),
            "evidence_sample_ids": [
                str(sample_id)
                for sample_id in evidence_by_version.get(version.id, [])
                if str(sample_id) in references.sample_ids
            ],
            "content": exported_content,
        }
        if kind == "scene":
            result["slots"] = [
                StyleTemplateSlotResponse.model_validate(slot).model_dump(mode="json")
                for slot in self._session.scalars(
                    select(StyleTemplateSlot)
                    .where(
                        StyleTemplateSlot.customer_id == version.customer_id,
                        StyleTemplateSlot.style_card_version_id == version.id,
                    )
                    .order_by(StyleTemplateSlot.position)
                )
            ]
        return result

    def _audit_export(
        self, event: AuditEvent, references: _OwnedReferenceSets
    ) -> dict[str, Any]:
        details: dict[str, int | str | list[str] | None] = {}
        for field in sorted(set(_AUDIT_DETAIL_FIELDS).intersection(event.payload)):
            value = _safe_audit_value(field, event.payload[field], references)
            if not isinstance(value, _Omit):
                details[field] = value
        return {
            "id": str(event.id),
            "action": event.event_type,
            "occurred_at": _timestamp(event.occurred_at),
            "details": details,
        }


def _timestamp(value: datetime) -> str:
    """Serialize persisted timestamps without adding a non-deterministic export time."""
    if value.tzinfo is None or value.utcoffset() is None:
        normalized = value.replace(tzinfo=UTC)
    else:
        normalized = value.astimezone(UTC)
    return normalized.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _string(value: object) -> str | None:
    """Keep only string labels from version content."""
    return value if isinstance(value, str) else None


def _safe_list(value: object) -> list[str]:
    """Preserve known style labels without copying arbitrary provider payloads."""
    if not isinstance(value, list):
        return []
    return [entry for entry in value if isinstance(entry, str)]


def _safe_value(value: object) -> str | int | list[str] | None:
    """Export known scalar labels and arrays, never nested arbitrary content."""
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        return value
    if isinstance(value, list):
        return _safe_list(value)
    return None


def _safe_scene_value(
    field: str, value: object, references: _OwnedReferenceSets
) -> str | list[str] | dict[str, list[str]] | _Omit | None:
    """Export scene names, pins, and validated override labels only."""
    if field == "overrides" and isinstance(value, dict):
        return {
            name: _safe_list(values)
            for name, values in sorted(value.items())
            if name in STYLE_ANALYSIS_LIST_FIELDS
        }
    if field == "main_style_card_version_id":
        return _safe_reference(value, references.main_style_version_ids)
    if field == "aspect_ratio":
        if isinstance(value, str) and value in SceneAspectRatio:
            return value
        return _OMIT
    if field == "purpose":
        if (
            isinstance(value, str)
            and value.strip()
            and len(value.strip()) <= STYLE_FIELD_ENTRY_MAX_LENGTH
        ):
            return value.strip()
        return _OMIT
    if field == "usage_rules":
        if (
            not isinstance(value, list)
            or len(value) > STYLE_FIELD_MAX_ITEMS
            or any(
                not isinstance(entry, str)
                or not entry.strip()
                or len(entry.strip()) > STYLE_FIELD_ENTRY_MAX_LENGTH
                for entry in value
            )
        ):
            return _OMIT
        return [entry.strip() for entry in value]
    return _string(value)


def _safe_reference(value: object, owned_values: frozenset[str]) -> str | _Omit:
    """Repeat an ID only after checking it belongs to this export customer."""
    if isinstance(value, str) and value in owned_values:
        return value
    return _OMIT


def _safe_reference_list(value: object, owned_values: frozenset[str]) -> list[str]:
    """Keep only this customer's ID values from an otherwise untrusted list."""
    if not isinstance(value, list):
        return []
    return [
        entry for entry in value if isinstance(entry, str) and entry in owned_values
    ]


def _safe_audit_value(
    field: str, value: object, references: _OwnedReferenceSets
) -> int | str | list[str] | None | _Omit:
    """Limit audit details to simple, known fields and omit secrets by construction."""
    reference_set_name = _AUDIT_REFERENCE_FIELDS.get(field)
    if reference_set_name is not None:
        return _safe_reference(value, getattr(references, reference_set_name))
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        return value
    if isinstance(value, list):
        return _safe_list(value)
    return None
