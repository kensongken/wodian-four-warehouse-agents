"""Fail-closed adapter for the explicitly built local macOS Vision executable."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path

from opc_style.domain.errors import DomainRuleError
from opc_style.domain.product_foreground import (
    ExtractedForeground,
    ForegroundExtractionRequest,
    validate_extracted_foreground,
)

_EXTRACTOR_VERSION = "macos-vision-foreground-v1"
_UNAVAILABLE = "foreground_extractor_unavailable"
_FAILED = "foreground_extraction_failed"

Runner = Callable[..., subprocess.CompletedProcess[bytes]]


class MacOSVisionForegroundExtractor:
    """Run a configured native extractor with private, caller-owned temp files."""

    def __init__(
        self,
        *,
        binary_path: Path,
        timeout_seconds: int = 30,
        max_output_bytes: int = 20 * 1024 * 1024,
        extractor_version: str = _EXTRACTOR_VERSION,
        platform_name: str | None = None,
        runner: Runner = subprocess.run,
    ) -> None:
        self._binary_path = binary_path
        self._timeout_seconds = timeout_seconds
        self._max_output_bytes = max_output_bytes
        self._extractor_version = extractor_version
        self._platform_name = platform_name or sys.platform
        self._runner = runner

    @property
    def extractor_version(self) -> str:
        """Return the configured local engine identity for runtime evidence."""
        return self._extractor_version

    def extract(self, request: ForegroundExtractionRequest) -> ExtractedForeground:
        """Return validated same-size PNGs or a stable, sanitized domain failure."""
        self._validate_availability()
        if (
            request.width <= 0
            or request.height <= 0
            or sha256(request.source_content).hexdigest()
            != request.source_content_sha256
        ):
            raise DomainRuleError("foreground_source_incomplete")

        try:
            with tempfile.TemporaryDirectory(prefix="opc-foreground-") as directory:
                root = Path(directory).resolve(strict=True)
                input_path = root / "source-image"
                mask_path = root / "mask.png"
                foreground_path = root / "foreground.png"
                self._require_owned_path(root, input_path, must_exist=False)
                self._require_owned_path(root, mask_path, must_exist=False)
                self._require_owned_path(root, foreground_path, must_exist=False)
                input_path.write_bytes(request.source_content)
                input_path.chmod(0o600)

                completed = self._runner(
                    [
                        os.fspath(self._binary_path),
                        "--input",
                        os.fspath(input_path),
                        "--mask-output",
                        os.fspath(mask_path),
                        "--foreground-output",
                        os.fspath(foreground_path),
                    ],
                    check=False,
                    capture_output=True,
                    timeout=self._timeout_seconds,
                    env={"LANG": "C", "PATH": "/usr/bin:/bin"},
                    shell=False,
                )
                if completed.returncode != 0:
                    raise DomainRuleError(_FAILED)

                mask = self._read_private_output(root, mask_path)
                foreground = self._read_private_output(root, foreground_path)
        except DomainRuleError:
            raise
        except (OSError, subprocess.SubprocessError, ValueError):
            raise DomainRuleError(_FAILED) from None
        except Exception:  # noqa: BLE001  # provider errors must never leak private data
            raise DomainRuleError(_FAILED) from None

        result = ExtractedForeground(
            foreground_png=foreground,
            foreground_sha256=sha256(foreground).hexdigest(),
            mask_png=mask,
            mask_sha256=sha256(mask).hexdigest(),
            width=request.width,
            height=request.height,
            extractor_version=self._extractor_version,
        )
        validate_extracted_foreground(result)
        return result

    def _validate_availability(self) -> None:
        if (
            self._platform_name != "darwin"
            or not self._binary_path.is_absolute()
            or not self._binary_path.is_file()
            or self._binary_path.is_symlink()
            or not os.access(self._binary_path, os.X_OK)
            or self._timeout_seconds <= 0
            or self._max_output_bytes <= 0
        ):
            raise DomainRuleError(_UNAVAILABLE)

    @staticmethod
    def _require_owned_path(root: Path, path: Path, *, must_exist: bool) -> None:
        if path.is_symlink():
            raise DomainRuleError(_FAILED)
        resolved = path.resolve(strict=must_exist)
        if resolved.parent != root:
            raise DomainRuleError(_FAILED)

    def _read_private_output(self, root: Path, path: Path) -> bytes:
        self._require_owned_path(root, path, must_exist=True)
        if not path.is_file():
            raise DomainRuleError(_FAILED)
        stat = path.stat()
        if stat.st_size <= 0 or stat.st_size > self._max_output_bytes:
            raise DomainRuleError(_FAILED)
        return path.read_bytes()
