from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
from jsonschema import Draft202012Validator
from PIL import Image

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "scripts/prepare_opc_real_sku_pilot.py"
INTAKE_SCHEMA = ROOT / "contracts/opc-real-sku-pilot-intake-v1.schema.json"
READINESS_SCHEMA = ROOT / "contracts/opc-real-sku-pilot-readiness-v1.schema.json"
CANDIDATE_SCHEMA = ROOT / "contracts/opc-controlled-upload-candidate-v1.schema.json"
IMAGE_IDS = (
    "11111111-1111-4111-8111-111111111111",
    "22222222-2222-4222-8222-222222222222",
    "44444444-4444-4444-8444-444444444444",
    "55555555-5555-4555-8555-555555555555",
)


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("prepare_real_sku_pilot", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load pilot preparer")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _manifest(
    *, count: int = 2, source_type: str = "first_sale_bead"
) -> dict[str, object]:
    images = [
        {
            "local_image_id": IMAGE_IDS[index],
            "role": "primary" if index == 0 else "detail",
            "position": index + 1,
            "captured_at": f"2026-08-17T10:0{index}:00Z",
            "human_privacy_reviewed": True,
        }
        for index in range(count)
    ]
    return {
        "schema_version": "opc-real-sku-pilot-intake/1.0",
        "opc_id": "opc-synthetic-pilot",
        "album_product_id": "album-product-synthetic-pilot-001",
        "source_type": source_type,
        "target_chain": "sellable_product_material",
        "data_classification": "opc_owned_product_material_no_customer_data",
        "material_owner": "opc",
        "authorization_basis": "opc_self_owned_product_material",
        "authorized_by": "user:33333333-3333-4333-8333-333333333333",
        "authorized_at": "2026-08-18T09:00:00Z",
        "privacy_review_confirmed": True,
        "customer_data_present": False,
        "images": images,
        "claims_not_evaluated": [
            "authenticity",
            "origin",
            "quality",
            "warehouse_acceptance",
        ],
        "mall_product_id": None,
        "bead_id": None,
        "auto_publish": False,
        "shared_market_opt_in": False,
    }


def _write_manifest(tmp_path: Path, value: object) -> Path:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _write_images(tmp_path: Path, count: int) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    for index, identity in enumerate(IMAGE_IDS[:count]):
        path = tmp_path / f"synthetic-{index}.{'jpg' if index % 2 == 0 else 'png'}"
        image = Image.new("RGB", (3, 3), "red" if index % 2 == 0 else "blue")
        image.save(path, format="JPEG" if index % 2 == 0 else "PNG")
        paths[identity] = path
    return paths


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _ordinary_args(
    manifest: Path,
    images: dict[str, Path],
    output: Path,
    *,
    reverse: bool = False,
) -> list[str]:
    mappings = list(images.items())
    if reverse:
        mappings.reverse()
    args = ["--manifest", str(manifest)]
    for identity, path in mappings:
        args.extend(("--image", f"{identity}={path}"))
    args.extend(("--output", str(output)))
    return args


def _candidate_args(
    readiness: Path,
    images: dict[str, Path],
    output: Path,
    *,
    reverse: bool = False,
) -> list[str]:
    mappings = list(images.items())
    if reverse:
        mappings.reverse()
    args = ["--readiness", str(readiness)]
    for identity, path in mappings:
        args.extend(("--image", f"{identity}={path}"))
    args.extend(("--output", str(output)))
    return args


def _prepare_readiness_fixture(
    tmp_path: Path,
) -> tuple[Path, dict[str, Path]]:
    manifest = _write_manifest(tmp_path, _manifest())
    images = _write_images(tmp_path, 2)
    readiness = tmp_path / "readiness.json"
    completed = _run(*_ordinary_args(manifest, images, readiness))
    assert completed.returncode == 0
    return readiness, images


@pytest.mark.parametrize("count", (2, 4))
def test_cli_writes_deterministic_redacted_receipt(
    count: int,
    tmp_path: Path,
) -> None:
    manifest = _write_manifest(tmp_path, _manifest(count=count))
    images = _write_images(tmp_path, count)
    first = tmp_path / "receipt-first.json"
    second = tmp_path / "receipt-second.json"

    result = _run(*_ordinary_args(manifest, images, first))
    reordered = _run(*_ordinary_args(manifest, images, second, reverse=True))

    assert result.returncode == reordered.returncode == 0
    assert result.stderr == reordered.stderr == ""
    assert first.read_bytes() == second.read_bytes()
    receipt = json.loads(first.read_text(encoding="utf-8"))
    assert result.stdout == f"{receipt['readiness_id']}\n"
    assert receipt["status"] == "eligible_for_controlled_import"
    assert receipt["next_action"] == "upload_to_controlled_ingestion"
    assert receipt["mall_product_id"] is None
    assert receipt["bead_id"] is None
    assert receipt["publication_state"] == "not_created"
    assert receipt["auto_publish"] is False
    assert receipt["shared_market_opt_in"] is False
    assert receipt["customer_data_present"] is False
    serialized = first.read_text(encoding="utf-8").casefold()
    assert str(tmp_path).casefold() not in serialized
    for forbidden in ("filename", "object_key", "download_url", "token", "exif"):
        assert forbidden not in serialized


def test_readiness_mode_revalidates_images_and_writes_redacted_candidate(
    tmp_path: Path,
) -> None:
    readiness, images = _prepare_readiness_fixture(tmp_path)
    first = tmp_path / "candidate-first.json"
    second = tmp_path / "candidate-second.json"

    completed = _run(*_candidate_args(readiness, images, first))
    reordered = _run(*_candidate_args(readiness, images, second, reverse=True))

    assert completed.returncode == reordered.returncode == 0
    assert completed.stderr == reordered.stderr == ""
    assert first.read_bytes() == second.read_bytes()
    candidate = json.loads(first.read_text(encoding="utf-8"))
    assert completed.stdout == f"{candidate['candidate_id']}\n"
    assert candidate["source_readiness_id"].startswith("opc-real-sku-pilot-readiness:")
    assert candidate["target_task_id"] is None
    assert candidate["task_binding_state"] == "not_bound"
    assert candidate["upload_state"] == "not_started"
    assert candidate["next_action"] == "bind_to_verified_opc_upload_task"
    assert candidate["mall_product_id"] is None
    assert candidate["bead_id"] is None
    assert candidate["auto_publish"] is False
    assert candidate["shared_market_opt_in"] is False
    serialized = first.read_text(encoding="utf-8").casefold()
    assert str(tmp_path).casefold() not in serialized
    for forbidden in (
        "download_url",
        "object_key",
        "access_token",
        "refresh_token",
        "image_bytes",
        "exif",
        "provider_upload_id",
        "signed_url",
    ):
        assert forbidden not in serialized


def test_candidate_check_is_exact_read_only_and_detects_tampering(
    tmp_path: Path,
) -> None:
    readiness, images = _prepare_readiness_fixture(tmp_path)
    output = tmp_path / "candidate.json"
    args = _candidate_args(readiness, images, output)
    assert _run(*args).returncode == 0
    original = output.read_bytes()
    inode = output.stat().st_ino

    current = _run(*args, "--check")
    output.write_text("{}\n", encoding="utf-8")
    stale = _run(*args, "--check")

    assert current.returncode == 0
    assert stale.returncode == 1
    assert stale.stdout == ""
    assert stale.stderr == "candidate_stale\n"
    assert output.read_text(encoding="utf-8") == "{}\n"
    assert output.stat().st_ino == inode
    assert original != output.read_bytes()


def test_candidate_mode_rejects_changed_image_bytes(tmp_path: Path) -> None:
    readiness, images = _prepare_readiness_fixture(tmp_path)
    Image.new("RGB", (3, 3), "green").save(images[IMAGE_IDS[0]], "JPEG")

    completed = _run(*_candidate_args(readiness, images, tmp_path / "candidate.json"))

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "readiness_image_mismatch\n"
    assert str(tmp_path) not in completed.stderr


def test_candidate_mode_rejects_extra_mapping_before_image_read(
    tmp_path: Path,
) -> None:
    readiness, images = _prepare_readiness_fixture(tmp_path)
    images[IMAGE_IDS[2]] = tmp_path / "private-missing-extra.jpg"

    completed = _run(*_candidate_args(readiness, images, tmp_path / "candidate.json"))

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "invalid_arguments\n"
    assert str(tmp_path) not in completed.stderr


@pytest.mark.parametrize(
    ("mutation", "expected"),
    (
        ({"material_owner": "customer"}, "unauthorized_material"),
        ({"authorization_basis": "customer_chat"}, "unauthorized_material"),
        ({"privacy_review_confirmed": False}, "privacy_boundary_failed"),
        ({"customer_data_present": True}, "privacy_boundary_failed"),
    ),
)
def test_candidate_mode_maps_readiness_boundaries_to_safe_errors(
    mutation: dict[str, object],
    expected: str,
    tmp_path: Path,
) -> None:
    readiness, images = _prepare_readiness_fixture(tmp_path)
    value = json.loads(readiness.read_text(encoding="utf-8"))
    value.update(mutation)
    readiness.write_text(json.dumps(value), encoding="utf-8")

    completed = _run(*_candidate_args(readiness, images, tmp_path / "candidate.json"))

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == f"{expected}\n"


def test_candidate_mode_rejects_malformed_readiness(tmp_path: Path) -> None:
    readiness = tmp_path / "readiness.json"
    readiness.write_text("[]", encoding="utf-8")
    images = _write_images(tmp_path, 2)

    completed = _run(*_candidate_args(readiness, images, tmp_path / "candidate.json"))

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "invalid_readiness\n"


def test_candidate_rejects_stale_readiness_before_any_image_read(
    tmp_path: Path,
) -> None:
    readiness, _ = _prepare_readiness_fixture(tmp_path)
    value = json.loads(readiness.read_text(encoding="utf-8"))
    value["readiness_id"] = f"opc-real-sku-pilot-readiness:{'0' * 64}"
    readiness.write_text(json.dumps(value), encoding="utf-8")
    missing_images = {
        IMAGE_IDS[0]: tmp_path / "private-missing-main.jpg",
        IMAGE_IDS[1]: tmp_path / "private-missing-detail.png",
    }

    completed = _run(
        *_candidate_args(readiness, missing_images, tmp_path / "candidate.json")
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "invalid_readiness\n"
    assert str(tmp_path) not in completed.stderr


def test_manifest_and_readiness_modes_are_mutually_exclusive(tmp_path: Path) -> None:
    readiness, images = _prepare_readiness_fixture(tmp_path)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(_manifest()), encoding="utf-8")
    args = _candidate_args(readiness, images, tmp_path / "candidate.json")
    args[0:0] = ["--manifest", str(manifest)]

    completed = _run(*args)

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "invalid_arguments\n"


def test_candidate_output_must_not_collide_with_readiness_or_image(
    tmp_path: Path,
) -> None:
    readiness, images = _prepare_readiness_fixture(tmp_path)

    for output in (readiness, next(iter(images.values()))):
        completed = _run(*_candidate_args(readiness, images, output))
        assert completed.returncode == 2
        assert completed.stdout == ""
        assert completed.stderr == "invalid_arguments\n"


def test_check_is_exact_read_only_and_detects_stale_image(tmp_path: Path) -> None:
    manifest = _write_manifest(tmp_path, _manifest())
    images = _write_images(tmp_path, 2)
    output = tmp_path / "receipt.json"
    args = _ordinary_args(manifest, images, output)
    assert _run(*args).returncode == 0
    original = output.read_bytes()
    inode = output.stat().st_ino

    current = _run(*args, "--check")
    Image.new("RGB", (3, 3), "green").save(next(iter(images.values())), "JPEG")
    stale = _run(*args, "--check")

    assert current.returncode == 0
    assert stale.returncode == 1
    assert stale.stdout == ""
    assert stale.stderr == "receipt_stale\n"
    assert output.read_bytes() == original
    assert output.stat().st_ino == inode


@pytest.mark.parametrize("source_type", ("legacy_material", "redesign_output"))
def test_unsupported_source_type_is_stable(
    source_type: str,
    tmp_path: Path,
) -> None:
    manifest = _write_manifest(tmp_path, _manifest(source_type=source_type))
    images = _write_images(tmp_path, 2)

    result = _run(*_ordinary_args(manifest, images, tmp_path / "receipt.json"))

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == "unsupported_source_context\n"


def test_unsupported_source_is_rejected_before_any_image_read(tmp_path: Path) -> None:
    manifest = _write_manifest(
        tmp_path,
        _manifest(source_type="legacy_material"),
    )
    missing = {
        IMAGE_IDS[0]: tmp_path / "private-missing-main.jpg",
        IMAGE_IDS[1]: tmp_path / "private-missing-detail.jpg",
    }

    result = _run(*_ordinary_args(manifest, missing, tmp_path / "receipt.json"))

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == "unsupported_source_context\n"
    assert str(tmp_path) not in result.stderr


@pytest.mark.parametrize("mutation", ("missing", "extra", "duplicate"))
def test_image_mapping_must_match_manifest_exactly(
    mutation: str,
    tmp_path: Path,
) -> None:
    manifest = _write_manifest(tmp_path, _manifest())
    images = _write_images(tmp_path, 2)
    output = tmp_path / "receipt.json"
    args = _ordinary_args(manifest, images, output)
    if mutation == "missing":
        args = args[:-4] + args[-2:]
    elif mutation == "extra":
        extra = tmp_path / "extra.jpg"
        Image.new("RGB", (3, 3), "red").save(extra, "JPEG")
        args[-2:-2] = ["--image", f"{IMAGE_IDS[2]}={extra}"]
    else:
        args[-2:-2] = ["--image", f"{IMAGE_IDS[0]}={images[IMAGE_IDS[0]]}"]

    result = _run(*args)

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == "invalid_arguments\n"


def test_unknown_secret_and_mixed_case_url_never_echo_input(tmp_path: Path) -> None:
    secret = "HtTpS://operator:private@invalid/a.jpg?token=do-not-print"
    unknown = _run("--database-url", secret)
    manifest = _write_manifest(tmp_path, _manifest())
    output = tmp_path / "receipt.json"
    mapped = _run(
        "--manifest",
        str(manifest),
        "--image",
        f"{IMAGE_IDS[0]}={secret}",
        "--output",
        str(output),
    )

    for result in (unknown, mapped):
        assert result.returncode == 2
        assert result.stdout == ""
        assert result.stderr == "invalid_arguments\n"
        assert secret not in result.stderr


def test_sensitive_exif_returns_only_privacy_error(tmp_path: Path) -> None:
    manifest = _write_manifest(tmp_path, _manifest())
    images = _write_images(tmp_path, 2)
    exif = Image.Exif()
    exif[315] = "customer-private-marker"
    Image.new("RGB", (3, 3), "red").save(
        images[IMAGE_IDS[0]],
        "JPEG",
        exif=exif,
    )

    result = _run(*_ordinary_args(manifest, images, tmp_path / "receipt.json"))

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == "privacy_boundary_failed\n"
    assert "customer-private-marker" not in result.stderr
    assert str(tmp_path) not in result.stderr


@pytest.mark.parametrize(
    ("mutation", "expected"),
    (
        ({"material_owner": "customer"}, "unauthorized_material"),
        ({"authorization_basis": "customer_chat"}, "unauthorized_material"),
        ({"privacy_review_confirmed": False}, "privacy_boundary_failed"),
        ({"customer_data_present": True}, "privacy_boundary_failed"),
    ),
)
def test_manifest_boundary_failures_use_stable_specific_codes(
    mutation: dict[str, object],
    expected: str,
    tmp_path: Path,
) -> None:
    value = _manifest()
    value.update(mutation)
    manifest = _write_manifest(tmp_path, value)
    images = _write_images(tmp_path, 2)

    result = _run(*_ordinary_args(manifest, images, tmp_path / "receipt.json"))

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == f"{expected}\n"


def test_manifest_parser_is_bounded_and_safe(tmp_path: Path) -> None:
    deep: object = "leaf"
    for _ in range(24):
        deep = {"nested": deep}
    deep_result = _run(
        "--manifest",
        str(_write_manifest(tmp_path, deep)),
        "--image",
        f"{IMAGE_IDS[0]}={tmp_path / 'missing'}",
        "--output",
        str(tmp_path / "receipt.json"),
    )
    large = tmp_path / "large.json"
    large.write_text(json.dumps({"padding": "x" * 70_000}), encoding="utf-8")
    large_result = _run(
        "--manifest",
        str(large),
        "--image",
        f"{IMAGE_IDS[0]}={tmp_path / 'missing'}",
        "--output",
        str(tmp_path / "receipt.json"),
    )

    for result in (deep_result, large_result):
        assert result.returncode == 2
        assert result.stdout == ""
        assert result.stderr == "invalid_manifest\n"
        assert "Traceback" not in result.stderr
        assert str(tmp_path) not in result.stderr


def test_output_must_not_collide_with_manifest_or_image(tmp_path: Path) -> None:
    manifest = _write_manifest(tmp_path, _manifest())
    images = _write_images(tmp_path, 2)
    first_image = next(iter(images.values()))

    for output in (manifest, first_image):
        result = _run(*_ordinary_args(manifest, images, output))
        assert result.returncode == 2
        assert result.stdout == ""
        assert result.stderr == "invalid_arguments\n"


def test_atomic_failure_preserves_existing_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_script()
    output = tmp_path / "receipt.json"
    output.write_text("existing\n", encoding="utf-8")

    def fail_replace(source: object, target: object) -> None:
        raise OSError("private-operating-system-message")

    monkeypatch.setattr(module.os, "replace", fail_replace)

    with pytest.raises(module.PilotPreparationError) as failure:
        module._atomic_write(output, "replacement\n")

    assert failure.value.code == "output_unavailable"
    assert output.read_text(encoding="utf-8") == "existing\n"
    assert list(tmp_path.iterdir()) == [output]


@pytest.mark.parametrize("existing", (False, True))
def test_directory_fsync_failure_rolls_back_output(
    existing: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_script()
    output = tmp_path / "receipt.json"
    if existing:
        output.write_text("existing\n", encoding="utf-8")

    def fail_fsync(directory: Path) -> None:
        raise OSError("private-operating-system-message")

    monkeypatch.setattr(module, "_fsync_directory", fail_fsync)

    with pytest.raises(module.PilotPreparationError) as failure:
        module._atomic_write(output, "replacement\n")

    assert failure.value.code == "output_unavailable"
    if existing:
        assert output.read_text(encoding="utf-8") == "existing\n"
    else:
        assert not output.exists()


def test_failed_atomic_restore_uses_hardlink_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_script()
    output = tmp_path / "receipt.json"
    output.write_text("existing\n", encoding="utf-8")
    real_replace = module.os.replace
    replace_calls = 0

    def fail_restore(source: object, target: object) -> None:
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls == 2:
            raise OSError("private-operating-system-message")
        real_replace(source, target)

    def fail_fsync(directory: Path) -> None:
        raise OSError("private-operating-system-message")

    monkeypatch.setattr(module.os, "replace", fail_restore)
    monkeypatch.setattr(module, "_fsync_directory", fail_fsync)

    with pytest.raises(module.PilotPreparationError) as failure:
        module._atomic_write(output, "replacement\n")

    assert failure.value.code == "output_unavailable"
    assert output.read_text(encoding="utf-8") == "existing\n"
    assert list(tmp_path.iterdir()) == [output]


def test_failed_backup_cleanup_restores_existing_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_script()
    output = tmp_path / "receipt.json"
    output.write_text("existing\n", encoding="utf-8")
    real_unlink = Path.unlink
    backup_unlinks = 0

    def fail_cleanup(
        path: Path,
        missing_ok: bool = False,
    ) -> None:
        nonlocal backup_unlinks
        if ".backup." in path.name:
            backup_unlinks += 1
            if backup_unlinks == 2:
                raise OSError("private-operating-system-message")
        real_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fail_cleanup)

    with pytest.raises(module.PilotPreparationError) as failure:
        module._atomic_write(output, "replacement\n")

    assert failure.value.code == "output_unavailable"
    assert output.read_text(encoding="utf-8") == "existing\n"
    assert list(tmp_path.iterdir()) == [output]


def test_checked_in_schemas_are_current_strict_and_require_semantic_check() -> None:
    module = _load_script()
    expected_intake, expected_readiness, expected_candidate = module.build_artifacts()

    actual_intake = json.loads(INTAKE_SCHEMA.read_text(encoding="utf-8"))
    actual_readiness = json.loads(READINESS_SCHEMA.read_text(encoding="utf-8"))
    actual_candidate = json.loads(CANDIDATE_SCHEMA.read_text(encoding="utf-8"))

    assert actual_intake == expected_intake
    assert actual_readiness == expected_readiness
    assert actual_candidate == expected_candidate
    for schema in (actual_intake, actual_readiness, actual_candidate):
        Draft202012Validator.check_schema(schema)
        assert schema["additionalProperties"] is False
        assert schema["x-semantic-validation"]["required"] is True
        assert "--check" in schema["x-semantic-validation"]["command"]
    readiness_properties = actual_readiness["properties"]
    assert readiness_properties["mall_product_id"]["type"] == "null"
    assert readiness_properties["bead_id"]["type"] == "null"
    assert readiness_properties["auto_publish"]["const"] is False
    assert readiness_properties["shared_market_opt_in"]["const"] is False
    candidate_properties = actual_candidate["properties"]
    assert candidate_properties["target_task_id"]["type"] == "null"
    assert candidate_properties["mall_product_id"]["type"] == "null"
    assert candidate_properties["bead_id"]["type"] == "null"
    assert candidate_properties["auto_publish"]["const"] is False
    assert candidate_properties["shared_market_opt_in"]["const"] is False
    assert "--readiness" in actual_candidate["x-semantic-validation"]["command"]
    assert (
        actual_candidate["x-semantic-validation"]["stable_error"] == "candidate_stale"
    )


def test_candidate_schema_is_required_by_artifact_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_script()
    missing = tmp_path / "missing-candidate-schema.json"
    monkeypatch.setattr(module, "CANDIDATE_SCHEMA_PATH", missing)

    with pytest.raises(module.PilotPreparationError) as failure:
        module._artifacts(check=True)

    assert failure.value.code == "artifact_stale"


def test_artifact_modes_are_exact_and_mutually_exclusive(tmp_path: Path) -> None:
    assert _run("--check-artifacts").returncode == 0
    invalid = _run(
        "--check-artifacts",
        "--output",
        str(tmp_path / "receipt.json"),
    )
    assert invalid.returncode == 2
    assert invalid.stdout == ""
    assert invalid.stderr == "invalid_arguments\n"


def test_pilot_slice_contains_no_real_manifest_receipt_or_binary_media() -> None:
    forbidden_suffixes = {
        ".heic",
        ".heif",
        ".jpeg",
        ".jpg",
        ".png",
        ".webp",
        ".xlsx",
        ".zip",
    }
    offenders: list[str] = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(ROOT)
        if any(part in {".git", ".venv", "build"} for part in relative.parts):
            continue
        if relative.parts[0] == "apps":
            continue
        normalized_name = path.name.casefold()
        if path.suffix.casefold() in forbidden_suffixes:
            offenders.append(relative.as_posix())
        if (
            any(
                marker in normalized_name
                for marker in ("real-sku-pilot", "controlled-upload-candidate")
            )
            and path.suffix == ".json"
            and "schema" not in normalized_name
        ):
            offenders.append(relative.as_posix())

    assert offenders == []
