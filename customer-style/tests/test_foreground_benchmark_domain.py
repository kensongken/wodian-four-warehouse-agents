from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from io import BytesIO

import pytest
from PIL import Image

from opc_style.domain.errors import DomainRuleError
from opc_style.domain.foreground_benchmark import (
    CriticalComponent,
    GoldSampleManifest,
    LocalForegroundPipelineManifest,
    LocalModelArtifactManifest,
    LocalModelManifest,
    benchmark_manifest_sha256,
    model_manifest_sha256,
    pipeline_manifest_sha256,
    score_foreground_mask,
)


def _mask(rows: tuple[str, ...]) -> bytes:
    image = Image.new("L", (len(rows[0]), len(rows)), 0)
    image.putdata([255 if value == "1" else 0 for row in rows for value in row])
    output = BytesIO()
    image.save(output, format="PNG", optimize=False)
    return output.getvalue()


def _sample(sample_id: str, source_digit: str, mask_digit: str) -> GoldSampleManifest:
    return GoldSampleManifest(
        sample_id=sample_id,
        source_sha256=source_digit * 64,
        width=4,
        height=4,
        mime_type="image/png",
        category="bracelet",
        difficulty="hard",
        scene_tags=("glove",),
        material_tags=("fine_chain",),
        gold_mask_sha256=mask_digit * 64,
        critical_components=(
            CriticalComponent(name="clasp", mask_sha256="c" * 64),
        ),
        human_reviewed=True,
        split="acceptance",
    )


def test_benchmark_manifest_hash_is_order_stable() -> None:
    first = _sample("sample-a", "a", "1")
    second = _sample("sample-b", "b", "2")

    expected = benchmark_manifest_sha256((first, second))

    assert benchmark_manifest_sha256((second, first)) == expected


def test_score_reports_false_positive_contamination() -> None:
    gold = _mask(("1100", "1100", "0000", "0000"))
    predicted = _mask(("1110", "1100", "0000", "0000"))

    result = score_foreground_mask(predicted, gold, 4, 4)

    assert result.true_positive_pixels == 4
    assert result.false_positive_pixels == 1
    assert result.false_negative_pixels == 0
    assert result.background_contamination == pytest.approx(0.2)


def test_score_reports_foreground_recall_and_iou() -> None:
    gold = _mask(("1110", "1100", "0000", "0000"))
    predicted = _mask(("1100", "1100", "0000", "0000"))

    result = score_foreground_mask(predicted, gold, 4, 4)

    assert result.foreground_recall == pytest.approx(0.8)
    assert result.iou == pytest.approx(0.8)
    assert result.background_contamination == 0


def test_auto_eligible_requires_each_image_below_two_percent_contamination() -> None:
    gold = _mask(("1111111111",) * 5 + ("0000000000",) * 5)
    one_false_positive = _mask(("1111111111",) * 5 + ("1000000000",) + ("0000000000",) * 4)
    two_false_positives = _mask(("1111111111",) * 5 + ("1100000000",) + ("0000000000",) * 4)

    below = score_foreground_mask(one_false_positive, gold, 10, 10)
    above = score_foreground_mask(two_false_positives, gold, 10, 10)

    assert below.background_contamination == pytest.approx(1 / 51)
    assert below.pixel_safe_for_auto_eligible is True
    assert above.background_contamination == pytest.approx(2 / 52)
    assert above.pixel_safe_for_auto_eligible is False


def test_critical_component_loss_is_not_hidden_by_high_global_iou() -> None:
    gold = _mask(("1111111111",) * 10)
    predicted = _mask(("0111111111",) + ("1111111111",) * 9)
    clasp = _mask(("1000000000",) + ("0000000000",) * 9)

    result = score_foreground_mask(
        predicted,
        gold,
        10,
        10,
        critical_component_masks={"clasp": clasp},
    )

    assert result.iou == pytest.approx(0.99)
    assert result.critical_component_recalls == (("clasp", 0.0),)
    assert result.critical_component_preservation_rate == 0
    assert result.pixel_safe_for_auto_eligible is False


@pytest.mark.parametrize(
    ("samples", "code"),
    [
        ((_sample("sample-a", "a", "1"), _sample("sample-a", "b", "2")), "foreground_benchmark_duplicate_sample"),
        ((_sample("sample-a", "a", "1"), _sample("sample-b", "a", "2")), "foreground_benchmark_duplicate_source"),
    ],
)
def test_benchmark_manifest_rejects_duplicate_identity(
    samples: tuple[GoldSampleManifest, ...], code: str
) -> None:
    with pytest.raises(DomainRuleError) as error:
        benchmark_manifest_sha256(samples)
    assert error.value.code == code


def test_benchmark_manifest_rejects_unreviewed_gold() -> None:
    sample = _sample("sample-a", "a", "1")
    unreviewed = GoldSampleManifest(
        **{**sample.__dict__, "human_reviewed": False}
    )

    with pytest.raises(DomainRuleError) as error:
        benchmark_manifest_sha256((unreviewed,))

    assert error.value.code == "foreground_benchmark_gold_unreviewed"


def test_model_manifest_hash_is_canonical() -> None:
    manifest = LocalModelManifest(
        model_name="sam2",
        model_version="2.1-small",
        source_url="https://github.com/facebookresearch/sam2",
        license_id="Apache-2.0",
        weights_sha256="a" * 64,
        model_tree_sha256="c" * 64,
        executable_sha256="b" * 64,
        runtime_versions=(
            ("pillow", "11.3.0"),
            ("python", "3.9.6"),
            ("safetensors", "0.7.0"),
            ("torch", "2.8.0"),
            ("torchvision", "0.23.0"),
            ("transformers", "4.57.6"),
        ),
        total_bytes=512_000_000,
    )

    digest = model_manifest_sha256(manifest)

    assert len(digest) == 64
    assert digest == sha256(manifest.canonical_json().encode()).hexdigest()


def _pipeline_artifact(
    role: str,
    name: str,
    digit: str,
) -> LocalModelArtifactManifest:
    filenames = (
        (
            "LICENSE",
            "config.json",
            "model.safetensors",
            "preprocessor_config.json",
            "processor_config.json",
            "worker.py",
        )
        if role == "coarse_segmenter"
        else (
            "BiRefNet_config.py",
            "LICENSE",
            "birefnet.py",
            "config.json",
            "model.safetensors",
        )
    )
    license_digest = digit * 64
    weights_digest = digit.upper() * 64
    return LocalModelArtifactManifest(
        role=role,  # type: ignore[arg-type]
        model_name=name,
        model_version="1.0.0",
        source_url=f"https://github.com/example/{name}",
        source_revision=digit * 40,
        license_id="Apache-2.0" if role == "coarse_segmenter" else "MIT",
        license_sha256=license_digest,
        weights_sha256=weights_digest,
        code_tree_sha256=("0" if digit != "0" else "1") * 64,
        weights_source_url=f"https://huggingface.co/example/{name}/weights",
        weights_source_revision=digit * 40,
        code_source_url=f"https://github.com/example/{name}/code",
        code_source_revision=digit * 40,
        license_source_url=f"https://github.com/example/{name}/license",
        license_source_revision=digit * 40,
        files_sha256=tuple(
            (
                filename,
                license_digest
                if filename == "LICENSE"
                else weights_digest
                if filename == "model.safetensors"
                else ("1" if digit != "1" else "2") * 64,
            )
            for filename in filenames
        ),
    )


def _pipeline() -> LocalForegroundPipelineManifest:
    return LocalForegroundPipelineManifest(
        pipeline_version="sam2-birefnet-official-v1",
        artifacts=(
            _pipeline_artifact("coarse_segmenter", "sam2", "a"),
            _pipeline_artifact("alpha_refiner", "birefnet", "b"),
        ),
        executable_sha256="c" * 64,
        worker_sha256="d" * 64,
        runtime_versions=(
            ("einops", "0.8.2"),
            ("kornia", "0.8.2"),
            ("pillow", "11.3.0"),
            ("python", "3.12.11"),
            ("safetensors", "0.7.0"),
            ("timm", "1.0.30"),
            ("torch", "2.8.0"),
            ("torchvision", "0.23.0"),
            ("transformers", "4.57.6"),
        ),
        total_bytes=900_000_000,
    )


def test_pipeline_manifest_hash_binds_ordered_sam_and_birefnet_artifacts() -> None:
    manifest = _pipeline()
    digest = pipeline_manifest_sha256(manifest)

    assert len(digest) == 64
    mutations = (
        replace(manifest.artifacts[0], model_version="2.1-small"),
        replace(
            manifest.artifacts[0],
            weights_sha256="d" * 64,
            files_sha256=tuple(
                (name, "d" * 64 if name == "model.safetensors" else digest)
                for name, digest in manifest.artifacts[0].files_sha256
            ),
        ),
        replace(manifest.artifacts[1], code_tree_sha256="e" * 64),
        replace(
            manifest.artifacts[1],
            license_sha256="f" * 64,
            files_sha256=tuple(
                (name, "f" * 64 if name == "LICENSE" else digest)
                for name, digest in manifest.artifacts[1].files_sha256
            ),
        ),
    )
    for changed_artifact in mutations:
        index = 0 if changed_artifact.role == "coarse_segmenter" else 1
        changed = replace(
            manifest,
            artifacts=manifest.artifacts[:index]
            + (changed_artifact,)
            + manifest.artifacts[index + 1 :],
        )
        assert pipeline_manifest_sha256(changed) != digest


def test_birefnet_manifest_binds_distinct_weight_code_and_license_provenance() -> None:
    manifest = _pipeline()
    digest = pipeline_manifest_sha256(manifest)
    birefnet = manifest.artifacts[1]

    mutations = (
        replace(birefnet, weights_source_url="https://example.com/other-weights"),
        replace(birefnet, code_source_revision="e" * 40),
        replace(birefnet, license_source_url="https://example.com/other-license"),
        replace(
            birefnet,
            files_sha256=tuple(
                (name, "f" * 64 if name == "birefnet.py" else digest)
                for name, digest in birefnet.files_sha256
            ),
        ),
    )

    for changed in mutations:
        assert pipeline_manifest_sha256(
            replace(manifest, artifacts=(manifest.artifacts[0], changed))
        ) != digest


@pytest.mark.parametrize(
    "artifacts",
    [
        (_pipeline().artifacts[0],),
        (_pipeline().artifacts[0], _pipeline().artifacts[0]),
        (
            _pipeline().artifacts[0],
            replace(_pipeline().artifacts[1], license_id="CC-BY-NC-4.0"),
        ),
        (
            _pipeline().artifacts[0],
            replace(_pipeline().artifacts[1], license_id="Apache-2.0"),
        ),
    ],
)
def test_pipeline_manifest_rejects_missing_duplicate_or_noncommercial_artifacts(
    artifacts: tuple[LocalModelArtifactManifest, ...],
) -> None:
    with pytest.raises(DomainRuleError) as error:
        pipeline_manifest_sha256(replace(_pipeline(), artifacts=artifacts))

    assert error.value.code == "foreground_pipeline_manifest_invalid"
