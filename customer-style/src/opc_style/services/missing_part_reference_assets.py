"""Read-only registry for already-searched same-product reference candidates."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from PIL import Image, UnidentifiedImageError

from opc_style.domain.errors import DomainRuleError
from opc_style.domain.missing_part_completion import (
    MIN_MULTIVIEW_DHASH_DISTANCE,
    reference_view_difference_hash,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
REFERENCE_AUDIT_ROOT_ENV = "OPC_MISSING_PART_REFERENCE_AUDIT_ROOT"
DEFAULT_REFERENCE_AUDIT_ROOT = (
    PROJECT_ROOT / "交付文档" / "第一百四十四轮全库同款参考检索_20261025"
)
MAX_SUGGESTED_VIEWS = 8
MAX_REFERENCE_INBOX_BYTES = 20 * 1024 * 1024


def reference_audit_root() -> Path:
    """Resolve the active evidence bundle without trusting a relative path."""

    configured = os.getenv(REFERENCE_AUDIT_ROOT_ENV, "").strip()
    root = Path(configured) if configured else DEFAULT_REFERENCE_AUDIT_ROOT
    if not root.is_absolute() or root.is_symlink():
        raise DomainRuleError("missing_part_reference_audit_root_unsafe")
    return root.resolve(strict=False)


def _audit_path(filename: str) -> Path:
    return reference_audit_root() / filename


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError("audit payload must be an object")
    return value


def _mapping_items(payload: dict[str, object], key: str) -> tuple[dict[str, Any], ...]:
    value = payload.get(key, [])
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, dict))


def reference_pool_count() -> int:
    """Return the evidence count declared by the active audit bundle."""

    try:
        payload = _read_json(_audit_path("全库检索技术结论.json"))
        count = int(payload.get("reference_pool_count", 0))
    except (DomainRuleError, OSError, json.JSONDecodeError, TypeError, ValueError):
        return 0
    return max(count, 0)


def _admitted_external_reference_paths() -> frozenset[Path]:
    """Allow only exact files named by the audit, not a whole user folder."""

    try:
        payload = _read_json(_audit_path("全库检索技术结论.json"))
    except (DomainRuleError, OSError, json.JSONDecodeError, TypeError):
        return frozenset()
    admitted: set[Path] = set()
    for item in payload.get("confirmed_exact_references", []):
        if not isinstance(item, dict):
            continue
        raw = str(item.get("reference_path", ""))
        if not raw:
            continue
        try:
            admitted.add(Path(raw).resolve(strict=True))
        except OSError:
            continue
    expanded = _expanded_search_payload()
    priority_ids = {
        str(value) for value in expanded.get("priority_human_review_target_ids", [])
    }
    for item in _mapping_items(expanded, "targets"):
        if str(item.get("target_id", "")) not in priority_ids:
            continue
        raw = str(item.get("best_candidate_path", ""))
        if not raw:
            continue
        try:
            admitted.add(Path(raw).resolve(strict=True))
        except OSError:
            continue
    return frozenset(admitted)


@dataclass(frozen=True)
class LocalReferenceAsset:
    asset_id: str
    target_id: str
    target_sha256: str
    path: Path
    content_sha256: str
    mime_type: str
    source_kind: str
    structure_evidence_state: str
    eligible_for_missing_part_completion: bool


@dataclass(frozen=True)
class PriorityReferenceCandidate:
    target_id: str
    target_sha256: str
    source_filename: str
    reference_path: Path
    reference_sha256: str
    reference_filename: str
    mime_type: str
    machine_visual_triage: str
    feature_distance: float


@dataclass(frozen=True)
class _PreparedInboxReference:
    target_id: str
    digest: str
    suffix: str
    content: bytes
    record: dict[str, object]


class MissingPartReferenceInboxStore:
    """Keep newly supplied references private and pending visual review."""

    def __init__(self, root: Path) -> None:
        if not root.is_absolute() or root.is_symlink():
            raise DomainRuleError("missing_part_reference_inbox_unsafe")
        root.mkdir(parents=True, mode=0o700, exist_ok=True)
        root.chmod(0o700)
        root_stat = root.lstat()
        if (
            not stat.S_ISDIR(root_stat.st_mode)
            or root_stat.st_uid != os.getuid()
            or stat.S_IMODE(root_stat.st_mode) & 0o077
        ):
            raise DomainRuleError("missing_part_reference_inbox_unsafe")
        self._root = root.resolve(strict=True)

    def submit(
        self,
        *,
        target_id: str,
        original_filename: str,
        content: bytes,
        same_product_confirmed: bool,
        structure_visible_confirmed: bool,
    ) -> dict[str, object]:
        prepared = self._prepare_submission(
            target_id=target_id,
            original_filename=original_filename,
            content=content,
            same_product_confirmed=same_product_confirmed,
            structure_visible_confirmed=structure_visible_confirmed,
        )
        return self._persist_atomically((prepared,))[0]

    def submit_batch(
        self,
        *,
        entries: tuple[tuple[str, str, bytes], ...],
        same_product_confirmed: bool,
        structure_visible_confirmed: bool,
    ) -> tuple[dict[str, object], ...]:
        """Validate the whole batch before creating any inbox file."""

        if not entries:
            raise DomainRuleError("missing_part_reference_batch_invalid")
        prepared = tuple(
            self._prepare_submission(
                target_id=target_id,
                original_filename=original_filename,
                content=content,
                same_product_confirmed=same_product_confirmed,
                structure_visible_confirmed=structure_visible_confirmed,
            )
            for target_id, original_filename, content in entries
        )
        if len({item.digest for item in prepared}) != len(prepared):
            raise DomainRuleError("missing_part_reference_batch_duplicate")
        return self._persist_atomically(prepared)

    def admit_reviewed_candidate(
        self,
        *,
        target_id: str,
        original_filename: str,
        content: bytes,
        note: str,
    ) -> dict[str, object]:
        """Save one side-by-side human-reviewed local candidate in one write."""

        prepared = self._prepare_submission(
            target_id=target_id,
            original_filename=original_filename,
            content=content,
            same_product_confirmed=True,
            structure_visible_confirmed=True,
        )
        prepared.record["review_state"] = "approved_for_completion_input"
        prepared.record["review_note"] = note.strip()[:1000]
        prepared.record["reviewed_at"] = datetime.now(UTC).isoformat()
        prepared.record["review_method"] = "priority_candidate_side_by_side"
        record = self._persist_atomically((prepared,))[0]
        if record.get("review_state") != "approved_for_completion_input":
            raise DomainRuleError("missing_part_reference_review_locked")
        return record

    def reject_reviewed_candidate(
        self,
        *,
        target_id: str,
        original_filename: str,
        content: bytes,
        note: str,
    ) -> dict[str, object]:
        """Persist one human-rejected priority candidate so it does not resurface."""

        prepared = self._prepare_submission(
            target_id=target_id,
            original_filename=original_filename,
            content=content,
            same_product_confirmed=True,
            structure_visible_confirmed=True,
        )
        prepared.record["same_product_confirmed"] = False
        prepared.record["structure_visible_confirmed"] = False
        prepared.record["review_state"] = "rejected_reference"
        prepared.record["review_note"] = note.strip()[:1000]
        prepared.record["reviewed_at"] = datetime.now(UTC).isoformat()
        prepared.record["review_method"] = "priority_candidate_side_by_side"
        record = self._persist_atomically((prepared,))[0]
        if record.get("review_state") != "rejected_reference":
            raise DomainRuleError("missing_part_reference_review_locked")
        return record

    def _prepare_submission(
        self,
        *,
        target_id: str,
        original_filename: str,
        content: bytes,
        same_product_confirmed: bool,
        structure_visible_confirmed: bool,
    ) -> _PreparedInboxReference:
        target = _gap_target(target_id)
        if target is None:
            raise DomainRuleError("missing_part_reference_target_invalid")
        if not same_product_confirmed or not structure_visible_confirmed:
            raise DomainRuleError("missing_part_reference_confirmation_required")
        if not content or len(content) > MAX_REFERENCE_INBOX_BYTES:
            raise DomainRuleError("missing_part_reference_invalid")
        digest = sha256(content).hexdigest()
        if digest == target["target_sha256"]:
            raise DomainRuleError("missing_part_reference_evidence_digest_mismatch")
        try:
            with Image.open(BytesIO(content)) as opened:
                opened.load()
                image_format = opened.format
                width, height = opened.size
        except (
            Image.DecompressionBombError,
            UnidentifiedImageError,
            OSError,
            ValueError,
        ):
            raise DomainRuleError("missing_part_reference_invalid") from None
        if image_format not in {"JPEG", "PNG"} or width < 16 or height < 16:
            raise DomainRuleError("missing_part_reference_invalid")
        suffix = ".jpg" if image_format == "JPEG" else ".png"
        record: dict[str, object] = {
            "target_id": target_id,
            "target_sha256": target["target_sha256"],
            "source_filename": target["source_filename"],
            "reference_sha256": digest,
            "reference_filename": Path(original_filename).name[:255],
            "mime_type": "image/jpeg" if image_format == "JPEG" else "image/png",
            "width": width,
            "height": height,
            "same_product_confirmed": True,
            "structure_visible_confirmed": True,
            "review_state": "pending_human_visual_review",
            "automatic_generation_allowed": False,
            "provider_calls": 0,
            "created_at": datetime.now(UTC).isoformat(),
        }
        return _PreparedInboxReference(
            target_id=target_id,
            digest=digest,
            suffix=suffix,
            content=content,
            record=record,
        )

    def _persist_atomically(
        self, prepared: tuple[_PreparedInboxReference, ...]
    ) -> tuple[dict[str, object], ...]:
        created_paths: list[Path] = []
        records: list[dict[str, object]] = []
        try:
            for item in prepared:
                target_dir = self._root / item.target_id
                target_dir.mkdir(mode=0o700, exist_ok=True)
                target_dir.chmod(0o700)
                image_path = target_dir / f"{item.digest}{item.suffix}"
                metadata_path = target_dir / f"{item.digest}.json"
                if not image_path.exists():
                    self._write_exclusive(image_path, item.content)
                    created_paths.append(image_path)
                if not metadata_path.exists():
                    self._write_exclusive(
                        metadata_path,
                        json.dumps(
                            item.record, ensure_ascii=False, sort_keys=True
                        ).encode(),
                    )
                    created_paths.append(metadata_path)
                    records.append(item.record)
                    continue
                try:
                    existing = json.loads(metadata_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError, TypeError):
                    raise DomainRuleError(
                        "missing_part_reference_inbox_unsafe"
                    ) from None
                if not isinstance(existing, dict):
                    raise DomainRuleError("missing_part_reference_inbox_unsafe")
                records.append(existing)
        except Exception:
            for path in reversed(created_paths):
                try:
                    path.unlink()
                except OSError:
                    pass
            raise
        return tuple(records)

    @staticmethod
    def _write_exclusive(path: Path, content: bytes) -> None:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        try:
            os.write(descriptor, content)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def summary(self) -> dict[str, object]:
        records: list[dict[str, object]] = []
        for path in sorted(self._root.glob("*/*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, TypeError):
                continue
            if isinstance(record, dict):
                records.append(record)
        return {
            "state": "pending_human_visual_review" if records else "empty",
            "reference_count": len(records),
            "target_count": len({str(item.get("target_id", "")) for item in records}),
            "pending_count": sum(
                item.get("review_state") == "pending_human_visual_review"
                for item in records
            ),
            "approved_count": sum(
                item.get("review_state") == "approved_for_completion_input"
                for item in records
            ),
            "rejected_count": sum(
                item.get("review_state") == "rejected_reference" for item in records
            ),
            "records": records,
            "automatic_generation_allowed": False,
            "provider_calls": 0,
        }

    def source_content(self, target_id: str) -> tuple[bytes, str]:
        return get_gap_target_source(target_id)

    def reference_content(
        self, target_id: str, reference_sha256: str
    ) -> tuple[bytes, str]:
        record, target_dir = self._record(target_id, reference_sha256)
        suffix = ".jpg" if record.get("mime_type") == "image/jpeg" else ".png"
        path = target_dir / f"{reference_sha256}{suffix}"
        try:
            content = path.read_bytes()
        except OSError:
            raise DomainRuleError("missing_part_reference_asset_unavailable") from None
        if sha256(content).hexdigest() != reference_sha256:
            raise DomainRuleError("missing_part_reference_asset_changed")
        return content, "image/jpeg" if suffix == ".jpg" else "image/png"

    def decide(
        self,
        *,
        target_id: str,
        reference_sha256: str,
        decision: Literal["approved", "rejected"],
        note: str = "",
    ) -> dict[str, object]:
        record, target_dir = self._record(target_id, reference_sha256)
        if record.get("review_state") != "pending_human_visual_review":
            raise DomainRuleError("missing_part_reference_review_locked")
        record["review_state"] = (
            "approved_for_completion_input"
            if decision == "approved"
            else "rejected_reference"
        )
        record["review_note"] = note.strip()[:1000]
        record["reviewed_at"] = datetime.now(UTC).isoformat()
        record["automatic_generation_allowed"] = False
        record["provider_calls"] = 0
        metadata_path = target_dir / f"{reference_sha256}.json"
        temporary = target_dir / f".{reference_sha256}.json.tmp"
        temporary.write_text(
            json.dumps(record, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )
        temporary.chmod(0o600)
        os.replace(temporary, metadata_path)
        return record

    def decide_batch(
        self,
        decisions: tuple[tuple[str, str, Literal["approved", "rejected"], str], ...],
    ) -> tuple[dict[str, object], ...]:
        """Validate all review decisions, then replace their metadata as one batch."""

        if not decisions or len(decisions) > 50:
            raise DomainRuleError("missing_part_reference_batch_review_invalid")
        seen: set[tuple[str, str]] = set()
        prepared: list[tuple[Path, bytes, dict[str, object]]] = []
        for target_id, reference_sha256, decision, note in decisions:
            key = (target_id, reference_sha256)
            if key in seen or decision not in {"approved", "rejected"}:
                raise DomainRuleError("missing_part_reference_batch_review_invalid")
            seen.add(key)
            record, target_dir = self._record(target_id, reference_sha256)
            if record.get("review_state") != "pending_human_visual_review":
                raise DomainRuleError("missing_part_reference_review_locked")
            updated = dict(record)
            updated["review_state"] = (
                "approved_for_completion_input"
                if decision == "approved"
                else "rejected_reference"
            )
            updated["review_note"] = note.strip()[:1000]
            updated["reviewed_at"] = datetime.now(UTC).isoformat()
            updated["automatic_generation_allowed"] = False
            updated["provider_calls"] = 0
            metadata_path = target_dir / f"{reference_sha256}.json"
            prepared.append(
                (
                    metadata_path,
                    metadata_path.read_bytes(),
                    updated,
                )
            )
        token = uuid4().hex
        temporary_paths: list[tuple[Path, Path]] = []
        replaced: list[tuple[Path, bytes]] = []
        try:
            for metadata_path, _, updated in prepared:
                temporary = metadata_path.with_name(
                    f".{metadata_path.stem}.{token}.json.tmp"
                )
                self._write_exclusive(
                    temporary,
                    json.dumps(updated, ensure_ascii=False, sort_keys=True).encode(),
                )
                temporary_paths.append((temporary, metadata_path))
            originals = {path: original for path, original, _ in prepared}
            for temporary, metadata_path in temporary_paths:
                os.replace(temporary, metadata_path)
                replaced.append((metadata_path, originals[metadata_path]))
        except Exception:
            for temporary, _ in temporary_paths:
                try:
                    temporary.unlink()
                except OSError:
                    pass
            for metadata_path, original in reversed(replaced):
                recovery = metadata_path.with_name(
                    f".{metadata_path.stem}.{token}.recovery.tmp"
                )
                try:
                    self._write_exclusive(recovery, original)
                    os.replace(recovery, metadata_path)
                except OSError:
                    pass
            raise
        return tuple(updated for _, _, updated in prepared)

    def approved_assets(self, source_sha256: str) -> tuple[dict[str, object], ...]:
        approved: list[dict[str, object]] = []
        for record in _mapping_items(self.summary(), "records"):
            if (
                record.get("target_sha256") != source_sha256
                or record.get("review_state") != "approved_for_completion_input"
            ):
                continue
            target_id = str(record.get("target_id", ""))
            reference_sha256 = str(record.get("reference_sha256", ""))
            approved.append(
                {
                    **record,
                    "asset_id": f"inbox-{target_id}-{reference_sha256[:20]}",
                    "content_url": (
                        "/local/style-suite/api/missing-part-completion/"
                        f"reference-inbox/{target_id}/references/{reference_sha256}"
                    ),
                }
            )
        return tuple(approved)

    def resolve_approved_assets(
        self, source_sha256: str, asset_ids: tuple[str, ...]
    ) -> tuple[tuple[bytes, ...], tuple[str, ...]]:
        if not asset_ids or len(set(asset_ids)) != len(asset_ids):
            raise DomainRuleError("missing_part_reference_asset_invalid")
        allowed = {
            str(asset["asset_id"]): asset
            for asset in self.approved_assets(source_sha256)
        }
        try:
            selected = [allowed[asset_id] for asset_id in asset_ids]
        except KeyError:
            raise DomainRuleError("missing_part_reference_asset_invalid") from None
        contents: list[bytes] = []
        mime_types: list[str] = []
        for asset in selected:
            content, mime_type = self.reference_content(
                str(asset["target_id"]), str(asset["reference_sha256"])
            )
            contents.append(content)
            mime_types.append(mime_type)
        return tuple(contents), tuple(mime_types)

    def _record(
        self, target_id: str, reference_sha256: str
    ) -> tuple[dict[str, object], Path]:
        if _gap_target(target_id) is None or len(reference_sha256) != 64:
            raise DomainRuleError("missing_part_reference_asset_invalid")
        target_dir = self._root / target_id
        metadata_path = target_dir / f"{reference_sha256}.json"
        try:
            record = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            raise DomainRuleError("missing_part_reference_asset_invalid") from None
        if (
            not isinstance(record, dict)
            or record.get("target_id") != target_id
            or record.get("reference_sha256") != reference_sha256
        ):
            raise DomainRuleError("missing_part_reference_asset_invalid")
        return record, target_dir


def _asset_id(target_id: str, content_sha256: str) -> str:
    return f"{target_id}-{content_sha256[:20]}"


def _content(path: Path) -> tuple[bytes, str, str]:
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        raise DomainRuleError("missing_part_reference_asset_unavailable") from None
    try:
        frames_root = _audit_path("视频帧").resolve(strict=False)
    except DomainRuleError:
        raise DomainRuleError("missing_part_reference_asset_invalid") from None
    if not (
        resolved.is_relative_to(frames_root)
        or resolved in _admitted_external_reference_paths()
    ):
        raise DomainRuleError("missing_part_reference_asset_invalid")
    if path.suffix.lower() in {".jpg", ".jpeg"}:
        mime_type = "image/jpeg"
    elif path.suffix.lower() == ".png":
        mime_type = "image/png"
    else:
        raise DomainRuleError("missing_part_reference_asset_invalid")
    try:
        content = path.read_bytes()
    except OSError:
        raise DomainRuleError("missing_part_reference_asset_unavailable") from None
    return content, sha256(content).hexdigest(), mime_type


def _evenly_spaced(paths: list[Path], limit: int) -> list[Path]:
    if len(paths) <= limit:
        return paths
    if limit <= 1:
        return paths[:limit]
    indexes = {
        round(position * (len(paths) - 1) / (limit - 1)) for position in range(limit)
    }
    return [paths[index] for index in sorted(indexes)]


def local_reference_assets() -> tuple[LocalReferenceAsset, ...]:
    """Return only candidates admitted by the active exact-match audit bundle."""

    try:
        payload = _read_json(_audit_path("全库检索技术结论.json"))
    except (DomainRuleError, OSError, json.JSONDecodeError, TypeError):
        return ()
    assets: list[LocalReferenceAsset] = []
    for exact in payload.get("confirmed_exact_references", []):
        target_id = str(exact.get("target_id", ""))
        target_sha256 = str(exact.get("target_sha256", ""))
        reference_path = Path(str(exact.get("reference_path", "")))
        if not target_id or len(target_sha256) != 64 or not reference_path.is_file():
            continue
        _, digest, mime_type = _content(reference_path)
        if digest != exact.get("reference_sha256"):
            continue
        assets.append(
            LocalReferenceAsset(
                asset_id=_asset_id(target_id, digest),
                target_id=target_id,
                target_sha256=target_sha256,
                path=reference_path,
                content_sha256=digest,
                mime_type=mime_type,
                source_kind="audited_near_duplicate",
                structure_evidence_state="same_occlusion_unresolved",
                eligible_for_missing_part_completion=False,
            )
        )
        frame_paths = sorted((_audit_path("视频帧") / target_id).glob("*.jpg"))
        for frame_path in _evenly_spaced(frame_paths, MAX_SUGGESTED_VIEWS - 1):
            _, frame_digest, frame_mime_type = _content(frame_path)
            if any(asset.content_sha256 == frame_digest for asset in assets):
                continue
            assets.append(
                LocalReferenceAsset(
                    asset_id=_asset_id(target_id, frame_digest),
                    target_id=target_id,
                    target_sha256=target_sha256,
                    path=frame_path,
                    content_sha256=frame_digest,
                    mime_type=frame_mime_type,
                    source_kind="same_video_frame_candidate",
                    structure_evidence_state="same_occlusion_unresolved",
                    eligible_for_missing_part_completion=False,
                )
            )
    return tuple(assets)


def reference_suggestions(source_sha256: str) -> tuple[LocalReferenceAsset, ...]:
    return tuple(
        asset
        for asset in local_reference_assets()
        if asset.target_sha256 == source_sha256
    )


def reference_sequence_audit(source_sha256: str) -> dict[str, object] | None:
    """Return reviewed sequence evidence without inferring hidden pixels."""

    try:
        payload = _read_json(_audit_path("完整视频遮挡覆盖复核.json"))
    except (DomainRuleError, OSError, json.JSONDecodeError, TypeError):
        return None
    if payload.get("target_sha256") != source_sha256:
        return None
    required = {
        "duration_seconds",
        "sampling_interval_seconds",
        "reviewed_frame_count",
        "same_occlusion_across_reviewed_frames",
        "missing_part_structure_revealed",
        "automatic_structure_claim_allowed",
        "recommended_action",
    }
    if not required.issubset(payload):
        return None
    return {key: payload[key] for key in sorted(required)}


def _expanded_search_payload() -> dict[str, Any]:
    """Prefer the all-target audit while retaining older evidence bundles."""

    for filename in (
        "全目标本机扩展检索复核.json",
        "扩展本机与ZIP同款检索复核.json",
    ):
        try:
            return _read_json(_audit_path(filename))
        except (DomainRuleError, OSError, json.JSONDecodeError, TypeError):
            continue
    return {}


def priority_reference_candidates() -> tuple[PriorityReferenceCandidate, ...]:
    """Return only explicitly flagged machine candidates, never approved truth."""

    payload = _expanded_search_payload()
    priority_ids = {
        str(value) for value in payload.get("priority_human_review_target_ids", [])
    }
    candidates: list[PriorityReferenceCandidate] = []
    for item in _mapping_items(payload, "targets"):
        target_id = str(item.get("target_id", ""))
        if target_id not in priority_ids:
            continue
        target = _gap_target(target_id)
        if target is None:
            continue
        path = Path(str(item.get("best_candidate_path", "")))
        expected_digest = str(item.get("best_candidate_sha256", ""))
        if path.is_symlink() or not path.is_file() or len(expected_digest) != 64:
            continue
        try:
            _, digest, mime_type = _content(path)
        except DomainRuleError:
            continue
        if digest != expected_digest:
            continue
        try:
            feature_distance = float(item.get("best_feature_distance", 0.0))
        except (TypeError, ValueError):
            feature_distance = 0.0
        candidates.append(
            PriorityReferenceCandidate(
                target_id=target_id,
                target_sha256=target["target_sha256"],
                source_filename=target["source_filename"],
                reference_path=path,
                reference_sha256=digest,
                reference_filename=path.name,
                mime_type=mime_type,
                machine_visual_triage=str(item.get("machine_visual_triage", "")),
                feature_distance=feature_distance,
            )
        )
    return tuple(candidates)


def get_priority_reference_candidate(
    target_id: str, reference_sha256: str
) -> tuple[bytes, str, PriorityReferenceCandidate]:
    for candidate in priority_reference_candidates():
        if (
            candidate.target_id == target_id
            and candidate.reference_sha256 == reference_sha256
        ):
            content, digest, mime_type = _content(candidate.reference_path)
            if digest != reference_sha256:
                raise DomainRuleError("missing_part_reference_asset_changed")
            return content, mime_type, candidate
    raise DomainRuleError("missing_part_reference_asset_invalid")


def reference_readiness_summary() -> dict[str, object]:
    """Summarize the audited batch without upgrading candidates to truth."""

    try:
        payload = _read_json(_audit_path("全库检索技术结论.json"))
    except (DomainRuleError, OSError, json.JSONDecodeError, TypeError):
        return {
            "state": "audit_unavailable",
            "target_count": 0,
            "ready_for_completion": 0,
            "same_occlusion_deferred": 0,
            "no_exact_reference_deferred": 0,
            "deferred_total": 0,
            "automatic_generation_allowed": False,
            "provider_calls": 0,
        }
    exact_targets = {
        str(item.get("target_sha256", ""))
        for item in payload.get("confirmed_exact_references", [])
        if len(str(item.get("target_sha256", ""))) == 64
    }
    eligible_targets = {
        asset.target_sha256
        for asset in local_reference_assets()
        if asset.eligible_for_missing_part_completion
    }
    same_occlusion_targets = exact_targets - eligible_targets
    no_exact_reference = len(payload.get("unresolved_target_ids", []))
    target_count = int(payload.get("target_count", 0))
    deferred_total = len(same_occlusion_targets) + no_exact_reference
    expanded = _expanded_search_payload()
    expanded_target_ids = {
        str(value) for value in expanded.get("expanded_target_ids", [])
    }
    priority_review_ids = {
        str(value) for value in expanded.get("priority_human_review_target_ids", [])
    }
    return {
        "state": "deferred_pending_clean_reference",
        "target_count": target_count,
        "ready_for_completion": len(eligible_targets),
        "same_occlusion_deferred": len(same_occlusion_targets),
        "no_exact_reference_deferred": no_exact_reference,
        "deferred_total": deferred_total,
        "automatic_generation_allowed": False,
        "provider_calls": 0,
        "expanded_search_target_count": int(
            expanded.get(
                "expanded_search_target_count",
                len(expanded_target_ids) or (1 if expanded else 0),
            )
        ),
        "expanded_unique_references_reviewed": int(
            expanded.get("global_unique_references_reviewed", 0)
        ),
        "expanded_clean_exact_reference_found": bool(
            expanded.get("clean_exact_reference_found", False)
        ),
        "expanded_priority_human_review_count": len(priority_review_ids),
    }


def reference_gap_manifest() -> dict[str, object]:
    """Describe exactly what evidence is still missing for every audited target."""

    try:
        conclusion = _read_json(_audit_path("全库检索技术结论.json"))
        search_input = _read_json(_audit_path("全库同款检索输入.json"))
    except (DomainRuleError, OSError, json.JSONDecodeError, TypeError):
        return {
            "state": "audit_unavailable",
            "target_count": 0,
            "items": [],
            "automatic_generation_allowed": False,
            "provider_calls": 0,
        }
    exact_targets = {
        str(item.get("target_sha256", ""))
        for item in conclusion.get("confirmed_exact_references", [])
        if len(str(item.get("target_sha256", ""))) == 64
    }
    expanded = _expanded_search_payload()
    expanded_target_ids = {
        str(value) for value in expanded.get("expanded_target_ids", [])
    }
    expanded_target_sha256 = str(expanded.get("target_sha256", ""))
    items: list[dict[str, object]] = []
    for target in search_input.get("targets", []):
        target_id = str(target.get("id", ""))
        target_sha256 = str(target.get("sha256", ""))
        source_path = Path(str(target.get("path", "")))
        if not target_id or len(target_sha256) != 64:
            continue
        same_occlusion = target_sha256 in exact_targets
        expanded_search = (
            target_id in expanded_target_ids or target_sha256 == expanded_target_sha256
        )
        items.append(
            {
                "target_id": target_id,
                "target_sha256": target_sha256,
                "source_filename": source_path.name,
                "source_available": source_path.is_file(),
                "status": (
                    "same_occlusion_deferred"
                    if same_occlusion
                    else "no_exact_reference_deferred"
                ),
                "reason": (
                    "找到同一商品候选，但缺失位置仍被同一遮挡盖住"
                    if same_occlusion
                    else "已审计素材中没有确证为同一件实物商品的参考图"
                ),
                "accepted_reference": (
                    "同一件实物商品，且缺失位置清楚可见的照片或视频帧"
                ),
                "rejected_evidence": [
                    "外观相似但不是同一件实物商品",
                    "仍遮住同一位置的连续帧",
                    "仅靠模型猜测隐藏结构",
                ],
                "next_action": "导入真实参考后重新校验；校验通过前不自动生成",
                "searched_unique_reference_count": (
                    int(expanded.get("global_unique_references_reviewed", 0))
                    if expanded_search
                    else int(conclusion.get("reference_pool_count", 0))
                ),
                "expanded_search_completed": expanded_search,
                "automatic_generation_allowed": False,
            }
        )
    return {
        "state": "deferred_pending_clean_reference",
        "target_count": len(items),
        "items": items,
        "accepted_reference_rule": (
            "必须是同一件实物商品，并能清楚看到本次被遮挡或缺失的位置"
        ),
        "automatic_generation_allowed": False,
        "provider_calls": 0,
    }


def _gap_target(target_id: str) -> dict[str, str] | None:
    manifest = reference_gap_manifest()
    for value in _mapping_items(manifest, "items"):
        if value.get("target_id") != target_id:
            continue
        return {
            "target_sha256": str(value.get("target_sha256", "")),
            "source_filename": str(value.get("source_filename", "")),
        }
    return None


def get_gap_target_source(target_id: str) -> tuple[bytes, str]:
    """Read an audited target only when its current bytes still match the manifest."""

    target = _gap_target(target_id)
    if target is None:
        raise DomainRuleError("missing_part_reference_target_invalid")
    try:
        search_input = _read_json(_audit_path("全库同款检索输入.json"))
    except (DomainRuleError, OSError, json.JSONDecodeError, TypeError):
        raise DomainRuleError("missing_part_reference_asset_unavailable") from None
    path_value = next(
        (
            value.get("path")
            for value in search_input.get("targets", [])
            if isinstance(value, dict) and value.get("id") == target_id
        ),
        None,
    )
    path = Path(str(path_value or ""))
    try:
        content = path.read_bytes()
    except OSError:
        raise DomainRuleError("missing_part_reference_asset_unavailable") from None
    if sha256(content).hexdigest() != target["target_sha256"]:
        raise DomainRuleError("missing_part_reference_asset_changed")
    mime_type = (
        "image/jpeg" if path.suffix.lower() in {".jpg", ".jpeg"} else "image/png"
    )
    return content, mime_type


def recommended_diverse_reference_pair(
    source_sha256: str,
) -> tuple[tuple[str, ...], int | None]:
    """Recommend the most visually distinct pair without claiming product identity."""

    candidates = tuple(
        candidate
        for candidate in reference_suggestions(source_sha256)
        if candidate.eligible_for_missing_part_completion
    )
    fingerprints: list[tuple[LocalReferenceAsset, int]] = []
    for candidate in candidates:
        content, _, _ = _content(candidate.path)
        try:
            with Image.open(BytesIO(content)) as opened:
                opened.load()
                fingerprint = reference_view_difference_hash(opened)
        except (
            Image.DecompressionBombError,
            UnidentifiedImageError,
            OSError,
            ValueError,
        ):
            continue
        fingerprints.append((candidate, fingerprint))
    if len(fingerprints) < 2:
        return (), None
    best_pair: tuple[str, str] | None = None
    best_distance = -1
    for index, (left_asset, left_hash) in enumerate(fingerprints):
        for right_asset, right_hash in fingerprints[index + 1 :]:
            distance = (left_hash ^ right_hash).bit_count()
            if distance > best_distance:
                best_pair = (left_asset.asset_id, right_asset.asset_id)
                best_distance = distance
    if best_pair is None or best_distance < MIN_MULTIVIEW_DHASH_DISTANCE:
        return (), best_distance if best_distance >= 0 else None
    return best_pair, best_distance


def get_reference_asset(asset_id: str) -> tuple[bytes, str]:
    for asset in local_reference_assets():
        if asset.asset_id != asset_id:
            continue
        content, digest, mime_type = _content(asset.path)
        if digest != asset.content_sha256 or mime_type != asset.mime_type:
            raise DomainRuleError("missing_part_reference_asset_changed")
        return content, mime_type
    raise DomainRuleError("missing_part_reference_asset_invalid")


def resolve_reference_assets(
    source_sha256: str, asset_ids: tuple[str, ...]
) -> tuple[tuple[bytes, ...], tuple[str, ...]]:
    if not asset_ids or len(set(asset_ids)) != len(asset_ids):
        raise DomainRuleError("missing_part_reference_asset_invalid")
    allowed = {asset.asset_id: asset for asset in reference_suggestions(source_sha256)}
    try:
        selected = [allowed[asset_id] for asset_id in asset_ids]
    except KeyError:
        raise DomainRuleError("missing_part_reference_asset_invalid") from None
    if any(not asset.eligible_for_missing_part_completion for asset in selected):
        raise DomainRuleError("missing_part_reference_same_occlusion")
    contents: list[bytes] = []
    mime_types: list[str] = []
    for asset in selected:
        content, digest, mime_type = _content(asset.path)
        if digest != asset.content_sha256 or mime_type != asset.mime_type:
            raise DomainRuleError("missing_part_reference_asset_changed")
        contents.append(content)
        mime_types.append(mime_type)
    return tuple(contents), tuple(mime_types)
