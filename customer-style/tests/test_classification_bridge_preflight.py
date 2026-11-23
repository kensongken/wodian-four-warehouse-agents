"""Strict repository preflight for the two-repository bridge acceptance."""

from __future__ import annotations

import subprocess
import sys
from hashlib import sha256
from pathlib import Path

import pytest

SCRIPTS_ROOT = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_ROOT))

from classification_style_bridge_preflight import (
    CONSUMER_TASK_BASE,
    PreflightError,
    inspect_repository,
    runtime_summary,
    verify_lock,
)

CONSUMER_ROOT = Path(__file__).resolve().parents[1]


def test_consumer_lint_waiver_is_pinned_to_normalized_migration_baseline() -> None:
    """Moving the waiver to an arbitrary newer commit must fail this test."""
    assert CONSUMER_TASK_BASE == "b8decd8f7561da538aaaac8322d17be2588f1c6a"


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _committed_repository(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "local-test@example.invalid")
    _git(root, "config", "user.name", "Local Test")
    (root / "src").mkdir()
    (root / "docs").mkdir()
    (root / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "docs" / "guide.md").write_text("# Guide\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        "[project]\nname='fixture'\n", encoding="utf-8"
    )
    (root / "ignored.bin").write_bytes(b"binary fixture")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "fixture")
    return root, _git(root, "rev-parse", "HEAD")


def test_clean_repository_preflight_hashes_every_relevant_tracked_file(
    tmp_path: Path,
) -> None:
    """Dropping source, docs, or config from the source seal must fail this test."""
    root, head = _committed_repository(tmp_path)

    evidence = inspect_repository(
        root,
        label="fixture",
        expected_head=head,
        task_base=head,
        lint_waiver_paths=(),
    )

    assert evidence["clean"] is True
    assert evidence["head"] == head
    assert evidence["task_base"] == head
    assert evidence["tracked_files"] == [
        {
            "path": "docs/guide.md",
            "sha256": sha256(b"# Guide\n").hexdigest(),
        },
        {
            "path": "pyproject.toml",
            "sha256": sha256(b"[project]\nname='fixture'\n").hexdigest(),
        },
        {
            "path": "src/app.py",
            "sha256": sha256(b"VALUE = 1\n").hexdigest(),
        },
    ]
    assert evidence["tracked_file_count"] == 3
    assert (
        evidence["tracked_tree_sha256"]
        == sha256(
            "".join(
                f"{item['path']}\0{item['sha256']}\n"
                for item in evidence["tracked_files"]
            ).encode("utf-8")
        ).hexdigest()
    )


@pytest.mark.parametrize("dirty_kind", ["tracked", "untracked"])
def test_repository_preflight_rejects_any_dirty_worktree(
    tmp_path: Path,
    dirty_kind: str,
) -> None:
    """Allowing either tracked or untracked drift must fail this test."""
    root, head = _committed_repository(tmp_path)
    target = root / ("src/app.py" if dirty_kind == "tracked" else "src/new.py")
    target.write_text("VALUE = 2\n", encoding="utf-8")

    with pytest.raises(PreflightError, match="fixture_worktree_not_clean"):
        inspect_repository(
            root,
            label="fixture",
            expected_head=head,
            task_base=head,
            lint_waiver_paths=(),
        )


def test_repository_preflight_rejects_head_and_lint_waiver_drift(
    tmp_path: Path,
) -> None:
    """Accepting a changed baseline-waiver file or wrong head must fail this test."""
    root, task_base = _committed_repository(tmp_path)
    (root / "src" / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    _git(root, "add", "src/app.py")
    _git(root, "commit", "-qm", "change baseline file")
    head = _git(root, "rev-parse", "HEAD")

    with pytest.raises(PreflightError, match="fixture_head_mismatch"):
        inspect_repository(
            root,
            label="fixture",
            expected_head=task_base,
            task_base=task_base,
            lint_waiver_paths=(),
        )
    with pytest.raises(PreflightError, match="fixture_lint_waiver_changed"):
        inspect_repository(
            root,
            label="fixture",
            expected_head=head,
            task_base=task_base,
            lint_waiver_paths=("src/app.py",),
        )


def test_runtime_and_lock_receipts_are_stable_observations() -> None:
    """Hardcoding a runtime or skipping the offline lock check must fail this test."""
    runtime = runtime_summary()
    lock = verify_lock(CONSUMER_ROOT, label="consumer")

    assert runtime["implementation"] == "CPython"
    assert runtime["python_version"]
    assert runtime["cache_tag"].startswith("cpython-")
    assert runtime["platform_system"]
    assert runtime["platform_machine"]
    assert runtime["dependencies"] == sorted(
        runtime["dependencies"], key=lambda item: item["name"]
    )
    assert lock == {
        "label": "consumer",
        "command": ["uv", "lock", "--check", "--offline"],
        "exit_code": 0,
        "result": "PASS",
    }
