"""Build zero-cost, human-review-only missing-part calibration proposals."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from hashlib import sha256
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

from opc_style.domain.errors import DomainRuleError
from opc_style.domain.missing_part_completion import (
    MissingPartCompletionRequest,
    measure_missing_part_mask,
)


@dataclass(frozen=True)
class CalibrationProposal:
    proposal_id: str
    source_name: str
    source_path: str
    source_sha256: str
    foreground_mask_sha256: str
    proposal_kind: str
    proposed_human_label: str
    missing_mask_path: str
    missing_mask_sha256: str
    overlay_path: str
    system_state: str
    state_matches_proposal: bool
    context_edge_mean: float
    strong_edge_fraction: float
    context_chroma_mean: float
    strong_chroma_fraction: float
    context_chroma_stddev: float
    category: str
    part_kind: str
    human_review_state: str = "pending"
    human_label: None = None
    training_eligible: bool = False
    provider_called: bool = False


def build_calibration_batch(
    *,
    source_dir: Path,
    foreground_root: Path,
    output_dir: Path,
    category: str,
    part_kind: str,
) -> dict[str, object]:
    """Create two proposals per source without treating either as truth."""

    sources = sorted(
        path
        for path in source_dir.iterdir()
        if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png"}
    )
    if not sources:
        raise DomainRuleError("missing_part_acquisition_sources_missing")
    output_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    output_dir.chmod(0o700)
    proposals: list[CalibrationProposal] = []
    for source_path in sources:
        foreground_path = foreground_root / source_path.stem / "mask.png"
        if not foreground_path.is_file():
            raise DomainRuleError("missing_part_acquisition_foreground_missing")
        proposals.extend(
            _build_source_proposals(
                source_path=source_path,
                foreground_path=foreground_path,
                output_dir=output_dir,
                category=category,
                part_kind=part_kind,
            )
        )
    manifest: dict[str, object] = {
        "schema_version": "missing-part-calibration-batch-v1",
        "category": category,
        "part_kind": part_kind,
        "source_count": len(sources),
        "proposal_count": len(proposals),
        "pending_human_review": len(proposals),
        "training_eligible_count": 0,
        "provider_call_count": 0,
        "automatic_threshold_update": False,
        "records": [asdict(proposal) for proposal in proposals],
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    manifest_path.chmod(0o600)
    _write_contact_sheet(proposals, output_dir)
    return manifest


def _build_source_proposals(
    *,
    source_path: Path,
    foreground_path: Path,
    output_dir: Path,
    category: str,
    part_kind: str,
) -> list[CalibrationProposal]:
    source_content = source_path.read_bytes()
    foreground_content = foreground_path.read_bytes()
    with Image.open(BytesIO(source_content)) as opened:
        source = opened.convert("RGB")
    with Image.open(BytesIO(foreground_content)) as opened:
        foreground = opened.convert("L").point(
            lambda value: 255 if value >= 128 else 0, mode="L"
        )
    if source.size != foreground.size:
        raise DomainRuleError("missing_part_acquisition_dimensions_invalid")

    preview = source.copy()
    preview.thumbnail((512, 512), Image.Resampling.LANCZOS)
    preview_foreground = foreground.resize(preview.size, Image.Resampling.NEAREST)
    radius = max(3, round(min(preview.size) * 0.009))
    centers = _rank_centers(preview, preview_foreground, radius)
    proposals = []
    for proposal_kind, expected_state, proposed_label in (
        ("structure_edge", "anchored_to_visible_structure", "confirmed_structure"),
        ("background", "blocked_unanchored", "confirmed_background"),
    ):
        candidates = centers[proposal_kind]
        if not candidates:
            raise DomainRuleError("missing_part_acquisition_candidate_missing")
        selected = None
        for preview_center in candidates[:20]:
            mask = _full_size_mask(source.size, preview.size, preview_center, radius)
            mask_content = _png_bytes(mask)
            request = MissingPartCompletionRequest(
                source_content=source_content,
                source_sha256=sha256(source_content).hexdigest(),
                source_mime_type=(
                    "image/png"
                    if source_path.suffix.lower() == ".png"
                    else "image/jpeg"
                ),
                missing_mask_png=mask_content,
                category=category,
                part_kind=part_kind,
                material_tags=(),
                case_id=f"proposal:{source_path.stem}:{proposal_kind}",
            )
            assessment = measure_missing_part_mask(request)
            selected = (mask, mask_content, assessment)
            if assessment.state == expected_state:
                break
        if selected is None:
            raise DomainRuleError("missing_part_acquisition_candidate_missing")
        mask, mask_content, assessment = selected
        proposal_id = f"{source_path.stem}-{proposal_kind}"
        mask_path = output_dir / f"{proposal_id}-mask.png"
        overlay_path = output_dir / f"{proposal_id}-overlay.jpg"
        mask_path.write_bytes(mask_content)
        mask_path.chmod(0o600)
        _write_overlay(source, mask, overlay_path, proposal_kind)
        overlay_path.chmod(0o600)
        proposals.append(
            CalibrationProposal(
                proposal_id=proposal_id,
                source_name=source_path.name,
                source_path=str(source_path),
                source_sha256=sha256(source_content).hexdigest(),
                foreground_mask_sha256=sha256(foreground_content).hexdigest(),
                proposal_kind=proposal_kind,
                proposed_human_label=proposed_label,
                missing_mask_path=mask_path.name,
                missing_mask_sha256=sha256(mask_content).hexdigest(),
                overlay_path=overlay_path.name,
                system_state=assessment.state,
                state_matches_proposal=assessment.state == expected_state,
                context_edge_mean=assessment.context_edge_mean,
                strong_edge_fraction=assessment.strong_edge_fraction,
                context_chroma_mean=assessment.context_chroma_mean,
                strong_chroma_fraction=assessment.strong_chroma_fraction,
                context_chroma_stddev=assessment.context_chroma_stddev,
                category=category,
                part_kind=part_kind,
            )
        )
    return proposals


def _rank_centers(
    source: Image.Image, foreground: Image.Image, radius: int
) -> dict[str, list[tuple[int, int]]]:
    edge = source.convert("L").filter(ImageFilter.FIND_EDGES)
    red, green, blue = source.split()
    chroma = ImageChops.lighter(
        ImageChops.difference(red, green),
        ImageChops.lighter(
            ImageChops.difference(red, blue), ImageChops.difference(green, blue)
        ),
    )
    safe_kernel = _odd_kernel(radius * 2 + 1, min(source.size))
    near_kernel = _odd_kernel(radius * 8 + 1, min(source.size))
    safe_foreground = foreground.filter(ImageFilter.MaxFilter(safe_kernel))
    near_foreground = foreground.filter(ImageFilter.MaxFilter(near_kernel))
    step = max(4, radius)
    structure: list[tuple[float, tuple[int, int]]] = []
    background: list[tuple[float, tuple[int, int]]] = []
    for y in range(radius * 2, source.height - radius * 2, step):
        for x in range(radius * 2, source.width - radius * 2, step):
            if safe_foreground.getpixel((x, y)) != 0:
                continue
            score = _window_mean(edge, x, y, radius * 2) + _window_mean(
                chroma, x, y, radius * 2
            )
            if near_foreground.getpixel((x, y)) != 0:
                structure.append((score, (x, y)))
            else:
                background.append((score, (x, y)))
    structure.sort(key=lambda item: item[0], reverse=True)
    background.sort(key=lambda item: item[0])
    return {
        "structure_edge": [center for _score, center in structure],
        "background": [center for _score, center in background],
    }


def _window_mean(image: Image.Image, x: int, y: int, radius: int) -> float:
    box = (
        max(0, x - radius),
        max(0, y - radius),
        min(image.width, x + radius + 1),
        min(image.height, y + radius + 1),
    )
    histogram = image.crop(box).histogram()
    count = sum(histogram)
    return sum(value * amount for value, amount in enumerate(histogram)) / count


def _odd_kernel(requested: int, maximum: int) -> int:
    value = min(requested, maximum if maximum % 2 == 1 else maximum - 1)
    return max(3, value if value % 2 == 1 else value - 1)


def _full_size_mask(
    full_size: tuple[int, int],
    preview_size: tuple[int, int],
    center: tuple[int, int],
    preview_radius: int,
) -> Image.Image:
    scale_x = full_size[0] / preview_size[0]
    scale_y = full_size[1] / preview_size[1]
    x = round(center[0] * scale_x)
    y = round(center[1] * scale_y)
    radius = max(2, round(preview_radius * min(scale_x, scale_y)))
    mask = Image.new("L", full_size, 0)
    ImageDraw.Draw(mask).ellipse(
        (x - radius, y - radius, x + radius, y + radius), fill=255
    )
    return mask


def _png_bytes(image: Image.Image) -> bytes:
    output = BytesIO()
    image.save(output, format="PNG", optimize=False)
    return output.getvalue()


def _write_overlay(
    source: Image.Image, mask: Image.Image, path: Path, proposal_kind: str
) -> None:
    preview = source.copy()
    preview.thumbnail((900, 900), Image.Resampling.LANCZOS)
    preview_mask = mask.resize(preview.size, Image.Resampling.NEAREST)
    tint = Image.new("RGB", preview.size, (230, 40, 40))
    overlay = Image.blend(preview, tint, 0.55)
    composed = Image.composite(overlay, preview, preview_mask)
    draw = ImageDraw.Draw(composed)
    draw.rectangle((0, 0, composed.width, 34), fill=(255, 255, 255))
    draw.text(
        (10, 10),
        f"PROPOSAL ONLY / {proposal_kind} / HUMAN REVIEW REQUIRED",
        fill=(120, 20, 20),
    )
    composed.save(path, format="JPEG", quality=90)


def _write_contact_sheet(
    proposals: list[CalibrationProposal], output_dir: Path
) -> None:
    tile_width, tile_height = 320, 360
    columns = 4
    rows = (len(proposals) + columns - 1) // columns
    sheet = Image.new("RGB", (columns * tile_width, rows * tile_height), "white")
    for index, proposal in enumerate(proposals):
        with Image.open(output_dir / proposal.overlay_path) as opened:
            tile = opened.convert("RGB")
            tile.thumbnail((300, 300), Image.Resampling.LANCZOS)
        x = (index % columns) * tile_width + 10
        y = (index // columns) * tile_height + 10
        sheet.paste(tile, (x, y))
        ImageDraw.Draw(sheet).text(
            (x, y + 305),
            f"{proposal.proposal_id} / {proposal.system_state}",
            fill="black",
        )
    contact_sheet = output_dir / "contact-sheet.jpg"
    sheet.save(contact_sheet, format="JPEG", quality=88)
    contact_sheet.chmod(0o600)
