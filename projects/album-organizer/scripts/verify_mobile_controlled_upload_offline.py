"""Run the local, non-SDK checks for the controlled mobile upload handoff."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_STATIC = ROOT / "scripts/verify_mobile_controlled_upload_static.py"
_SDK_GATE = ROOT / "scripts/check_mobile_sdk_gate.py"


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError("invalid arguments")


def main(arguments: list[str] | None = None) -> int:
    parser = _SafeArgumentParser(add_help=False)
    parser.add_argument("--root", type=Path, default=ROOT)
    try:
        args = parser.parse_args(arguments)
    except (SystemExit, ValueError):
        return _invalid()

    static = _run(_STATIC, "--root", str(args.root))
    if static.returncode != 0 or static.stdout != "mobile_controlled_upload_static_ok\n":
        return _invalid()

    sdk = _run(_SDK_GATE)
    if sdk.returncode == 0 and sdk.stdout == "mobile_sdk_ready\n":
        print("mobile_controlled_upload_offline_ok sdk=ready")
        return 0
    if sdk.returncode == 1 and sdk.stderr == "mobile_sdk_missing\n":
        print("mobile_controlled_upload_offline_ok sdk=missing")
        return 0
    return _invalid()


def _run(script: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script), *arguments],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _invalid() -> int:
    print("mobile_controlled_upload_offline_invalid", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
