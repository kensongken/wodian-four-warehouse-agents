from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest


def _runtime_module() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "scripts/verify_local_foreground_runtime.py"
    spec = importlib.util.spec_from_file_location("verify_local_foreground_runtime", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runtime_preflight_requires_pinned_dependencies() -> None:
    module = _runtime_module()
    installed = dict(module.PINNED_EXTRA_DEPENDENCIES)

    assert module.verify_runtime_versions(lambda name: installed[name]) == tuple(
        sorted(installed.items())
    )

    for dependency, _ in module.PINNED_EXTRA_DEPENDENCIES:
        missing = {name: version for name, version in installed.items() if name != dependency}
        with pytest.raises(RuntimeError, match="runtime_dependency_mismatch"):
            module.verify_runtime_versions(lambda name, values=missing: values[name])

    wrong = {**installed, "timm": "0.9.0"}
    with pytest.raises(RuntimeError, match="runtime_dependency_mismatch"):
        module.verify_runtime_versions(lambda name: wrong[name])
