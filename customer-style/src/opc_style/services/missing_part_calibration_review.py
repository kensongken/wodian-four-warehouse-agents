"""Private review store for offline missing-part calibration proposals."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
from datetime import UTC, datetime
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from typing import Literal, cast
from uuid import UUID, uuid4

from PIL import Image

from opc_style.domain.errors import DomainRuleError

CalibrationDecision = Literal[
    "confirmed_structure", "confirmed_background", "invalid_position"
]
CalibrationAssetKind = Literal["source", "mask", "overlay", "zoom"]
AgentSuggestion = Literal[
    "confirmed_structure", "confirmed_background", "invalid_position", "uncertain"
]
AgentConfidence = Literal["low", "medium", "high"]

_PROPOSAL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class MissingPartCalibrationReviewStore:
    """Import verified proposal batches and keep append-only human decisions."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve(strict=False)
        self._root.mkdir(parents=True, mode=0o700, exist_ok=True)
        self._root.chmod(0o700)
        root_stat = self._root.lstat()
        if stat.S_ISLNK(root_stat.st_mode) or root_stat.st_uid != os.getuid():
            raise DomainRuleError("missing_part_calibration_store_unsafe")
        self._manifest_path = self._root / "manifest.json"
        self._events_path = self._root / "review-events.jsonl"
        self._agent_events_path = self._root / "agent-review-events.jsonl"
        self._assets_root = self._root / "assets"

    def import_batch(self, source_root: Path) -> dict[str, object]:
        source_root = source_root.resolve(strict=True)
        manifest_path = source_root / "manifest.json"
        incoming_manifest_sha256 = _file_sha256(manifest_path)
        if self._manifest_path.exists() and (self._events() or self._agent_events()):
            current = _read_json_object(self._manifest_path)
            if current.get("source_manifest_sha256") != incoming_manifest_sha256:
                raise DomainRuleError("missing_part_calibration_batch_locked")
            return self.queue()
        payload = _read_json_object(manifest_path)
        if payload.get("schema_version") != "missing-part-calibration-batch-v1":
            raise DomainRuleError("missing_part_calibration_manifest_invalid")
        records = payload.get("records")
        if not isinstance(records, list) or not records:
            raise DomainRuleError("missing_part_calibration_manifest_invalid")
        normalized: list[dict[str, object]] = []
        seen: set[str] = set()
        staging = self._root / f".import-{uuid4()}"
        staging.mkdir(mode=0o700)
        try:
            assets = staging / "assets"
            assets.mkdir(mode=0o700)
            for value in records:
                if not isinstance(value, dict):
                    raise DomainRuleError("missing_part_calibration_manifest_invalid")
                record = cast(dict[str, object], value)
                proposal_id = _required_text(record, "proposal_id", 128)
                if not _PROPOSAL_ID.fullmatch(proposal_id) or proposal_id in seen:
                    raise DomainRuleError("missing_part_calibration_manifest_invalid")
                seen.add(proposal_id)
                proposal_dir = assets / proposal_id
                proposal_dir.mkdir(mode=0o700)
                source_path = Path(_required_text(record, "source_path", 4096))
                if not source_path.is_absolute():
                    raise DomainRuleError("missing_part_calibration_manifest_invalid")
                mask_path = _safe_child(
                    source_root, _required_text(record, "missing_mask_path", 255)
                )
                overlay_path = _safe_child(
                    source_root, _required_text(record, "overlay_path", 255)
                )
                source_sha = _required_sha(record, "source_sha256")
                mask_sha = _required_sha(record, "missing_mask_sha256")
                if _file_sha256(source_path) != source_sha:
                    raise DomainRuleError("missing_part_calibration_source_changed")
                if _file_sha256(mask_path) != mask_sha:
                    raise DomainRuleError("missing_part_calibration_source_changed")
                source_target = proposal_dir / ("source" + source_path.suffix.lower())
                mask_target = proposal_dir / "mask.png"
                overlay_target = proposal_dir / (
                    "overlay" + overlay_path.suffix.lower()
                )
                for source, target in (
                    (source_path, source_target),
                    (mask_path, mask_target),
                    (overlay_path, overlay_target),
                ):
                    shutil.copyfile(source, target)
                    target.chmod(0o600)
                normalized.append(
                    {
                        "proposal_id": proposal_id,
                        "source_name": _required_text(record, "source_name", 255),
                        "source_sha256": source_sha,
                        "missing_mask_sha256": mask_sha,
                        "overlay_sha256": _file_sha256(overlay_target),
                        "proposal_kind": _choice(
                            record, "proposal_kind", {"structure_edge", "background"}
                        ),
                        "system_state": _choice(
                            record,
                            "system_state",
                            {"anchored_to_visible_structure", "blocked_unanchored"},
                        ),
                        "category": _required_text(record, "category", 64),
                        "part_kind": _required_text(record, "part_kind", 64),
                        "context_edge_mean": _finite_number(
                            record, "context_edge_mean"
                        ),
                        "strong_edge_fraction": _finite_number(
                            record, "strong_edge_fraction"
                        ),
                        "context_chroma_mean": _finite_number(
                            record, "context_chroma_mean"
                        ),
                        "strong_chroma_fraction": _finite_number(
                            record, "strong_chroma_fraction"
                        ),
                        "context_chroma_stddev": _finite_number(
                            record, "context_chroma_stddev"
                        ),
                        "source_asset": str(source_target.relative_to(staging)),
                        "mask_asset": str(mask_target.relative_to(staging)),
                        "overlay_asset": str(overlay_target.relative_to(staging)),
                    }
                )
            imported = {
                "schema_version": "missing-part-calibration-review-v1",
                "source_manifest_sha256": incoming_manifest_sha256,
                "imported_at": datetime.now(UTC).isoformat(),
                "records": normalized,
            }
            imported_path = staging / "manifest.json"
            imported_path.write_text(
                json.dumps(imported, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            imported_path.chmod(0o600)
            if self._assets_root.exists():
                shutil.rmtree(self._assets_root)
            shutil.move(str(assets), self._assets_root)
            shutil.move(str(imported_path), self._manifest_path)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        return self.queue()

    def queue(self) -> dict[str, object]:
        records = self._records()
        latest = self._latest_reviews()
        latest_agent = self._latest_agent_reviews()
        items = []
        reviewed = 0
        eligible = 0
        invalid = 0
        priority_counts = {
            "integrity_conflict": 0,
            "critical_threshold": 0,
            "near_threshold": 0,
            "clear_signal": 0,
        }
        for record in records:
            proposal_id = cast(str, record["proposal_id"])
            review = latest.get(proposal_id)
            agent_review = latest_agent.get(proposal_id)
            decision = review.get("decision") if review else None
            if review:
                reviewed += 1
            if decision in {"confirmed_structure", "confirmed_background"}:
                eligible += 1
            if decision == "invalid_position":
                invalid += 1
            triage = calibration_review_triage(record)
            if not review:
                priority_counts[str(triage["review_priority"])] += 1
            items.append(
                {
                    **record,
                    **triage,
                    "human_review_state": "reviewed" if review else "pending",
                    "human_label": decision,
                    "training_eligible": decision
                    in {"confirmed_structure", "confirmed_background"},
                    "reviewed_at": review.get("created_at") if review else None,
                    "agent_suggestion": (
                        agent_review.get("suggestion") if agent_review else None
                    ),
                    "agent_reason": agent_review.get("reason")
                    if agent_review
                    else None,
                    "agent_confidence": (
                        agent_review.get("confidence") if agent_review else None
                    ),
                    "agent_reviewer_version": (
                        agent_review.get("reviewer_version") if agent_review else None
                    ),
                    "agent_reviewed_at": (
                        agent_review.get("created_at") if agent_review else None
                    ),
                    "agent_training_eligible": False,
                    "source_url": f"calibration/assets/{proposal_id}/source",
                    "mask_url": f"calibration/assets/{proposal_id}/mask",
                    "overlay_url": f"calibration/assets/{proposal_id}/overlay",
                    "zoom_url": f"calibration/assets/{proposal_id}/zoom",
                }
            )
        pending_items = [
            item for item in items if item["human_review_state"] == "pending"
        ]
        priority = {
            "integrity_conflict": 0,
            "critical_threshold": 1,
            "near_threshold": 2,
            "clear_signal": 3,
        }
        pending_items.sort(
            key=lambda item: (
                priority[str(item["review_priority"])],
                cast(float, item["threshold_margin"]),
                str(item["proposal_id"]),
            )
        )
        return {
            "batch_loaded": bool(records),
            "total": len(records),
            "pending": len(records) - reviewed,
            "reviewed": reviewed,
            "training_eligible": eligible,
            "invalid_position": invalid,
            "agent_reviewed": len(latest_agent),
            "provider_call_count": 0,
            "threshold_updated_automatically": False,
            "priority_counts": priority_counts,
            "items": pending_items
            + [item for item in items if item not in pending_items],
        }

    def record_review(
        self,
        *,
        proposal_id: str,
        decision: CalibrationDecision,
        reviewer_id: UUID,
        note: str,
    ) -> dict[str, object]:
        record = self.proposal(proposal_id)
        if proposal_id in self._latest_reviews():
            raise DomainRuleError("missing_part_calibration_already_reviewed")
        event = {
            "event_id": str(uuid4()),
            "proposal_id": proposal_id,
            "decision": decision,
            "reviewer_ref": sha256(str(reviewer_id).encode()).hexdigest()[:16],
            "note": note.strip()[:300],
            "source_sha256": record["source_sha256"],
            "missing_mask_sha256": record["missing_mask_sha256"],
            "previous_event_sha256": self._last_event_hash(),
            "created_at": datetime.now(UTC).isoformat(),
        }
        event["event_sha256"] = _json_sha256(event)
        fd = os.open(
            self._events_path,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW,
            0o600,
        )
        try:
            os.write(
                fd,
                (json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n").encode(),
            )
            os.fsync(fd)
        finally:
            os.close(fd)
        return {**event, "record": record}

    def record_agent_review(
        self,
        *,
        proposal_id: str,
        suggestion: AgentSuggestion,
        confidence: AgentConfidence,
        reviewer_version: str,
        reason: str,
    ) -> dict[str, object]:
        """Record a provisional model opinion without creating a human label."""

        record = self.proposal(proposal_id)
        if proposal_id in self._latest_agent_reviews():
            raise DomainRuleError("missing_part_agent_review_already_recorded")
        normalized_version = reviewer_version.strip()[:120]
        normalized_reason = reason.strip()[:500]
        if not normalized_version or not normalized_reason:
            raise DomainRuleError("missing_part_agent_review_invalid")
        event = {
            "event_id": str(uuid4()),
            "proposal_id": proposal_id,
            "suggestion": suggestion,
            "confidence": confidence,
            "reviewer_version": normalized_version,
            "reason": normalized_reason,
            "source_sha256": record["source_sha256"],
            "missing_mask_sha256": record["missing_mask_sha256"],
            "training_eligible": False,
            "provenance": "agent_visual_review",
            "previous_event_sha256": self._last_agent_event_hash(),
            "created_at": datetime.now(UTC).isoformat(),
        }
        event["event_sha256"] = _json_sha256(event)
        fd = os.open(
            self._agent_events_path,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW,
            0o600,
        )
        try:
            os.write(
                fd,
                (json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n").encode(),
            )
            os.fsync(fd)
        finally:
            os.close(fd)
        return {**event, "record": record}

    def is_reviewed(self, proposal_id: str) -> bool:
        self.proposal(proposal_id)
        return proposal_id in self._latest_reviews()

    def proposal(self, proposal_id: str) -> dict[str, object]:
        if not _PROPOSAL_ID.fullmatch(proposal_id):
            raise DomainRuleError("missing_part_calibration_proposal_not_found")
        for record in self._records():
            if record["proposal_id"] == proposal_id:
                return record
        raise DomainRuleError("missing_part_calibration_proposal_not_found")

    def asset(self, proposal_id: str, kind: CalibrationAssetKind) -> tuple[bytes, str]:
        record = self.proposal(proposal_id)
        if kind == "zoom":
            return self._zoom_asset(record), "image/jpeg"
        rel = cast(str, record[f"{kind}_asset"])
        path = _safe_child(self._root, rel)
        mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
        return path.read_bytes(), mime

    def _zoom_asset(self, record: dict[str, object]) -> bytes:
        source_path = _safe_child(self._root, cast(str, record["source_asset"]))
        mask_path = _safe_child(self._root, cast(str, record["mask_asset"]))
        with Image.open(source_path) as opened:
            source = opened.convert("RGB")
        with Image.open(mask_path) as opened:
            mask = opened.convert("L")
        bbox = mask.getbbox()
        if bbox is None or mask.size != source.size:
            raise DomainRuleError("missing_part_calibration_manifest_invalid")
        red_layer = Image.new("RGB", source.size, (220, 45, 45))
        tinted = Image.blend(source, red_layer, 0.58)
        overlay = Image.composite(tinted, source, mask)
        left, top, right, bottom = bbox
        region_size = max(right - left, bottom - top)
        padding = max(48, region_size * 3)
        crop_box = (
            max(0, left - padding),
            max(0, top - padding),
            min(overlay.width, right + padding),
            min(overlay.height, bottom + padding),
        )
        cropped = overlay.crop(crop_box)
        maximum_side = 1200
        if max(cropped.size) > maximum_side:
            cropped.thumbnail((maximum_side, maximum_side), Image.Resampling.LANCZOS)
        output = BytesIO()
        cropped.save(output, format="JPEG", quality=94, optimize=False)
        return output.getvalue()

    def _records(self) -> list[dict[str, object]]:
        if not self._manifest_path.exists():
            return []
        payload = _read_json_object(self._manifest_path)
        records = payload.get("records")
        if not isinstance(records, list):
            raise DomainRuleError("missing_part_calibration_manifest_invalid")
        return [cast(dict[str, object], record) for record in records]

    def _events(self) -> list[dict[str, object]]:
        if not self._events_path.exists():
            return []
        events: list[dict[str, object]] = []
        previous: str | None = None
        for line in self._events_path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError as error:
                raise DomainRuleError(
                    "missing_part_calibration_chain_invalid"
                ) from error
            if (
                not isinstance(event, dict)
                or event.get("previous_event_sha256") != previous
            ):
                raise DomainRuleError("missing_part_calibration_chain_invalid")
            expected = event.get("event_sha256")
            body = {key: value for key, value in event.items() if key != "event_sha256"}
            if expected != _json_sha256(body):
                raise DomainRuleError("missing_part_calibration_chain_invalid")
            previous = cast(str, expected)
            events.append(cast(dict[str, object], event))
        return events

    def _latest_reviews(self) -> dict[str, dict[str, object]]:
        return {cast(str, event["proposal_id"]): event for event in self._events()}

    def _agent_events(self) -> list[dict[str, object]]:
        if not self._agent_events_path.exists():
            return []
        events: list[dict[str, object]] = []
        previous: str | None = None
        for line in self._agent_events_path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError as error:
                raise DomainRuleError(
                    "missing_part_agent_review_chain_invalid"
                ) from error
            if (
                not isinstance(event, dict)
                or event.get("previous_event_sha256") != previous
            ):
                raise DomainRuleError("missing_part_agent_review_chain_invalid")
            expected = event.get("event_sha256")
            body = {key: value for key, value in event.items() if key != "event_sha256"}
            if (
                expected != _json_sha256(body)
                or event.get("training_eligible") is not False
            ):
                raise DomainRuleError("missing_part_agent_review_chain_invalid")
            previous = cast(str, expected)
            events.append(cast(dict[str, object], event))
        return events

    def _latest_agent_reviews(self) -> dict[str, dict[str, object]]:
        return {
            cast(str, event["proposal_id"]): event for event in self._agent_events()
        }

    def _last_event_hash(self) -> str | None:
        events = self._events()
        return cast(str, events[-1]["event_sha256"]) if events else None

    def _last_agent_event_hash(self) -> str | None:
        events = self._agent_events()
        return cast(str, events[-1]["event_sha256"]) if events else None


def _read_json_object(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DomainRuleError("missing_part_calibration_manifest_invalid") from error
    if not isinstance(payload, dict):
        raise DomainRuleError("missing_part_calibration_manifest_invalid")
    return cast(dict[str, object], payload)


def _safe_child(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve(strict=True)
    if candidate == root or root not in candidate.parents or not candidate.is_file():
        raise DomainRuleError("missing_part_calibration_manifest_invalid")
    return candidate


def _required_text(record: dict[str, object], key: str, limit: int) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise DomainRuleError("missing_part_calibration_manifest_invalid")
    return value.strip()


def _required_sha(record: dict[str, object], key: str) -> str:
    value = _required_text(record, key, 64)
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise DomainRuleError("missing_part_calibration_manifest_invalid")
    return value


def _choice(record: dict[str, object], key: str, choices: set[str]) -> str:
    value = _required_text(record, key, 64)
    if value not in choices:
        raise DomainRuleError("missing_part_calibration_manifest_invalid")
    return value


def _finite_number(record: dict[str, object], key: str) -> float:
    value = record.get(key)
    if not isinstance(value, (int, float)) or not float("-inf") < float(value) < float(
        "inf"
    ):
        raise DomainRuleError("missing_part_calibration_manifest_invalid")
    return float(value)


def _file_sha256(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _json_sha256(payload: dict[str, object]) -> str:
    return sha256(
        json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def calibration_review_triage(record: dict[str, object]) -> dict[str, object]:
    """Prioritize ambiguous evidence without turning it into a human label."""

    edge_score = min(
        cast(float, record["context_edge_mean"]) / 8.0,
        cast(float, record["strong_edge_fraction"]) / 0.04,
    )
    chroma_score = min(
        cast(float, record["context_chroma_stddev"]) / 8.0,
        cast(float, record["context_chroma_mean"]) / 20.0,
        cast(float, record["strong_chroma_fraction"]) / 0.10,
    )
    evidence_score = max(edge_score, chroma_score)
    margin = abs(evidence_score - 1.0)
    system_state = str(record["system_state"])
    proposal_kind = str(record["proposal_kind"])
    expected_state = (
        "anchored_to_visible_structure"
        if proposal_kind == "structure_edge"
        else "blocked_unanchored"
    )
    if system_state != expected_state:
        review_priority = "integrity_conflict"
    elif margin < 0.1:
        review_priority = "critical_threshold"
    elif margin < 0.25:
        review_priority = "near_threshold"
    else:
        review_priority = "clear_signal"
    suggestion = (
        "confirmed_structure"
        if system_state == "anchored_to_visible_structure"
        else "confirmed_background"
    )
    return {
        "evidence_score": round(evidence_score, 4),
        "threshold_margin": round(margin, 4),
        "review_priority": review_priority,
        "provisional_suggestion": suggestion,
        "suggestion_provenance": "deterministic_preflight_v1",
        "suggestion_training_eligible": False,
    }
