from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "scripts" / "phase1_runtime_readiness.sh"


def test_runtime_readiness_forwards_an_explicit_flutter_sdk_path(
    tmp_path: Path,
) -> None:
    python = tmp_path / "python-shim"
    android = tmp_path / "android-gate"
    android_sdk = tmp_path / "android-sdk-gate"
    ios = tmp_path / "ios-gate"
    integration = tmp_path / "integration-gate"
    python.write_text(
        "#!/bin/sh\n"
        '[ "$*" = "$EXPECTED_MOBILE_ARGS" ] || exit 1\n'
        "printf '%s\\n' mobile_sdk_ready\n",
        encoding="utf-8",
    )
    for gate, output in (
        (android, "PHASE1_ANDROID_NATIVE_OFFLINE_CHECK_OK"),
        (android_sdk, "PHASE1_ANDROID_SDK_OFFLINE_CHECK_OK"),
        (ios, "PHASE1_IOS_SIMULATOR_OFFLINE_CHECK_OK"),
        (integration, "PHASE1_ISOLATED_INTEGRATION_CHECK_OK"),
    ):
        gate.write_text(f"#!/bin/sh\nprintf '%s\\n' {output}\n", encoding="utf-8")
    for executable in (python, android, android_sdk, ios, integration):
        executable.chmod(0o755)

    result = subprocess.run(
        ["sh", str(SCRIPT)],
        cwd=ROOT,
        env={
            "PATH": "/usr/bin:/bin",
            "PHOTO_ORGANIZER_PYTHON": str(python),
            "PHOTO_ORGANIZER_FLUTTER_SDK": "/approved/flutter",
            "EXPECTED_MOBILE_ARGS": (
                f"{ROOT / 'scripts' / 'check_mobile_sdk_gate.py'} "
                "--flutter-sdk /approved/flutter"
            ),
            "PHOTO_ORGANIZER_ANDROID_GATE": str(android),
            "PHOTO_ORGANIZER_ANDROID_SDK_GATE": str(android_sdk),
            "PHOTO_ORGANIZER_IOS_GATE": str(ios),
            "PHOTO_ORGANIZER_INTEGRATION_GATE": str(integration),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == (
        "PHASE1_RUNTIME_READINESS mobile=ready android=ready android_sdk=ready ios=ready integration=ready\n"
    )
    assert result.stderr == ""
