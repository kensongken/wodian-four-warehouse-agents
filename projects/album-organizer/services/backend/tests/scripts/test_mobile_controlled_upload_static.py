from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from shutil import copyfile

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "scripts/verify_mobile_controlled_upload_static.py"


def test_offline_static_entry_verifies_controlled_upload_handoff() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == "mobile_controlled_upload_static_ok\n"
    assert result.stderr == ""


def test_offline_static_entry_rejects_a_missing_handoff_step(tmp_path: Path) -> None:
    for relative in (
        "apps/mobile/lib/features/controlled_upload/controlled_upload_candidate.dart",
        "apps/mobile/lib/features/controlled_upload/controlled_candidate_handoff_service.dart",
        "apps/mobile/lib/features/controlled_upload/controlled_upload_binding_repository.dart",
        "apps/mobile/lib/core/upload/upload_queue_repository.dart",
    ):
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        copyfile(ROOT / relative, destination)
    handoff = (
        tmp_path
        / "apps/mobile/lib/features/controlled_upload/controlled_candidate_handoff_service.dart"
    )
    handoff.write_text(
        handoff.read_text(encoding="utf-8").replace(
            "_queue.enqueueControlledCandidate(",
            "_queue.enqueue(",
            1,
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(tmp_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "mobile_controlled_upload_static_invalid\n"


def test_offline_static_entry_rejects_out_of_order_handoff_steps(
    tmp_path: Path,
) -> None:
    for relative in (
        "apps/mobile/lib/features/controlled_upload/controlled_upload_candidate.dart",
        "apps/mobile/lib/features/controlled_upload/controlled_candidate_handoff_service.dart",
        "apps/mobile/lib/features/controlled_upload/controlled_upload_binding_repository.dart",
        "apps/mobile/lib/core/upload/upload_queue_repository.dart",
    ):
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        copyfile(ROOT / relative, destination)
    handoff = (
        tmp_path
        / "apps/mobile/lib/features/controlled_upload/controlled_candidate_handoff_service.dart"
    )
    source = handoff.read_text(encoding="utf-8")
    source = source.replace(
        "    approval.requireCandidate(candidate.candidateId);\n",
        "",
        1,
    ).replace(
        "    final localById = <String, LocalAssetRef>{\n",
        "    final localById = <String, LocalAssetRef>{\n",
        1,
    )
    source = source.replace(
        "    final binding = await _binding.bind(\n",
        "    approval.requireCandidate(candidate.candidateId);\n"
        "    final binding = await _binding.bind(\n",
        1,
    )
    handoff.write_text(source, encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(tmp_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "mobile_controlled_upload_static_invalid\n"


def test_offline_static_entry_rejects_comment_masked_handoff_order(
    tmp_path: Path,
) -> None:
    for relative in (
        "apps/mobile/lib/features/controlled_upload/controlled_upload_candidate.dart",
        "apps/mobile/lib/features/controlled_upload/controlled_candidate_handoff_service.dart",
        "apps/mobile/lib/features/controlled_upload/controlled_upload_binding_repository.dart",
        "apps/mobile/lib/core/upload/upload_queue_repository.dart",
    ):
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        copyfile(ROOT / relative, destination)
    handoff = (
        tmp_path
        / "apps/mobile/lib/features/controlled_upload/controlled_candidate_handoff_service.dart"
    )
    source = handoff.read_text(encoding="utf-8")
    source = source.replace(
        "    final localById = <String, LocalAssetRef>{\n",
        "    // _mediaLibrary.computeSha256(local)\n"
        "    final localById = <String, LocalAssetRef>{\n",
        1,
    ).replace(
        "        actualDigest = await _mediaLibrary.computeSha256(local);\n",
        "        actualDigest = await _mediaLibrary\n"
        "            .computeSha256(local);\n",
        1,
    )
    handoff.write_text(source, encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(tmp_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "mobile_controlled_upload_static_invalid\n"


def test_offline_static_entry_redacts_invalid_arguments() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--unexpected", "sensitive-value"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "mobile_controlled_upload_static_invalid\n"
