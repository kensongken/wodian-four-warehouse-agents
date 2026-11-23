from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "scripts" / "phase1_mobile_synthetic_check.sh"


def test_mobile_synthetic_check_forwards_approved_java_home_to_gradle(
    tmp_path: Path,
) -> None:
    flutter = tmp_path / "flutter"
    dart = tmp_path / "dart"
    gradle = tmp_path / "gradle"
    java_home = tmp_path / "approved-jdk"
    invocation = tmp_path / "gradle-invocation"
    java_home.mkdir()
    flutter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    dart.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    gradle.write_text(
        '#!/bin/sh\nprintf "%s|%s\\n" "$JAVA_HOME" "$*" > "$GRADLE_INVOCATION"\n',
        encoding="utf-8",
    )
    for executable in (flutter, dart, gradle):
        executable.chmod(0o755)

    result = subprocess.run(
        ["sh", str(SCRIPT)],
        cwd=ROOT,
        env={
            "PATH": "/usr/bin:/bin",
            "PHOTO_ORGANIZER_FLUTTER": str(flutter),
            "PHOTO_ORGANIZER_DART": str(dart),
            "PHOTO_ORGANIZER_RUN_ANDROID_TESTS": "1",
            "PHOTO_ORGANIZER_GRADLE": str(gradle),
            "PHOTO_ORGANIZER_JAVA_HOME": str(java_home),
            "GRADLE_INVOCATION": str(invocation),
            "PHOTO_ORGANIZER_FLUTTER_CONFIG_HOME": str(tmp_path / "flutter-config"),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == "PHASE1_MOBILE_SYNTHETIC_CHECK_OK android=checked\n"
    assert result.stderr == ""
    assert invocation.read_text(encoding="utf-8") == (
        f"{java_home}|:app:testDebugUnitTest --offline --no-daemon\n"
    )
