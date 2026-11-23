#!/usr/bin/env python3
"""Fail-closed, path-free preflight for the sealed foreground runtime."""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path

PINNED_EXTRA_DEPENDENCIES = (
    ("einops", "0.8.2"),
    ("kornia", "0.8.2"),
    ("timm", "1.0.30"),
)
_BIREFNET_FILENAMES = (
    "BiRefNet_config.py",
    "LICENSE",
    "birefnet.py",
    "config.json",
    "model.safetensors",
)


def verify_runtime_versions(
    version_reader: Callable[[str], str] = version,
) -> tuple[tuple[str, str], ...]:
    """Return exact pinned versions or fail without leaking an environment path."""
    try:
        installed = tuple(
            (name, version_reader(name)) for name, _ in PINNED_EXTRA_DEPENDENCIES
        )
    except Exception as error:
        raise RuntimeError("runtime_dependency_mismatch") from error
    if installed != PINNED_EXTRA_DEPENDENCIES:
        raise RuntimeError("runtime_dependency_mismatch")
    return installed


def _file_digests(root: Path) -> tuple[tuple[str, str], ...]:
    sealed_root = root.resolve(strict=True)
    if root.is_symlink() or tuple(sorted(path.name for path in sealed_root.iterdir())) != _BIREFNET_FILENAMES:
        raise RuntimeError("birefnet_seal_invalid")
    values: list[tuple[str, str]] = []
    for filename in _BIREFNET_FILENAMES:
        path = sealed_root / filename
        if path.is_symlink() or not path.is_file():
            raise RuntimeError("birefnet_seal_invalid")
        values.append((filename, sha256(path.read_bytes()).hexdigest()))
    return tuple(values)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--birefnet-root", type=Path, required=True)
    parser.add_argument("--pipeline-manifest", type=Path, required=True)
    parser.add_argument("--expected-manifest-sha256", required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    content = args.pipeline_manifest.read_bytes()
    if sha256(content).hexdigest() != args.expected_manifest_sha256:
        raise RuntimeError("pipeline_manifest_mismatch")
    output = {
        "birefnet_files_sha256": _file_digests(args.birefnet_root),
        "pipeline_manifest_sha256": args.expected_manifest_sha256,
        "runtime_versions": verify_runtime_versions(),
    }
    print(json.dumps(output, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
