"""Append-only human feedback memory for missing-part completion."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from hashlib import sha256
from math import isfinite
from pathlib import Path
from typing import Literal, cast
from uuid import uuid4

from opc_style.domain.errors import DomainRuleError

Decision = Literal["accepted", "edited", "rejected"]
PreflightHumanLabel = Literal["confirmed_structure", "confirmed_background"]
PreflightOutcome = Literal["agreed", "false_pass", "false_block"]


@dataclass(frozen=True)
class CompletionFeedback:
    event_id: str
    case_id: str
    request_fingerprint: str
    category: str
    part_kind: str
    material_tags: tuple[str, ...]
    decision: Decision
    candidate_sha256: str
    final_sha256: str | None
    model_version: str
    note: str
    created_at: str

    @classmethod
    def create(
        cls,
        *,
        case_id: str,
        request_fingerprint: str,
        category: str,
        part_kind: str,
        material_tags: tuple[str, ...],
        decision: Decision,
        candidate_sha256: str,
        final_sha256: str | None,
        model_version: str,
        note: str,
    ) -> CompletionFeedback:
        return cls(
            event_id=str(uuid4()),
            case_id=case_id,
            request_fingerprint=request_fingerprint,
            category=category,
            part_kind=part_kind,
            material_tags=tuple(sorted(set(material_tags))),
            decision=decision,
            candidate_sha256=candidate_sha256,
            final_sha256=final_sha256,
            model_version=model_version,
            note=note.strip()[:300],
            created_at=datetime.now(UTC).isoformat(),
        )


@dataclass(frozen=True)
class PreflightFeedback:
    event_id: str
    case_id: str
    request_fingerprint: str
    category: str
    part_kind: str
    material_tags: tuple[str, ...]
    system_state: str
    human_label: PreflightHumanLabel
    context_edge_mean: float
    strong_edge_fraction: float
    context_chroma_mean: float
    strong_chroma_fraction: float
    context_chroma_stddev: float
    note: str
    created_at: str

    @classmethod
    def create(
        cls,
        *,
        case_id: str,
        request_fingerprint: str,
        category: str,
        part_kind: str,
        material_tags: tuple[str, ...],
        system_state: str,
        human_label: PreflightHumanLabel,
        context_edge_mean: float,
        strong_edge_fraction: float,
        context_chroma_mean: float,
        strong_chroma_fraction: float,
        context_chroma_stddev: float,
        note: str,
    ) -> PreflightFeedback:
        return cls(
            event_id=str(uuid4()),
            case_id=case_id.strip()[:128],
            request_fingerprint=request_fingerprint,
            category=category.strip()[:64],
            part_kind=part_kind.strip()[:64],
            material_tags=tuple(sorted(set(material_tags))),
            system_state=system_state,
            human_label=human_label,
            context_edge_mean=context_edge_mean,
            strong_edge_fraction=strong_edge_fraction,
            context_chroma_mean=context_chroma_mean,
            strong_chroma_fraction=strong_chroma_fraction,
            context_chroma_stddev=context_chroma_stddev,
            note=note.strip()[:300],
            created_at=datetime.now(UTC).isoformat(),
        )


class MissingPartLearningStore:
    """Durable case memory; only latest accepted/edited cases teach future prompts."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve(strict=False)
        self._root.mkdir(parents=True, mode=0o700, exist_ok=True)
        self._root.chmod(0o700)
        root_stat = self._root.lstat()
        if stat.S_ISLNK(root_stat.st_mode) or root_stat.st_uid != os.getuid():
            raise DomainRuleError("missing_part_learning_store_unsafe")
        self._events_path = self._root / "events.jsonl"
        self._events = self._load()
        self._preflight_events_path = self._root / "preflight-events.jsonl"
        self._preflight_events = self._load_path(self._preflight_events_path)

    @property
    def event_count(self) -> int:
        return len(self._events)

    @property
    def preflight_event_count(self) -> int:
        return len(self._preflight_events)

    def append(self, feedback: CompletionFeedback) -> None:
        _validate_feedback(feedback)
        previous_hash = self._events[-1]["event_sha256"] if self._events else None
        payload = _feedback_payload(feedback)
        record_without_hash = {**payload, "previous_event_sha256": previous_hash}
        event_hash = _record_sha256(record_without_hash)
        record = {**record_without_hash, "event_sha256": event_hash}
        fd = os.open(
            self._events_path,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW,
            0o600,
        )
        try:
            os.write(
                fd,
                (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode(
                    "utf-8"
                ),
            )
            os.fsync(fd)
        finally:
            os.close(fd)
        self._events.append(record)

    def append_preflight(self, feedback: PreflightFeedback) -> None:
        _validate_preflight_feedback(feedback)
        previous_hash = (
            self._preflight_events[-1]["event_sha256"]
            if self._preflight_events
            else None
        )
        payload = asdict(feedback)
        payload["material_tags"] = list(feedback.material_tags)
        record_without_hash = {**payload, "previous_event_sha256": previous_hash}
        event_hash = _record_sha256(record_without_hash)
        record = {**record_without_hash, "event_sha256": event_hash}
        fd = os.open(
            self._preflight_events_path,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW,
            0o600,
        )
        try:
            os.write(
                fd,
                (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode(
                    "utf-8"
                ),
            )
            os.fsync(fd)
        finally:
            os.close(fd)
        self._preflight_events.append(record)

    def preflight_summary(self) -> dict[str, int]:
        latest = self._latest_preflight_records()
        summary = {
            "total": len(latest),
            "agreed": 0,
            "false_pass": 0,
            "false_block": 0,
            "confirmed_structure": 0,
            "confirmed_background": 0,
        }
        for record in latest:
            state = record["system_state"]
            label = record["human_label"]
            if label == "confirmed_structure":
                summary["confirmed_structure"] += 1
                if state == "anchored_to_visible_structure":
                    summary["agreed"] += 1
                else:
                    summary["false_block"] += 1
            elif label == "confirmed_background":
                summary["confirmed_background"] += 1
                if state == "blocked_unanchored":
                    summary["agreed"] += 1
                else:
                    summary["false_pass"] += 1
            else:
                raise DomainRuleError("missing_part_learning_chain_invalid")
        return summary

    def preflight_review_queue(
        self,
        *,
        category: str = "",
        part_kind: str = "",
        outcome: str = "all",
        limit: int = 100,
        target_per_label: int = 20,
    ) -> dict[str, object]:
        if outcome not in {"all", "agreed", "false_pass", "false_block"}:
            raise DomainRuleError("missing_part_review_filter_invalid")
        latest = self._latest_preflight_records()
        readiness_records = [
            record
            for record in latest
            if (not category or record["category"] == category)
            and (not part_kind or record["part_kind"] == part_kind)
        ]
        groups: dict[tuple[str, str], dict[str, object]] = {}
        for record in readiness_records:
            _preflight_outcome(record)
            key = (str(record["category"]), str(record["part_kind"]))
            group = groups.setdefault(
                key,
                {
                    "category": key[0],
                    "part_kind": key[1],
                    "confirmed_structure": 0,
                    "confirmed_background": 0,
                },
            )
            label = str(record["human_label"])
            group[label] = cast(int, group[label]) + 1
        readiness = []
        for group in groups.values():
            structure = cast(int, group["confirmed_structure"])
            background = cast(int, group["confirmed_background"])
            readiness.append(
                {
                    **group,
                    "structure_remaining": max(0, target_per_label - structure),
                    "background_remaining": max(0, target_per_label - background),
                    "ready_for_offline_comparison": (
                        structure >= target_per_label and background >= target_per_label
                    ),
                }
            )
        readiness.sort(key=lambda item: (str(item["category"]), str(item["part_kind"])))

        queued = []
        for record in latest:
            record_outcome = _preflight_outcome(record)
            if category and record["category"] != category:
                continue
            if part_kind and record["part_kind"] != part_kind:
                continue
            if outcome != "all" and record_outcome != outcome:
                continue
            queued.append((record_outcome, record))
        priority = {"false_pass": 0, "false_block": 1, "agreed": 2}
        queued.sort(key=lambda item: str(item[1]["created_at"]), reverse=True)
        queued.sort(key=lambda item: priority[item[0]])
        items = [
            {
                "case_ref": sha256(str(record["case_id"]).encode()).hexdigest()[:12],
                "category": record["category"],
                "part_kind": record["part_kind"],
                "system_state": record["system_state"],
                "human_label": record["human_label"],
                "outcome": record_outcome,
                "context_edge_mean": record["context_edge_mean"],
                "strong_edge_fraction": record["strong_edge_fraction"],
                "context_chroma_mean": record["context_chroma_mean"],
                "strong_chroma_fraction": record["strong_chroma_fraction"],
                "context_chroma_stddev": record["context_chroma_stddev"],
                "note": record["note"],
                "created_at": record["created_at"],
            }
            for record_outcome, record in queued[: max(1, min(limit, 200))]
        ]
        return {
            "total_cases": len(latest),
            "matching_cases": len(queued),
            "target_per_label": target_per_label,
            "groups": readiness,
            "items": items,
            "source_preview_available": False,
            "threshold_updated_automatically": False,
            "paid_override_enabled": False,
        }

    def _latest_preflight_records(self) -> list[dict[str, object]]:
        latest: dict[str, dict[str, object]] = {}
        for record in self._preflight_events:
            latest[str(record["case_id"])] = record
        return list(latest.values())

    def similar_lessons(
        self,
        category: str,
        part_kind: str,
        material_tags: tuple[str, ...],
        *,
        limit: int = 5,
    ) -> tuple[str, ...]:
        latest: dict[str, dict[str, object]] = {}
        for record in self._events:
            latest[str(record["case_id"])] = record
        requested_materials = set(material_tags)
        candidates: list[tuple[int, str, dict[str, object]]] = []
        for record in latest.values():
            if record["decision"] not in {"accepted", "edited"}:
                continue
            record_materials = record.get("material_tags")
            if not isinstance(record_materials, list) or any(
                not isinstance(value, str) for value in record_materials
            ):
                raise DomainRuleError("missing_part_learning_chain_invalid")
            score = 0
            if record["part_kind"] == part_kind:
                score += 4
            if record["category"] == category:
                score += 2
            score += len(requested_materials & set(record_materials))
            if score >= 4:
                candidates.append((score, str(record["created_at"]), record))
        candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return tuple(
            (
                f"human-{record['decision']} {record['category']}/"
                f"{record['part_kind']}: {record['note']}"
            )
            for _score, _created_at, record in candidates[:limit]
        )

    def _load(self) -> list[dict[str, object]]:
        return self._load_path(self._events_path)

    def _load_path(self, path: Path) -> list[dict[str, object]]:
        if not path.exists():
            return []
        if path.is_symlink():
            raise DomainRuleError("missing_part_learning_store_unsafe")
        events: list[dict[str, object]] = []
        previous_hash: str | None = None
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                record = json.loads(line)
                event_hash = record.pop("event_sha256")
                if (
                    record.get("previous_event_sha256") != previous_hash
                    or _record_sha256(record) != event_hash
                ):
                    raise DomainRuleError("missing_part_learning_chain_invalid")
                record["event_sha256"] = event_hash
                events.append(record)
                previous_hash = event_hash
        except DomainRuleError:
            raise
        except (OSError, ValueError, TypeError, KeyError):
            raise DomainRuleError("missing_part_learning_chain_invalid") from None
        return events


def _feedback_payload(feedback: CompletionFeedback) -> dict[str, object]:
    payload = asdict(feedback)
    payload["material_tags"] = list(feedback.material_tags)
    return payload


def _record_sha256(record: dict[str, object]) -> str:
    canonical = json.dumps(
        record, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return sha256(canonical).hexdigest()


def _preflight_outcome(record: dict[str, object]) -> PreflightOutcome:
    state = record.get("system_state")
    label = record.get("human_label")
    if label == "confirmed_structure":
        return "agreed" if state == "anchored_to_visible_structure" else "false_block"
    if label == "confirmed_background":
        return "agreed" if state == "blocked_unanchored" else "false_pass"
    raise DomainRuleError("missing_part_learning_chain_invalid")


def _validate_feedback(feedback: CompletionFeedback) -> None:
    digests = [
        feedback.request_fingerprint,
        feedback.candidate_sha256,
        *([feedback.final_sha256] if feedback.final_sha256 is not None else []),
    ]
    if any(
        len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
        for value in digests
    ):
        raise DomainRuleError("missing_part_feedback_invalid")
    if feedback.decision in {"accepted", "edited"} and feedback.final_sha256 is None:
        raise DomainRuleError("missing_part_feedback_invalid")
    if not feedback.case_id or not feedback.model_version:
        raise DomainRuleError("missing_part_feedback_invalid")


def _validate_preflight_feedback(feedback: PreflightFeedback) -> None:
    if (
        len(feedback.request_fingerprint) != 64
        or any(
            character not in "0123456789abcdef"
            for character in feedback.request_fingerprint
        )
        or not feedback.case_id
        or feedback.system_state
        not in {"anchored_to_visible_structure", "blocked_unanchored"}
        or feedback.human_label not in {"confirmed_structure", "confirmed_background"}
    ):
        raise DomainRuleError("missing_part_feedback_invalid")
    metrics = (
        feedback.context_edge_mean,
        feedback.strong_edge_fraction,
        feedback.context_chroma_mean,
        feedback.strong_chroma_fraction,
        feedback.context_chroma_stddev,
    )
    if any(not isfinite(value) or value < 0 for value in metrics):
        raise DomainRuleError("missing_part_feedback_invalid")
