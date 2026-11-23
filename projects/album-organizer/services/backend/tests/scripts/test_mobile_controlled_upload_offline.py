from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from shutil import copyfile

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "scripts/verify_mobile_controlled_upload_offline.py"


def test_offline_entry_reports_a_valid_chain_and_missing_sdk() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == "mobile_controlled_upload_offline_ok sdk=missing\n"
    assert result.stderr == ""


def test_offline_entry_rejects_a_broken_source_snapshot(tmp_path: Path) -> None:
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
    assert result.stderr == "mobile_controlled_upload_offline_invalid\n"
