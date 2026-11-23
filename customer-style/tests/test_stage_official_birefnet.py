from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest


def _module() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "scripts/stage_official_birefnet.py"
    spec = importlib.util.spec_from_file_location("stage_official_birefnet", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _snapshot(root: Path) -> Path:
    root.mkdir()
    for name in ("BiRefNet_config.py", "birefnet.py", "config.json", "model.safetensors"):
        (root / name).write_bytes(f"official-{name}".encode())
    return root


def test_stager_requires_exact_revision_and_allowlist(tmp_path: Path) -> None:
    module = _module()
    snapshot = _snapshot(tmp_path / "snapshot")
    license_text = b"MIT License\nPermission is hereby granted, free of charge\n"

    with pytest.raises(RuntimeError, match="birefnet_revision_invalid"):
        module.stage_official_birefnet(
            tmp_path / "wrong-revision",
            "f" * 40,
            lambda _: (snapshot, "f" * 40),
            lambda: license_text,
        )

    (snapshot / "unexpected.py").write_text("unexpected")
    with pytest.raises(RuntimeError, match="birefnet_snapshot_invalid"):
        module.stage_official_birefnet(
            tmp_path / "extra-file",
            module.PINNED_REVISION,
            lambda _: (snapshot, module.PINNED_REVISION),
            lambda: license_text,
        )


def test_stager_rejects_card_license_without_mit_text(tmp_path: Path) -> None:
    module = _module()
    snapshot = _snapshot(tmp_path / "snapshot")

    with pytest.raises(RuntimeError, match="birefnet_license_invalid"):
        module.stage_official_birefnet(
            tmp_path / "stage",
            module.PINNED_REVISION,
            lambda _: (snapshot, module.PINNED_REVISION),
            lambda: b"license: mit",
        )


def test_stager_never_accepts_customer_content() -> None:
    parser = _module()._parser()

    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "--revision",
                "e2bf8e4460fc8fa32bba5ea4d94b3233d367b0e4",
                "--destination",
                "/tmp/stage",
                "--customer-image",
                "/tmp/private.jpg",
            ]
        )
