"""Representative style references keep large libraries within a safe request."""

from hashlib import sha256
from io import BytesIO
from uuid import UUID

import pytest
from PIL import Image

from opc_style.domain.analyzer import VisualStyleSample
from opc_style.domain.errors import DomainRuleError
from opc_style.domain.style_reference_selection import (
    REFERENCE_SELECTION_VERSION,
    select_representative_style_samples,
)


def _sample(index: int, color: tuple[int, int, int]) -> VisualStyleSample:
    stream = BytesIO()
    Image.new("RGB", (24 + index % 3, 28 + index % 5), color).save(
        stream, format="PNG"
    )
    content = stream.getvalue()
    return VisualStyleSample(
        UUID(int=index + 1), content, sha256(content).hexdigest(), "image/png"
    )


@pytest.mark.parametrize("count", [1, 4, 8])
def test_small_unique_library_keeps_every_reference(count: int) -> None:
    samples = tuple(_sample(index, (index * 20, 40, 80)) for index in range(count))

    result = select_representative_style_samples(samples)

    assert len(result.selected) == count
    assert result.library_sample_ids == tuple(sample.sample_id for sample in samples)
    assert result.algorithm_version == REFERENCE_SELECTION_VERSION
    assert len(result.manifest_sha256) == 64


@pytest.mark.parametrize("count", [15, 70])
def test_large_library_selects_eight_diverse_references_deterministically(
    count: int,
) -> None:
    samples = tuple(
        _sample(index, ((index * 37) % 256, (index * 67) % 256, (index * 97) % 256))
        for index in range(count)
    )

    first = select_representative_style_samples(samples)
    replay = select_representative_style_samples(tuple(reversed(samples)))

    assert len(first.selected) == 8
    assert tuple(sample.sample_id for sample in first.selected) == tuple(
        sample.sample_id for sample in replay.selected
    )
    assert first.manifest_sha256 == replay.manifest_sha256
    assert len(first.library_sample_ids) == count


def test_duplicate_pixels_cannot_crowd_out_reference_diversity() -> None:
    duplicated = _sample(0, (20, 20, 20))
    samples = (duplicated,) + tuple(
        VisualStyleSample(
            UUID(int=index + 2),
            duplicated.content,
            duplicated.content_sha256,
            duplicated.mime_type,
        )
        for index in range(20)
    ) + tuple(_sample(index + 30, (index * 30, 120, 210)) for index in range(7))

    result = select_representative_style_samples(samples)

    assert len(result.selected) == 8
    assert len({sample.content_sha256 for sample in result.selected}) == 8
    assert len(result.library_sample_ids) == len(samples)


def test_invalid_pixels_fail_before_selection() -> None:
    sample = VisualStyleSample(UUID(int=1), b"broken", sha256(b"broken").hexdigest(), "image/png")

    with pytest.raises(DomainRuleError, match="style_visual_analysis_unavailable"):
        select_representative_style_samples((sample,))
