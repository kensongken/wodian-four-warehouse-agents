"""Offline structural verification for the controlled mobile upload handoff.

This deliberately does not compile Dart, access a photo library, read image
bytes, or call the binding API.  It is only a reproducible static boundary
check while a local Flutter/Dart SDK is unavailable.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_REQUIRED: dict[str, tuple[str, ...]] = {
    "apps/mobile/lib/features/controlled_upload/controlled_upload_candidate.dart": (
        "mall_product_id",
        "bead_id",
        "auto_publish",
        "shared_market_opt_in",
        "claims_not_evaluated",
    ),
    "apps/mobile/lib/features/controlled_upload/controlled_candidate_handoff_service.dart": (
        "approval.requireCandidate(candidate.candidateId)",
        "_mediaLibrary.computeSha256(local)",
        "await _binding.bind(",
        "_queue.enqueueControlledCandidate(",
    ),
    "apps/mobile/lib/features/controlled_upload/controlled_upload_binding_repository.dart": (
        "'/v1/opcs/$opcId/upload-bindings'",
        "'opc-controlled-upload-binding-request/1.0'",
        "'ready_for_queue_handoff'",
        "'auto_publish', false",
        "'shared_market_opt_in', false",
    ),
    "apps/mobile/lib/core/upload/upload_queue_repository.dart": (
        "Future<ImportQueueSnapshot> enqueueControlledCandidate(",
        "required UploadAuthority authority",
        "_candidateQueueConflict()",
    ),
}
_FORBIDDEN = ("ImagePicker", "PhotoManager", "PHPhotoLibrary")
_HANDOFF_STEPS = (
    "approval.requireCandidate(candidate.candidateId)",
    "_mediaLibrary.computeSha256(local)",
    "await _binding.bind(",
    "_queue.enqueueControlledCandidate(",
)


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError("invalid arguments")


def main(arguments: list[str] | None = None) -> int:
    parser = _SafeArgumentParser(add_help=False)
    parser.add_argument("--root", type=Path, default=ROOT)
    try:
        args = parser.parse_args(arguments)
    except (SystemExit, ValueError):
        return _fail()
    return verify(args.root)


def verify(root: Path) -> int:
    try:
        sources = {
            relative: _without_line_comments(
                (root / relative).read_text(encoding="utf-8")
            )
            for relative in _REQUIRED
        }
    except (OSError, UnicodeError):
        return _fail()

    if any(
        required not in sources[relative]
        for relative, required_values in _REQUIRED.items()
        for required in required_values
    ):
        return _fail()
    handoff = sources[
        "apps/mobile/lib/features/controlled_upload/controlled_candidate_handoff_service.dart"
    ]
    if [handoff.index(step) for step in _HANDOFF_STEPS] != sorted(
        handoff.index(step) for step in _HANDOFF_STEPS
    ):
        return _fail()
    if any(forbidden in source for source in sources.values() for forbidden in _FORBIDDEN):
        return _fail()

    print("mobile_controlled_upload_static_ok")
    return 0


def _without_line_comments(source: str) -> str:
    return re.sub(r"//.*$", "", source, flags=re.MULTILINE)


def _fail() -> int:
    print("mobile_controlled_upload_static_invalid", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
