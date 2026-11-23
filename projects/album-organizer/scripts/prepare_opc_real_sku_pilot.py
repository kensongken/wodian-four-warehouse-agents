"""Prepare one OPC-owned real-SKU image pilot entirely offline."""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import tempfile
from itertools import combinations
from pathlib import Path
from typing import Never
from uuid import RFC_4122, UUID

from pydantic import ValidationError

from photo_organizer.catalog.controlled_upload_candidate import (
    ControlledUploadCandidateError,
    build_controlled_upload_candidate,
    validate_controlled_upload_readiness,
)
from photo_organizer.catalog.local_product_image import (
    LocalProductImageError,
    validate_local_product_image,
)
from photo_organizer.catalog.real_sku_pilot import (
    RealSkuPilotError,
    build_real_sku_pilot_readiness,
)
from photo_organizer.categorization.contracts import ProductSourceType
from photo_organizer.schemas.controlled_upload_candidate import (
    ControlledUploadCandidate,
)
from photo_organizer.schemas.real_sku_pilot import (
    PilotImageIntent,
    RealSkuPilotIntakeManifest,
    RealSkuPilotReadinessReceipt,
)

ROOT = Path(__file__).resolve().parents[1]
INTAKE_SCHEMA_PATH = ROOT / "contracts/opc-real-sku-pilot-intake-v1.schema.json"
READINESS_SCHEMA_PATH = ROOT / "contracts/opc-real-sku-pilot-readiness-v1.schema.json"
CANDIDATE_SCHEMA_PATH = (
    ROOT / "contracts/opc-controlled-upload-candidate-v1.schema.json"
)
_MAX_MANIFEST_BYTES = 64 * 1024
_MAX_RECEIPT_BYTES = 256 * 1024
_MAX_DEPTH = 16
_MAX_NODES = 2048
_MAX_STRING_LENGTH = 512
_FORBIDDEN_KEYS = {
    "access_token",
    "address",
    "chat_text",
    "customer_id",
    "customer_name",
    "download_url",
    "exif",
    "filename",
    "image_bytes",
    "mobile",
    "object_key",
    "order_id",
    "path",
    "phone",
    "private_key",
    "refresh_token",
    "secret",
    "source_object_key",
    "token",
    "wechat",
}


class PilotPreparationError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise PilotPreparationError("invalid_arguments")


def render_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def build_artifacts() -> tuple[
    dict[str, object],
    dict[str, object],
    dict[str, object],
]:
    command = (
        "python scripts/prepare_opc_real_sku_pilot.py "
        "--manifest <manifest.json> --image <local_image_id=local_path>... "
        "--output <readiness.json> --check"
    )
    intake = RealSkuPilotIntakeManifest.model_json_schema()
    intake.update(
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "urn:photo-organizer:opc-real-sku-pilot-intake:1.0",
            "$comment": (
                "Structural schema only. No real SKU, customer data, image path, "
                "Mall Product ID, or Bead ID belongs in this artifact."
            ),
            "x-semantic-validation": {
                "required": True,
                "command": command,
                "stable_error": "receipt_stale",
            },
        }
    )
    readiness = RealSkuPilotReadinessReceipt.model_json_schema()
    readiness.update(
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "urn:photo-organizer:opc-real-sku-pilot-readiness:1.0",
            "$comment": (
                "Technical readiness only: this is not upload, confirmation, "
                "Mall Draft, SKU creation, order creation, or listing."
            ),
            "x-semantic-validation": {
                "required": True,
                "command": command,
                "stable_error": "receipt_stale",
            },
        }
    )
    candidate = ControlledUploadCandidate.model_json_schema()
    candidate.update(
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "urn:photo-organizer:opc-controlled-upload-candidate:1.0",
            "$comment": (
                "Technical handoff candidate only; task binding and upload "
                "have not started."
            ),
            "x-semantic-validation": {
                "required": True,
                "command": (
                    "python scripts/prepare_opc_real_sku_pilot.py "
                    "--readiness <readiness.json> "
                    "--image <local_image_id=local_path>... "
                    "--output <candidate.json> --check"
                ),
                "stable_error": "candidate_stale",
            },
        }
    )
    return intake, readiness, candidate


def _parser() -> _SafeArgumentParser:
    parser = _SafeArgumentParser()
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--readiness", type=Path)
    parser.add_argument("--image", action="append")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--write-artifacts", action="store_true")
    parser.add_argument("--check-artifacts", action="store_true")
    return parser


def _validate_args(args: argparse.Namespace) -> None:
    artifact_mode = args.write_artifacts or args.check_artifacts
    if args.write_artifacts and args.check_artifacts:
        raise PilotPreparationError("invalid_arguments")
    if artifact_mode:
        if (
            args.manifest is not None
            or args.readiness is not None
            or args.image is not None
            or args.output is not None
            or args.check
        ):
            raise PilotPreparationError("invalid_arguments")
        return
    input_count = int(args.manifest is not None) + int(args.readiness is not None)
    if input_count != 1 or not args.image or args.output is None:
        raise PilotPreparationError("invalid_arguments")


def _read_bounded_regular_file(path: Path, limit: int, error_code: str) -> bytes:
    try:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise PilotPreparationError(error_code)
        flags = (
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as source:
            opened = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or (metadata.st_dev, metadata.st_ino)
                != (opened.st_dev, opened.st_ino)
                or metadata.st_size != opened.st_size
                or not 1 <= opened.st_size <= limit
            ):
                raise PilotPreparationError(error_code)
            content = source.read(limit + 1)
            if len(content) != opened.st_size or source.read(1):
                raise PilotPreparationError(error_code)
            return content
    except PilotPreparationError:
        raise
    except OSError:
        raise PilotPreparationError(error_code) from None


def _load_manifest(path: Path) -> RealSkuPilotIntakeManifest:
    try:
        raw = _read_bounded_regular_file(path, _MAX_MANIFEST_BYTES, "invalid_manifest")
        value = json.loads(raw.decode("utf-8"))
        _assert_bounded_safe(value)
        _classify_manifest_boundary(value)
        return RealSkuPilotIntakeManifest.model_validate(value)
    except PilotPreparationError:
        raise
    except (UnicodeError, json.JSONDecodeError, ValidationError, RecursionError):
        raise PilotPreparationError("invalid_manifest") from None


def _load_readiness(path: Path) -> RealSkuPilotReadinessReceipt:
    try:
        raw = _read_bounded_regular_file(path, _MAX_RECEIPT_BYTES, "invalid_readiness")
        value = json.loads(raw.decode("utf-8"))
        _assert_bounded_safe(value, error_code="invalid_readiness")
        _classify_manifest_boundary(value)
        return RealSkuPilotReadinessReceipt.model_validate(value)
    except PilotPreparationError:
        raise
    except (UnicodeError, json.JSONDecodeError, ValidationError, RecursionError):
        raise PilotPreparationError("invalid_readiness") from None


def _classify_manifest_boundary(value: object) -> None:
    if not isinstance(value, dict):
        return
    if (
        "material_owner" in value
        and value["material_owner"] != "opc"
        or "authorization_basis" in value
        and value["authorization_basis"] != "opc_self_owned_product_material"
    ):
        raise PilotPreparationError("unauthorized_material")
    if (
        "privacy_review_confirmed" in value
        and value["privacy_review_confirmed"] is not True
        or "customer_data_present" in value
        and value["customer_data_present"] is not False
    ):
        raise PilotPreparationError("privacy_boundary_failed")
    images = value.get("images")
    if isinstance(images, list) and any(
        isinstance(image, dict)
        and "human_privacy_reviewed" in image
        and image["human_privacy_reviewed"] is not True
        for image in images
    ):
        raise PilotPreparationError("privacy_boundary_failed")


def _assert_bounded_safe(
    value: object,
    *,
    error_code: str = "invalid_manifest",
) -> None:
    node_count = 0

    def visit(child: object, depth: int) -> None:
        nonlocal node_count
        node_count += 1
        if node_count > _MAX_NODES or depth > _MAX_DEPTH:
            raise PilotPreparationError(error_code)
        if isinstance(child, dict):
            for key, nested in child.items():
                if not isinstance(key, str) or len(key) > _MAX_STRING_LENGTH:
                    raise PilotPreparationError(error_code)
                normalized = key.casefold()
                if (
                    normalized in _FORBIDDEN_KEYS
                    or normalized.endswith(("_token", "_url", "_path", "_filename"))
                ):
                    raise PilotPreparationError(error_code)
                visit(nested, depth + 1)
        elif isinstance(child, list):
            for nested in child:
                visit(nested, depth + 1)
        elif isinstance(child, str) and (
            len(child) > _MAX_STRING_LENGTH
            or child.casefold().startswith(
                ("http://", "https://", "file:", "content:")
            )
        ):
            raise PilotPreparationError(error_code)

    visit(value, 0)


def _image_paths(values: list[str]) -> dict[UUID, Path]:
    result: dict[UUID, Path] = {}
    for value in values:
        identity, separator, raw_path = value.partition("=")
        if (
            not separator
            or not raw_path
            or raw_path.casefold().startswith(
                ("http://", "https://", "file:", "content:")
            )
        ):
            raise PilotPreparationError("invalid_arguments")
        try:
            image_id = UUID(identity)
        except ValueError:
            raise PilotPreparationError("invalid_arguments") from None
        if (
            str(image_id) != identity
            or image_id.version not in {1, 2, 3, 4, 5}
            or image_id.variant != RFC_4122
            or image_id in result
        ):
            raise PilotPreparationError("invalid_arguments")
        result[image_id] = Path(raw_path)
    return result


def _paths_collide(first: Path, second: Path) -> bool:
    try:
        return os.path.samefile(first, second)
    except OSError:
        return os.path.abspath(os.fspath(first)) == os.path.abspath(os.fspath(second))


def _validate_path_set(manifest: Path, images: dict[UUID, Path], output: Path) -> None:
    inputs = (manifest, *images.values())
    if any(_paths_collide(first, second) for first, second in combinations(inputs, 2)):
        raise PilotPreparationError("invalid_arguments")
    if any(_paths_collide(source, output) for source in inputs):
        raise PilotPreparationError("invalid_arguments")


def _prepare(
    manifest_path: Path,
    image_values: list[str],
    output: Path,
) -> tuple[str, str]:
    manifest = _load_manifest(manifest_path)
    if manifest.source_type is not ProductSourceType.FIRST_SALE_BEAD:
        raise RealSkuPilotError("unsupported_source_context")
    paths = _image_paths(image_values)
    intended_ids = {image.local_image_id for image in manifest.images}
    if set(paths) != intended_ids:
        raise PilotPreparationError("invalid_arguments")
    _validate_path_set(manifest_path, paths, output)
    intents = sorted(manifest.images, key=lambda image: image.position)
    validated = tuple(
        validate_local_product_image(intent, paths[intent.local_image_id])
        for intent in intents
    )
    receipt = build_real_sku_pilot_readiness(manifest, validated)
    return receipt.readiness_id, render_json(receipt.model_dump(mode="json"))


def _prepare_candidate(
    readiness_path: Path,
    image_values: list[str],
    output: Path,
) -> tuple[str, str]:
    readiness = validate_controlled_upload_readiness(
        _load_readiness(readiness_path)
    )
    paths = _image_paths(image_values)
    intended_ids = {image.local_image_id for image in readiness.images}
    if set(paths) != intended_ids:
        raise PilotPreparationError("invalid_arguments")
    _validate_path_set(readiness_path, paths, output)
    validated = tuple(
        validate_local_product_image(
            PilotImageIntent(
                local_image_id=image.local_image_id,
                role=image.role,
                position=image.position,
                captured_at=image.captured_at,
                human_privacy_reviewed=True,
            ),
            paths[image.local_image_id],
        )
        for image in sorted(readiness.images, key=lambda value: value.position)
    )
    candidate = build_controlled_upload_candidate(readiness, validated)
    return candidate.candidate_id, render_json(candidate.model_dump(mode="json"))


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write(path: Path, content: str) -> None:
    temporary_path: Path | None = None
    backup_path: Path | None = None
    preserve_backup = False
    try:
        backup_path = _backup_existing_output(path)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as temporary:
            os.fchmod(temporary.fileno(), 0o600)
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, path)
        temporary_path = None
        try:
            _fsync_directory(path.parent)
        except OSError:
            if _rollback_replacement(path, backup_path):
                backup_path = None
            else:
                preserve_backup = True
            raise
        if backup_path is not None:
            try:
                backup_path.unlink()
                backup_path = None
            except OSError:
                if _rollback_replacement(path, backup_path):
                    backup_path = None
                else:
                    preserve_backup = True
                raise
    except OSError:
        raise PilotPreparationError("output_unavailable") from None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
        if backup_path is not None and not preserve_backup:
            try:
                backup_path.unlink(missing_ok=True)
            except OSError:
                pass


def _backup_existing_output(path: Path) -> Path | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise OSError("unsafe output")
    descriptor, backup_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.backup.",
    )
    os.close(descriptor)
    backup_path = Path(backup_name)
    try:
        backup_path.unlink()
        os.link(path, backup_path, follow_symlinks=False)
        return backup_path
    except OSError:
        backup_path.unlink(missing_ok=True)
        raise


def _rollback_replacement(path: Path, backup_path: Path | None) -> bool:
    restored = False
    try:
        if backup_path is None:
            path.unlink(missing_ok=True)
            restored = True
        else:
            os.replace(backup_path, path)
            restored = True
    except OSError:
        if backup_path is None:
            return False
        try:
            path.unlink(missing_ok=True)
            os.link(backup_path, path, follow_symlinks=False)
            backup_path.unlink()
            restored = True
        except OSError:
            return False
    try:
        _fsync_directory(path.parent)
    except OSError:
        pass
    return restored


def _check_output(path: Path, expected: str, error_code: str) -> None:
    try:
        current = _read_bounded_regular_file(
            path,
            _MAX_RECEIPT_BYTES,
            error_code,
        ).decode("utf-8")
    except (UnicodeError, PilotPreparationError):
        raise PilotPreparationError(error_code) from None
    if current != expected:
        raise PilotPreparationError(error_code)


def _artifacts(*, check: bool) -> None:
    intake, readiness, candidate = build_artifacts()
    expected = (
        (INTAKE_SCHEMA_PATH, render_json(intake)),
        (READINESS_SCHEMA_PATH, render_json(readiness)),
        (CANDIDATE_SCHEMA_PATH, render_json(candidate)),
    )
    if check:
        for path, content in expected:
            try:
                if path.read_text(encoding="utf-8") != content:
                    raise PilotPreparationError("artifact_stale")
            except (OSError, UnicodeError):
                raise PilotPreparationError("artifact_stale") from None
        return
    for path, content in expected:
        _atomic_write(path, content)


def main() -> int:
    try:
        args = _parser().parse_args()
        _validate_args(args)
        if args.write_artifacts:
            _artifacts(check=False)
            return 0
        if args.check_artifacts:
            _artifacts(check=True)
            return 0
        if args.readiness is not None:
            output_id, content = _prepare_candidate(
                args.readiness,
                args.image,
                args.output,
            )
            stale_code = "candidate_stale"
        else:
            output_id, content = _prepare(args.manifest, args.image, args.output)
            stale_code = "receipt_stale"
        if args.check:
            _check_output(args.output, content, stale_code)
        else:
            _atomic_write(args.output, content)
        print(output_id)
        return 0
    except PilotPreparationError as error:
        print(error.code, file=sys.stderr)
        return (
            1
            if error.code in {"receipt_stale", "candidate_stale", "artifact_stale"}
            else 2
        )
    except (
        ControlledUploadCandidateError,
        RealSkuPilotError,
        LocalProductImageError,
    ) as error:
        print(error.code, file=sys.stderr)
        return 2
    except (OSError, TypeError, ValueError, ValidationError, RecursionError):
        print("invalid_manifest", file=sys.stderr)
        return 2


def entrypoint() -> Never:
    raise SystemExit(main())


if __name__ == "__main__":
    entrypoint()
