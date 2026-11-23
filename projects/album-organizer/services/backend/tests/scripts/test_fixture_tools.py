import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from uuid import UUID, uuid5

import httpx
import pytest
import yaml
from PIL import Image


def test_local_verification_entrypoints_forbid_external_dispatch() -> None:
    root = Path(__file__).resolve().parents[4]
    forbidden = (
        "curl",
        "wget",
        "git push",
        "git pull",
        "git fetch",
        "docker ",
        "ssh ",
        "scp ",
        "rsync ",
    )
    entrypoints = (
        "mini_night_check.sh",
        "mini_night_handoff_gate.sh",
        "phase1_mobile_synthetic_check.sh",
        "phase1_android_native_offline_check.sh",
        "phase1_android_sdk_offline_check.sh",
        "phase1_ios_simulator_offline_check.sh",
        "phase1_ios_native_check.sh",
        "phase1_isolated_integration_check.sh",
        "phase1_synthetic_upload_preflight.sh",
    )

    for entrypoint in entrypoints:
        source = (root / "scripts" / entrypoint).read_text(encoding="utf-8")
        assert not any(command in source for command in forbidden), entrypoint


def test_minio_bucket_initialization_uses_only_supported_idempotent_commands(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[4]
    command_log = tmp_path / "mc-commands.log"
    mc = tmp_path / "mc"
    mc.write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$*" >> "$PHOTO_ORGANIZER_COMMAND_LOG"\n'
        'case "$*" in\n'
        "  'alias set local http://minio:9000 minioadmin minioadmin') exit 0 ;;\n"
        "  'mb --ignore-existing local/photo-organizer-dev') exit 0 ;;\n"
        "  *) exit 9 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    mc.chmod(0o755)

    result = subprocess.run(
        ["sh", str(root / "infra" / "minio" / "create-bucket.sh")],
        cwd=root,
        env={
            "PATH": f"{tmp_path}:/usr/bin:/bin",
            "MINIO_ENDPOINT": "http://minio:9000",
            "MINIO_ROOT_USER": "minioadmin",
            "MINIO_ROOT_PASSWORD": "minioadmin",
            "MINIO_BUCKET": "photo-organizer-dev",
            "PHOTO_ORGANIZER_COMMAND_LOG": str(command_log),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stderr == ""
    assert command_log.read_text(encoding="utf-8").splitlines() == [
        "alias set local http://minio:9000 minioadmin minioadmin",
        "mb --ignore-existing local/photo-organizer-dev",
    ]


def test_local_minio_explicitly_expires_stale_multipart_uploads() -> None:
    root = Path(__file__).resolve().parents[4]
    compose = yaml.safe_load(
        (root / "infra" / "compose.yaml").read_text(encoding="utf-8")
    )
    minio = compose["services"]["minio"]

    assert minio["environment"]["MINIO_API_STALE_UPLOADS_EXPIRY"] == "24h"
    assert minio["environment"]["MINIO_API_STALE_UPLOADS_CLEANUP_INTERVAL"] == "1h"


def test_runtime_readiness_reports_only_sanitized_local_gate_states(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[4]
    python = tmp_path / "python-shim"
    android = tmp_path / "android-gate"
    android_sdk = tmp_path / "android-sdk-gate"
    integration = tmp_path / "integration-gate"
    ios = tmp_path / "ios-gate"
    python.write_text("#!/bin/sh\nprintf '%s\\n' mobile_sdk_missing >&2\nexit 1\n")
    android.write_text(
        "#!/bin/sh\nprintf '%s\\n' phase1_android_java_missing >&2\nexit 1\n"
    )
    android_sdk.write_text(
        "#!/bin/sh\nprintf '%s\\n' phase1_android_sdk_missing >&2\nexit 1\n"
    )
    integration.write_text(
        "#!/bin/sh\nprintf '%s\\n' phase1_integration_environment_missing >&2\nexit 1\n"
    )
    ios.write_text(
        "#!/bin/sh\nprintf '%s\\n' phase1_ios_simulator_sdk_missing >&2\nexit 1\n"
    )
    for executable in (python, android, android_sdk, integration, ios):
        executable.chmod(0o755)

    result = subprocess.run(
        ["sh", str(root / "scripts" / "phase1_runtime_readiness.sh")],
        cwd=root,
        env={
            "PATH": "/usr/bin:/bin",
            "PHOTO_ORGANIZER_PYTHON": str(python),
            "PHOTO_ORGANIZER_ANDROID_GATE": str(android),
            "PHOTO_ORGANIZER_ANDROID_SDK_GATE": str(android_sdk),
            "PHOTO_ORGANIZER_INTEGRATION_GATE": str(integration),
            "PHOTO_ORGANIZER_IOS_GATE": str(ios),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stderr == ""
    assert result.stdout == (
        "PHASE1_RUNTIME_READINESS mobile=missing android=java_missing android_sdk=missing ios=simulator_sdk_missing "
        "integration=environment_missing\n"
    )


def test_runtime_readiness_reports_all_local_gates_ready(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[4]
    python = tmp_path / "python-shim"
    android = tmp_path / "android-gate"
    android_sdk = tmp_path / "android-sdk-gate"
    ios = tmp_path / "ios-gate"
    integration = tmp_path / "integration-gate"
    python.write_text("#!/bin/sh\nprintf '%s\\n' mobile_sdk_ready\n", encoding="utf-8")
    android.write_text(
        "#!/bin/sh\nprintf '%s\\n' PHASE1_ANDROID_NATIVE_OFFLINE_CHECK_OK\n",
        encoding="utf-8",
    )
    android_sdk.write_text(
        "#!/bin/sh\nprintf '%s\\n' PHASE1_ANDROID_SDK_OFFLINE_CHECK_OK\n",
        encoding="utf-8",
    )
    ios.write_text(
        "#!/bin/sh\nprintf '%s\\n' PHASE1_IOS_SIMULATOR_OFFLINE_CHECK_OK\n",
        encoding="utf-8",
    )
    integration.write_text(
        "#!/bin/sh\nprintf '%s\\n' PHASE1_ISOLATED_INTEGRATION_CHECK_OK\n",
        encoding="utf-8",
    )
    for executable in (python, android, android_sdk, ios, integration):
        executable.chmod(0o755)

    result = subprocess.run(
        ["sh", str(root / "scripts" / "phase1_runtime_readiness.sh")],
        cwd=root,
        env={
            "PATH": "/usr/bin:/bin",
            "PHOTO_ORGANIZER_PYTHON": str(python),
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


@pytest.mark.parametrize(
    ("ios_error", "expected_state"),
    (
        ("phase1_ios_simulator_runtime_missing", "simulator_runtime_missing"),
        ("phase1_ios_simulator_device_missing", "simulator_device_missing"),
    ),
)
def test_runtime_readiness_reports_ios_runtime_and_device_gaps(
    tmp_path: Path,
    ios_error: str,
    expected_state: str,
) -> None:
    root = Path(__file__).resolve().parents[4]
    python = tmp_path / "python-shim"
    android = tmp_path / "android-gate"
    android_sdk = tmp_path / "android-sdk-gate"
    ios = tmp_path / "ios-gate"
    integration = tmp_path / "integration-gate"
    python.write_text("#!/bin/sh\nprintf '%s\\n' mobile_sdk_ready\n", encoding="utf-8")
    android.write_text(
        "#!/bin/sh\nprintf '%s\\n' PHASE1_ANDROID_NATIVE_OFFLINE_CHECK_OK\n",
        encoding="utf-8",
    )
    android_sdk.write_text(
        "#!/bin/sh\nprintf '%s\\n' PHASE1_ANDROID_SDK_OFFLINE_CHECK_OK\n",
        encoding="utf-8",
    )
    ios.write_text(
        f"#!/bin/sh\nprintf '%s\\n' {ios_error} >&2\nexit 1\n",
        encoding="utf-8",
    )
    integration.write_text(
        "#!/bin/sh\nprintf '%s\\n' PHASE1_ISOLATED_INTEGRATION_CHECK_OK\n",
        encoding="utf-8",
    )
    for executable in (python, android, android_sdk, ios, integration):
        executable.chmod(0o755)

    result = subprocess.run(
        ["sh", str(root / "scripts" / "phase1_runtime_readiness.sh")],
        cwd=root,
        env={
            "PATH": "/usr/bin:/bin",
            "PHOTO_ORGANIZER_PYTHON": str(python),
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
        "PHASE1_RUNTIME_READINESS mobile=ready android=ready android_sdk=ready "
        f"ios={expected_state} integration=ready\n"
    )
    assert result.stderr == ""


def test_runtime_readiness_reports_android_sdk_readiness_separately(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[4]
    python = tmp_path / "python-shim"
    android = tmp_path / "android-gate"
    android_sdk = tmp_path / "android-sdk-gate"
    ios = tmp_path / "ios-gate"
    integration = tmp_path / "integration-gate"
    python.write_text("#!/bin/sh\nprintf '%s\\n' mobile_sdk_ready\n", encoding="utf-8")
    android.write_text(
        "#!/bin/sh\nprintf '%s\\n' PHASE1_ANDROID_NATIVE_OFFLINE_CHECK_OK\n",
        encoding="utf-8",
    )
    android_sdk.write_text(
        "#!/bin/sh\nprintf '%s\\n' phase1_android_sdk_missing >&2\nexit 1\n",
        encoding="utf-8",
    )
    ios.write_text(
        "#!/bin/sh\nprintf '%s\\n' PHASE1_IOS_SIMULATOR_OFFLINE_CHECK_OK\n",
        encoding="utf-8",
    )
    integration.write_text(
        "#!/bin/sh\nprintf '%s\\n' PHASE1_ISOLATED_INTEGRATION_CHECK_OK\n",
        encoding="utf-8",
    )
    for executable in (python, android, android_sdk, ios, integration):
        executable.chmod(0o755)

    result = subprocess.run(
        ["sh", str(root / "scripts" / "phase1_runtime_readiness.sh")],
        cwd=root,
        env={
            "PATH": "/usr/bin:/bin",
            "PHOTO_ORGANIZER_PYTHON": str(python),
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
        "PHASE1_RUNTIME_READINESS mobile=ready android=ready android_sdk=missing "
        "ios=ready integration=ready\n"
    )
    assert result.stderr == ""


def test_runtime_readiness_redacts_unexpected_gate_output(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[4]
    python = tmp_path / "python-shim"
    android = tmp_path / "android-gate"
    android_sdk = tmp_path / "android-sdk-gate"
    ios = tmp_path / "ios-gate"
    integration = tmp_path / "integration-gate"
    python.write_text("#!/bin/sh\nprintf '%s\\n' /private/secret/flutter >&2\nexit 9\n")
    android.write_text(
        "#!/bin/sh\nprintf '%s\\n' PHASE1_ANDROID_NATIVE_OFFLINE_CHECK_OK\n",
        encoding="utf-8",
    )
    android_sdk.write_text(
        "#!/bin/sh\nprintf '%s\\n' PHASE1_ANDROID_SDK_OFFLINE_CHECK_OK\n",
        encoding="utf-8",
    )
    ios.write_text(
        "#!/bin/sh\nprintf '%s\\n' PHASE1_IOS_SIMULATOR_OFFLINE_CHECK_OK\n",
        encoding="utf-8",
    )
    integration.write_text(
        "#!/bin/sh\nprintf '%s\\n' PHASE1_ISOLATED_INTEGRATION_CHECK_OK\n",
        encoding="utf-8",
    )
    for executable in (python, android, android_sdk, ios, integration):
        executable.chmod(0o755)

    result = subprocess.run(
        ["sh", str(root / "scripts" / "phase1_runtime_readiness.sh")],
        cwd=root,
        env={
            "PATH": "/usr/bin:/bin",
            "PHOTO_ORGANIZER_PYTHON": str(python),
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
        "PHASE1_RUNTIME_READINESS mobile=unknown android=ready android_sdk=ready ios=ready integration=ready\n"
    )
    assert result.stderr == ""
    assert "secret" not in result.stdout


def test_synthetic_upload_preflight_requires_all_runtime_gates(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[4]
    readiness = tmp_path / "readiness-gate"
    readiness.write_text(
        "#!/bin/sh\nprintf '%s\\n' "
        "'PHASE1_RUNTIME_READINESS mobile=ready android=ready android_sdk=ready ios=ready integration=ready'\n",
        encoding="utf-8",
    )
    readiness.chmod(0o755)

    result = subprocess.run(
        ["sh", str(root / "scripts" / "phase1_synthetic_upload_preflight.sh")],
        cwd=root,
        env={
            "PATH": "/usr/bin:/bin",
            "PHOTO_ORGANIZER_RUNTIME_READINESS_GATE": str(readiness),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == "PHASE1_SYNTHETIC_UPLOAD_PREFLIGHT_OK\n"
    assert result.stderr == ""


def test_synthetic_upload_preflight_fails_closed_when_runtime_is_not_ready(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[4]
    readiness = tmp_path / "readiness-gate"
    readiness.write_text(
        "#!/bin/sh\nprintf '%s\\n' "
        "'PHASE1_RUNTIME_READINESS mobile=missing android=ready android_sdk=ready ios=ready integration=ready'\n",
        encoding="utf-8",
    )
    readiness.chmod(0o755)

    result = subprocess.run(
        ["sh", str(root / "scripts" / "phase1_synthetic_upload_preflight.sh")],
        cwd=root,
        env={
            "PATH": "/usr/bin:/bin",
            "PHOTO_ORGANIZER_RUNTIME_READINESS_GATE": str(readiness),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "phase1_synthetic_upload_preflight_not_ready\n"


def test_ios_simulator_offline_check_rejects_a_missing_simulator_sdk(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[4]
    xcrun = tmp_path / "xcrun"
    xcrun.write_text("#!/bin/sh\nexit 1\n")
    xcrun.chmod(0o755)

    result = subprocess.run(
        ["sh", str(root / "scripts" / "phase1_ios_simulator_offline_check.sh")],
        cwd=root,
        env={"PATH": f"{tmp_path}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr == "phase1_ios_simulator_sdk_missing\n"


def test_ios_simulator_offline_check_rejects_a_nonexistent_sdk_path(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[4]
    xcrun = tmp_path / "xcrun"
    xcrun.write_text(
        "#!/bin/sh\nprintf '%s\\n' /private/missing-simulator-sdk\n",
        encoding="utf-8",
    )
    xcrun.chmod(0o755)

    result = subprocess.run(
        ["sh", str(root / "scripts" / "phase1_ios_simulator_offline_check.sh")],
        cwd=root,
        env={"PATH": f"{tmp_path}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr == "phase1_ios_simulator_sdk_missing\n"


def test_ios_simulator_offline_check_rejects_a_missing_runtime(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[4]
    sdk_path = tmp_path / "simulator-sdk"
    sdk_path.mkdir()
    xcrun = tmp_path / "xcrun"
    xcrun.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        f"  '--sdk iphonesimulator --show-sdk-path') printf '%s\\n' '{sdk_path}' ;;\n"
        "  'simctl list runtimes available') printf '%s\\n' '== Runtimes ==' ;;\n"
        "  *) exit 1 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    xcrun.chmod(0o755)

    result = subprocess.run(
        ["sh", str(root / "scripts" / "phase1_ios_simulator_offline_check.sh")],
        cwd=root,
        env={"PATH": f"{tmp_path}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr == "phase1_ios_simulator_runtime_missing\n"


def test_ios_simulator_offline_check_rejects_a_missing_device(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[4]
    sdk_path = tmp_path / "simulator-sdk"
    sdk_path.mkdir()
    xcrun = tmp_path / "xcrun"
    xcrun.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        f"  '--sdk iphonesimulator --show-sdk-path') printf '%s\\n' '{sdk_path}' ;;\n"
        "  'simctl list runtimes available') printf '%s\\n' "
        "'iOS 26.5 (26.5 - 23F77) - com.apple.CoreSimulator.SimRuntime.iOS-26-5' ;;\n"
        "  'simctl list devices available') printf '%s\\n' '== Devices ==' ;;\n"
        "  *) exit 1 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    xcrun.chmod(0o755)

    result = subprocess.run(
        ["sh", str(root / "scripts" / "phase1_ios_simulator_offline_check.sh")],
        cwd=root,
        env={"PATH": f"{tmp_path}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr == "phase1_ios_simulator_device_missing\n"


def test_ios_simulator_offline_check_rejects_a_non_ios_device(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[4]
    sdk_path = tmp_path / "simulator-sdk"
    sdk_path.mkdir()
    xcrun = tmp_path / "xcrun"
    xcrun.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        f"  '--sdk iphonesimulator --show-sdk-path') printf '%s\\n' '{sdk_path}' ;;\n"
        "  'simctl list runtimes available') printf '%s\\n' "
        "'iOS 26.5 (26.5 - 23F77) - com.apple.CoreSimulator.SimRuntime.iOS-26-5' ;;\n"
        "  'simctl list devices available') printf '%s\\n' "
        "'-- iOS 26.5 --' "
        "'-- tvOS 26.5 --' "
        "'    Apple TV (01234567-89AB-CDEF-0123-456789ABCDEF) (Shutdown)' ;;\n"
        "  *) exit 1 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    xcrun.chmod(0o755)

    result = subprocess.run(
        ["sh", str(root / "scripts" / "phase1_ios_simulator_offline_check.sh")],
        cwd=root,
        env={"PATH": f"{tmp_path}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr == "phase1_ios_simulator_device_missing\n"


def test_ios_simulator_offline_check_accepts_a_complete_simulator_environment(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[4]
    sdk_path = tmp_path / "simulator-sdk"
    sdk_path.mkdir()
    xcrun = tmp_path / "xcrun"
    xcrun.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        f"  '--sdk iphonesimulator --show-sdk-path') printf '%s\\n' '{sdk_path}' ;;\n"
        "  'simctl list runtimes available') printf '%s\\n' "
        "'iOS 26.5 (26.5 - 23F77) - com.apple.CoreSimulator.SimRuntime.iOS-26-5' ;;\n"
        "  'simctl list devices available') printf '%s\\n' "
        "'-- iOS 26.5 --' "
        "'    iPhone 17 Pro (01234567-89AB-CDEF-0123-456789ABCDEF) (Shutdown)' ;;\n"
        "  *) exit 1 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    xcrun.chmod(0o755)

    result = subprocess.run(
        ["sh", str(root / "scripts" / "phase1_ios_simulator_offline_check.sh")],
        cwd=root,
        env={"PATH": f"{tmp_path}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == "PHASE1_IOS_SIMULATOR_OFFLINE_CHECK_OK\n"
    assert result.stderr == ""


def test_ios_native_check_builds_and_tests_on_an_available_simulator(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[4]
    sdk_path = tmp_path / "simulator-sdk"
    sdk_path.mkdir()
    command_log = tmp_path / "commands.log"
    device_id = "01234567-89AB-CDEF-0123-456789ABCDEF"

    xcrun = tmp_path / "xcrun"
    xcrun.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        f"  '--sdk iphonesimulator --show-sdk-path') printf '%s\\n' '{sdk_path}' ;;\n"
        "  'simctl list runtimes available') printf '%s\\n' "
        "'iOS 26.5 (26.5 - 23F77) - com.apple.CoreSimulator.SimRuntime.iOS-26-5' ;;\n"
        "  'simctl list devices available') printf '%s\\n' "
        "'-- iOS 26.5 --' "
        f"'    iPhone 17 Pro ({device_id}) (Shutdown)' ;;\n"
        "  *) exit 1 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    flutter = tmp_path / "flutter"
    flutter.write_text(
        '#!/bin/sh\nprintf \'flutter:%s\\n\' "$*" >> "$PHOTO_ORGANIZER_COMMAND_LOG"\n',
        encoding="utf-8",
    )
    xcodebuild = tmp_path / "xcodebuild"
    xcodebuild.write_text(
        '#!/bin/sh\nprintf \'xcodebuild:%s\\n\' "$*" >> "$PHOTO_ORGANIZER_COMMAND_LOG"\n',
        encoding="utf-8",
    )
    for executable in (xcrun, flutter, xcodebuild):
        executable.chmod(0o755)

    result = subprocess.run(
        ["sh", str(root / "scripts" / "phase1_ios_native_check.sh")],
        cwd=root,
        env={
            "PATH": f"{tmp_path}:/usr/bin:/bin",
            "PHOTO_ORGANIZER_FLUTTER": str(flutter),
            "PHOTO_ORGANIZER_XCODEBUILD": str(xcodebuild),
            "PHOTO_ORGANIZER_COMMAND_LOG": str(command_log),
            "PHOTO_ORGANIZER_FLUTTER_CONFIG_HOME": str(tmp_path / "flutter-home"),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == "PHASE1_IOS_NATIVE_CHECK_OK\n"
    assert result.stderr == ""
    assert command_log.read_text(encoding="utf-8").splitlines() == [
        "flutter:pub get --offline",
        "flutter:build ios --simulator --debug",
        (
            "xcodebuild:test -quiet -project ios/Runner.xcodeproj -scheme Runner "
            f"-destination platform=iOS Simulator,id={device_id} CODE_SIGNING_ALLOWED=NO"
        ),
    ]


def test_android_sdk_offline_check_accepts_a_configured_sdk_directory(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[4]
    sdk_path = tmp_path / "android-sdk"
    (sdk_path / "platforms").mkdir(parents=True)
    (sdk_path / "build-tools").mkdir()
    properties = tmp_path / "local.properties"
    properties.write_text(f"sdk.dir={sdk_path}\n", encoding="utf-8")

    result = subprocess.run(
        ["sh", str(root / "scripts" / "phase1_android_sdk_offline_check.sh")],
        cwd=root,
        env={
            "PATH": "/usr/bin:/bin",
            "PHOTO_ORGANIZER_ANDROID_LOCAL_PROPERTIES": str(properties),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == "PHASE1_ANDROID_SDK_OFFLINE_CHECK_OK\n"
    assert result.stderr == ""


def _load_script(name: str) -> ModuleType:
    root = Path(__file__).resolve().parents[4]
    path = root / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fixture_images_are_valid_deterministic_jpegs(tmp_path: Path) -> None:
    generator = _load_script("generate_fixture_images")
    first = tmp_path / "first"
    second = tmp_path / "second"

    generator.generate(count=3, output=first, overwrite=False)
    generator.generate(count=3, output=second, overwrite=False)

    first_manifest = json.loads((first / "manifest.json").read_text())
    second_manifest = json.loads((second / "manifest.json").read_text())
    assert first_manifest == second_manifest
    assert first_manifest["count"] == 3
    assert len({asset["sha256"] for asset in first_manifest["assets"]}) == 3
    for asset in first_manifest["assets"]:
        image_path = first / asset["filename"]
        assert image_path.stat().st_size == asset["byte_size"]
        with Image.open(image_path) as image:
            assert image.format == "JPEG"
            assert image.size == (640, 640)


def test_fixture_generator_refuses_nonempty_output_without_overwrite(
    tmp_path: Path,
) -> None:
    generator = _load_script("generate_fixture_images")
    output = tmp_path / "fixtures"
    output.mkdir()
    (output / "keep.txt").write_text("do not replace")

    with pytest.raises(FileExistsError, match="non-empty"):
        generator.generate(count=1, output=output, overwrite=False)

    assert (output / "keep.txt").read_text() == "do not replace"


def test_fixture_generator_supports_ten_thousand_rehearsal_assets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generator = _load_script("generate_fixture_images")
    generated_indexes: list[int] = []

    def write_lightweight_fixture(*, index: int, output: Path, font: object):
        del output, font
        generated_indexes.append(index)
        return {
            "index": index,
            "local_asset_id": f"fixture-{index:04d}",
            "filename": f"image-{index:04d}.jpg",
            "media_type": "image/jpeg",
            "byte_size": index,
            "sha256": f"{index:064x}",
        }

    monkeypatch.setattr(generator, "_write_fixture", write_lightweight_fixture)

    manifest_path = generator.generate(
        count=10_000,
        output=tmp_path / "fixtures",
        overwrite=False,
    )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert generated_indexes == list(range(1, 10_001))
    assert manifest["count"] == 10_000
    assert manifest["assets"][0]["local_asset_id"] == "fixture-0001"
    assert manifest["assets"][-1]["local_asset_id"] == "fixture-10000"


def test_fixture_generator_rejects_more_than_ten_thousand_assets(
    tmp_path: Path,
) -> None:
    generator = _load_script("generate_fixture_images")

    with pytest.raises(ValueError, match="between 1 and 10000"):
        generator.generate(
            count=10_001,
            output=tmp_path / "fixtures",
            overwrite=False,
        )


@pytest.mark.asyncio
async def test_smoke_uses_real_multipart_contract_without_leaking_token(
    tmp_path: Path,
) -> None:
    generator = _load_script("generate_fixture_images")
    smoke = _load_script("ingestion_smoke")
    manifest = generator.generate(
        count=2,
        output=tmp_path / "fixtures",
        overwrite=False,
    )
    task_id = "11111111-1111-4111-8111-111111111111"
    assets: dict[str, str] = {}
    completed = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal completed
        if request.url.host == "storage.test":
            assert request.method == "PUT"
            assert "Authorization" not in request.headers
            return httpx.Response(200, headers={"ETag": '"fixture-etag"'})
        assert request.headers["Authorization"] == "Bearer test-access-token"
        if request.method == "POST" and request.url.path == "/v1/tasks":
            return httpx.Response(201, json={"id": task_id})
        if request.url.path.endswith("/assets/initiate"):
            body = json.loads(request.content)
            asset_id = str(uuid5(UUID(task_id), body["local_asset_id"]))
            assets[asset_id] = body["local_asset_id"]
            return httpx.Response(
                201,
                json={
                    "asset_id": asset_id,
                    "already_uploaded": False,
                    "provider_upload_id": "provider-id",
                    "part_size": 8 * 1024 * 1024,
                    "completed_parts": [],
                },
            )
        if request.url.path.endswith("/parts/sign"):
            asset_id = request.url.path.split("/")[3]
            return httpx.Response(
                200,
                json={
                    "url": f"https://storage.test/{asset_id}/part",
                    "expires_seconds": 900,
                },
            )
        if request.url.path.endswith("/complete"):
            body = json.loads(request.content)
            assert body["parts"] == [{"part_number": 1, "etag": "fixture-etag"}]
            completed += 1
            return httpx.Response(200, json={"status": "uploaded"})
        if request.url.path.endswith("/seal"):
            return httpx.Response(200, json={"status": "ai_processing"})
        if request.method == "GET" and request.url.path == f"/v1/tasks/{task_id}":
            return httpx.Response(
                200,
                json={
                    "id": task_id,
                    "status": "ai_processing",
                    "total_assets": 2,
                    "uploaded_assets": 2,
                    "failed_assets": 0,
                    "unsupported_assets": 0,
                },
            )
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    object_counts = iter((0, 2))
    async with httpx.AsyncClient(
        base_url="http://api.test",
        transport=httpx.MockTransport(handler),
    ) as client:
        result = await smoke.run_smoke(
            client=client,
            access_token="test-access-token",
            manifest_path=manifest,
            object_count=lambda: next(object_counts),
        )

    assert completed == 2
    assert len(assets) == 2
    assert result == "PASS: 2 accepted, 2 uploaded, 0 omitted, task sealed once"


def test_offline_smoke_exercises_the_contract_without_network_access(
    tmp_path: Path,
) -> None:
    generator = _load_script("generate_fixture_images")
    manifest = generator.generate(
        count=2,
        output=tmp_path / "fixtures",
        overwrite=False,
    )
    root = Path(__file__).resolve().parents[4]

    result = subprocess.run(
        [
            sys.executable,
            str(root / "scripts" / "ingestion_smoke.py"),
            "--offline-manifest",
            str(manifest),
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == (
        "OFFLINE PASS: 2 accepted, 2 uploaded, 0 omitted, task sealed once\n"
    )
    assert result.stderr == ""


def test_offline_smoke_redacts_a_missing_manifest_path() -> None:
    root = Path(__file__).resolve().parents[4]
    missing_manifest = Path("/private/secret/missing-fixture-manifest.json")

    result = subprocess.run(
        [
            sys.executable,
            str(root / "scripts" / "ingestion_smoke.py"),
            "--offline-manifest",
            str(missing_manifest),
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr == "ingestion_smoke_failed\n"


def test_mini_night_entry_runs_only_the_synthetic_offline_checks(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[4]

    result = subprocess.run(
        ["sh", str(root / "scripts" / "mini_night_check.sh")],
        cwd=root,
        env={
            "PATH": "/usr/bin:/bin",
            "PHOTO_ORGANIZER_PYTHON": sys.executable,
            "PHOTO_ORGANIZER_FIXTURE_COUNT": "2",
            "TMPDIR": str(tmp_path),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "mobile_controlled_upload_static_ok" in result.stdout
    assert "mobile_controlled_upload_offline_ok sdk=missing" in result.stdout
    assert (
        "OFFLINE PASS: 2 accepted, 2 uploaded, 0 omitted, task sealed once"
        in result.stdout
    )
    assert "MINI_OFFLINE_CHECK_OK count=2 sdk=missing" in result.stdout
    assert "generated " not in result.stdout
    assert "photo-organizer-mini" not in result.stdout
    assert result.stderr == "mobile_sdk_missing\n"


def test_mini_night_entry_cleans_synthetic_files_after_a_fixture_failure(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[4]

    result = subprocess.run(
        ["sh", str(root / "scripts" / "mini_night_check.sh")],
        cwd=root,
        env={
            "PATH": "/usr/bin:/bin",
            "PHOTO_ORGANIZER_PYTHON": sys.executable,
            "PHOTO_ORGANIZER_FIXTURE_COUNT": "0",
            "TMPDIR": str(tmp_path),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert result.stderr == "mobile_sdk_missing\nmini_fixture_generation_failed\n"
    assert list(tmp_path.iterdir()) == []


def test_mini_night_entry_redacts_a_missing_python_runtime(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[4]

    result = subprocess.run(
        ["sh", str(root / "scripts" / "mini_night_check.sh")],
        cwd=root,
        env={
            "PATH": "/usr/bin:/bin",
            "PHOTO_ORGANIZER_PYTHON": "/private/tmp/not-a-mini-runtime",
            "TMPDIR": str(tmp_path),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr == "mini_python_missing\n"
    assert list(tmp_path.iterdir()) == []


def test_mini_night_entry_redacts_an_unavailable_temp_directory() -> None:
    root = Path(__file__).resolve().parents[4]

    result = subprocess.run(
        ["sh", str(root / "scripts" / "mini_night_check.sh")],
        cwd=root,
        env={
            "PATH": "/usr/bin:/bin",
            "PHOTO_ORGANIZER_PYTHON": sys.executable,
            "TMPDIR": "/private/tmp/not-an-available-mini-temp-directory",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr == "mini_temp_directory_unavailable\n"


def test_mini_night_entry_redacts_an_offline_smoke_failure(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[4]
    shim = tmp_path / "mini-python-shim"
    shim.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  */ingestion_smoke.py) echo '/private/secret/offline-smoke-error' >&2; exit 9 ;;\n"
        '  *) exec "$MINI_REAL_PYTHON" "$@" ;;\n'
        "esac\n"
    )
    shim.chmod(0o755)

    result = subprocess.run(
        ["sh", str(root / "scripts" / "mini_night_check.sh")],
        cwd=root,
        env={
            "PATH": "/usr/bin:/bin",
            "MINI_REAL_PYTHON": sys.executable,
            "PHOTO_ORGANIZER_PYTHON": str(shim),
            "PHOTO_ORGANIZER_FIXTURE_COUNT": "2",
            "TMPDIR": str(tmp_path),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "/private/secret" not in result.stdout + result.stderr
    assert result.stderr == "mobile_sdk_missing\nmini_offline_smoke_failed\n"
    assert list(tmp_path.iterdir()) == [shim]


def test_mini_night_entry_redacts_a_static_check_failure(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[4]
    shim = tmp_path / "mini-python-shim"
    shim.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  */verify_mobile_controlled_upload_static.py) echo '/private/secret/static-check-error' >&2; exit 9 ;;\n"
        '  *) exec "$MINI_REAL_PYTHON" "$@" ;;\n'
        "esac\n"
    )
    shim.chmod(0o755)

    result = subprocess.run(
        ["sh", str(root / "scripts" / "mini_night_check.sh")],
        cwd=root,
        env={
            "PATH": "/usr/bin:/bin",
            "MINI_REAL_PYTHON": sys.executable,
            "PHOTO_ORGANIZER_PYTHON": str(shim),
            "TMPDIR": str(tmp_path),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "/private/secret" not in result.stdout + result.stderr
    assert result.stderr == "mini_mobile_static_check_failed\n"
    assert list(tmp_path.iterdir()) == [shim]


def test_mini_night_entry_redacts_an_unexpected_sdk_gate_failure(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[4]
    shim = tmp_path / "mini-python-shim"
    shim.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  */check_mobile_sdk_gate.py) echo '/private/secret/sdk-gate-error' >&2; exit 9 ;;\n"
        '  *) exec "$MINI_REAL_PYTHON" "$@" ;;\n'
        "esac\n"
    )
    shim.chmod(0o755)

    result = subprocess.run(
        ["sh", str(root / "scripts" / "mini_night_check.sh")],
        cwd=root,
        env={
            "PATH": "/usr/bin:/bin",
            "MINI_REAL_PYTHON": sys.executable,
            "PHOTO_ORGANIZER_PYTHON": str(shim),
            "TMPDIR": str(tmp_path),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "/private/secret" not in result.stdout + result.stderr
    assert result.stderr == "mini_mobile_sdk_gate_invalid\n"
    assert list(tmp_path.iterdir()) == [shim]


def test_mini_night_handoff_gate_reports_a_clean_synthetic_candidate(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[4]
    git = tmp_path / "mini-git-shim"
    candidate_sha = "a" * 40
    git.write_text(
        "#!/bin/sh\n"
        'case "$1 $2" in\n'
        "  'diff --check') exit 0 ;;\n"
        "  'status --porcelain') exit 0 ;;\n"
        f"  'rev-parse --verify') echo '{candidate_sha}' ;;\n"
        "  *) exit 9 ;;\n"
        "esac\n"
    )
    git.chmod(0o755)

    result = subprocess.run(
        ["sh", str(root / "scripts" / "mini_night_handoff_gate.sh")],
        cwd=root,
        env={
            "PATH": "/usr/bin:/bin",
            "PHOTO_ORGANIZER_GIT": str(git),
            "PHOTO_ORGANIZER_PYTHON": sys.executable,
            "PHOTO_ORGANIZER_FIXTURE_COUNT": "2",
            "TMPDIR": str(tmp_path),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert (
        "OFFLINE PASS: 2 accepted, 2 uploaded, 0 omitted, task sealed once"
        in result.stdout
    )
    assert (
        f"MINI_NIGHT_HANDOFF_GATE_OK sha={candidate_sha} scope=synthetic_only"
        in result.stdout
    )
    assert result.stderr == "mobile_sdk_missing\n"


def test_mini_night_handoff_gate_fails_closed_when_worktree_status_is_unknown(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[4]
    git = tmp_path / "mini-git-shim"
    git.write_text(
        "#!/bin/sh\n"
        'case "$1 $2" in\n'
        "  'diff --check') exit 0 ;;\n"
        "  'status --porcelain') exit 9 ;;\n"
        "  *) exit 9 ;;\n"
        "esac\n"
    )
    git.chmod(0o755)

    result = subprocess.run(
        ["sh", str(root / "scripts" / "mini_night_handoff_gate.sh")],
        cwd=root,
        env={
            "PATH": "/usr/bin:/bin",
            "PHOTO_ORGANIZER_GIT": str(git),
            "PHOTO_ORGANIZER_PYTHON": sys.executable,
            "TMPDIR": str(tmp_path),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr == "mini_candidate_worktree_status_invalid\n"


def test_mobile_synthetic_check_runs_flutter_validation_without_photo_access(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[4]
    flutter = tmp_path / "flutter-shim"
    dart = tmp_path / "dart-shim"
    flutter_config = tmp_path / "flutter-config"
    invocations = tmp_path / "flutter-invocations"
    flutter.write_text(
        '#!/bin/sh\nprintf \'flutter:%s|%s|%s\\n\' "$BOT" "$XDG_CONFIG_HOME" "$*" >> "$FLUTTER_INVOCATIONS"\nexit 0\n'
    )
    dart.write_text(
        '#!/bin/sh\nprintf \'dart:%s\\n\' "$*" >> "$FLUTTER_INVOCATIONS"\nexit 0\n'
    )
    flutter.chmod(0o755)
    dart.chmod(0o755)

    result = subprocess.run(
        ["sh", str(root / "scripts" / "phase1_mobile_synthetic_check.sh")],
        cwd=root,
        env={
            "PATH": "/usr/bin:/bin",
            "PHOTO_ORGANIZER_FLUTTER": str(flutter),
            "PHOTO_ORGANIZER_DART": str(dart),
            "PHOTO_ORGANIZER_FLUTTER_CONFIG_HOME": str(flutter_config),
            "FLUTTER_INVOCATIONS": str(invocations),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == "PHASE1_MOBILE_SYNTHETIC_CHECK_OK android=not_requested\n"
    assert result.stderr == ""
    assert invocations.read_text().splitlines() == [
        f"flutter:true|{flutter_config}|pub get --offline",
        "dart:analyze",
        f"flutter:true|{flutter_config}|test",
    ]


def test_mobile_synthetic_check_optionally_runs_android_unit_tests(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[4]
    flutter = tmp_path / "flutter-shim"
    dart = tmp_path / "dart-shim"
    flutter_config = tmp_path / "flutter-config"
    gradle = tmp_path / "gradle-shim"
    invocations = tmp_path / "mobile-invocations"
    flutter.write_text(
        '#!/bin/sh\nprintf \'flutter:%s|%s|%s\\n\' "$BOT" "$XDG_CONFIG_HOME" "$*" >> "$MOBILE_INVOCATIONS"\nexit 0\n'
    )
    dart.write_text(
        '#!/bin/sh\nprintf \'dart:%s\\n\' "$*" >> "$MOBILE_INVOCATIONS"\nexit 0\n'
    )
    gradle.write_text(
        '#!/bin/sh\nprintf \'gradle:%s|%s\\n\' "$PWD" "$*" >> "$MOBILE_INVOCATIONS"\nexit 0\n'
    )
    flutter.chmod(0o755)
    dart.chmod(0o755)
    gradle.chmod(0o755)

    result = subprocess.run(
        ["sh", str(root / "scripts" / "phase1_mobile_synthetic_check.sh")],
        cwd=root,
        env={
            "PATH": "/usr/bin:/bin",
            "PHOTO_ORGANIZER_FLUTTER": str(flutter),
            "PHOTO_ORGANIZER_DART": str(dart),
            "PHOTO_ORGANIZER_FLUTTER_CONFIG_HOME": str(flutter_config),
            "PHOTO_ORGANIZER_RUN_ANDROID_TESTS": "1",
            "PHOTO_ORGANIZER_GRADLE": str(gradle),
            "MOBILE_INVOCATIONS": str(invocations),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == "PHASE1_MOBILE_SYNTHETIC_CHECK_OK android=checked\n"
    assert result.stderr == ""
    assert invocations.read_text().splitlines() == [
        f"flutter:true|{flutter_config}|pub get --offline",
        "dart:analyze",
        f"flutter:true|{flutter_config}|test",
        f"gradle:{root / 'apps' / 'mobile' / 'android'}|:app:testDebugUnitTest --offline --no-daemon",
    ]


def test_mobile_synthetic_check_rejects_an_invalid_android_option_before_flutter(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[4]
    flutter = tmp_path / "flutter-shim"
    invocations = tmp_path / "flutter-invocations"
    flutter.write_text(
        '#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$FLUTTER_INVOCATIONS"\nexit 0\n'
    )
    flutter.chmod(0o755)

    result = subprocess.run(
        ["sh", str(root / "scripts" / "phase1_mobile_synthetic_check.sh")],
        cwd=root,
        env={
            "PATH": "/usr/bin:/bin",
            "PHOTO_ORGANIZER_FLUTTER": str(flutter),
            "PHOTO_ORGANIZER_RUN_ANDROID_TESTS": "unexpected",
            "FLUTTER_INVOCATIONS": str(invocations),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr == "phase1_mobile_android_option_invalid\n"
    assert not invocations.exists()


def test_android_native_offline_check_requires_a_local_java_runtime(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[4]
    gradle = tmp_path / "gradle-shim"
    gradle.write_text("#!/bin/sh\nexit 0\n")
    gradle.chmod(0o755)

    result = subprocess.run(
        ["sh", str(root / "scripts" / "phase1_android_native_offline_check.sh")],
        cwd=root,
        env={
            "PATH": "/usr/bin:/bin",
            "PHOTO_ORGANIZER_GRADLE": str(gradle),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr == "phase1_android_java_missing\n"


def test_android_native_offline_check_runs_gradle_offline(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[4]
    java = tmp_path / "java"
    gradle = tmp_path / "gradle-shim"
    invocations = tmp_path / "gradle-invocations"
    java.write_text("#!/bin/sh\nexit 0\n")
    gradle.write_text(
        '#!/bin/sh\nprintf \'%s|%s\\n\' "$PWD" "$*" >> "$GRADLE_INVOCATIONS"\nexit 0\n'
    )
    java.chmod(0o755)
    gradle.chmod(0o755)

    result = subprocess.run(
        ["sh", str(root / "scripts" / "phase1_android_native_offline_check.sh")],
        cwd=root,
        env={
            "PATH": f"{tmp_path}:/usr/bin:/bin",
            "PHOTO_ORGANIZER_GRADLE": str(gradle),
            "GRADLE_INVOCATIONS": str(invocations),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == "PHASE1_ANDROID_NATIVE_OFFLINE_CHECK_OK\n"
    assert result.stderr == ""
    assert invocations.read_text().splitlines() == [
        f"{root / 'apps' / 'mobile' / 'android'}|:app:testDebugUnitTest --offline --no-daemon"
    ]


def test_android_native_offline_check_accepts_an_explicit_approved_java(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[4]
    java = tmp_path / "approved-java"
    gradle = tmp_path / "gradle-shim"
    java.write_text("#!/bin/sh\nexit 0\n")
    gradle.write_text("#!/bin/sh\nexit 0\n")
    java.chmod(0o755)
    gradle.chmod(0o755)

    result = subprocess.run(
        ["sh", str(root / "scripts" / "phase1_android_native_offline_check.sh")],
        cwd=root,
        env={
            "PATH": "/usr/bin:/bin",
            "PHOTO_ORGANIZER_JAVA": str(java),
            "PHOTO_ORGANIZER_GRADLE": str(gradle),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == "PHASE1_ANDROID_NATIVE_OFFLINE_CHECK_OK\n"
    assert result.stderr == ""


def test_isolated_integration_check_uses_only_explicit_test_environment(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[4]
    pytest_bin = tmp_path / "pytest-shim"
    invocations = tmp_path / "pytest-invocations"
    pytest_bin.write_text(
        '#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$PYTEST_INVOCATIONS"\nexit 0\n'
    )
    pytest_bin.chmod(0o755)

    result = subprocess.run(
        ["sh", str(root / "scripts" / "phase1_isolated_integration_check.sh")],
        cwd=root,
        env={
            "PATH": "/usr/bin:/bin",
            "PHOTO_ORGANIZER_PYTEST": str(pytest_bin),
            "PHOTO_ORGANIZER_TEST_DATABASE_URL": "postgresql://test:test@127.0.0.1:5432/photo_organizer_test",
            "MINIO_TEST_ENDPOINT": "http://127.0.0.1:9000",
            "MINIO_TEST_BUCKET": "photo-organizer-test",
            "MINIO_TEST_ACCESS_KEY": "test-access",
            "MINIO_TEST_SECRET_KEY": "test-secret",
            "PYTEST_INVOCATIONS": str(invocations),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == "PHASE1_ISOLATED_INTEGRATION_CHECK_OK\n"
    assert result.stderr == ""
    assert invocations.read_text().splitlines() == [
        "tests/integration -q -m integration"
    ]


def test_isolated_integration_check_rejects_a_nonlocal_storage_endpoint(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[4]
    pytest_bin = tmp_path / "pytest-shim"
    invocations = tmp_path / "pytest-invocations"
    pytest_bin.write_text(
        '#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$PYTEST_INVOCATIONS"\nexit 0\n'
    )
    pytest_bin.chmod(0o755)

    result = subprocess.run(
        ["sh", str(root / "scripts" / "phase1_isolated_integration_check.sh")],
        cwd=root,
        env={
            "PATH": "/usr/bin:/bin",
            "PHOTO_ORGANIZER_PYTEST": str(pytest_bin),
            "PHOTO_ORGANIZER_TEST_DATABASE_URL": "postgresql://test:test@127.0.0.1:5432/photo_organizer_test",
            "MINIO_TEST_ENDPOINT": "https://production.invalid",
            "MINIO_TEST_BUCKET": "photo-organizer-test",
            "MINIO_TEST_ACCESS_KEY": "test-access",
            "MINIO_TEST_SECRET_KEY": "test-secret",
            "PYTEST_INVOCATIONS": str(invocations),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr == "phase1_integration_endpoint_invalid\n"
    assert not invocations.exists()


def test_isolated_integration_check_rejects_a_nonlocal_database_url(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[4]
    pytest_bin = tmp_path / "pytest-shim"
    invocations = tmp_path / "pytest-invocations"
    pytest_bin.write_text(
        '#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$PYTEST_INVOCATIONS"\nexit 0\n'
    )
    pytest_bin.chmod(0o755)

    result = subprocess.run(
        ["sh", str(root / "scripts" / "phase1_isolated_integration_check.sh")],
        cwd=root,
        env={
            "PATH": "/usr/bin:/bin",
            "PHOTO_ORGANIZER_PYTEST": str(pytest_bin),
            "PHOTO_ORGANIZER_TEST_DATABASE_URL": "postgresql://production.invalid/test",
            "MINIO_TEST_ENDPOINT": "http://127.0.0.1:9000",
            "MINIO_TEST_BUCKET": "photo-organizer-test",
            "MINIO_TEST_ACCESS_KEY": "test-access",
            "MINIO_TEST_SECRET_KEY": "test-secret",
            "PYTEST_INVOCATIONS": str(invocations),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr == "phase1_integration_database_invalid\n"
    assert not invocations.exists()


def test_isolated_integration_check_rejects_a_non_test_bucket(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[4]
    pytest_bin = tmp_path / "pytest-shim"
    invocations = tmp_path / "pytest-invocations"
    pytest_bin.write_text(
        '#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$PYTEST_INVOCATIONS"\nexit 0\n'
    )
    pytest_bin.chmod(0o755)

    result = subprocess.run(
        ["sh", str(root / "scripts" / "phase1_isolated_integration_check.sh")],
        cwd=root,
        env={
            "PATH": "/usr/bin:/bin",
            "PHOTO_ORGANIZER_PYTEST": str(pytest_bin),
            "PHOTO_ORGANIZER_TEST_DATABASE_URL": "postgresql://test:test@127.0.0.1:5432/photo_organizer_test",
            "MINIO_TEST_ENDPOINT": "http://127.0.0.1:9000",
            "MINIO_TEST_BUCKET": "production-assets",
            "MINIO_TEST_ACCESS_KEY": "test-access",
            "MINIO_TEST_SECRET_KEY": "test-secret",
            "PYTEST_INVOCATIONS": str(invocations),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr == "phase1_integration_bucket_invalid\n"
    assert not invocations.exists()


def test_isolated_integration_check_rejects_a_local_non_test_database(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[4]
    pytest_bin = tmp_path / "pytest-shim"
    invocations = tmp_path / "pytest-invocations"
    pytest_bin.write_text(
        '#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$PYTEST_INVOCATIONS"\nexit 0\n'
    )
    pytest_bin.chmod(0o755)

    result = subprocess.run(
        ["sh", str(root / "scripts" / "phase1_isolated_integration_check.sh")],
        cwd=root,
        env={
            "PATH": "/usr/bin:/bin",
            "PHOTO_ORGANIZER_PYTEST": str(pytest_bin),
            "PHOTO_ORGANIZER_TEST_DATABASE_URL": "postgresql://test:test@127.0.0.1:5432/photo_organizer",
            "MINIO_TEST_ENDPOINT": "http://127.0.0.1:9000",
            "MINIO_TEST_BUCKET": "photo-organizer-test",
            "MINIO_TEST_ACCESS_KEY": "test-access",
            "MINIO_TEST_SECRET_KEY": "test-secret",
            "PYTEST_INVOCATIONS": str(invocations),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr == "phase1_integration_database_name_invalid\n"
    assert not invocations.exists()


def test_isolated_integration_check_rejects_non_test_storage_credentials(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[4]
    pytest_bin = tmp_path / "pytest-shim"
    invocations = tmp_path / "pytest-invocations"
    pytest_bin.write_text(
        '#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$PYTEST_INVOCATIONS"\nexit 0\n'
    )
    pytest_bin.chmod(0o755)

    result = subprocess.run(
        ["sh", str(root / "scripts" / "phase1_isolated_integration_check.sh")],
        cwd=root,
        env={
            "PATH": "/usr/bin:/bin",
            "PHOTO_ORGANIZER_PYTEST": str(pytest_bin),
            "PHOTO_ORGANIZER_TEST_DATABASE_URL": "postgresql://test:test@127.0.0.1:5432/photo_organizer_test",
            "MINIO_TEST_ENDPOINT": "http://127.0.0.1:9000",
            "MINIO_TEST_BUCKET": "photo-organizer-test",
            "MINIO_TEST_ACCESS_KEY": "minioadmin",
            "MINIO_TEST_SECRET_KEY": "minioadmin",
            "PYTEST_INVOCATIONS": str(invocations),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr == "phase1_integration_credentials_invalid\n"
    assert not invocations.exists()
