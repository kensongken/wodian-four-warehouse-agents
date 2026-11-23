"""macOS Vision foreground adapter contracts."""

import subprocess
from hashlib import sha256
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from opc_style.domain.subject_seal import SubjectSealError, SubjectSegmentationRequest
from opc_style.providers.macos_vision_subjects import MacOSVisionSubjectSegmenter


def _png(mode: str, size: tuple[int, int], color: int | tuple[int, ...]) -> bytes:
    output = BytesIO()
    Image.new(mode, size, color).save(output, format="PNG")
    return output.getvalue()


def _request() -> SubjectSegmentationRequest:
    content = _png("RGB", (4, 3), (50, 80, 120))
    return SubjectSegmentationRequest(
        content=content,
        content_sha256=sha256(content).hexdigest(),
        mime_type="image/png",
        target_canvas=(8, 10),
    )


def test_vision_adapter_seals_verified_mask_and_removes_private_temp_files(
    tmp_path: Path,
) -> None:
    """Skipping mask verification or cleanup would leak media or admit bad evidence."""
    observed_temp_root: Path | None = None

    def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
        nonlocal observed_temp_root
        input_path = Path(command[-5])
        subject_path = Path(command[-4])
        output_path = Path(command[-3])
        observed_temp_root = input_path.parent
        assert input_path.read_bytes() == _request().content
        normalized = Image.new("RGB", (2, 2), (50, 80, 120))
        normalized.save(subject_path, format="PNG")
        mask = Image.new("L", (2, 2), 0)
        mask.putpixel((1, 1), 255)
        mask.save(output_path, format="PNG")
        return subprocess.CompletedProcess(command, 0, "", "")

    sealed = MacOSVisionSubjectSegmenter(
        runner=runner,
        temporary_parent=tmp_path,
    ).seal(_request())

    assert sealed.adapter_version == "macos-vision-foreground-v2"
    assert sealed.synthetic_test_only is False
    assert sealed.core_mask_pixel_count == 1
    assert (sealed.width, sealed.height) == (2, 2)
    assert observed_temp_root is not None
    assert not observed_temp_root.exists()


@pytest.mark.parametrize(
    ("returncode", "stderr", "code"),
    [
        (2, "vision_unavailable", "subject_segmentation_unavailable"),
        (3, "foreground_missing", "subject_segmentation_failed"),
        (4, "foreground_ambiguous", "subject_segmentation_ambiguous"),
    ],
)
def test_vision_adapter_maps_helper_failures_to_stable_errors(
    tmp_path: Path,
    returncode: int,
    stderr: str,
    code: str,
) -> None:
    """Helper failure must not fall back to an unsegmented full photograph."""

    def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, returncode, "", stderr)

    with pytest.raises(SubjectSealError) as raised:
        MacOSVisionSubjectSegmenter(
            runner=runner,
            temporary_parent=tmp_path,
        ).seal(_request())

    assert raised.value.code == code
    assert list(tmp_path.iterdir()) == []


def test_vision_adapter_rejects_success_without_a_mask(tmp_path: Path) -> None:
    """A zero exit without an artifact must remain a failed segmentation."""

    def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, "", "")

    with pytest.raises(SubjectSealError) as raised:
        MacOSVisionSubjectSegmenter(
            runner=runner,
            temporary_parent=tmp_path,
        ).seal(_request())

    assert raised.value.code == "subject_segmentation_failed"


def test_vision_adapter_maps_missing_local_runtime_to_unavailable(tmp_path: Path) -> None:
    """A missing xcrun runtime must fail closed instead of becoming an HTTP 500."""

    def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError(command[0])

    with pytest.raises(SubjectSealError) as raised:
        MacOSVisionSubjectSegmenter(
            runner=runner,
            temporary_parent=tmp_path,
        ).seal(_request())

    assert raised.value.code == "subject_segmentation_unavailable"
    assert list(tmp_path.iterdir()) == []
