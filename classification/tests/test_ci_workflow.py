import subprocess
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace


def test_ci_runs_contracts_tests_and_local_gates() -> None:
    workflow = (
        Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml"
    ).read_text(encoding="utf-8")
    for required in (
        "make lint",
        "make contracts",
        "make test",
        "make verify-task7 verify-task9 verify-task10",
    ):
        assert required in workflow
    assert "deploy" not in workflow.lower()


def test_smoke_cli_is_fail_closed_for_invalid_target() -> None:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "scripts/integration_smoke.py", "--base-url", "invalid"],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2
    assert result.stdout == "invalid_base_url=1\n"


def test_smoke_cli_accepts_healthy_local_endpoint(monkeypatch, capsys) -> None:
    root = Path(__file__).resolve().parents[1]
    spec = spec_from_file_location(
        "integration_smoke", root / "scripts/integration_smoke.py"
    )
    assert spec and spec.loader
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(
        sys,
        "argv",
        ["integration_smoke.py", "--base-url", "http://local.test", "--allow-remote"],
    )
    requested_urls = []

    def healthy_response(url, timeout):
        requested_urls.append((url, timeout))
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(module.httpx, "get", healthy_response)
    assert module.main() == 0
    assert requested_urls == [("http://local.test/health/live", 5)]
    assert capsys.readouterr().out == "health_ok=1\n"


def test_smoke_cli_rejects_unhealthy_status(monkeypatch, capsys) -> None:
    root = Path(__file__).resolve().parents[1]
    spec = spec_from_file_location(
        "integration_smoke", root / "scripts/integration_smoke.py"
    )
    assert spec and spec.loader
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(
        sys,
        "argv",
        ["integration_smoke.py", "--base-url", "http://local.test", "--allow-remote"],
    )
    monkeypatch.setattr(
        module.httpx, "get", lambda url, timeout: SimpleNamespace(status_code=503)
    )
    assert module.main() == 1
    assert capsys.readouterr().out == "health_status_invalid=1\n"


def test_smoke_cli_rejects_remote_target_without_explicit_override(monkeypatch, capsys) -> None:
    root = Path(__file__).resolve().parents[1]
    spec = spec_from_file_location("integration_smoke", root / "scripts/integration_smoke.py")
    assert spec and spec.loader
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(sys, "argv", ["integration_smoke.py", "--base-url", "https://example.test"])
    assert module.main() == 2
    assert capsys.readouterr().out == "remote_target_not_allowed=1\n"
