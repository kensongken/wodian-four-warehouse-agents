from __future__ import annotations

import subprocess
import sys
from os import chmod
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "scripts/check_mobile_sdk_gate.py"


def test_local_mobile_sdk_gate_reports_the_current_missing_sdk() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "mobile_sdk_missing\n"


def test_local_mobile_sdk_gate_accepts_a_complete_controlled_sdk(
    tmp_path: Path,
) -> None:
    sdk_root = tmp_path / "flutter"
    sdk_bin = sdk_root / "bin"
    sdk_bin.mkdir(parents=True)
    flutter = sdk_bin / "flutter"
    dart = sdk_bin / "dart"
    flutter.write_text(
        '#!/bin/sh\n[ "$*" = "--no-version-check --version" ]\n',
        encoding="utf-8",
    )
    dart.write_text('#!/bin/sh\n[ "$*" = "--version" ]\n', encoding="utf-8")
    chmod(flutter, 0o755)
    chmod(dart, 0o755)
    properties = tmp_path / "local.properties"
    properties.write_text(f"flutter.sdk={sdk_root}\n", encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--local-properties",
            str(properties),
        ],
        cwd=ROOT,
        env={"PATH": str(sdk_bin)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == "mobile_sdk_ready\n"
    assert result.stderr == ""


def test_local_mobile_sdk_gate_accepts_an_explicit_controlled_sdk_path(
    tmp_path: Path,
) -> None:
    sdk_root = tmp_path / "flutter"
    sdk_bin = sdk_root / "bin"
    sdk_bin.mkdir(parents=True)
    flutter = sdk_bin / "flutter"
    dart = sdk_bin / "dart"
    flutter.write_text(
        '#!/bin/sh\n[ "$*" = "--no-version-check --version" ]\n',
        encoding="utf-8",
    )
    dart.write_text('#!/bin/sh\n[ "$*" = "--version" ]\n', encoding="utf-8")
    chmod(flutter, 0o755)
    chmod(dart, 0o755)

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--flutter-sdk", str(sdk_root)],
        cwd=ROOT,
        env={"PATH": str(sdk_bin)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == "mobile_sdk_ready\n"
    assert result.stderr == ""


def test_local_mobile_sdk_gate_rejects_a_nonfunctional_controlled_sdk(
    tmp_path: Path,
) -> None:
    sdk_root = tmp_path / "flutter"
    sdk_bin = sdk_root / "bin"
    sdk_bin.mkdir(parents=True)
    flutter = sdk_bin / "flutter"
    dart = sdk_bin / "dart"
    flutter.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    dart.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    chmod(flutter, 0o755)
    chmod(dart, 0o755)
    properties = tmp_path / "local.properties"
    properties.write_text(f"flutter.sdk={sdk_root}\n", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--local-properties", str(properties)],
        cwd=ROOT,
        env={"PATH": str(sdk_bin)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "mobile_sdk_missing\n"


def test_local_mobile_sdk_gate_redacts_invalid_arguments() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--unexpected", "sensitive-value"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "mobile_sdk_missing\n"
