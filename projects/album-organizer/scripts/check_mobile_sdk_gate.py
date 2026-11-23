"""Report whether the existing local mobile SDK can run project checks.

This gate is intentionally read-only: it never downloads, installs, or
contacts a remote service.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCAL_PROPERTIES = ROOT / "apps/mobile/android/local.properties"


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError("invalid arguments")


def main(arguments: list[str] | None = None) -> int:
    parser = _SafeArgumentParser(add_help=False)
    parser.add_argument("--local-properties", type=Path, default=LOCAL_PROPERTIES)
    parser.add_argument("--flutter-sdk", type=Path)
    try:
        args = parser.parse_args(arguments)
    except (SystemExit, ValueError):
        print("mobile_sdk_missing", file=sys.stderr)
        return 1
    sdk_root = args.flutter_sdk or _configured_sdk_root(args.local_properties)
    if sdk_root is None or not _has_usable_controlled_tools(sdk_root):
        print("mobile_sdk_missing", file=sys.stderr)
        return 1
    print("mobile_sdk_ready")
    return 0


def _configured_sdk_root(local_properties: Path) -> Path | None:
    try:
        lines = local_properties.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return None
    for line in lines:
        if line.startswith("flutter.sdk="):
            value = line.removeprefix("flutter.sdk=")
            return Path(value) if value else None
    return None


def _has_usable_controlled_tools(sdk_root: Path) -> bool:
    for name in ("flutter", "dart"):
        expected = sdk_root / "bin" / name
        discovered = shutil.which(name)
        if not expected.is_file() or discovered is None:
            return False
        try:
            if Path(discovered).resolve() != expected.resolve():
                return False
            version_arguments = (
                ["--no-version-check", "--version"]
                if name == "flutter"
                else ["--version"]
            )
            completed = subprocess.run(
                [str(expected), *version_arguments],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        if completed.returncode != 0:
            return False
    return True


if __name__ == "__main__":
    raise SystemExit(main())
