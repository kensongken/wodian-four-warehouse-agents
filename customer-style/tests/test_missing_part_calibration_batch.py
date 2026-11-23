from __future__ import annotations

import stat
from pathlib import Path

from PIL import Image, ImageDraw

from opc_style.services.missing_part_calibration_batch import build_calibration_batch


def test_batch_proposals_remain_pending_and_non_training(tmp_path: Path) -> None:
    source_dir = tmp_path / "sources"
    foreground_root = tmp_path / "foreground"
    output_dir = tmp_path / "output"
    source_dir.mkdir()
    (foreground_root / "bracelet-001").mkdir(parents=True)

    source = Image.new("RGB", (256, 256), (225, 225, 225))
    draw = ImageDraw.Draw(source)
    draw.ellipse((60, 60, 196, 196), outline=(180, 30, 30), width=18)
    source.save(source_dir / "bracelet-001.jpg", quality=95)
    foreground = Image.new("L", source.size, 0)
    ImageDraw.Draw(foreground).ellipse((60, 60, 196, 196), outline=255, width=18)
    foreground.save(foreground_root / "bracelet-001" / "mask.png")

    manifest = build_calibration_batch(
        source_dir=source_dir,
        foreground_root=foreground_root,
        output_dir=output_dir,
        category="bracelet",
        part_kind="bead_edge",
    )

    assert manifest["source_count"] == 1
    assert manifest["proposal_count"] == 2
    assert manifest["pending_human_review"] == 2
    assert manifest["training_eligible_count"] == 0
    assert manifest["provider_call_count"] == 0
    records = manifest["records"]
    assert isinstance(records, list)
    assert {record["proposal_kind"] for record in records} == {
        "structure_edge",
        "background",
    }
    assert all(record["human_review_state"] == "pending" for record in records)
    assert all(record["human_label"] is None for record in records)
    assert all(record["training_eligible"] is False for record in records)
    assert all(record["provider_called"] is False for record in records)
    assert (output_dir / "manifest.json").is_file()
    assert (output_dir / "contact-sheet.jpg").is_file()
    assert stat.S_IMODE(output_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((output_dir / "manifest.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((output_dir / "contact-sheet.jpg").stat().st_mode) == 0o600
