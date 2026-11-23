from __future__ import annotations

import json
import os
import subprocess
import sys
from hashlib import sha256
from pathlib import Path
from uuid import UUID

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from photo_organizer.catalog.standard_product_package import (
    write_standard_product_package,
)
from photo_organizer.catalog.standard_product_package_acceptance import (
    open_verified_standard_product_package,
)
from photo_organizer.schemas.mall_draft_export import MallProductDraftBatch

ROOT = Path(__file__).resolve().parents[4]
SCRIPT_PATH = ROOT / "scripts/prepare_opc_mall_draft_import.py"
TEN_ITEM_BATCH = ROOT / "contracts/fixtures/opc-mall-product-draft-batch-synthetic.json"


def _current_one_item_batch(
    tmp_path: Path,
) -> tuple[MallProductDraftBatch, dict[UUID, Path]]:
    source = MallProductDraftBatch.model_validate_json(TEN_ITEM_BATCH.read_text())
    images = []
    image_paths: dict[UUID, Path] = {}
    for image in source.items[0].images:
        content = b"\xff\xd8\xff" + image.asset_id.bytes
        path = tmp_path / f"source-{image.asset_id}.jpg"
        path.write_bytes(content)
        image_paths[image.asset_id] = path
        images.append(
            image.model_copy(
                update={
                    "sha256": sha256(content).hexdigest(),
                    "byte_size": len(content),
                    "media_type": "image/jpeg",
                }
            )
        )
    item = source.items[0].model_copy(update={"images": tuple(images)})
    payload = source.model_dump(mode="json")
    payload["items"] = [item.model_dump(mode="json")]
    canonical = json.dumps(
        {
            "opc_id": payload["opc_id"],
            "data_classification": payload["data_classification"],
            "export_version": 1,
            "operation": "create_draft_only",
            "existing_product_policy": "emit_change_proposal_do_not_overwrite",
            "auto_publish": False,
            "shared_market_opt_in": False,
            "items": payload["items"],
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    payload["batch_id"] = f"mall-draft-batch:{sha256(canonical.encode()).hexdigest()}"
    return MallProductDraftBatch.model_validate(payload), image_paths


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _input_paths(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path]:
    batch, image_paths = _current_one_item_batch(tmp_path)
    package = tmp_path / "package.zip"
    write_standard_product_package(batch, image_paths, package)
    with open_verified_standard_product_package(package) as verified:
        acceptance = verified.receipt
    acceptance_path = tmp_path / "package-acceptance.json"
    batch_path = tmp_path / "mall-draft-batch.json"
    plan_path = tmp_path / "import-plan.json"
    receipt_path = tmp_path / "import-receipt.json"
    acceptance_path.write_text(acceptance.model_dump_json(), encoding="utf-8")
    batch_path.write_text(batch.model_dump_json(), encoding="utf-8")
    return package, acceptance_path, batch_path, plan_path, receipt_path


def _cli_args(paths: tuple[Path, Path, Path, Path, Path]) -> list[str]:
    package, acceptance, batch, plan, receipt = paths
    return [
        "--package",
        str(package),
        "--package-acceptance",
        str(acceptance),
        "--batch",
        str(batch),
        "--plan-output",
        str(plan),
        "--receipt-output",
        str(receipt),
    ]


def test_cli_writes_only_a_prepared_dry_run_pair(tmp_path: Path) -> None:
    batch, image_paths = _current_one_item_batch(tmp_path)
    package = tmp_path / "package.zip"
    write_standard_product_package(batch, image_paths, package)
    with open_verified_standard_product_package(package) as verified:
        acceptance = verified.receipt
    acceptance_path = tmp_path / "package-acceptance.json"
    batch_path = tmp_path / "mall-draft-batch.json"
    plan_path = tmp_path / "import-plan.json"
    receipt_path = tmp_path / "import-receipt.json"
    acceptance_path.write_text(acceptance.model_dump_json(), encoding="utf-8")
    batch_path.write_text(batch.model_dump_json(), encoding="utf-8")

    result = _run(
        "--package",
        str(package),
        "--package-acceptance",
        str(acceptance_path),
        "--batch",
        str(batch_path),
        "--plan-output",
        str(plan_path),
        "--receipt-output",
        str(receipt_path),
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == result.stderr == ""
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert plan["operation"] == "create_draft_only"
    assert receipt["execution_status"] == "prepared"
    assert {item["status"] for item in receipt["results"]} == {"prepared"}
    serialized = json.dumps({"plan": plan, "receipt": receipt})
    for forbidden in (
        "http://",
        "https://",
        "token",
        "private_key",
        "customer_id",
        "customer_name",
        "order_id",
        "chat_text",
        "source-",
        ".jpg",
        "package.zip",
        "ff d8 ff",
    ):
        assert forbidden not in serialized


def test_check_is_read_only_and_detects_each_stale_artifact(tmp_path: Path) -> None:
    paths = _input_paths(tmp_path)
    assert _run(*_cli_args(paths)).returncode == 0
    before = tuple(path.stat().st_mtime_ns for path in paths[3:])
    checked = _run(*_cli_args(paths), "--check")
    assert checked.returncode == 0
    assert tuple(path.stat().st_mtime_ns for path in paths[3:]) == before

    paths[3].write_text("{}\n", encoding="utf-8")
    stale_plan = _run(*_cli_args(paths), "--check")
    assert stale_plan.returncode == 1
    assert stale_plan.stdout == ""
    assert stale_plan.stderr == "import_artifact_stale\n"

    assert _run(*_cli_args(paths)).returncode == 0
    paths[4].write_text("{}\n", encoding="utf-8")
    stale_receipt = _run(*_cli_args(paths), "--check")
    assert stale_receipt.returncode == 1
    assert stale_receipt.stdout == ""
    assert stale_receipt.stderr == "import_artifact_stale\n"


def test_cli_rejects_unknown_secret_and_url_arguments_without_echoing_them() -> None:
    secret = "https://operator:private-token@mall.invalid/import"

    result = _run("--database-url", secret)

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == "invalid_arguments\n"
    assert secret not in result.stderr


@pytest.mark.parametrize(
    "url",
    (
        "HTTPS://operator:private-token@mall.invalid/import",
        "hTtP://operator:private-token@mall.invalid/import",
    ),
)
def test_cli_rejects_case_independent_http_scheme_without_echoing_it(
    url: str,
) -> None:
    result = _run(
        "--package",
        url,
        "--package-acceptance",
        "acceptance.json",
        "--batch",
        "batch.json",
        "--plan-output",
        "plan.json",
        "--receipt-output",
        "receipt.json",
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == "invalid_arguments\n"
    assert url not in result.stderr


@pytest.mark.parametrize("input_index", (0, 1, 2))
@pytest.mark.parametrize("kind", ("directory", "fifo", "symlink"))
def test_cli_rejects_non_regular_inputs_with_a_stable_redacted_code(
    tmp_path: Path, input_index: int, kind: str
) -> None:
    paths = list(_input_paths(tmp_path))
    bad = tmp_path / f"secret-input-{kind}-{input_index}"
    if kind == "directory":
        bad.mkdir()
    elif kind == "fifo":
        os.mkfifo(bad)
    else:
        bad.symlink_to(paths[input_index])
    paths[input_index] = bad

    result = _run(*_cli_args(tuple(paths)))

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == "invalid_input\n"
    assert str(bad) not in result.stderr


@pytest.mark.parametrize(
    ("left_index", "right_index"),
    ((0, 1), (0, 2), (0, 3), (0, 4), (1, 2), (1, 3), (1, 4), (2, 3), (2, 4), (3, 4)),
)
def test_cli_rejects_input_output_path_collisions(
    tmp_path: Path, left_index: int, right_index: int
) -> None:
    paths = list(_input_paths(tmp_path))
    paths[right_index] = paths[left_index]

    result = _run(*_cli_args(tuple(paths)))

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == "invalid_arguments\n"


@pytest.mark.parametrize("content", ("{", "[" * 65 + "]" * 65))
def test_cli_rejects_malformed_or_deep_json_without_echoing_it(
    tmp_path: Path, content: str
) -> None:
    paths = list(_input_paths(tmp_path))
    paths[1].write_text(content, encoding="utf-8")

    result = _run(*_cli_args(tuple(paths)))

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == "invalid_input\n"
    assert content not in result.stderr


def test_pair_write_restores_the_prior_pair_when_the_second_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location("mall_preparer", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load preparer")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    plan = tmp_path / "plan.json"
    receipt = tmp_path / "receipt.json"
    plan.write_text("old-plan\n", encoding="utf-8")
    receipt.write_text("old-receipt\n", encoding="utf-8")
    original_replace = os.replace
    calls = 0

    def fail_second_replace(source: object, destination: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("private failure")
        original_replace(source, destination)

    monkeypatch.setattr(module.os, "replace", fail_second_replace)

    with pytest.raises(module.ImportPreparationError) as error:
        module._atomic_write_pair(plan, receipt, "new-plan\n", "new-receipt\n")

    assert error.value.code == "output_unavailable"
    assert plan.read_text(encoding="utf-8") == "old-plan\n"
    assert receipt.read_text(encoding="utf-8") == "old-receipt\n"


def test_pair_write_uses_fallback_restore_after_receipt_and_restore_replaces_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "mall_preparer_restore_fallback", SCRIPT_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load preparer")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    plan = tmp_path / "plan.json"
    receipt = tmp_path / "receipt.json"
    plan.write_text("old-plan\n", encoding="utf-8")
    receipt.write_text("old-receipt\n", encoding="utf-8")
    original_replace = os.replace
    calls = 0

    def fail_receipt_and_restore(source: object, destination: object) -> None:
        nonlocal calls
        calls += 1
        if calls in {2, 3}:
            raise OSError("private failure")
        original_replace(source, destination)

    monkeypatch.setattr(module.os, "replace", fail_receipt_and_restore)

    with pytest.raises(module.ImportPreparationError) as error:
        module._atomic_write_pair(plan, receipt, "new-plan\n", "new-receipt\n")

    assert error.value.code == "output_unavailable"
    assert plan.read_text(encoding="utf-8") == "old-plan\n"
    assert receipt.read_text(encoding="utf-8") == "old-receipt\n"
    assert not list(tmp_path.glob(".*.backup.*"))
    assert not list(tmp_path.glob(".*.json.*"))


def test_pair_write_restores_from_backup_when_atomic_restore_methods_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "mall_preparer_backup_restore", SCRIPT_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load preparer")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    plan = tmp_path / "plan.json"
    receipt = tmp_path / "receipt.json"
    plan.write_text("old-plan\n", encoding="utf-8")
    receipt.write_text("old-receipt\n", encoding="utf-8")
    original_replace = os.replace
    calls = 0

    def fail_receipt_and_restore(source: object, destination: object) -> None:
        nonlocal calls
        calls += 1
        if calls in {2, 3}:
            raise OSError("private failure")
        original_replace(source, destination)

    def fail_rename(source: object, destination: object) -> None:
        raise OSError("private failure")

    monkeypatch.setattr(module.os, "replace", fail_receipt_and_restore)
    monkeypatch.setattr(module.os, "rename", fail_rename)

    with pytest.raises(module.ImportPreparationError) as error:
        module._atomic_write_pair(plan, receipt, "new-plan\n", "new-receipt\n")

    assert error.value.code == "output_unavailable"
    assert plan.read_text(encoding="utf-8") == "old-plan\n"
    assert receipt.read_text(encoding="utf-8") == "old-receipt\n"
    assert not list(tmp_path.glob(".*.backup.*"))
    assert not list(tmp_path.glob(".*.json.*"))


def test_pair_write_failure_before_replacement_preserves_the_prior_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location("mall_preparer_prepare", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load preparer")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    plan = tmp_path / "plan.json"
    receipt = tmp_path / "receipt.json"
    plan.write_text("old-plan\n", encoding="utf-8")
    receipt.write_text("old-receipt\n", encoding="utf-8")
    original_temporary = module._write_temporary
    calls = 0

    def fail_second_temporary(path: Path, content: str) -> Path:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise module.ImportPreparationError("output_unavailable")
        return original_temporary(path, content)

    monkeypatch.setattr(module, "_write_temporary", fail_second_temporary)

    with pytest.raises(module.ImportPreparationError) as error:
        module._atomic_write_pair(plan, receipt, "new-plan\n", "new-receipt\n")

    assert error.value.code == "output_unavailable"
    assert plan.read_text(encoding="utf-8") == "old-plan\n"
    assert receipt.read_text(encoding="utf-8") == "old-receipt\n"


def test_subprocess_crash_between_pair_publications_recovers_before_cli_check(
    tmp_path: Path,
) -> None:
    """Catches a process death leaving a new plan beside an old receipt."""
    paths = _input_paths(tmp_path)
    assert _run(*_cli_args(paths)).returncode == 0
    plan_path, receipt_path = paths[3:]
    old_plan = plan_path.read_bytes()
    old_receipt = receipt_path.read_bytes()
    crash_script = """
import importlib.util
import os
import sys
from pathlib import Path

script_path = Path(sys.argv[1])
plan_path = Path(sys.argv[2])
receipt_path = Path(sys.argv[3])
spec = importlib.util.spec_from_file_location("crashing_mall_preparer", script_path)
if spec is None or spec.loader is None:
    raise SystemExit(90)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
original_replace = module.os.replace

def crash_after_plan_publish(source, destination):
    original_replace(source, destination)
    if Path(destination) == plan_path:
        os._exit(86)

module.os.replace = crash_after_plan_publish
module._atomic_write_pair(
    plan_path,
    receipt_path,
    "crash-window-plan\\n",
    "crash-window-receipt\\n",
)
"""

    crashed = subprocess.run(
        [
            sys.executable,
            "-c",
            crash_script,
            str(SCRIPT_PATH),
            str(plan_path),
            str(receipt_path),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert crashed.returncode == 86
    assert plan_path.read_bytes() != old_plan
    assert receipt_path.read_bytes() == old_receipt
    journals = list(tmp_path.glob(".opc-mall-draft-pair-*.txn"))
    journal_contents = [journal.read_text(encoding="utf-8") for journal in journals]

    recovered = _run(*_cli_args(paths), "--check")

    assert recovered.returncode == 0, recovered.stderr
    assert recovered.stdout == recovered.stderr == ""
    assert plan_path.read_bytes() == old_plan
    assert receipt_path.read_bytes() == old_receipt
    assert len(journal_contents) == 1
    assert str(plan_path) not in journal_contents[0]
    assert str(receipt_path) not in journal_contents[0]
    assert plan_path.name not in journal_contents[0]
    assert receipt_path.name not in journal_contents[0]
    assert not list(tmp_path.glob(".opc-mall-draft-pair-*.txn"))


def test_schema_artifacts_are_strict_and_semantic_check_catches_valid_tampering(
    tmp_path: Path,
) -> None:
    paths = _input_paths(tmp_path)
    assert _run(*_cli_args(paths)).returncode == 0
    written = _run("--write-artifacts")
    checked = _run("--check-artifacts")
    assert written.returncode == checked.returncode == 0
    plan_schema = json.loads(
        (ROOT / "contracts/opc-mall-draft-import-plan-v1.schema.json").read_text()
    )
    receipt_schema = json.loads(
        (ROOT / "contracts/opc-mall-draft-import-receipt-v1.schema.json").read_text()
    )
    plan = json.loads(paths[3].read_text())
    receipt = json.loads(paths[4].read_text())
    Draft202012Validator(plan_schema, format_checker=FormatChecker()).validate(plan)
    Draft202012Validator(receipt_schema, format_checker=FormatChecker()).validate(
        receipt
    )
    assert plan_schema["additionalProperties"] is False
    assert receipt_schema["additionalProperties"] is False
    plan["plan_id"] = "mall-draft-import-plan:" + "0" * 64
    Draft202012Validator(plan_schema, format_checker=FormatChecker()).validate(plan)
    paths[3].write_text(json.dumps(plan), encoding="utf-8")

    result = _run(*_cli_args(paths), "--check")

    assert result.returncode == 1
    assert result.stderr == "import_artifact_stale\n"
