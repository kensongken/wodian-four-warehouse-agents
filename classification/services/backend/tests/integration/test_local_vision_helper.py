import platform
from pathlib import Path

import pytest

from product_engine.services.on_device_vision import (
    ensure_local_vision_executable,
    run_local_vision,
)

pytestmark = pytest.mark.skipif(
    platform.system() != "Darwin",
    reason="Apple Vision is available only on macOS",
)


@pytest.mark.asyncio
async def test_real_swift_helper_reads_a_controlled_green_image(tmp_path: Path) -> None:
    backend_root = Path(__file__).resolve().parents[2]
    source = backend_root / "src" / "product_engine" / "native" / "local_vision.swift"
    executable = ensure_local_vision_executable(source, tmp_path / "local-vision")
    pixels = bytes([25, 45, 90]) * 40 + bytes([235, 235, 235]) * 35 + bytes([20, 180, 70]) * 25
    ppm = b"P6\n10 10\n255\n" + pixels

    result = await run_local_vision(executable, ppm, "image/x-portable-pixmap")

    assert result.image_width == 10
    assert result.image_height == 10
    assert result.dominant_color == "green"
    assert isinstance(result.classifications, list)
