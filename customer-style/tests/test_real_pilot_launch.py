"""Behavioral safety checks for the Keychain-backed paid-pilot launcher."""

from __future__ import annotations

import re
import stat
import subprocess
from pathlib import Path

import pytest

LAUNCH_SCRIPT = Path("scripts/run_real_style_pilot.sh").resolve()
KEYCHAIN_ARGS = [
    "find-generic-password",
    "-w",
    "-a",
    "customer-photo-style-local",
    "-s",
    "REAL_IMAGE_API_KEY",
]
LAUNCH_ARGS = ["scripts/launch_real_style_pilot.py"]
_ALLOWED_COMMANDS = (
    r"set\s+-euo\s+pipefail",
    r'runtime_root=\$\{LOCAL_STYLE_RUNTIME_ROOT:-"\$PWD/\.local-style-runtime"\}',
    r'/bin/mkdir\s+-p\s+"\$runtime_root/assets"',
    r'/bin/chmod\s+700\s+"\$runtime_root"\s+"\$runtime_root/assets"',
    r'export\s+DATABASE_URL="sqlite:///\$runtime_root/opc_style\.sqlite3"',
    r'export\s+LOCAL_STYLE_ASSET_ROOT="\$runtime_root/assets"',
    r'foreground_binary="\$runtime_root/bin/macos-vision-foreground"',
    r'if\s+\[\[\s+!\s+-x\s+"\$foreground_binary"\s+\]\]\s*;\s*then',
    r'\./scripts/build_macos_vision_foreground\.sh\s+--output\s+"\$foreground_binary"',
    r"fi",
    r"export\s+FOREGROUND_EXTRACTOR_ENABLED\s*=\s*true",
    r'export\s+FOREGROUND_EXTRACTOR_BINARY_PATH="\$foreground_binary"',
    r"export\s+FOREGROUND_EXTRACTOR_VERSION\s*=\s*macos-vision-foreground-v1",
    (
        r'pipeline_link="\$PWD/\.local-style-assets/model-runtimes/'
        r'sam2-birefnet-biref-primary-candidate"'
    ),
    r'pipeline_root=\$\(CDPATH=\s+cd\s+--\s+"\$pipeline_link"\s+&&\s+pwd\s+-P\)',
    r"export\s+FOREGROUND_PIPELINE_ENABLED\s*=\s*true",
    r'export\s+FOREGROUND_PIPELINE_EXECUTABLE_PATH="\$pipeline_root/runner"',
    r'export\s+FOREGROUND_PIPELINE_MODEL_ROOT="\$pipeline_root"',
    r'export\s+FOREGROUND_PIPELINE_MANIFEST_PATH="\$pipeline_root/pipeline-manifest\.json"',
    (
        r"export\s+FOREGROUND_PIPELINE_MANIFEST_SHA256\s*=\s*"
        r"504791f0ddf7fddd3ef50150ef86633c908f87e6303deafcfc4e7cc81999992b"
    ),
    r"export\s+FOREGROUND_PIPELINE_TIMEOUT_SECONDS\s*=\s*240",
    r"export\s+FOREGROUND_PIPELINE_MAX_OUTPUT_BYTES\s*=\s*20971520",
    (
        r"pilot_openai_key\s*=\s*\$\(\s*security\s+find-generic-password\s+-w\s+"
        r"-a\s+customer-photo-style-local\s+-s\s+REAL_IMAGE_API_KEY\s*\)"
    ),
    r'if\s+\[\[\s+"\$pilot_openai_key"\s+!=\s+sk-\*\s+\]\]\s*;\s*then',
    r"print\s+-u2\s+'REAL_IMAGE_API_KEY is missing or invalid in macOS Keychain'",
    r"exit\s+2",
    r"fi",
    (
        r"trap\s+'unset\s+pilot_openai_key\s+REAL_STYLE_ANALYSIS_API_KEY\s+"
        r"REAL_PRODUCT_IDENTITY_API_KEY\s+REAL_IMAGE_API_KEY\s+"
        r"REAL_FIDELITY_API_KEY\s+OPC_STYLE_REAL_BACKGROUND_API_KEY\s+"
        r"MISSING_PART_COMPLETION_API_KEY'\s+EXIT"
    ),
    r"export\s+REAL_IMAGE_ENABLED\s*=\s*true",
    r'export\s+REAL_IMAGE_API_KEY\s*=\s*"\$pilot_openai_key"',
    r"export\s+REAL_IMAGE_MODEL\s*=\s*gpt-image-2",
    r"export\s+REAL_IMAGE_MAX_RUN_USD\s*=\s*3\.00",
    r"export\s+REAL_IMAGE_CANDIDATE_COUNT\s*=\s*1",
    r"export\s+OPC_STYLE_REAL_BACKGROUND_ENABLED\s*=\s*true",
    r'export\s+OPC_STYLE_REAL_BACKGROUND_API_KEY\s*=\s*"\$pilot_openai_key"',
    r"export\s+OPC_STYLE_REAL_BACKGROUND_MODEL\s*=\s*gpt-image-2",
    r"export\s+OPC_STYLE_REAL_BACKGROUND_MAX_RUN_USD\s*=\s*0\.30",
    r"export\s+OPC_STYLE_REAL_BACKGROUND_TIMEOUT_SECONDS\s*=\s*300",
    r"export\s+REAL_STYLE_ANALYSIS_ENABLED\s*=\s*true",
    r'export\s+REAL_STYLE_ANALYSIS_API_KEY\s*=\s*"\$pilot_openai_key"',
    r"export\s+REAL_STYLE_ANALYSIS_MODEL\s*=\s*gpt-4\.1-mini-2025-04-14",
    r"export\s+REAL_STYLE_ANALYSIS_MAX_RUN_USD\s*=\s*0\.422722",
    r"export\s+REAL_STYLE_ANALYSIS_TIMEOUT_SECONDS\s*=\s*120",
    r"export\s+REAL_PRODUCT_IDENTITY_ENABLED\s*=\s*true",
    r'export\s+REAL_PRODUCT_IDENTITY_API_KEY\s*=\s*"\$pilot_openai_key"',
    r"export\s+REAL_PRODUCT_IDENTITY_MODEL\s*=\s*gpt-4\.1-mini-2025-04-14",
    r"export\s+REAL_PRODUCT_IDENTITY_MAX_RUN_USD\s*=\s*0\.10",
    r"export\s+REAL_FIDELITY_ENABLED\s*=\s*true",
    r'export\s+REAL_FIDELITY_API_KEY\s*=\s*"\$pilot_openai_key"',
    r"export\s+REAL_FIDELITY_MODEL\s*=\s*gpt-4\.1-mini-2025-04-14",
    r"export\s+REAL_FIDELITY_MAX_RUN_USD\s*=\s*1\.00",
    r"export\s+STYLE_SUITE_CANDIDATE_ESTIMATE_USD\s*=\s*0\.12",
    r"export\s+MISSING_PART_COMPLETION_ENABLED\s*=\s*true",
    r'export\s+MISSING_PART_COMPLETION_API_KEY\s*=\s*"\$pilot_openai_key"',
    (
        r"export\s+MISSING_PART_COMPLETION_MODEL\s*=\s*"
        r"gpt-image-2\.5-sunburst-2026-09-08"
    ),
    r"export\s+MISSING_PART_COMPLETION_MAX_RUN_USD\s*=\s*0\.50",
    r"export\s+MISSING_PART_COMPLETION_TIMEOUT_SECONDS\s*=\s*300",
    r"exec\s+\.venv/bin/python\s+scripts/launch_real_style_pilot\.py",
)


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _active_commands(script: str) -> list[str]:
    return [
        line.strip()
        for line in script.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _preflight_launcher(script: str) -> None:
    """Reject every executable line outside the launcher command allowlist."""
    commands = _active_commands(script)
    if len(commands) != len(_ALLOWED_COMMANDS):
        raise ValueError("launcher_preflight_rejected:command_count")
    for command, allowed in zip(commands, _ALLOWED_COMMANDS, strict=True):
        if not re.fullmatch(allowed, command):
            raise ValueError(f"launcher_preflight_rejected:{command}")


def _prepare_sandbox(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path]:
    sandbox = tmp_path / "sandbox"
    workdir = sandbox / "workdir"
    home = sandbox / "home"
    temp = sandbox / "tmp"
    zdotdir = sandbox / "zdotdir"
    repo_sentinel = sandbox / "repo-sentinel"
    for directory in (workdir, home, temp, zdotdir, repo_sentinel):
        directory.mkdir(parents=True)
    foreground_binary = (
        workdir / ".local-style-runtime/bin/macos-vision-foreground"
    )
    foreground_binary.parent.mkdir(parents=True)
    _write_executable(foreground_binary, "#!/bin/zsh -f\nexit 0\n")
    pipeline_root = (
        workdir
        / ".local-style-assets/model-runtimes/"
        "sam2-birefnet-biref-primary-candidate"
    )
    pipeline_root.mkdir(parents=True)
    _write_executable(pipeline_root / "runner", "#!/bin/zsh -f\nexit 0\n")
    (pipeline_root / "pipeline-manifest.json").write_text(
        "{}\n", encoding="utf-8"
    )
    (repo_sentinel / "scope.txt").write_text("repo sentinel", encoding="utf-8")
    return workdir, home, temp, zdotdir, repo_sentinel


def _base_environment(
    stub_bin: Path, home: Path, temp: Path, zdotdir: Path
) -> dict[str, str]:
    test_key = "sk-" + "launch-harness-value"
    return {
        "PATH": str(stub_bin),
        "HOME": str(home),
        "TMPDIR": str(temp),
        "ZDOTDIR": str(zdotdir),
        "LAUNCH_EXPECTED_KEY": test_key,
        "LAUNCH_SECURITY_MODE": "valid",
    }


def _validate_zsh_syntax(script_path: Path, environment: dict[str, str], cwd: Path) -> None:
    parsed = subprocess.run(
        ["/bin/zsh", "-f", "-n", str(script_path)],
        cwd=cwd,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    if parsed.returncode != 0:
        raise ValueError(f"launcher_preflight_rejected:syntax:{parsed.stderr}")


def _install_stubs(workdir: Path) -> tuple[Path, Path]:
    capture = workdir / "capture"
    capture.mkdir()
    stub_bin = workdir / "bin"
    stub_bin.mkdir()
    python_stub = workdir / ".venv" / "bin" / "python"
    python_stub.parent.mkdir(parents=True)

    _write_executable(
        stub_bin / "security",
        """#!/bin/zsh -f
set -eu
print -rl -- "$@" > "$LAUNCH_CAPTURE/security-argv"
case "$LAUNCH_SECURITY_MODE" in
  valid) print -r -- "$LAUNCH_EXPECTED_KEY" ;;
  invalid) print -r -- "not-a-valid-key" ;;
  missing) exit 44 ;;
esac
""",
    )
    _write_executable(
        python_stub,
        """#!/bin/zsh -f
set -eu
key_matches=false
if [[ "${REAL_IMAGE_API_KEY-}" == "${LAUNCH_EXPECTED_KEY-}" ]]; then
  key_matches=true
fi
all_keys_match=false
if [[ "${REAL_STYLE_ANALYSIS_API_KEY-}" == "${LAUNCH_EXPECTED_KEY-}" &&
      "${REAL_PRODUCT_IDENTITY_API_KEY-}" == "${LAUNCH_EXPECTED_KEY-}" &&
      "${REAL_FIDELITY_API_KEY-}" == "${LAUNCH_EXPECTED_KEY-}" &&
      "${MISSING_PART_COMPLETION_API_KEY-}" == "${LAUNCH_EXPECTED_KEY-}" ]]; then
  all_keys_match=true
fi
print -rl -- "$@" > "$LAUNCH_CAPTURE/python-argv"
{
  print -r -- "REAL_IMAGE_ENABLED=${REAL_IMAGE_ENABLED-}"
  print -r -- "REAL_IMAGE_MODEL=${REAL_IMAGE_MODEL-}"
  print -r -- "REAL_IMAGE_MAX_RUN_USD=${REAL_IMAGE_MAX_RUN_USD-}"
  print -r -- "REAL_IMAGE_CANDIDATE_COUNT=${REAL_IMAGE_CANDIDATE_COUNT-}"
  print -r -- "DATABASE_URL=${DATABASE_URL-}"
  print -r -- "LOCAL_STYLE_ASSET_ROOT=${LOCAL_STYLE_ASSET_ROOT-}"
  print -r -- "REAL_IMAGE_API_KEY_MATCHES=$key_matches"
  print -r -- "REAL_CHAIN_KEYS_MATCH=$all_keys_match"
  print -r -- "REAL_PRODUCT_IDENTITY_ENABLED=${REAL_PRODUCT_IDENTITY_ENABLED-}"
  print -r -- "REAL_FIDELITY_ENABLED=${REAL_FIDELITY_ENABLED-}"
  print -r -- "MISSING_PART_COMPLETION_ENABLED=${MISSING_PART_COMPLETION_ENABLED-}"
  print -r -- "MISSING_PART_COMPLETION_MODEL=${MISSING_PART_COMPLETION_MODEL-}"
  print -r -- "MISSING_PART_COMPLETION_MAX_RUN_USD=${MISSING_PART_COMPLETION_MAX_RUN_USD-}"
} > "$LAUNCH_CAPTURE/python-env"
""",
    )
    return capture, stub_bin


def _run_launcher(
    tmp_path: Path, security_mode: str, script_text: str | None = None
) -> tuple[subprocess.CompletedProcess[str], Path, tuple[Path, ...]]:
    workdir, home, temp, zdotdir, repo_sentinel = _prepare_sandbox(tmp_path)
    script_path = LAUNCH_SCRIPT
    if script_text is not None:
        script_path = workdir / "candidate-launcher.sh"
        _write_executable(script_path, script_text)

    script = script_path.read_text(encoding="utf-8")
    _preflight_launcher(script)
    preflight_env = _base_environment(workdir / "empty-bin", home, temp, zdotdir)
    _validate_zsh_syntax(script_path, preflight_env, workdir)

    capture, stub_bin = _install_stubs(workdir)
    environment = _base_environment(stub_bin, home, temp, zdotdir)
    environment["LAUNCH_SECURITY_MODE"] = security_mode
    environment["LAUNCH_CAPTURE"] = str(capture)
    completed = subprocess.run(
        ["/bin/zsh", "-f", str(script_path)],
        cwd=workdir,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    return completed, capture, (workdir, home, temp, repo_sentinel)


def _assert_secret_is_not_persisted(
    completed: subprocess.CompletedProcess[str], scopes: tuple[Path, ...]
) -> None:
    test_key = "sk-" + "launch-harness-value"
    assert test_key not in completed.stdout
    assert test_key not in completed.stderr
    for scope in scopes:
        for path in scope.rglob("*"):
            if path.is_file():
                assert test_key not in path.read_text(encoding="utf-8")


def test_launcher_runs_only_stubs_with_exact_keychain_and_loopback_contract(
    tmp_path: Path,
) -> None:
    """A valid lookup reaches only the stub child with the fixed local contract."""
    completed, capture, scopes = _run_launcher(tmp_path, "valid")

    assert stat.S_IMODE(LAUNCH_SCRIPT.stat().st_mode) & stat.S_IXUSR
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == ""
    assert completed.stderr == ""
    assert (capture / "security-argv").read_text().splitlines() == KEYCHAIN_ARGS
    assert (capture / "python-argv").read_text().splitlines() == LAUNCH_ARGS
    workdir = scopes[0]
    assert (capture / "python-env").read_text().splitlines() == [
        "REAL_IMAGE_ENABLED=true",
        "REAL_IMAGE_MODEL=gpt-image-2",
        "REAL_IMAGE_MAX_RUN_USD=3.00",
        "REAL_IMAGE_CANDIDATE_COUNT=1",
        f"DATABASE_URL=sqlite:///{workdir / '.local-style-runtime/opc_style.sqlite3'}",
        f"LOCAL_STYLE_ASSET_ROOT={workdir / '.local-style-runtime/assets'}",
        "REAL_IMAGE_API_KEY_MATCHES=true",
        "REAL_CHAIN_KEYS_MATCH=true",
        "REAL_PRODUCT_IDENTITY_ENABLED=true",
        "REAL_FIDELITY_ENABLED=true",
        "MISSING_PART_COMPLETION_ENABLED=true",
        "MISSING_PART_COMPLETION_MODEL=gpt-image-2.5-sunburst-2026-09-08",
        "MISSING_PART_COMPLETION_MAX_RUN_USD=0.50",
    ]
    _assert_secret_is_not_persisted(completed, scopes)


def test_launcher_enables_complete_real_chain_with_one_candidate() -> None:
    script = LAUNCH_SCRIPT.read_text(encoding="utf-8")

    for setting in (
        "REAL_STYLE_ANALYSIS_ENABLED=true",
        "REAL_PRODUCT_IDENTITY_ENABLED=true",
        "REAL_IMAGE_ENABLED=true",
        "REAL_IMAGE_CANDIDATE_COUNT=1",
        "REAL_FIDELITY_ENABLED=true",
        "MISSING_PART_COMPLETION_ENABLED=true",
    ):
        assert setting in script
    assert script.count("security find-generic-password") == 1
    assert "REAL_PRODUCT_IDENTITY_API_KEY" in script
    assert "REAL_FIDELITY_API_KEY" in script


@pytest.mark.parametrize("migration_fails", [False, True])
def test_launcher_upgrades_the_local_database_before_starting_the_server(
    monkeypatch: pytest.MonkeyPatch, migration_fails: bool
) -> None:
    import runpy

    import uvicorn

    from alembic import command

    calls: list[str] = []
    def upgrade(*args: object) -> None:
        calls.append("upgrade")
        if migration_fails:
            raise RuntimeError("migration_failed")
    def serve(*args: object, **kwargs: object) -> None:
        calls.append("serve")
    monkeypatch.setattr(command, "upgrade", upgrade)
    monkeypatch.setattr(uvicorn, "run", serve)
    entry = runpy.run_path("scripts/launch_real_style_pilot.py")["main"]
    if migration_fails:
        with pytest.raises(RuntimeError, match="migration_failed"):
            entry()
        assert calls == ["upgrade"]
    else:
        entry()
        assert calls == ["upgrade", "serve"]


@pytest.mark.parametrize("security_mode", ["missing", "invalid"])
def test_launcher_fails_closed_without_a_usable_key(
    tmp_path: Path, security_mode: str
) -> None:
    """No invalid or missing Keychain result may reach the Python child."""
    completed, capture, scopes = _run_launcher(tmp_path, security_mode)

    assert completed.returncode != 0
    assert not (capture / "python-argv").exists()
    assert not (capture / "python-env").exists()
    _assert_secret_is_not_persisted(completed, scopes)


@pytest.mark.parametrize(
    "script_text",
    [
        LAUNCH_SCRIPT.read_text().replace(
            "security find-generic-password",
            "/usr/bin/security find-generic-password",
        ),
        LAUNCH_SCRIPT.read_text() + '\nprint -r -- "$pilot_api_key" > pilot.env\n',
    ],
)
def test_preflight_rejects_unsafe_mutations_before_subprocess_execution(
    tmp_path: Path, script_text: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absolute lookups and file-persistence extras cannot reach any stub."""
    def fail_if_subprocess_called(*args: object, **kwargs: object) -> object:
        raise AssertionError(f"preflight started a subprocess: {args!r} {kwargs!r}")

    monkeypatch.setattr(subprocess, "run", fail_if_subprocess_called)
    with pytest.raises(ValueError, match="launcher_preflight_rejected"):
        _run_launcher(tmp_path, "valid", script_text)

    workdir = tmp_path / "sandbox" / "workdir"
    assert not (workdir / "capture").exists()
    assert not (workdir / ".venv").exists()
