import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "scripts" / "start_local_upload.py"


def load_launcher() -> ModuleType:
    spec = importlib.util.spec_from_file_location("start_local_upload_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_configured_port_accepts_the_launch_agent_port() -> None:
    launcher = load_launcher()

    assert launcher.configured_port({"LOCAL_UPLOAD_PORT": "8124"}) == 8124


def test_backend_source_supports_repository_and_deployed_backend_layouts(
    tmp_path: Path,
) -> None:
    launcher = load_launcher()
    repository = tmp_path / "repository"
    repository_source = repository / "services" / "backend" / "src"
    repository_source.mkdir(parents=True)
    deployed = tmp_path / "deployed-backend"
    deployed_source = deployed / "src"
    deployed_source.mkdir(parents=True)

    assert launcher.resolve_backend_source(repository) == repository_source
    assert launcher.resolve_backend_source(deployed) == deployed_source


@pytest.mark.parametrize("value", ["", "abc", "0", "65536"])
def test_configured_port_rejects_invalid_values(value: str) -> None:
    launcher = load_launcher()

    with pytest.raises(ValueError, match="local_upload_port_invalid"):
        launcher.configured_port({"LOCAL_UPLOAD_PORT": value})


def test_main_builds_local_vision_before_starting_the_configured_port(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    launcher = load_launcher()
    executable = tmp_path / "product-image-local-vision"
    executable.write_bytes(b"binary")
    captured: dict[str, object] = {}
    fake_app = object()
    fake_local_app = ModuleType("product_engine.api.local_app")
    fake_local_app.app = fake_app

    monkeypatch.setenv("LOCAL_UPLOAD_PORT", "8124")
    monkeypatch.delenv("LOCAL_VISION_EXECUTABLE", raising=False)
    monkeypatch.setitem(sys.modules, "product_engine.api.local_app", fake_local_app)
    monkeypatch.setattr(launcher.os, "chdir", lambda path: captured.update(cwd=path))
    monkeypatch.setattr(
        "product_engine.services.on_device_vision.ensure_local_vision_executable",
        lambda source, output: executable,
    )
    monkeypatch.setattr(
        launcher.uvicorn,
        "run",
        lambda app, **kwargs: captured.update(app=app, **kwargs),
    )

    launcher.main()

    assert os.environ["LOCAL_VISION_EXECUTABLE"] == str(executable)
    assert captured["app"] is fake_app
    assert captured["cwd"] == launcher.BACKEND_SOURCE.parent
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8124
    monkeypatch.delenv("LOCAL_VISION_EXECUTABLE")
