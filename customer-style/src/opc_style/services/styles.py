"""Customer-scoped generation and immutable versioning for main style cards."""

import json
import sqlite3
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, Literal, NoReturn, cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import and_, func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from opc_style.domain.analyzer import (
    STYLE_ANALYSIS_LIST_FIELDS,
    STYLE_FIELD_ENTRY_MAX_LENGTH,
    STYLE_FIELD_MAX_ITEMS,
    SYNTHETIC_METADATA_TEST_MODE,
    DeterministicLocalStyleAnalyzer,
    SampleDescriptor,
    StyleAnalysis,
    StyleAnalyzer,
    VisualStyleAnalyzer,
)
from opc_style.domain.errors import DomainRuleError
from opc_style.domain.style_evidence import (
    CUSTOMER_UPLOAD_STYLE_ORIGIN,
    LOCAL_DEMO_STYLE_ORIGIN,
    LOCAL_SYNTHETIC_CONSENT_PROVENANCES,
)
from opc_style.domain.style_rules import (
    SCENE_CHANNEL_FIELDS,
    SampleReadiness,
    assess_sample_count,
    enforce_scene_limit,
)
from opc_style.models import (
    AuditEvent,
    SceneStyle,
    StyleCard,
    StyleCardSampleLink,
    StyleCardVersion,
    StyleFieldEvidence,
    StyleSample,
    StyleTemplateSlot,
)
from opc_style.schemas import (
    HistoricalStyleGroupConfirmRequest,
    HistoricalStyleGroupsConfirmRequest,
    SceneStyleCardEditRequest,
    StyleTemplateSlotRequest,
    StyleTemplateSlotResponse,
)
from opc_style.services.real_photos import AssetContentStore
from opc_style.services.style_analysis import (
    StyleAnalysisProposal,
    StyleAnalysisService,
)
from opc_style.services.style_consent import coordinate_style_consent


class InsufficientStyleSamplesError(Exception):
    """Raised when fewer than ten active authorized samples are available."""

    def __init__(self, readiness: SampleReadiness) -> None:
        self.readiness = readiness
        super().__init__("insufficient_style_samples")


class MainStyleCardNotFoundError(Exception):
    """Raised when the requesting customer has no active main style card."""


class SceneStyleCardNotFoundError(Exception):
    """Raised when a scene is absent, inactive, or owned by another customer."""


@dataclass(frozen=True)
class MainStyleCardSnapshot:
    """Persistence pair representing one immutable customer-visible revision."""

    card: StyleCard
    version: StyleCardVersion


@dataclass(frozen=True)
class SceneStyleCardSnapshot:
    """One scene revision plus its customer-visible inherited content."""

    scene: SceneStyle
    card: StyleCard
    version: StyleCardVersion
    resolved_content: dict[str, Any]


class MainStyleCardService:
    """Create and read main-card revisions within one customer boundary."""

    def __init__(
        self,
        session: Session,
        analyzer: StyleAnalyzer | None = None,
        *,
        content_store: AssetContentStore | None = None,
        visual_analyzer: VisualStyleAnalyzer | None = None,
    ) -> None:
        self._session = session
        self._analyzer = analyzer or DeterministicLocalStyleAnalyzer()
        self._visual_analysis = StyleAnalysisService(
            session, content_store=content_store, analyzer=visual_analyzer
        )

    def propose(self, customer_id: UUID) -> StyleAnalysisProposal:
        """Return evidence-linked pixel suggestions without authoring a version."""
        return self._visual_analysis.propose(customer_id)

    def generate(self, customer_id: UUID) -> MainStyleCardSnapshot:
        """Generate a new version from this customer's active authorized samples."""
        try:
            return self._generate(customer_id)
        except IntegrityError as error:
            self._raise_write_conflict(error)
        except OperationalError as error:
            self._raise_operational_write_conflict(error)

    def _generate(self, customer_id: UUID) -> MainStyleCardSnapshot:
        """Run generation inside the public write-conflict boundary."""
        samples = self._active_samples(customer_id)
        readiness = assess_sample_count(len(samples))
        if not readiness.can_generate:
            raise InsufficientStyleSamplesError(readiness)
        analyzed_sample_ids = frozenset(sample.id for sample in samples)

        analysis = self._analyzer.analyze(
            [
                SampleDescriptor(
                    sample_id=sample.id,
                    photo_id=sample.photo_id,
                    synthetic_metadata={
                        "analysis_mode": SYNTHETIC_METADATA_TEST_MODE,
                        "record_type": "authorized_style_sample_descriptor",
                    },
                )
                for sample in samples
            ]
        )
        self._begin_serialized_sqlite_write()
        if self._active_sample_ids(customer_id) != analyzed_sample_ids:
            self._session.rollback()
            raise DomainRuleError("style_samples_changed")
        try:
            analysis = self._validate_analysis_style_content(analysis)
            evidence_samples = self._validate_evidence(
                samples, analysis.evidence_photo_ids
            )
        except DomainRuleError:
            self._session.rollback()
            raise

        card = self._active_main_card(customer_id)
        if card is None:
            card = StyleCard(customer_id=customer_id, kind="main", active=True)
            self._session.add(card)
            self._session.flush()

        content: dict[str, Any] = {
            **asdict(analysis),
            "source": "analyzer",
            "analyzer": self._analyzer.label,
            "analysis_mode": self._analyzer.analysis_mode,
            "stability": readiness.stability,
            "sample_count": readiness.active_sample_count,
        }
        version = self._create_version(customer_id, card, content)
        self._link_samples(customer_id, version, evidence_samples)
        self._record_audit(
            customer_id,
            "style_card.main.generated",
            {
                "style_card_id": str(card.id),
                "style_card_version_id": str(version.id),
                "version": version.version,
                "analyzer": self._analyzer.label,
                "analysis_mode": self._analyzer.analysis_mode,
                "active_sample_count": len(samples),
            },
        )
        self._session.commit()
        self._session.refresh(card)
        self._session.refresh(version)
        return MainStyleCardSnapshot(card=card, version=version)

    def edit(
        self, customer_id: UUID, changes: dict[str, list[str]]
    ) -> MainStyleCardSnapshot:
        """Copy the latest version, apply customer fields, and append a revision."""
        try:
            return self._edit(customer_id, changes)
        except IntegrityError as error:
            self._raise_write_conflict(error)
        except OperationalError as error:
            self._raise_operational_write_conflict(error)

    def _edit(
        self, customer_id: UUID, changes: dict[str, list[str]]
    ) -> MainStyleCardSnapshot:
        """Run editing inside the public write-conflict boundary."""
        self._begin_serialized_sqlite_write()
        current = self.get(customer_id)
        if all(
            current.version.content.get(field) == value
            for field, value in changes.items()
        ):
            raise DomainRuleError("style_card_no_changes")
        content = dict(current.version.content)
        content.update(changes)
        content["source"] = "customer_edit"
        version = self._create_version(customer_id, current.card, content)

        previous_links = list(
            self._session.scalars(
                select(StyleCardSampleLink).where(
                    StyleCardSampleLink.customer_id == customer_id,
                    StyleCardSampleLink.style_card_version_id == current.version.id,
                )
            )
        )
        for link in previous_links:
            self._session.add(
                StyleCardSampleLink(
                    customer_id=customer_id,
                    style_card_version_id=version.id,
                    style_sample_id=link.style_sample_id,
                )
            )

        self._record_audit(
            customer_id,
            "style_card.main.edited",
            {
                "style_card_id": str(current.card.id),
                "style_card_version_id": str(version.id),
                "previous_style_card_version_id": str(current.version.id),
                "version": version.version,
                "source": "customer_edit",
                "changed_fields": sorted(changes),
            },
        )
        self._session.commit()
        self._session.refresh(version)
        return MainStyleCardSnapshot(card=current.card, version=version)

    def get(self, customer_id: UUID) -> MainStyleCardSnapshot:
        """Return only the requesting customer's latest active main-card version."""
        card = self._active_main_card(customer_id)
        if card is None:
            raise MainStyleCardNotFoundError
        version = self._latest_version(customer_id, card.id)
        if version is None:
            raise MainStyleCardNotFoundError
        return MainStyleCardSnapshot(card=card, version=version)

    def _active_samples(self, customer_id: UUID) -> list[StyleSample]:
        return [
            sample
            for sample in self._session.scalars(
                select(StyleSample)
                .where(
                    StyleSample.customer_id == customer_id,
                    StyleSample.withdrawn_at.is_(None),
                    StyleSample.consent_provenance.not_in(
                        LOCAL_SYNTHETIC_CONSENT_PROVENANCES
                    ),
                )
                .order_by(StyleSample.photo_id, StyleSample.id)
            )
            if sample.has_complete_content_seal
        ]

    def _active_sample_ids(self, customer_id: UUID) -> frozenset[UUID]:
        """Re-read the exact active input set after reserving the writer slot."""
        return frozenset(
            sample.id
            for sample in self._session.scalars(
                select(StyleSample).where(
                    StyleSample.customer_id == customer_id,
                    StyleSample.withdrawn_at.is_(None),
                    StyleSample.consent_provenance.not_in(
                        LOCAL_SYNTHETIC_CONSENT_PROVENANCES
                    ),
                )
            )
            if sample.has_complete_content_seal
        )

    def _validate_evidence(
        self, samples: list[StyleSample], evidence_photo_ids: object
    ) -> list[StyleSample]:
        """Resolve a non-empty unique evidence subset from the exact active input."""
        if (
            not isinstance(evidence_photo_ids, list)
            or not evidence_photo_ids
            or any(
                not isinstance(photo_id, str) or not photo_id.strip()
                for photo_id in evidence_photo_ids
            )
        ):
            raise DomainRuleError("invalid_analyzer_evidence")
        validated_photo_ids = cast(list[str], evidence_photo_ids)
        evidence_set = set(validated_photo_ids)
        input_by_photo_id = {sample.photo_id: sample for sample in samples}
        if len(evidence_set) != len(validated_photo_ids) or not evidence_set.issubset(
            input_by_photo_id
        ):
            raise DomainRuleError("invalid_analyzer_evidence")
        return [input_by_photo_id[photo_id] for photo_id in validated_photo_ids]

    def _validate_analysis_style_content(
        self, analysis: StyleAnalysis
    ) -> StyleAnalysis:
        """Normalize and bound every provider-owned editable style list."""
        normalized: dict[str, list[str]] = {}
        for field_name in STYLE_ANALYSIS_LIST_FIELDS:
            values = getattr(analysis, field_name)
            if len(values) > STYLE_FIELD_MAX_ITEMS or any(
                not isinstance(value, str)
                or not value.strip()
                or len(value.strip()) > STYLE_FIELD_ENTRY_MAX_LENGTH
                for value in values
            ):
                raise DomainRuleError("invalid_analyzer_style_content")
            normalized[field_name] = [value.strip() for value in values]
        return replace(analysis, **normalized)

    def _active_main_card(self, customer_id: UUID) -> StyleCard | None:
        return self._session.scalar(
            select(StyleCard).where(
                StyleCard.customer_id == customer_id,
                StyleCard.kind == "main",
                StyleCard.active.is_(True),
            )
        )

    def _latest_version(
        self, customer_id: UUID, style_card_id: UUID
    ) -> StyleCardVersion | None:
        return self._session.scalar(
            select(StyleCardVersion)
            .where(
                StyleCardVersion.customer_id == customer_id,
                StyleCardVersion.style_card_id == style_card_id,
            )
            .order_by(StyleCardVersion.version.desc())
            .limit(1)
        )

    def _create_version(
        self, customer_id: UUID, card: StyleCard, content: dict[str, Any]
    ) -> StyleCardVersion:
        highest_version = self._session.scalar(
            select(func.max(StyleCardVersion.version)).where(
                StyleCardVersion.customer_id == customer_id,
                StyleCardVersion.style_card_id == card.id,
            )
        )
        version = StyleCardVersion(
            customer_id=customer_id,
            style_card_id=card.id,
            version=(highest_version or 0) + 1,
            content=content,
        )
        self._session.add(version)
        self._session.flush()
        return version

    def _begin_serialized_sqlite_write(self) -> None:
        """Reserve SQLite's single writer slot before allocating card versions."""
        connection = self._session.connection()
        if connection.dialect.name != "sqlite":
            return
        try:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        except OperationalError as error:
            if "cannot start a transaction within a transaction" not in str(error):
                raise

    def _raise_write_conflict(self, error: IntegrityError) -> NoReturn:
        """Rollback and translate a residual uniqueness race into a stable code."""
        self._session.rollback()
        if "UNIQUE constraint failed" in str(error):
            raise DomainRuleError("style_card_write_conflict") from error
        raise error

    def _raise_operational_write_conflict(self, error: OperationalError) -> NoReturn:
        """Translate only SQLite busy/locked failures into retryable conflicts."""
        sqlite_error_code = getattr(error.orig, "sqlite_errorcode", None)
        base_error_code = (
            sqlite_error_code & 0xFF if isinstance(sqlite_error_code, int) else None
        )
        message = str(error.orig).lower()
        if base_error_code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED} or any(
            lock_message in message
            for lock_message in ("database is locked", "database table is locked")
        ):
            self._session.rollback()
            raise DomainRuleError("style_card_write_conflict") from error
        raise error

    def _link_samples(
        self,
        customer_id: UUID,
        version: StyleCardVersion,
        samples: list[StyleSample],
    ) -> None:
        for sample in samples:
            self._session.add(
                StyleCardSampleLink(
                    customer_id=customer_id,
                    style_card_version_id=version.id,
                    style_sample_id=sample.id,
                )
            )

    def _record_audit(
        self, customer_id: UUID, event_type: str, payload: dict[str, Any]
    ) -> None:
        self._session.add(
            AuditEvent(
                customer_id=customer_id,
                event_type=event_type,
                payload=payload,
                occurred_at=datetime.now(UTC),
            )
        )


class SceneStyleCardService(MainStyleCardService):
    """Create versioned scene cards pinned to one immutable main-card revision."""

    def create(
        self,
        customer_id: UUID,
        name: str,
        changes: dict[str, Any],
    ) -> SceneStyleCardSnapshot:
        """Create an active scene within the customer limit."""
        try:
            return self._create_scene(customer_id, name, changes)
        except IntegrityError as error:
            self._raise_scene_write_conflict(error)
        except OperationalError as error:
            self._raise_operational_write_conflict(error)

    def _create_scene(
        self,
        customer_id: UUID,
        name: str,
        changes: dict[str, Any],
    ) -> SceneStyleCardSnapshot:
        self._begin_serialized_sqlite_write()
        main_card = self._active_main_card(customer_id)
        if main_card is None:
            raise MainStyleCardNotFoundError
        main_version = self._latest_version(customer_id, main_card.id)
        if main_version is None:
            raise MainStyleCardNotFoundError
        overrides, channel_fields = self._split_scene_changes(changes)

        if self._active_scene_name_exists(
            customer_id, name, CUSTOMER_UPLOAD_STYLE_ORIGIN
        ):
            raise DomainRuleError("scene_name_conflict")

        active_scene_count = self._session.scalar(
            select(func.count())
            .select_from(SceneStyle)
            .where(
                SceneStyle.customer_id == customer_id,
                SceneStyle.active.is_(True),
            )
        )
        enforce_scene_limit(active_scene_count or 0)

        scene_id = uuid4()
        card = StyleCard(
            id=scene_id,
            customer_id=customer_id,
            kind="scene",
            active=True,
        )
        self._session.add(card)
        self._session.flush()
        scene = SceneStyle(
            id=scene_id,
            customer_id=customer_id,
            style_card_id=card.id,
            name=name,
            style_origin=CUSTOMER_UPLOAD_STYLE_ORIGIN,
            active=True,
            rules={},
        )
        self._session.add(scene)
        version = self._create_version(
            customer_id,
            card,
            {
                "main_style_card_version_id": str(main_version.id),
                "name": name,
                "style_origin": CUSTOMER_UPLOAD_STYLE_ORIGIN,
                "overrides": overrides,
                "source": "scene_create",
                **channel_fields,
            },
        )
        self._record_audit(
            customer_id,
            "style_card.scene.created",
            {
                "style_card_id": str(card.id),
                "style_card_version_id": str(version.id),
                "main_style_card_version_id": str(main_version.id),
                "version": version.version,
                "changed_fields": sorted(changes),
            },
        )
        self._session.commit()
        self._session.refresh(scene)
        self._session.refresh(card)
        self._session.refresh(version)
        return SceneStyleCardSnapshot(
            scene=scene,
            card=card,
            version=version,
            resolved_content=self._resolve_scene_content(
                main_version.content,
                version.content,
            ),
        )

    def edit_scene(
        self,
        customer_id: UUID,
        scene_id: UUID,
        changes: dict[str, Any],
    ) -> SceneStyleCardSnapshot:
        """Append one revision while retaining the scene's original main-card pin."""
        try:
            with coordinate_style_consent(customer_id):
                return self._edit_scene(customer_id, scene_id, changes)
        except DomainRuleError:
            self._session.rollback()
            raise
        except IntegrityError as error:
            self._raise_scene_write_conflict(error)
        except OperationalError as error:
            self._raise_operational_write_conflict(error)

    def _edit_scene(
        self,
        customer_id: UUID,
        scene_id: UUID,
        changes: dict[str, Any],
    ) -> SceneStyleCardSnapshot:
        self._begin_serialized_sqlite_write()
        scene = self._active_scene(customer_id, scene_id)
        if scene is None or scene.style_card_id is None:
            raise SceneStyleCardNotFoundError
        card = self._session.scalar(
            select(StyleCard).where(
                StyleCard.customer_id == customer_id,
                StyleCard.id == scene.style_card_id,
                StyleCard.kind == "scene",
                StyleCard.active.is_(True),
            )
        )
        if card is None:
            raise SceneStyleCardNotFoundError
        current_version = self._latest_version(customer_id, card.id)
        if current_version is None:
            raise SceneStyleCardNotFoundError
        if current_version.content.get("historical_style") is True:
            return HistoricalStyleConfirmationService(
                self._session
            )._edit_confirmed_scene(customer_id, scene, card, current_version, changes)

        requested_changes = dict(changes)
        current_name = cast(str, current_version.content["name"])
        current_overrides = dict(
            cast(dict[str, list[str]], current_version.content["overrides"])
        )
        current_channel_fields = {
            field: current_version.content[field]
            for field in SCENE_CHANNEL_FIELDS
            if field in current_version.content
        }
        next_name = cast(str, requested_changes.pop("name", current_name))
        next_overrides = dict(current_overrides)
        next_channel_fields = dict(current_channel_fields)
        changed_fields: set[str] = set()
        missing = object()
        for field, value in requested_changes.items():
            if field in STYLE_ANALYSIS_LIST_FIELDS:
                previous_value = current_overrides.get(field, missing)
                if value is None:
                    next_overrides.pop(field, None)
                    next_value = missing
                else:
                    next_overrides[field] = cast(list[str], value)
                    next_value = value
            elif field in SCENE_CHANNEL_FIELDS:
                previous_value = current_channel_fields.get(field, missing)
                if value is None:
                    next_channel_fields.pop(field, None)
                    next_value = missing
                else:
                    next_channel_fields[field] = value
                    next_value = value
            else:
                raise DomainRuleError("invalid_scene_field")
            if previous_value != next_value:
                changed_fields.add(field)
        if next_name != current_name:
            changed_fields.add("name")
        if not changed_fields:
            raise DomainRuleError("style_card_no_changes")
        if next_name != current_name and self._active_scene_name_exists(
            customer_id,
            next_name,
            scene.style_origin,
            exclude_scene_id=scene.id,
        ):
            raise DomainRuleError("scene_name_conflict")
        main_version = self._pinned_main_version(customer_id, current_version)
        if main_version is None:
            raise MainStyleCardNotFoundError

        scene.name = next_name
        version = self._create_version(
            customer_id,
            card,
            {
                "main_style_card_version_id": str(main_version.id),
                "name": next_name,
                "style_origin": scene.style_origin,
                "overrides": next_overrides,
                "source": "scene_edit",
                **next_channel_fields,
            },
        )
        self._record_audit(
            customer_id,
            "style_card.scene.edited",
            {
                "style_card_id": str(card.id),
                "style_card_version_id": str(version.id),
                "previous_style_card_version_id": str(current_version.id),
                "main_style_card_version_id": str(main_version.id),
                "version": version.version,
                "changed_fields": sorted(changed_fields),
            },
        )
        self._session.commit()
        self._session.refresh(scene)
        self._session.refresh(version)
        return SceneStyleCardSnapshot(
            scene=scene,
            card=card,
            version=version,
            resolved_content=self._resolve_scene_content(
                main_version.content,
                version.content,
            ),
        )

    def _active_scene(self, customer_id: UUID, scene_id: UUID) -> SceneStyle | None:
        return self._session.scalar(
            select(SceneStyle).where(
                SceneStyle.customer_id == customer_id,
                SceneStyle.id == scene_id,
                SceneStyle.active.is_(True),
            )
        )

    @staticmethod
    def _split_scene_changes(
        changes: dict[str, Any],
    ) -> tuple[dict[str, list[str]], dict[str, Any]]:
        """Keep inherited style overrides separate from scene-only channel fields."""
        overrides: dict[str, list[str]] = {}
        channel_fields: dict[str, Any] = {}
        for field, value in changes.items():
            if field in STYLE_ANALYSIS_LIST_FIELDS:
                if value is not None:
                    overrides[field] = cast(list[str], value)
            elif field in SCENE_CHANNEL_FIELDS:
                if value is not None:
                    channel_fields[field] = value
            else:
                raise DomainRuleError("invalid_scene_field")
        return overrides, channel_fields

    @staticmethod
    def _resolve_scene_content(
        main_content: dict[str, Any],
        scene_version_content: dict[str, Any],
    ) -> dict[str, Any]:
        """Resolve the pinned main fields plus explicit scene and channel content."""
        overrides = cast(
            dict[str, list[str]], scene_version_content.get("overrides", {})
        )
        if scene_version_content.get("historical_style") is True:
            main_content = scene_version_content
        return {
            **main_content,
            **overrides,
            "aspect_ratio": scene_version_content.get("aspect_ratio"),
            "purpose": scene_version_content.get("purpose"),
            "usage_rules": cast(
                list[str], scene_version_content.get("usage_rules", [])
            ),
            "source": scene_version_content.get("source"),
            "slots": scene_version_content.get("slots", []),
        }

    def _scene(self, customer_id: UUID, scene_id: UUID) -> SceneStyle | None:
        return self._session.scalar(
            select(SceneStyle).where(
                SceneStyle.customer_id == customer_id,
                SceneStyle.id == scene_id,
            )
        )

    def _active_scene_name_exists(
        self,
        customer_id: UUID,
        name: str,
        style_origin: str,
        *,
        exclude_scene_id: UUID | None = None,
    ) -> bool:
        statement = select(SceneStyle.id).where(
            SceneStyle.customer_id == customer_id,
            SceneStyle.name == name,
            SceneStyle.style_origin == style_origin,
            SceneStyle.active.is_(True),
        )
        if exclude_scene_id is not None:
            statement = statement.where(SceneStyle.id != exclude_scene_id)
        return self._session.scalar(statement.limit(1)) is not None

    def _active_scene_named(
        self,
        customer_id: UUID,
        name: str,
        style_origin: str,
    ) -> SceneStyle | None:
        return self._session.scalar(
            select(SceneStyle).where(
                SceneStyle.customer_id == customer_id,
                SceneStyle.name == name,
                SceneStyle.style_origin == style_origin,
                SceneStyle.active.is_(True),
            )
        )

    def _raise_scene_write_conflict(self, error: IntegrityError) -> NoReturn:
        """Translate only the active scene-name constraint as non-retryable."""
        message = str(error)
        if (
            "uq_scene_styles_active_customer_origin_name" in message
            or "UNIQUE constraint failed: scene_styles.customer_id, "
            "scene_styles.style_origin, scene_styles.name"
            in message
            or "uq_scene_styles_active_customer_name" in message
            or "UNIQUE constraint failed: scene_styles.customer_id, scene_styles.name"
            in message
        ):
            self._session.rollback()
            raise DomainRuleError("scene_name_conflict") from error
        self._raise_write_conflict(error)

    def deactivate(self, customer_id: UUID, scene_id: UUID) -> SceneStyleCardSnapshot:
        """Deactivate a scene and its card without removing historical revisions."""
        try:
            return self._deactivate_scene(customer_id, scene_id)
        except IntegrityError as error:
            self._raise_write_conflict(error)
        except OperationalError as error:
            self._raise_operational_write_conflict(error)

    def _deactivate_scene(
        self, customer_id: UUID, scene_id: UUID
    ) -> SceneStyleCardSnapshot:
        self._begin_serialized_sqlite_write()
        scene = self._scene(customer_id, scene_id)
        if scene is None or scene.style_card_id is None:
            raise SceneStyleCardNotFoundError
        card = self._session.scalar(
            select(StyleCard).where(
                StyleCard.customer_id == customer_id,
                StyleCard.id == scene.style_card_id,
                StyleCard.kind == "scene",
            )
        )
        if card is None:
            raise SceneStyleCardNotFoundError
        version = self._latest_version(customer_id, card.id)
        if version is None:
            raise SceneStyleCardNotFoundError
        main_version = self._pinned_main_version(customer_id, version)

        if not scene.active:
            self._session.commit()
            return SceneStyleCardSnapshot(
                scene=scene,
                card=card,
                version=version,
                resolved_content=self._resolve_scene_content(
                    main_version.content if main_version else {},
                    version.content,
                ),
            )

        scene.active = False
        card.active = False
        self._record_audit(
            customer_id,
            "style_card.scene.deactivated",
            {
                "style_card_id": str(card.id),
                "style_card_version_id": str(version.id),
                "main_style_card_version_id": str(main_version.id)
                if main_version
                else None,
                "version": version.version,
            },
        )
        self._session.commit()
        self._session.refresh(scene)
        self._session.refresh(card)
        return SceneStyleCardSnapshot(
            scene=scene,
            card=card,
            version=version,
            resolved_content=self._resolve_scene_content(
                main_version.content if main_version else {},
                version.content,
            ),
        )

    def _pinned_main_version(
        self, customer_id: UUID, scene_version: StyleCardVersion
    ) -> StyleCardVersion | None:
        if (
            scene_version.content.get("historical_style") is True
            and scene_version.content.get("main_style_card_version_id") is None
        ):
            return None
        main_version_id = UUID(
            cast(str, scene_version.content["main_style_card_version_id"])
        )
        main_version = self._session.scalar(
            select(StyleCardVersion)
            .join(StyleCard, StyleCard.id == StyleCardVersion.style_card_id)
            .where(
                StyleCardVersion.customer_id == customer_id,
                StyleCardVersion.id == main_version_id,
                StyleCard.customer_id == customer_id,
                StyleCard.kind == "main",
            )
        )
        if main_version is None:
            raise MainStyleCardNotFoundError
        return main_version

    def list(self, customer_id: UUID) -> list[SceneStyleCardSnapshot]:
        """List this customer's active and inactive scenes with latest revisions."""
        scenes = list(
            self._session.scalars(
                select(SceneStyle)
                .where(SceneStyle.customer_id == customer_id)
                .order_by(SceneStyle.created_at, SceneStyle.id)
            )
        )
        snapshots: list[SceneStyleCardSnapshot] = []
        for scene in scenes:
            if scene.style_card_id is None:
                continue
            card = self._session.scalar(
                select(StyleCard).where(
                    StyleCard.customer_id == customer_id,
                    StyleCard.id == scene.style_card_id,
                    StyleCard.kind == "scene",
                )
            )
            if card is None:
                continue
            version = self._latest_version(customer_id, card.id)
            if version is None:
                continue
            main_version = self._pinned_main_version(customer_id, version)
            snapshots.append(
                SceneStyleCardSnapshot(
                    scene=scene,
                    card=card,
                    version=version,
                    resolved_content=self._resolve_scene_content(
                        main_version.content if main_version else {},
                        version.content,
                    ),
                )
            )
        return snapshots

    def get_version(
        self, customer_id: UUID, scene_id: UUID, version_id: UUID
    ) -> SceneStyleCardSnapshot:
        """Read an exact owned revision, including inactive historical scenes."""
        scene = self._scene(customer_id, scene_id)
        if scene is None or scene.style_card_id is None:
            raise SceneStyleCardNotFoundError
        card = self._session.scalar(
            select(StyleCard).where(
                StyleCard.customer_id == customer_id,
                StyleCard.id == scene.style_card_id,
                StyleCard.kind == "scene",
            )
        )
        version = self._session.scalar(
            select(StyleCardVersion).where(
                StyleCardVersion.customer_id == customer_id,
                StyleCardVersion.style_card_id == scene.style_card_id,
                StyleCardVersion.id == version_id,
            )
        )
        if card is None or version is None:
            raise SceneStyleCardNotFoundError
        main = self._pinned_main_version(customer_id, version)
        return SceneStyleCardSnapshot(
            scene,
            card,
            version,
            self._resolve_scene_content(main.content if main else {}, version.content),
        )


class HistoricalStyleConfirmationService(SceneStyleCardService):
    """Author customer-reviewed groups atomically without calling any analyzer."""

    def confirm(
        self,
        customer_id: UUID,
        request: HistoricalStyleGroupsConfirmRequest,
        *,
        style_origin: Literal["customer_upload", "local_demo"] = "customer_upload",
        commit: bool = True,
    ) -> list[SceneStyleCardSnapshot]:
        try:
            with coordinate_style_consent(customer_id):
                self._begin_serialized_sqlite_write()
                snapshots = self._confirm_without_commit(
                    customer_id, request, style_origin
                )
                if commit:
                    self._session.commit()
                    for snapshot in snapshots:
                        self._session.refresh(snapshot.version)
                else:
                    self._session.flush()
                return snapshots
        except DomainRuleError:
            self._session.rollback()
            raise
        except IntegrityError as error:
            self._raise_scene_write_conflict(error)
        except OperationalError as error:
            self._raise_operational_write_conflict(error)

    def _confirmed_evidence(
        self, customer_id: UUID, sample_ids: list[UUID]
    ) -> list[StyleSample]:
        rows = list(
            self._session.scalars(
                select(StyleSample)
                .where(
                    StyleSample.customer_id == customer_id,
                    StyleSample.id.in_(sample_ids),
                    StyleSample.withdrawn_at.is_(None),
                )
                .execution_options(populate_existing=True)
            )
        )
        if len(rows) != len(sample_ids) or any(
            not row.has_complete_content_seal for row in rows
        ):
            raise DomainRuleError("historical_style_evidence_unavailable")
        by_id = {row.id: row for row in rows}
        return [by_id[sample_id] for sample_id in sample_ids]

    def _confirm_without_commit(
        self,
        customer_id: UUID,
        request: HistoricalStyleGroupsConfirmRequest,
        style_origin: Literal["customer_upload", "local_demo"],
    ) -> list[SceneStyleCardSnapshot]:
        fingerprint = sha256(
            json.dumps(
                {
                    "request": request.model_dump(mode="json"),
                    "style_origin": style_origin,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        receipt_id = uuid5(
            NAMESPACE_URL,
            f"opc-style-confirmation:{customer_id}:{request.confirmation_key}",
        )
        receipt = self._session.get(AuditEvent, receipt_id)
        if receipt is not None:
            if (
                receipt.customer_id != customer_id
                or receipt.payload.get("fingerprint") != fingerprint
            ):
                raise DomainRuleError("style_confirmation_conflict")
            return [
                self.get_version(
                    customer_id, UUID(row["scene_id"]), UUID(row["version_id"])
                )
                for row in receipt.payload["versions"]
            ]

        evidence_groups = [
            self._confirmed_evidence(customer_id, group.evidence_sample_ids)
            for group in request.groups
        ]
        declared_library_counts = {
            group.library_sample_count
            for group in request.groups
            if group.library_sample_count is not None
        }
        if declared_library_counts:
            eligible_library = list(
                self._session.scalars(
                    select(StyleSample).where(
                        StyleSample.customer_id == customer_id,
                        StyleSample.withdrawn_at.is_(None),
                    )
                )
            )
            actual_library_count = sum(
                sample.has_complete_content_seal
                and sample.consent_provenance not in LOCAL_SYNTHETIC_CONSENT_PROVENANCES
                for sample in eligible_library
            )
            if declared_library_counts != {actual_library_count}:
                raise DomainRuleError("historical_style_library_changed")
        for samples in evidence_groups:
            demo_markers = [
                sample.consent_provenance in LOCAL_SYNTHETIC_CONSENT_PROVENANCES
                for sample in samples
            ]
            if (style_origin == LOCAL_DEMO_STYLE_ORIGIN and not all(demo_markers)) or (
                style_origin == CUSTOMER_UPLOAD_STYLE_ORIGIN and any(demo_markers)
            ):
                raise DomainRuleError("historical_style_origin_mismatch")
        for group in request.groups:
            if (
                style_origin == CUSTOMER_UPLOAD_STYLE_ORIGIN
                and group.analysis_mode is not None
                and (
                    "synthetic" in group.analysis_mode.lower()
                    or "demo" in group.analysis_mode.lower()
                )
            ):
                raise DomainRuleError("historical_style_real_analysis_required")
        existing = self._exact_confirmed_proposal(
            customer_id, request.groups, style_origin
        )
        if existing is not None:
            self._session.add(
                AuditEvent(
                    id=receipt_id,
                    customer_id=customer_id,
                    event_type="style_card.historical.confirmed",
                    payload={
                        "fingerprint": fingerprint,
                        "style_origin": style_origin,
                        "versions": [
                            {
                                "scene_id": str(snapshot.scene.id),
                                "version_id": str(snapshot.version.id),
                            }
                            for snapshot in existing
                        ],
                    },
                    occurred_at=datetime.now(UTC),
                )
            )
            return existing
        latest_versions = (
            select(
                StyleCardVersion.customer_id,
                StyleCardVersion.style_card_id,
                func.max(StyleCardVersion.version).label("version"),
            )
            .where(StyleCardVersion.customer_id == customer_id)
            .group_by(
                StyleCardVersion.customer_id,
                StyleCardVersion.style_card_id,
            )
            .subquery()
        )
        count = (
            self._session.scalar(
                select(func.count(func.distinct(SceneStyle.id)))
                .select_from(SceneStyle)
                .join(
                    StyleCardVersion,
                    and_(
                        StyleCardVersion.customer_id == SceneStyle.customer_id,
                        StyleCardVersion.style_card_id == SceneStyle.style_card_id,
                    ),
                )
                .join(
                    latest_versions,
                    and_(
                        latest_versions.c.customer_id == StyleCardVersion.customer_id,
                        latest_versions.c.style_card_id
                        == StyleCardVersion.style_card_id,
                        latest_versions.c.version == StyleCardVersion.version,
                    ),
                )
                .where(
                    SceneStyle.customer_id == customer_id,
                    SceneStyle.active.is_(True),
                    SceneStyle.style_origin == style_origin,
                )
            )
            or 0
        )
        existing_scenes = [
            self._active_scene_named(customer_id, group.name, style_origin)
            for group in request.groups
        ]
        new_scene_offset = 0
        for group, existing_scene in zip(request.groups, existing_scenes, strict=True):
            if existing_scene is not None:
                continue
            enforce_scene_limit(count + new_scene_offset)
            new_scene_offset += 1

        snapshots = []
        for group, samples, existing_scene in zip(
            request.groups, evidence_groups, existing_scenes, strict=True
        ):
            if existing_scene is None:
                scene_id = uuid4()
                card = StyleCard(
                    id=scene_id, customer_id=customer_id, kind="scene", active=True
                )
                self._session.add(card)
                self._session.flush()
                scene = SceneStyle(
                    id=scene_id,
                    customer_id=customer_id,
                    style_card_id=card.id,
                    name=group.name,
                    style_origin=style_origin,
                    active=True,
                    rules={},
                )
                self._session.add(scene)
                self._session.flush()
            else:
                scene = existing_scene
                existing_card = self._session.get(StyleCard, scene.style_card_id)
                if existing_card is None or not existing_card.active:
                    raise DomainRuleError("historical_style_evidence_unavailable")
                card = existing_card
            overrides = {
                field: value
                for field, value in group.model_dump(exclude_none=True).items()
                if field in STYLE_ANALYSIS_LIST_FIELDS
            }
            content: dict[str, Any] = {
                **{
                    field: list(overrides.get(field, []))
                    for field in STYLE_ANALYSIS_LIST_FIELDS
                },
                "name": group.name,
                "main_style_card_version_id": None,
                "historical_style": True,
                "style_origin": style_origin,
                "overrides": overrides,
                "source": "customer_reconfirmation"
                if existing_scene is not None
                else "customer_confirmation",
                "analyzer": group.analyzer_version or "customer_reviewed_proposal",
                "analysis_mode": group.analysis_mode
                or "customer_confirmed_historical_style",
                "stability": "customer_confirmed",
                "sample_count": len(samples),
                "evidence_photo_ids": [sample.photo_id for sample in samples],
                "proposal_keys": group.proposal_keys,
            }
            if group.library_sample_count is not None:
                content["library_sample_count"] = group.library_sample_count
            if group.proposal_fingerprint is not None:
                content["proposal_fingerprint"] = group.proposal_fingerprint
            if group.reference_selection_version is not None:
                content["reference_selection_version"] = (
                    group.reference_selection_version
                )
                content["reference_manifest_sha256"] = group.reference_manifest_sha256
            version = self._append_confirmed_version(
                customer_id, scene, card, content, samples, group.slots
            )
            evidence_by_id = {sample.id: sample for sample in samples}
            self._session.add_all(
                StyleFieldEvidence(
                    id=uuid4(),
                    customer_id=customer_id,
                    style_card_version_id=version.id,
                    field_name=field,
                    field_value=value,
                    style_sample_id=sample_id,
                    created_at=datetime.now(UTC),
                )
                for field, sample_ids in group.field_evidence.items()
                for value in list(overrides.get(field, []))
                for sample_id in sample_ids
                if sample_id in evidence_by_id
            )
            self._session.flush()
            snapshots.append(
                SceneStyleCardSnapshot(
                    scene,
                    card,
                    version,
                    self._resolve_scene_content({}, version.content),
                )
            )
        self._session.add(
            AuditEvent(
                id=receipt_id,
                customer_id=customer_id,
                event_type="style_card.historical.confirmed",
                payload={
                    "fingerprint": fingerprint,
                    "style_origin": style_origin,
                    "versions": [
                        {"scene_id": str(s.scene.id), "version_id": str(s.version.id)}
                        for s in snapshots
                    ],
                },
                occurred_at=datetime.now(UTC),
            )
        )
        return snapshots

    def _exact_confirmed_proposal(
        self,
        customer_id: UUID,
        groups: list[HistoricalStyleGroupConfirmRequest],
        style_origin: str,
    ) -> list[SceneStyleCardSnapshot] | None:
        """Return existing scenes only when a reviewed proposal is an exact replay."""
        if any(group.proposal_fingerprint is None for group in groups):
            return None
        candidates = [
            snapshot
            for snapshot in self.list(customer_id)
            if snapshot.scene.active
            and snapshot.scene.style_origin == style_origin
            and snapshot.version.content.get("proposal_fingerprint")
            == groups[0].proposal_fingerprint
            and snapshot.version.content.get("reference_manifest_sha256")
            == groups[0].reference_manifest_sha256
        ]
        if len(candidates) != len(groups):
            return None
        by_name = {snapshot.scene.name: snapshot for snapshot in candidates}
        if len(by_name) != len(groups):
            return None
        matched: list[SceneStyleCardSnapshot] = []
        for group in groups:
            snapshot = by_name.get(group.name)
            if snapshot is None or not self._confirmed_group_matches(group, snapshot):
                return None
            matched.append(snapshot)
        return matched

    def _confirmed_group_matches(
        self,
        group: HistoricalStyleGroupConfirmRequest,
        snapshot: SceneStyleCardSnapshot,
    ) -> bool:
        content = snapshot.version.content
        expected_metadata = {
            "analyzer": group.analyzer_version or "customer_reviewed_proposal",
            "analysis_mode": group.analysis_mode
            or "customer_confirmed_historical_style",
            "proposal_fingerprint": group.proposal_fingerprint,
            "reference_selection_version": group.reference_selection_version,
            "reference_manifest_sha256": group.reference_manifest_sha256,
            "proposal_keys": group.proposal_keys,
        }
        if any(content.get(key) != value for key, value in expected_metadata.items()):
            return False
        if any(
            list(content.get(field, [])) != list(getattr(group, field) or [])
            for field in STYLE_ANALYSIS_LIST_FIELDS
        ):
            return False
        expected_slots = [slot.model_dump(mode="json") for slot in group.slots]
        existing_slots = [
            {
                "slot_key": slot["slot_key"],
                "label": slot["label"],
                "rules": slot["rules"],
                "active": slot["active"],
            }
            for slot in content.get("slots", [])
        ]
        if existing_slots != expected_slots:
            return False
        linked_sample_ids = set(
            self._session.scalars(
                select(StyleCardSampleLink.style_sample_id).where(
                    StyleCardSampleLink.customer_id == snapshot.scene.customer_id,
                    StyleCardSampleLink.style_card_version_id == snapshot.version.id,
                )
            )
        )
        if linked_sample_ids != set(group.evidence_sample_ids):
            return False
        evidence_rows = list(
            self._session.scalars(
                select(StyleFieldEvidence).where(
                    StyleFieldEvidence.customer_id == snapshot.scene.customer_id,
                    StyleFieldEvidence.style_card_version_id == snapshot.version.id,
                )
            )
        )
        existing_field_evidence = {
            field: {
                row.style_sample_id for row in evidence_rows if row.field_name == field
            }
            for field in group.field_evidence
        }
        return existing_field_evidence == {
            field: set(sample_ids) for field, sample_ids in group.field_evidence.items()
        }

    def _append_confirmed_version(
        self,
        customer_id: UUID,
        scene: SceneStyle,
        card: StyleCard,
        content: dict[str, Any],
        samples: list[StyleSample],
        slots: list[StyleTemplateSlotRequest],
    ) -> StyleCardVersion:
        # Allocate the immutable version ID before sealing its slot snapshots.
        version_id = uuid4()
        slot_rows = [
            StyleTemplateSlot(
                id=uuid4(),
                customer_id=customer_id,
                scene_style_id=scene.id,
                style_card_version_id=version_id,
                slot_key=slot.slot_key,
                label=slot.label,
                rules=slot.rules.model_dump(mode="json"),
                active=slot.active,
                position=position,
            )
            for position, slot in enumerate(slots)
        ]
        content = {
            **content,
            "slots": [
                StyleTemplateSlotResponse.model_validate(row).model_dump(mode="json")
                for row in slot_rows
            ],
        }
        latest = self._latest_version(customer_id, card.id)
        version = StyleCardVersion(
            id=version_id,
            customer_id=customer_id,
            style_card_id=card.id,
            version=latest.version + 1 if latest else 1,
            content=content,
        )
        self._session.add(version)
        self._session.flush()
        self._link_samples(customer_id, version, samples)
        self._session.add_all(slot_rows)
        self._session.flush()
        return version

    def _edit_confirmed_scene(
        self,
        customer_id: UUID,
        scene: SceneStyle,
        card: StyleCard,
        current: StyleCardVersion,
        changes: dict[str, Any],
    ) -> SceneStyleCardSnapshot:
        validated = SceneStyleCardEditRequest.model_validate(changes)
        previous_links = list(
            self._session.scalars(
                select(StyleCardSampleLink)
                .where(
                    StyleCardSampleLink.customer_id == customer_id,
                    StyleCardSampleLink.style_card_version_id == current.id,
                )
                .order_by(StyleCardSampleLink.created_at, StyleCardSampleLink.id)
            )
        )
        samples = self._confirmed_evidence(
            customer_id, [link.style_sample_id for link in previous_links]
        )
        if not samples:
            raise DomainRuleError("historical_style_evidence_unavailable")
        content = dict(current.content)
        overrides = dict(content["overrides"])
        previous_slots = [
            StyleTemplateSlotRequest.model_validate(
                {
                    key: value
                    for key, value in slot.items()
                    if key in StyleTemplateSlotRequest.model_fields
                }
            )
            for slot in content["slots"]
        ]
        next_slots = validated.slots if validated.slots is not None else previous_slots
        for field, value in validated.model_dump(exclude_unset=True).items():
            if field in STYLE_ANALYSIS_LIST_FIELDS:
                if value is None:
                    overrides.pop(field, None)
                else:
                    overrides[field] = value
            elif field in SCENE_CHANNEL_FIELDS and value is None:
                content.pop(field, None)
            elif field != "slots":
                content[field] = value
        content["overrides"] = overrides
        if content == current.content and next_slots == previous_slots:
            raise DomainRuleError("style_card_no_changes")
        if content["name"] != scene.name and self._active_scene_name_exists(
            customer_id,
            content["name"],
            scene.style_origin,
            exclude_scene_id=scene.id,
        ):
            raise DomainRuleError("scene_name_conflict")
        scene.name = content["name"]
        content["source"] = "customer_edit"
        version = self._append_confirmed_version(
            customer_id, scene, card, content, samples, next_slots
        )
        self._record_audit(
            customer_id,
            "style_card.scene.edited",
            {
                "style_card_id": str(card.id),
                "style_card_version_id": str(version.id),
                "previous_style_card_version_id": str(current.id),
                "version": version.version,
                "changed_fields": sorted(changes),
            },
        )
        self._session.commit()
        self._session.refresh(version)
        return SceneStyleCardSnapshot(
            scene, card, version, self._resolve_scene_content({}, version.content)
        )
