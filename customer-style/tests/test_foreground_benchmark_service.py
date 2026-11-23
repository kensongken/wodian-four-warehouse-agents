from __future__ import annotations

import json
from hashlib import sha256
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from opc_style.domain.errors import DomainRuleError
from opc_style.services.foreground_benchmarks import freeze_dataset, validate_dataset
from opc_style.settings import ForegroundBenchmarkSettings


def _png(index: int, *, mask: bool = False) -> bytes:
    mode = "L" if mask else "RGB"
    color: int | tuple[int, int, int]
    color = 255 if mask else (index % 251, (index * 3) % 251, (index * 7) % 251)
    image = Image.new(mode, (4, 4), color)
    if mask:
        image.putpixel((0, 0), 0)
    output = BytesIO()
    image.save(output, format="PNG", optimize=False)
    return output.getvalue()


def _private_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(0o600)


def _build_dataset(
    root: Path,
    *,
    acceptance_count: int = 50,
    include_calibration: bool = True,
    reviewed: bool = True,
) -> Path:
    root.mkdir(mode=0o700)
    samples: list[dict[str, object]] = []
    difficulties = ("easy", "medium", "hard")
    scenes = ("complex_background", "glove", "tray", "bust")
    materials = ("fine_chain", "transparent", "reflective")
    components = ("clasp", "pendant")
    total = acceptance_count + (1 if include_calibration else 0)
    for index in range(total):
        split = "calibration" if include_calibration and index == total - 1 else "acceptance"
        source = _png(index)
        gold = _png(index, mask=True)
        component = _png(index + 1, mask=True)
        source_rel = f"samples/{index}/source.png"
        gold_rel = f"samples/{index}/gold.png"
        component_rel = f"samples/{index}/component.png"
        _private_write(root / source_rel, source)
        _private_write(root / gold_rel, gold)
        _private_write(root / component_rel, component)
        samples.append(
            {
                "sample_id": f"sample-{index:03d}",
                "source_file": source_rel,
                "source_sha256": sha256(source).hexdigest(),
                "width": 4,
                "height": 4,
                "mime_type": "image/png",
                "category": "bracelet",
                "difficulty": difficulties[index % len(difficulties)],
                "scene_tags": [scenes[index % len(scenes)]],
                "material_tags": [materials[index % len(materials)]],
                "gold_mask_file": gold_rel,
                "gold_mask_sha256": sha256(gold).hexdigest(),
                "critical_components": [
                    {
                        "name": components[index % len(components)],
                        "mask_file": component_rel,
                        "mask_sha256": sha256(component).hexdigest(),
                    }
                ],
                "human_reviewed": reviewed,
                "split": split,
            }
        )
    manifest = root / "manifest.json"
    manifest.write_text(json.dumps({"version": 1, "samples": samples}), encoding="utf-8")
    manifest.chmod(0o600)
    return root


def _manifest(root: Path) -> dict[str, object]:
    return json.loads((root / "manifest.json").read_text(encoding="utf-8"))


def _rewrite_manifest(root: Path, payload: dict[str, object]) -> None:
    path = root / "manifest.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)


def test_validates_fifty_real_acceptance_samples_without_exposing_paths(
    tmp_path: Path,
) -> None:
    root = _build_dataset(tmp_path / "private-benchmark")

    result = validate_dataset(root)

    assert result.acceptance_count == 50
    assert result.calibration_count == 1
    assert len(result.seal_sha256) == 64
    assert len(result.samples) == 51
    assert not hasattr(result.samples[0], "source_path")
    assert result.samples[0].source_content.startswith(b"\x89PNG")


def test_allows_optional_sample_tags_and_components_when_coverage_is_complete(
    tmp_path: Path,
) -> None:
    root = _build_dataset(tmp_path / "optional-sample-components")
    payload = _manifest(root)
    samples = payload["samples"]
    assert isinstance(samples, list) and isinstance(samples[0], dict)
    samples[0]["critical_components"] = []
    samples[0]["material_tags"] = []
    _rewrite_manifest(root, payload)

    result = validate_dataset(root)

    assert result.samples[0].manifest.critical_components == ()
    assert result.samples[0].manifest.material_tags == ()
    assert result.acceptance_count == 50


@pytest.mark.parametrize("count", [49, 51])
def test_rejects_acceptance_set_not_equal_to_fifty(tmp_path: Path, count: int) -> None:
    root = _build_dataset(tmp_path / f"count-{count}", acceptance_count=count)

    with pytest.raises(DomainRuleError) as error:
        validate_dataset(root)

    assert error.value.code == "foreground_benchmark_acceptance_count_invalid"


def test_rejects_missing_file_and_digest_mismatch(tmp_path: Path) -> None:
    missing_root = _build_dataset(tmp_path / "missing")
    (missing_root / "samples/0/source.png").unlink()
    with pytest.raises(DomainRuleError) as missing:
        validate_dataset(missing_root)
    assert missing.value.code == "foreground_benchmark_file_invalid"

    digest_root = _build_dataset(tmp_path / "digest")
    payload = _manifest(digest_root)
    samples = payload["samples"]
    assert isinstance(samples, list) and isinstance(samples[0], dict)
    samples[0]["source_sha256"] = "f" * 64
    _rewrite_manifest(digest_root, payload)
    with pytest.raises(DomainRuleError) as digest:
        validate_dataset(digest_root)
    assert digest.value.code == "foreground_benchmark_digest_mismatch"


def test_rejects_symlink_escape_and_readable_private_file(tmp_path: Path) -> None:
    symlink_root = _build_dataset(tmp_path / "symlink")
    outside = tmp_path / "outside.png"
    outside.write_bytes(_png(200))
    source = symlink_root / "samples/0/source.png"
    source.unlink()
    source.symlink_to(outside)
    with pytest.raises(DomainRuleError) as symlink:
        validate_dataset(symlink_root)
    assert symlink.value.code == "foreground_benchmark_file_invalid"

    readable_root = _build_dataset(tmp_path / "readable")
    (readable_root / "samples/0/source.png").chmod(0o644)
    with pytest.raises(DomainRuleError) as readable:
        validate_dataset(readable_root)
    assert readable.value.code == "foreground_benchmark_file_permissions"


def test_rejects_missing_risk_coverage_and_unreviewed_gold(tmp_path: Path) -> None:
    coverage_root = _build_dataset(tmp_path / "coverage")
    payload = _manifest(coverage_root)
    samples = payload["samples"]
    assert isinstance(samples, list)
    for sample in samples:
        assert isinstance(sample, dict)
        sample["scene_tags"] = ["complex_background"]
        sample["material_tags"] = ["reflective"]
        components = sample["critical_components"]
        assert isinstance(components, list) and isinstance(components[0], dict)
        components[0]["name"] = "clasp"
    _rewrite_manifest(coverage_root, payload)
    with pytest.raises(DomainRuleError) as coverage:
        validate_dataset(coverage_root)
    assert coverage.value.code == "foreground_benchmark_coverage_incomplete"

    unreviewed_root = _build_dataset(tmp_path / "unreviewed", reviewed=False)
    with pytest.raises(DomainRuleError) as unreviewed:
        validate_dataset(unreviewed_root)
    assert unreviewed.value.code == "foreground_benchmark_gold_unreviewed"


def test_rejects_calibration_acceptance_source_overlap(tmp_path: Path) -> None:
    root = _build_dataset(tmp_path / "overlap")
    payload = _manifest(root)
    samples = payload["samples"]
    assert isinstance(samples, list)
    acceptance = samples[0]
    calibration = samples[-1]
    assert isinstance(acceptance, dict) and isinstance(calibration, dict)
    calibration["source_file"] = acceptance["source_file"]
    calibration["source_sha256"] = acceptance["source_sha256"]
    _rewrite_manifest(root, payload)

    with pytest.raises(DomainRuleError) as error:
        validate_dataset(root)

    assert error.value.code == "foreground_benchmark_split_overlap"


def test_freeze_writes_only_canonical_manifest_and_seal(tmp_path: Path) -> None:
    root = _build_dataset(tmp_path / "freeze")

    result = freeze_dataset(root)

    frozen = root / "frozen-manifest.json"
    seal = root / "seal.sha256"
    assert frozen.is_file() and frozen.stat().st_mode & 0o777 == 0o600
    assert seal.is_file() and seal.stat().st_mode & 0o777 == 0o600
    assert seal.read_text(encoding="ascii").strip() == result.seal_sha256
    assert "source_file" not in frozen.read_text(encoding="utf-8")


def test_settings_require_explicit_absolute_root() -> None:
    assert ForegroundBenchmarkSettings().enabled is False
    with pytest.raises(ValueError, match="foreground_benchmark_unavailable"):
        ForegroundBenchmarkSettings(enabled=True, root=Path("relative"))
