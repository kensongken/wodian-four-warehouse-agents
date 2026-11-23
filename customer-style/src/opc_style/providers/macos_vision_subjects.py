"""Local macOS Vision foreground extraction behind the subject-seal contract."""

from collections.abc import Callable
from pathlib import Path
from shutil import rmtree
from subprocess import CompletedProcess, run
from tempfile import mkdtemp

from opc_style.domain.subject_seal import (
    SealedSubject,
    SubjectSealError,
    SubjectSegmentationRequest,
    seal_normalized_subject,
)

MACOS_VISION_SUBJECT_ADAPTER_VERSION = "macos-vision-foreground-v2"
type CommandRunner = Callable[[list[str]], CompletedProcess[str]]


def _run_helper(command: list[str]) -> CompletedProcess[str]:
    return run(command, check=False, capture_output=True, text=True)


class MacOSVisionSubjectSegmenter:
    """Run Apple's local foreground-mask request without network or media logs."""

    adapter_version = MACOS_VISION_SUBJECT_ADAPTER_VERSION
    synthetic_test_only = False

    def __init__(
        self,
        *,
        runner: CommandRunner = _run_helper,
        temporary_parent: Path | None = None,
        helper_path: Path | None = None,
    ) -> None:
        self._runner = runner
        self._temporary_parent = temporary_parent
        self._helper_path = helper_path or (
            Path(__file__).resolve().parents[3]
            / "scripts"
            / "macos_foreground_mask.swift"
        )

    def seal(self, request: SubjectSegmentationRequest) -> SealedSubject:
        suffix = ".png" if request.mime_type == "image/png" else ".jpg"
        temp_root = Path(
            mkdtemp(prefix="opc-subject-mask-", dir=self._temporary_parent)
        )
        input_path = temp_root / f"source{suffix}"
        subject_path = temp_root / "subject.png"
        output_path = temp_root / "mask.png"
        try:
            input_path.write_bytes(request.content)
            command = [
                "/usr/bin/xcrun",
                "swift",
                str(self._helper_path),
                str(input_path),
                str(subject_path),
                str(output_path),
                str(request.target_canvas[0]),
                str(request.target_canvas[1]),
            ]
            try:
                completed = self._runner(command)
            except OSError as error:
                raise SubjectSealError("subject_segmentation_unavailable") from error
            if completed.returncode != 0:
                code = {
                    2: "subject_segmentation_unavailable",
                    4: "subject_segmentation_ambiguous",
                }.get(completed.returncode, "subject_segmentation_failed")
                raise SubjectSealError(code)
            try:
                normalized_source_png = subject_path.read_bytes()
                mask_png = output_path.read_bytes()
            except OSError as error:
                raise SubjectSealError("subject_segmentation_failed") from error
            return seal_normalized_subject(
                request,
                normalized_source_png,
                mask_png,
                adapter_version=self.adapter_version,
                synthetic_test_only=self.synthetic_test_only,
            )
        finally:
            rmtree(temp_root, ignore_errors=True)
