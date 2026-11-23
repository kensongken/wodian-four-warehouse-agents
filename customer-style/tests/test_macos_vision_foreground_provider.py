from __future__ import annotations

import os
import subprocess
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from uuid import UUID

import pytest
from PIL import Image

from opc_style.domain.errors import DomainRuleError
from opc_style.domain.product_foreground import ForegroundExtractionRequest
from opc_style.providers.macos_vision_foreground import MacOSVisionForegroundExtractor


def _png(mode: str, size: tuple[int, int], box: tuple[int, int, int, int]) -> bytes:
    image = Image.new(mode, size, 0 if mode == "L" else (0, 0, 0, 0))
    fill = 255 if mode == "L" else (15, 120, 75, 255)
    for x in range(box[0], box[2]):
        for y in range(box[1], box[3]):
            image.putpixel((x, y), fill)
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _request() -> ForegroundExtractionRequest:
    source = _png("RGBA", (8, 8), (2, 2, 6, 6))
    return ForegroundExtractionRequest(
        customer_id=UUID("00000000-0000-0000-0000-000000000001"),
        asset_id=UUID("00000000-0000-0000-0000-000000000002"),
        product_id="bracelet-1",
        source_content=source,
        source_content_sha256=sha256(source).hexdigest(),
        mime_type="image/png",
        width=8,
        height=8,
    )


def _binary(tmp_path: Path) -> Path:
    binary = tmp_path / "macos-vision-foreground"
    binary.write_bytes(b"fake executable")
    binary.chmod(0o700)
    return binary


def _success_runner(command, **kwargs):
    del kwargs
    mask_path = Path(command[command.index("--mask-output") + 1])
    foreground_path = Path(command[command.index("--foreground-output") + 1])
    mask_path.write_bytes(_png("L", (8, 8), (2, 2, 6, 6)))
    foreground_path.write_bytes(_png("RGBA", (8, 8), (2, 2, 6, 6)))
    return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")


def test_missing_binary_is_unavailable_without_leaking_path(tmp_path: Path) -> None:
    missing = tmp_path / "private-customer-path" / "missing"
    extractor = MacOSVisionForegroundExtractor(
        binary_path=missing,
        platform_name="darwin",
    )

    with pytest.raises(DomainRuleError) as exc_info:
        extractor.extract(_request())
    assert exc_info.value.code == "foreground_extractor_unavailable"
    assert str(missing) not in str(exc_info.value)


def test_non_macos_runtime_is_unavailable(tmp_path: Path) -> None:
    extractor = MacOSVisionForegroundExtractor(
        binary_path=_binary(tmp_path),
        platform_name="linux",
    )

    with pytest.raises(DomainRuleError) as exc_info:
        extractor.extract(_request())
    assert exc_info.value.code == "foreground_extractor_unavailable"


def test_output_symlink_outside_temporary_directory_is_rejected(tmp_path: Path) -> None:
    leaked_output = tmp_path / "outside-mask.png"

    def runner(command, **kwargs):
        del kwargs
        mask_path = Path(command[command.index("--mask-output") + 1])
        foreground_path = Path(command[command.index("--foreground-output") + 1])
        leaked_output.write_bytes(_png("L", (8, 8), (2, 2, 6, 6)))
        mask_path.symlink_to(leaked_output)
        foreground_path.write_bytes(_png("RGBA", (8, 8), (2, 2, 6, 6)))
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    extractor = MacOSVisionForegroundExtractor(
        binary_path=_binary(tmp_path), platform_name="darwin", runner=runner
    )

    with pytest.raises(DomainRuleError) as exc_info:
        extractor.extract(_request())
    assert exc_info.value.code == "foreground_extraction_failed"
    assert str(leaked_output) not in str(exc_info.value)


def test_nonzero_exit_hides_stderr_paths_and_image_bytes(tmp_path: Path) -> None:
    secret = "/private/customer/source.png"

    def runner(command, **kwargs):
        del kwargs
        return subprocess.CompletedProcess(
            command,
            9,
            stdout=_request().source_content,
            stderr=f"failed at {secret}".encode(),
        )

    extractor = MacOSVisionForegroundExtractor(
        binary_path=_binary(tmp_path), platform_name="darwin", runner=runner
    )

    with pytest.raises(DomainRuleError) as exc_info:
        extractor.extract(_request())
    assert exc_info.value.code == "foreground_extraction_failed"
    assert secret not in str(exc_info.value)
    assert "PNG" not in str(exc_info.value)


def test_timeout_is_sanitized(tmp_path: Path) -> None:
    def runner(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"], stderr=b"private")

    extractor = MacOSVisionForegroundExtractor(
        binary_path=_binary(tmp_path), platform_name="darwin", runner=runner
    )

    with pytest.raises(DomainRuleError) as exc_info:
        extractor.extract(_request())
    assert exc_info.value.code == "foreground_extraction_failed"
    assert "private" not in str(exc_info.value)


def test_oversized_output_is_rejected(tmp_path: Path) -> None:
    extractor = MacOSVisionForegroundExtractor(
        binary_path=_binary(tmp_path),
        platform_name="darwin",
        runner=_success_runner,
        max_output_bytes=16,
    )

    with pytest.raises(DomainRuleError) as exc_info:
        extractor.extract(_request())
    assert exc_info.value.code == "foreground_extraction_failed"


def test_malformed_png_is_rejected(tmp_path: Path) -> None:
    def runner(command, **kwargs):
        del kwargs
        mask_path = Path(command[command.index("--mask-output") + 1])
        foreground_path = Path(command[command.index("--foreground-output") + 1])
        mask_path.write_bytes(b"not-a-png")
        foreground_path.write_bytes(b"also-not-a-png")
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    extractor = MacOSVisionForegroundExtractor(
        binary_path=_binary(tmp_path), platform_name="darwin", runner=runner
    )

    with pytest.raises(DomainRuleError) as exc_info:
        extractor.extract(_request())
    assert exc_info.value.code == "foreground_mask_invalid"


def test_wrong_size_mask_is_rejected(tmp_path: Path) -> None:
    def runner(command, **kwargs):
        del kwargs
        mask_path = Path(command[command.index("--mask-output") + 1])
        foreground_path = Path(command[command.index("--foreground-output") + 1])
        mask_path.write_bytes(_png("L", (7, 8), (2, 2, 6, 6)))
        foreground_path.write_bytes(_png("RGBA", (8, 8), (2, 2, 6, 6)))
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    extractor = MacOSVisionForegroundExtractor(
        binary_path=_binary(tmp_path), platform_name="darwin", runner=runner
    )

    with pytest.raises(DomainRuleError) as exc_info:
        extractor.extract(_request())
    assert exc_info.value.code == "foreground_mask_invalid"


def test_successful_subprocess_uses_bounded_sanitized_invocation(
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    def runner(command, **kwargs):
        captured.update({"command": command, **kwargs})
        return _success_runner(command, **kwargs)

    binary = _binary(tmp_path)
    extractor = MacOSVisionForegroundExtractor(
        binary_path=binary,
        platform_name="darwin",
        runner=runner,
        timeout_seconds=7,
        max_output_bytes=1024 * 1024,
    )

    result = extractor.extract(_request())

    assert result.extractor_version == "macos-vision-foreground-v1"
    assert result.width == 8
    assert result.height == 8
    assert captured["shell"] is False
    assert captured["timeout"] == 7
    assert captured["capture_output"] is True
    assert captured["check"] is False
    assert captured["env"] == {"LANG": "C", "PATH": "/usr/bin:/bin"}
    command = captured["command"]
    assert command[0] == os.fspath(binary)
    assert command[1::2] == ["--input", "--mask-output", "--foreground-output"]


def test_explicit_extractor_version_is_preserved(tmp_path: Path) -> None:
    extractor = MacOSVisionForegroundExtractor(
        binary_path=_binary(tmp_path),
        platform_name="darwin",
        runner=_success_runner,
        extractor_version="local-birefnet-official-v1:123456789abc",
    )

    result = extractor.extract(_request())

    assert result.extractor_version == "local-birefnet-official-v1:123456789abc"
    assert extractor.extractor_version == "local-birefnet-official-v1:123456789abc"
