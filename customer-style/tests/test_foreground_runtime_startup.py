from __future__ import annotations

from pathlib import Path

import pytest

from opc_style.domain.errors import DomainRuleError
from opc_style.main import build_product_foreground_extractor
from opc_style.providers.local_foreground_worker import LocalForegroundWorker
from opc_style.settings import ForegroundExtractorSettings, ForegroundPipelineSettings


def _pipeline_settings(tmp_path: Path) -> ForegroundPipelineSettings:
    executable = tmp_path / "runner"
    executable.write_text("not-a-real-runner")
    executable.chmod(0o700)
    model_root = tmp_path / "pipeline"
    model_root.mkdir()
    manifest = model_root / "pipeline-manifest.json"
    manifest.write_text("{}")
    return ForegroundPipelineSettings(
        _env_file=None,
        enabled=True,
        executable_path=executable,
        model_root=model_root,
        manifest_path=manifest,
        pipeline_manifest_sha256="a" * 64,
    )


def test_startup_injects_local_worker_when_sealed_pipeline_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(LocalForegroundWorker, "verify_installation", lambda self: "a" * 64)

    extractor = build_product_foreground_extractor(
        _pipeline_settings(tmp_path), ForegroundExtractorSettings(_env_file=None)
    )

    assert isinstance(extractor, LocalForegroundWorker)


def test_startup_fails_closed_on_manifest_mismatch(tmp_path: Path) -> None:
    with pytest.raises(DomainRuleError, match="foreground_candidate_manifest_mismatch"):
        build_product_foreground_extractor(
            _pipeline_settings(tmp_path), ForegroundExtractorSettings(_env_file=None)
        )
