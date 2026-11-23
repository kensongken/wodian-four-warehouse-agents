"""Deterministically choose a compact, diverse style-reference set."""

import json
from dataclasses import dataclass
from hashlib import sha256
from typing import cast
from uuid import UUID

from opc_style.domain.analyzer import VisualStyleSample, decode_visual_sample

REFERENCE_SELECTION_VERSION = "pixel-diversity-farthest-v1"
MAX_SELECTED_STYLE_REFERENCES = 8


@dataclass(frozen=True)
class StyleReferenceSelection:
    selected: tuple[VisualStyleSample, ...]
    library_sample_ids: tuple[UUID, ...]
    algorithm_version: str
    manifest_sha256: str


def select_representative_style_samples(
    samples: tuple[VisualStyleSample, ...],
) -> StyleReferenceSelection:
    """Keep the full manifest while selecting at most eight unique pixel examples."""
    ordered = tuple(sorted(samples, key=lambda sample: str(sample.sample_id)))
    features = {sample.sample_id: _pixel_feature(sample) for sample in ordered}
    unique_by_digest: dict[str, VisualStyleSample] = {}
    for sample in ordered:
        unique_by_digest.setdefault(sample.content_sha256, sample)
    candidates = tuple(unique_by_digest.values())
    if len(candidates) <= MAX_SELECTED_STYLE_REFERENCES:
        selected = candidates
    else:
        selected_rows: list[VisualStyleSample] = []
        first = max(
            candidates,
            key=lambda candidate: (
                sum(
                    _distance(
                        features[candidate.sample_id], features[other.sample_id]
                    )
                    for other in candidates
                ),
                -candidate.sample_id.int,
            ),
        )
        selected_rows.append(first)
        remaining = [candidate for candidate in candidates if candidate is not first]
        while len(selected_rows) < MAX_SELECTED_STYLE_REFERENCES:
            next_sample = max(
                remaining,
                key=lambda candidate: (
                    min(
                        _distance(
                            features[candidate.sample_id],
                            features[chosen.sample_id],
                        )
                        for chosen in selected_rows
                    ),
                    -candidate.sample_id.int,
                ),
            )
            selected_rows.append(next_sample)
            remaining.remove(next_sample)
        selected = tuple(selected_rows)
    manifest = {
        "algorithm_version": REFERENCE_SELECTION_VERSION,
        "library": [
            {"sample_id": str(sample.sample_id), "sha256": sample.content_sha256}
            for sample in ordered
        ],
        "selected_sample_ids": [str(sample.sample_id) for sample in selected],
    }
    manifest_sha256 = sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return StyleReferenceSelection(
        selected=selected,
        library_sample_ids=tuple(sample.sample_id for sample in ordered),
        algorithm_version=REFERENCE_SELECTION_VERSION,
        manifest_sha256=manifest_sha256,
    )


def _pixel_feature(sample: VisualStyleSample) -> tuple[int, ...]:
    with decode_visual_sample(sample) as image:
        width, height = image.size
        resized = image.resize((4, 4))
        rgb = tuple(
            channel
            for y in range(4)
            for x in range(4)
            for channel in cast(tuple[int, int, int], resized.getpixel((x, y)))
        )
    aspect = round(width * 255 / height)
    return (*rgb, aspect)


def _distance(left: tuple[int, ...], right: tuple[int, ...]) -> int:
    return sum((a - b) ** 2 for a, b in zip(left, right, strict=True))
