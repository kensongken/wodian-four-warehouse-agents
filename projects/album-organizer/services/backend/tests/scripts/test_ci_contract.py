import plistlib
import subprocess
import sys
from math import prod
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]


def _integer_product(source: str, declaration: str) -> int:
    expression = next(
        line.split("=", maxsplit=1)[1].strip().rstrip(";")
        for line in source.splitlines()
        if declaration in line
        and "=" in line
        and ("const" in line or "static let" in line)
    )
    factors = (
        factor.strip().replace("_", "").rstrip("L") for factor in expression.split("*")
    )
    return prod(int(factor) for factor in factors)


def _braced_function(source: str, marker: str) -> str:
    start = source.index(marker)
    opening = source.index("{", start)
    depth = 0
    for index in range(opening, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"unterminated function containing {marker!r}")


def _source_section(source: str, start_marker: str, end_marker: str) -> str:
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    return source[start:end]


def test_mobile_native_capacity_and_image_byte_contracts_stay_aligned() -> None:
    dart = (ROOT / "apps/mobile/lib/core/ingestion/ingestion_limits.dart").read_text(
        encoding="utf-8"
    )
    dart_media = (
        ROOT / "apps/mobile/lib/core/media/method_channel_media_library.dart"
    ).read_text(encoding="utf-8")
    swift_media = (ROOT / "apps/mobile/ios/Runner/MediaBridge.swift").read_text(
        encoding="utf-8"
    )
    swift_background = (
        ROOT / "apps/mobile/ios/Runner/BackgroundTransferBridge.swift"
    ).read_text(encoding="utf-8")
    kotlin_media = (
        ROOT
        / "apps/mobile/android/app/src/main/kotlin/com/example/photoorganizer/MediaBridge.kt"
    ).read_text(encoding="utf-8")
    kotlin_background = (
        ROOT
        / "apps/mobile/android/app/src/main/kotlin/com/example/photoorganizer/BackgroundTransferBridge.kt"
    ).read_text(encoding="utf-8")

    accepted_asset_ceilings = (
        _integer_product(dart, "maximumAcceptedAssets"),
        _integer_product(swift_media, "maximumAcceptedAssets"),
        _integer_product(kotlin_media, "MAXIMUM_ACCEPTED_ASSETS"),
    )
    ios_picker_selection_ceiling = _integer_product(
        swift_media, "maximumPickerSelectionLimit"
    )
    background_query_ceilings = (
        _integer_product(swift_background, "maximumQueryJobIds"),
        _integer_product(kotlin_background, "MAXIMUM_QUERY_JOB_IDS"),
    )
    image_byte_ceilings = (
        _integer_product(dart, "maximumImageBytes"),
        _integer_product(swift_media, "maximumAssetBytes"),
        _integer_product(kotlin_media, "MAXIMUM_ASSET_BYTES"),
    )

    assert accepted_asset_ceilings == (10_000, 10_000, 10_000)
    assert ios_picker_selection_ceiling == 10_000
    assert background_query_ceilings == (10_000, 10_000)
    assert image_byte_ceilings == (104_857_600, 104_857_600, 104_857_600)
    assert "remainingSlots > maximumAcceptedAssets" in dart_media
    assert "Self.isValidRemainingSlots(remainingSlots)" in swift_media
    assert "(1...maximumAcceptedAssets).contains(value)" in swift_media
    assert "ids.count <= maximumQueryJobIds" in swift_background
    assert "!isValidRemainingSlots(remainingSlots)" in kotlin_media
    assert "value in 1..MAXIMUM_ACCEPTED_ASSETS" in kotlin_media
    assert "ids.size > MAXIMUM_QUERY_JOB_IDS" in kotlin_background

    swift_background_handle = _braced_function(swift_background, "func handle(")
    swift_background_query = _braced_function(swift_background, "private func query(")
    kotlin_background_handle = _braced_function(
        kotlin_background, "override fun onMethodCall("
    )
    kotlin_background_query = _braced_function(kotlin_background, "private fun query(")
    kotlin_media_handle = _braced_function(kotlin_media, "override fun onMethodCall(")
    kotlin_media_pick = _braced_function(kotlin_media, "private fun pickImages(")
    kotlin_current_limit = _source_section(
        kotlin_media,
        "fun currentPickerLimit(",
        "internal fun chooseDurableAccess(",
    )

    assert (
        'case "queryJobs": query(call.arguments, result: result)'
        in swift_background_handle
    )
    assert "Self.validatedQueryJobIds(arguments)" in swift_background_query
    assert '"queryJobs" -> query(call.arguments, result)' in kotlin_background_handle
    assert "validatedQueryJobIds(arguments)" in kotlin_background_query
    assert '"pickImages" -> pickImages(call, reply)' in kotlin_media_handle
    assert "currentPickerLimit(remainingSlots)" in kotlin_media_pick
    assert "publicPickerLimit(" in kotlin_current_limit
    assert "MediaStore.getPickImagesMaxLimit()" in kotlin_current_limit


def test_ios_device_rehearsal_allows_only_scoped_local_network_access() -> None:
    info_plist = plistlib.loads(
        (ROOT / "apps/mobile/ios/Runner/Info.plist").read_bytes()
    )

    usage_description = info_plist.get("NSLocalNetworkUsageDescription")
    assert isinstance(usage_description, str) and usage_description.strip()

    transport_security = info_plist.get("NSAppTransportSecurity")
    assert isinstance(transport_security, dict)
    assert transport_security.get("NSAllowsLocalNetworking") is True
    assert transport_security.get("NSAllowsArbitraryLoads") is not True


@pytest.mark.skipif(sys.platform != "darwin", reason="Xcode is available on macOS")
def test_ios_device_rehearsal_uses_unique_non_release_bundle_identifiers() -> None:
    resolved: dict[str, str] = {}
    for configuration in ("Debug", "Profile", "Release"):
        result = subprocess.run(
            [
                "xcodebuild",
                "-project",
                "apps/mobile/ios/Runner.xcodeproj",
                "-scheme",
                "Runner",
                "-configuration",
                configuration,
                "-showBuildSettings",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr
        resolved[configuration] = next(
            line.split("=", maxsplit=1)[1].strip()
            for line in result.stdout.splitlines()
            if line.strip().startswith("PRODUCT_BUNDLE_IDENTIFIER =")
        )

    assert resolved == {
        "Debug": "com.wumuze.photoOrganizer.phase1",
        "Profile": "com.wumuze.photoOrganizer.phase1",
        "Release": "com.example.photoOrganizer",
    }


def test_ci_keeps_required_phase_one_checks_independent() -> None:
    workflow = (ROOT / ".github/workflows/ci.yaml").read_text(encoding="utf-8")

    for job in (
        "backend:",
        "integration:",
        "offline-synthetic:",
        "flutter:",
        "android:",
        "openapi:",
    ):
        assert job in workflow

    for command in (
        "uv run ruff check .",
        "uv run ruff format --check .",
        "uv run mypy src",
        "alembic downgrade base",
        "alembic upgrade head",
        "pytest tests/integration",
        "dart format --output=none --set-exit-if-changed",
        "flutter analyze",
        "flutter test",
        "./gradlew :app:testDebugUnitTest",
        "scripts/export_openapi.py",
        "scripts/export_classification_contract.py --check",
        "git diff --exit-code -- contracts/openapi.json",
    ):
        assert command in workflow


def test_ci_runs_the_synthetic_handoff_gate_without_external_services() -> None:
    workflow = (ROOT / ".github/workflows/ci.yaml").read_text(encoding="utf-8")
    offline_job = workflow.split("\n  offline-synthetic:", maxsplit=1)[1].split(
        "\n  flutter:", maxsplit=1
    )[0]

    assert "uv sync --frozen" in offline_job
    assert "scripts/mini_night_handoff_gate.sh" in offline_job
    assert "PHOTO_ORGANIZER_PYTHON: services/backend/.venv/bin/python" in offline_job
    assert "services:" not in offline_job


def test_ci_accepts_a_non_main_night_candidate_without_auto_merging() -> None:
    workflow = (ROOT / ".github/workflows/ci.yaml").read_text(encoding="utf-8")

    assert "workflow_dispatch:" in workflow
    assert '"codex/mini-night-*"' in workflow


def test_backend_ci_runs_lint_and_type_checks_from_the_backend_directory() -> None:
    workflow = (ROOT / ".github/workflows/ci.yaml").read_text(encoding="utf-8")
    backend_job = workflow.split("\n  integration:", maxsplit=1)[0]

    for command in (
        "uv run ruff check .",
        "uv run ruff format --check .",
        "uv run mypy src",
    ):
        assert (
            f"- run: {command}\n        working-directory: services/backend"
            in backend_job
        )


def test_makefile_defaults_match_the_isolated_integration_gate() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    for assignment in (
        "TEST_DATABASE_URL ?= postgresql://photo_organizer:photo_organizer@127.0.0.1:5432/photo_organizer_test",
        "MINIO_TEST_ENDPOINT ?= http://127.0.0.1:9000",
        "MINIO_TEST_BUCKET ?= photo-organizer-test",
        "MINIO_TEST_ACCESS_KEY ?= test-access",
        "MINIO_TEST_SECRET_KEY ?= test-secret-key",
    ):
        assert assignment in makefile


def test_makefile_exposes_the_same_local_verification_boundaries() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    for target in (
        "lint:",
        "test:",
        "offline-verify:",
        "backend-offline-verify:",
        "runtime-readiness:",
        "synthetic-upload-preflight:",
        "mobile-synthetic-check:",
        "android-native-offline-check:",
        "android-sdk-offline-check:",
        "ios-simulator-offline-check:",
        "ios-native-check:",
        "isolated-integration-check:",
        "integration-test:",
        "migration-check:",
        "openapi-check:",
        "classification-contract-check:",
        "android-test:",
        "verify:",
    ):
        assert target in makefile


def test_makefile_offline_verify_uses_the_synthetic_handoff_gate() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert "PHOTO_ORGANIZER_PYTHON ?= services/backend/.venv/bin/python" in makefile
    assert (
        "PHOTO_ORGANIZER_PYTHON=$(PHOTO_ORGANIZER_PYTHON) sh scripts/mini_night_handoff_gate.sh"
        in makefile
    )


def test_makefile_exposes_the_read_only_runtime_readiness_summary() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert (
        "PHOTO_ORGANIZER_PYTHON=$(PHOTO_ORGANIZER_PYTHON) sh scripts/phase1_runtime_readiness.sh"
        in makefile
    )


def test_makefile_exposes_the_controlled_mobile_synthetic_check() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert (
        "PHOTO_ORGANIZER_FLUTTER=$(FLUTTER) sh scripts/phase1_mobile_synthetic_check.sh"
        in makefile
    )


def test_makefile_exposes_the_android_native_offline_check() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert "sh scripts/phase1_android_native_offline_check.sh" in makefile


def test_makefile_exposes_the_ios_native_check() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert (
        "PHOTO_ORGANIZER_FLUTTER=$(FLUTTER) sh scripts/phase1_ios_native_check.sh"
        in makefile
    )


def test_makefile_exposes_the_isolated_integration_gate() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert "sh scripts/phase1_isolated_integration_check.sh" in makefile


def test_make_verify_uses_the_isolated_integration_gate() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    verify_line = next(
        line for line in makefile.splitlines() if line.startswith("verify:")
    )

    assert "isolated-integration-check" in verify_line
    assert "integration-test" not in verify_line


def test_makefile_exposes_full_backend_offline_verification() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert "BACKEND_PYTHON ?= .venv/bin/python" in makefile
    for command in (
        "cd services/backend && $(BACKEND_PYTHON) -m ruff check .",
        "cd services/backend && $(BACKEND_PYTHON) -m ruff format --check .",
        "cd services/backend && $(BACKEND_PYTHON) -m mypy src",
        'cd services/backend && $(BACKEND_PYTHON) -m pytest tests -m "not integration" -q',
        "PHOTO_ORGANIZER_PYTHON=services/backend/$(BACKEND_PYTHON) $(MAKE) offline-verify",
    ):
        assert command in makefile


def test_make_lint_checks_backend_formatting() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    lint_block = makefile.split("lint:\n", maxsplit=1)[1].split(
        "\ntest:\n", maxsplit=1
    )[0]

    assert "cd services/backend && $(UV) run ruff format --check ." in lint_block


def test_make_lint_uses_dart_analysis_in_unicode_workspaces() -> None:
    result = subprocess.run(
        [
            "make",
            "-n",
            "lint",
            "UV=true",
            "FLUTTER=true",
            "DART=/approved/dart",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "cd apps/mobile && /approved/dart analyze" in result.stdout


def test_local_stack_installs_source_and_migrates_before_workers_start() -> None:
    dockerfile = (ROOT / "services/backend/Dockerfile").read_text(encoding="utf-8")
    compose = (ROOT / "infra/compose.yaml").read_text(encoding="utf-8")

    assert "uv sync --frozen --no-dev --no-install-project" in dockerfile
    assert dockerfile.count("uv sync --frozen --no-dev") == 2
    assert "migrate:" in compose
    assert "command: alembic upgrade head" in compose
    assert compose.count("condition: service_completed_successfully") >= 3


def test_backend_runtime_dependencies_include_the_migration_cli() -> None:
    result = subprocess.run(
        [
            "uv",
            "export",
            "--frozen",
            "--offline",
            "--no-dev",
            "--no-emit-project",
            "--no-header",
            "--no-annotate",
        ],
        cwd=ROOT / "services" / "backend",
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert any(line.startswith("alembic==") for line in result.stdout.splitlines()), (
        "the runtime image invokes alembic but the production dependency set omits it"
    )


def test_verify_includes_read_only_classification_contract_drift_check() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert "scripts/export_classification_contract.py --check" in makefile
    verify_line = next(
        line for line in makefile.splitlines() if line.startswith("verify:")
    )
    assert "classification-contract-check" in verify_line


def test_non_integration_collection_excludes_migration_safety_checks() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "services/backend/tests",
            "-m",
            "not integration",
            "--collect-only",
            "-q",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "test_migration_safety.py" not in result.stdout


def test_backend_coverage_database_does_not_dirty_release_candidates() -> None:
    result = subprocess.run(
        ["git", "check-ignore", "services/backend/.coverage"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
