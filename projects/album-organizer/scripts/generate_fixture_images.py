#!/usr/bin/env python3
"""Generate a deterministic JPEG corpus for the Phase 1 ingestion smoke test."""

import argparse
import hashlib
import json
import shutil
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def generate(*, count: int, output: Path, overwrite: bool) -> Path:
    if count < 1 or count > 10_000:
        raise ValueError("count must be between 1 and 10000")
    output = output.expanduser().resolve()
    _prepare_output(output, overwrite=overwrite)
    font = ImageFont.load_default(size=28)
    assets = [
        _write_fixture(index=index, output=output, font=font)
        for index in range(1, count + 1)
    ]
    manifest = {
        "version": 1,
        "count": count,
        "assets": assets,
    }
    manifest_path = output / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest_path


def _write_fixture(
    *,
    index: int,
    output: Path,
    font: ImageFont.ImageFont | ImageFont.FreeTypeFont,
) -> dict[str, object]:
    filename = f"image-{index:04d}.jpg"
    path = output / filename
    color = (
        (index * 73 + 31) % 256,
        (index * 151 + 47) % 256,
        (index * 199 + 89) % 256,
    )
    image = Image.new("RGB", (640, 640), color=color)
    draw = ImageDraw.Draw(image)
    draw.rectangle((32, 32, 607, 607), outline="white", width=8)
    draw.text(
        (56, 56),
        f"PHOTO ORGANIZER\nFIXTURE {index:04d}",
        fill="white",
        font=font,
        spacing=10,
    )
    image.save(
        path,
        format="JPEG",
        quality=90,
        optimize=False,
        progressive=False,
        subsampling=0,
    )
    content = path.read_bytes()
    return {
        "index": index,
        "local_asset_id": f"fixture-{index:04d}",
        "filename": filename,
        "media_type": "image/jpeg",
        "byte_size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def _prepare_output(output: Path, *, overwrite: bool) -> None:
    prohibited = {Path(output.anchor), Path.home().resolve(), Path.cwd().resolve()}
    if output in prohibited:
        raise ValueError("refusing a broad output directory")
    if output.exists() and not output.is_dir():
        raise FileExistsError(f"output is not a directory: {output}")
    if output.exists() and any(output.iterdir()):
        if not overwrite:
            raise FileExistsError(f"output directory is non-empty: {output}")
        for child in output.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
    output.mkdir(parents=True, exist_ok=True)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    manifest = generate(
        count=args.count,
        output=args.output,
        overwrite=args.overwrite,
    )
    print(f"generated {args.count} deterministic JPEGs: {manifest}")


if __name__ == "__main__":
    main()
