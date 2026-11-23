"""Fail-closed settings for the optional paid image provider."""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from opc_style.settings import (
    ForegroundExtractorSettings,
    RealImageProviderSettings,
    RealStyleAnalysisSettings,
)


def test_real_provider_is_disabled_by_default() -> None:
    settings = RealImageProviderSettings(_env_file=None)

    assert settings.enabled is False
    assert settings.api_key is None
    assert settings.max_run_usd == Decimal(0)


def test_enabled_provider_requires_api_key() -> None:
    with pytest.raises(ValidationError, match="real_image_api_key_required"):
        RealImageProviderSettings(
            enabled=True,
            max_run_usd="1.00",
            _env_file=None,
        )


def test_enabled_provider_requires_positive_budget() -> None:
    with pytest.raises(ValidationError, match="real_image_budget_required"):
        RealImageProviderSettings(
            enabled=True,
            api_key="test-secret-value",
            _env_file=None,
        )


def test_api_key_is_masked_in_settings_representation() -> None:
    settings = RealImageProviderSettings(
        enabled=True,
        api_key="test-secret-value",
        max_run_usd="1.00",
        _env_file=None,
    )

    assert "test-secret-value" not in repr(settings)
    assert settings.candidate_count == 3
    assert settings.max_retries_per_candidate == 0


def test_controlled_batch_accepts_exactly_one_candidate_per_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REAL_IMAGE_CANDIDATE_COUNT", "1")

    assert RealImageProviderSettings(_env_file=None).candidate_count == 1


def test_real_style_analysis_timeout_has_explicit_environment_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REAL_STYLE_ANALYSIS_TIMEOUT_SECONDS", "120")

    settings = RealStyleAnalysisSettings(_env_file=None)

    assert settings.timeout_seconds == 120


def test_candidate_count_rejects_unapproved_middle_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REAL_IMAGE_CANDIDATE_COUNT", "2")

    with pytest.raises(ValidationError):
        RealImageProviderSettings(_env_file=None)


def test_paid_provider_does_not_read_api_key_from_dotenv(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    (tmp_path / ".env").write_text(
        "REAL_IMAGE_ENABLED=true\n"
        "REAL_IMAGE_API_KEY=dotenv-secret\n"
        "REAL_IMAGE_MAX_RUN_USD=1.00\n"
    )
    monkeypatch.delenv("REAL_IMAGE_ENABLED", raising=False)
    monkeypatch.delenv("REAL_IMAGE_API_KEY", raising=False)
    monkeypatch.delenv("REAL_IMAGE_MAX_RUN_USD", raising=False)
    monkeypatch.chdir(tmp_path)

    settings = RealImageProviderSettings(_env_file=tmp_path / ".env")

    assert settings.enabled is False
    assert settings.api_key is None
    assert settings.max_run_usd == Decimal(0)


def test_foreground_extractor_is_disabled_by_default() -> None:
    settings = ForegroundExtractorSettings(_env_file=None)

    assert settings.enabled is False
    assert settings.binary_path is None
    assert settings.timeout_seconds == 30


def test_enabled_foreground_extractor_requires_absolute_binary_path(
    tmp_path,
) -> None:
    with pytest.raises(ValidationError, match="foreground_extractor_unavailable"):
        ForegroundExtractorSettings(enabled=True, _env_file=None)

    with pytest.raises(ValidationError, match="foreground_extractor_unavailable"):
        ForegroundExtractorSettings(
            enabled=True,
            binary_path=tmp_path.name,
            _env_file=None,
        )


def test_foreground_extractor_accepts_explicit_local_limits(tmp_path) -> None:
    settings = ForegroundExtractorSettings(
        enabled=True,
        binary_path=tmp_path / "macos-vision-foreground",
        timeout_seconds=12,
        max_output_bytes=2_000_000,
        _env_file=None,
    )

    assert settings.binary_path == tmp_path / "macos-vision-foreground"
    assert settings.timeout_seconds == 12
    assert settings.max_output_bytes == 2_000_000


def test_foreground_extractor_accepts_explicit_version(tmp_path) -> None:
    settings = ForegroundExtractorSettings(
        enabled=True,
        binary_path=tmp_path / "local-birefnet",
        extractor_version="local-birefnet-official-v1:123456789abc",
        _env_file=None,
    )

    assert settings.extractor_version == "local-birefnet-official-v1:123456789abc"
