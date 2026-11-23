#!/usr/bin/env python3
"""Setup-only stager for one pinned official BiRefNet snapshot."""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path
from typing import cast
from urllib.request import urlopen

PINNED_REVISION = "e2bf8e4460fc8fa32bba5ea4d94b3233d367b0e4"
PINNED_LICENSE_REVISION = "ebcc0bc8ec7fe919cec829f2dea656b3078acddc"
_REPOSITORY_ID = "ZhengPeng7/BiRefNet"
_SNAPSHOT_FILENAMES = (
    "BiRefNet_config.py",
    "birefnet.py",
    "config.json",
    "model.safetensors",
)
_STAGED_FILENAMES = (
    "BiRefNet_config.py",
    "LICENSE",
    "birefnet.py",
    "config.json",
    "model.safetensors",
)
SnapshotFetcher = Callable[[str], tuple[Path, str]]
LicenseFetcher = Callable[[], bytes]


def _default_snapshot_fetcher(revision: str) -> tuple[Path, str]:
    from huggingface_hub import HfApi, hf_hub_download  # type: ignore[import-not-found]

    resolved = str(HfApi().model_info(_REPOSITORY_ID, revision=revision).sha)
    snapshot = Path(tempfile.mkdtemp(prefix="opc-birefnet-public-snapshot-"))
    for filename in _SNAPSHOT_FILENAMES:
        cached = hf_hub_download(
            _REPOSITORY_ID,
            filename=filename,
            revision=revision,
        )
        shutil.copyfile(Path(cached).resolve(strict=True), snapshot / filename)
    return snapshot, resolved


def _default_license_fetcher() -> bytes:
    url = (
        "https://raw.githubusercontent.com/ZhengPeng7/BiRefNet/"
        f"{PINNED_LICENSE_REVISION}/LICENSE"
    )
    with urlopen(url, timeout=30) as response:
        return cast(bytes, response.read())


def stage_official_birefnet(
    destination: Path,
    revision: str,
    snapshot_fetcher: SnapshotFetcher = _default_snapshot_fetcher,
    license_fetcher: LicenseFetcher = _default_license_fetcher,
) -> dict[str, object]:
    """Stage exact public model artifacts; no customer-content parameter exists."""
    if revision != PINNED_REVISION:
        raise RuntimeError("birefnet_revision_invalid")
    if destination.exists() or not destination.is_absolute():
        raise RuntimeError("birefnet_destination_invalid")
    snapshot, resolved_revision = snapshot_fetcher(revision)
    if resolved_revision != PINNED_REVISION:
        raise RuntimeError("birefnet_revision_invalid")
    if snapshot.is_symlink() or not snapshot.is_dir():
        raise RuntimeError("birefnet_snapshot_invalid")
    if tuple(sorted(path.name for path in snapshot.iterdir())) != tuple(
        sorted(_SNAPSHOT_FILENAMES)
    ):
        raise RuntimeError("birefnet_snapshot_invalid")
    if any((snapshot / name).is_symlink() or not (snapshot / name).is_file() for name in _SNAPSHOT_FILENAMES):
        raise RuntimeError("birefnet_snapshot_invalid")
    license_content = license_fetcher()
    if b"MIT License" not in license_content or b"Permission is hereby granted" not in license_content:
        raise RuntimeError("birefnet_license_invalid")

    destination.mkdir(mode=0o700, parents=False)
    try:
        for name in _SNAPSHOT_FILENAMES:
            shutil.copyfile(snapshot / name, destination / name)
        (destination / "LICENSE").write_bytes(license_content)
        for path in destination.iterdir():
            path.chmod(0o600)
        files = tuple(
            (name, sha256((destination / name).read_bytes()).hexdigest())
            for name in _STAGED_FILENAMES
        )
        receipt: dict[str, object] = {
            "files_sha256": files,
            "license_revision": PINNED_LICENSE_REVISION,
            "repository_id": _REPOSITORY_ID,
            "revision": PINNED_REVISION,
        }
        receipt_path = destination.parent / f"{destination.name}.source-receipt.json"
        receipt_path.write_text(
            json.dumps(receipt, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        receipt_path.chmod(0o600)
        return receipt
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--revision", required=True)
    parser.add_argument("--destination", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    receipt = stage_official_birefnet(args.destination, args.revision)
    print(json.dumps(receipt, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
