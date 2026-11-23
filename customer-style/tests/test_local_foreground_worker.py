from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from dataclasses import replace
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from threading import Event, Thread
from types import ModuleType, SimpleNamespace

import pytest
from PIL import Image

from opc_style.domain.errors import DomainRuleError
from opc_style.domain.foreground_benchmark import (
    LocalForegroundPipelineManifest,
    LocalModelArtifactManifest,
    LocalModelManifest,
    model_manifest_sha256,
    pipeline_manifest_sha256,
)
from opc_style.domain.product_foreground import (
    ForegroundExtractionRequest,
    ForegroundPromptHints,
)
from opc_style.providers.local_foreground_worker import LocalForegroundWorker

ROOT = Path(__file__).resolve().parents[1]


def _worker_module() -> ModuleType:
    path = ROOT / "tools/local_foreground_worker/main.py"
    spec = importlib.util.spec_from_file_location("local_foreground_runtime", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_pipeline_does_not_clip_birefnet_alpha_to_sam_support(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _worker_module()
    coarse = Image.new("L", (21, 1), 0)
    coarse.putpixel((0, 0), 255)
    refined = Image.new("L", (21, 1), 0)
    refined.putpixel((20, 0), 255)

    class FakePrediction:
        dtype = "float16"

        def __getitem__(self, _: object) -> FakePrediction:
            return self

        def clamp(self, *_: object) -> FakePrediction:
            return self

        def cpu(self) -> FakePrediction:
            return self

        def numpy(self) -> FakePrediction:
            return self

        def __mul__(self, _: object) -> FakePrediction:
            return self

        def astype(self, _: object) -> FakePrediction:
            return self

        def float(self) -> FakePrediction:
            return self

    class FakeTensor:
        def unsqueeze(self, _: int) -> FakeTensor:
            return self

        def to(self, *_: object, **__: object) -> FakeTensor:
            return self

    class FakeInferenceMode:
        def __enter__(self) -> None:
            return None

        def __exit__(self, *_: object) -> None:
            return None

    fake_transforms = SimpleNamespace(
        Compose=lambda _: lambda __: FakeTensor(),
        Resize=lambda _: object(),
        ToTensor=lambda: object(),
        Normalize=lambda *_: object(),
    )
    monkeypatch.setitem(
        sys.modules,
        "torch",
        SimpleNamespace(
            inference_mode=lambda: FakeInferenceMode(),
            sigmoid=lambda prediction: prediction,
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "torch.nn",
        SimpleNamespace(
            functional=SimpleNamespace(
                interpolate=lambda prediction, **_: prediction,
            )
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "torchvision",
        SimpleNamespace(transforms=fake_transforms),
    )
    class FakeModel:
        def parameters(self):
            yield FakePrediction()

        def __call__(self, _: object) -> list[FakePrediction]:
            return [FakePrediction()]

    monkeypatch.setattr(runtime, "_load_birefnet_model", lambda *_: FakeModel())
    monkeypatch.setattr(Image, "fromarray", lambda *_args, **_kwargs: refined.copy())

    result = runtime._birefnet_alpha(
        Path("/sealed/birefnet"),
        Image.new("RGB", (21, 1), "white"),
        coarse,
        "cpu",
    )

    assert result.getpixel((20, 0)) == 255
    assert result.getpixel((0, 0)) == 0


def _source() -> bytes:
    image = Image.new("RGB", (4, 4), (120, 10, 40))
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _outputs() -> tuple[bytes, bytes]:
    mask = Image.new("L", (4, 4), 0)
    for point in ((1, 1), (1, 2), (2, 1), (2, 2)):
        mask.putpixel(point, 255)
    mask_output = BytesIO()
    mask.save(mask_output, format="PNG")
    foreground = Image.new("RGBA", (4, 4), (120, 10, 40, 0))
    foreground.putalpha(mask)
    foreground_output = BytesIO()
    foreground.save(foreground_output, format="PNG")
    return mask_output.getvalue(), foreground_output.getvalue()


def _installed_candidate(
    root: Path,
    *,
    license_id: str = "Apache-2.0",
    manifest_override: dict[str, object] | None = None,
) -> tuple[Path, Path, Path, str]:
    root.mkdir(parents=True, exist_ok=True)
    executable = root / "worker"
    executable.write_bytes(b"worker-v1")
    executable.chmod(0o700)
    model_root = root / "model"
    model_root.mkdir()
    weights = model_root / "model.safetensors"
    weights.write_bytes(b"safetensors-v1")
    weights.chmod(0o600)
    model_files = {
        "config.json": b'{"model_type":"sam2"}',
        "preprocessor_config.json": b'{"size":1024}',
        "processor_config.json": b'{"processor_class":"Sam2Processor"}',
        "worker.py": b"sealed-worker-v1",
    }
    for filename, content in model_files.items():
        path = model_root / filename
        path.write_bytes(content)
        path.chmod(0o600)
    tree_payload = "".join(
        f"{filename}\0{sha256(content).hexdigest()}\n"
        for filename, content in sorted(
            {"model.safetensors": weights.read_bytes(), **model_files}.items()
        )
    ).encode()
    manifest = {
        "model_name": "sam2",
        "model_version": "2.1-small",
        "source_url": "https://github.com/facebookresearch/sam2",
        "license_id": license_id,
        "weights_sha256": sha256(weights.read_bytes()).hexdigest(),
        "model_tree_sha256": sha256(tree_payload).hexdigest(),
        "executable_sha256": sha256(executable.read_bytes()).hexdigest(),
        "runtime_versions": [
            ["pillow", "11.3.0"],
            ["python", "3.9.6"],
            ["safetensors", "0.7.0"],
            ["torch", "2.8.0"],
            ["torchvision", "0.23.0"],
            ["transformers", "4.57.6"],
        ],
        "total_bytes": sum(
            path.stat().st_size for path in model_root.iterdir() if path.is_file()
        ),
    }
    if manifest_override:
        manifest.update(manifest_override)
    manifest_path = model_root / "model-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    manifest_path.chmod(0o600)
    canonical = LocalModelManifest(**manifest)  # type: ignore[arg-type]
    return executable, model_root, manifest_path, model_manifest_sha256(canonical)


def _request(*, digest: str | None = None) -> ForegroundExtractionRequest:
    source = _source()
    return ForegroundExtractionRequest(
        customer_id=__import__("uuid").UUID("11111111-1111-1111-1111-111111111111"),
        asset_id=__import__("uuid").UUID("22222222-2222-2222-2222-222222222222"),
        product_id="product-1",
        source_content=source,
        source_content_sha256=digest or sha256(source).hexdigest(),
        mime_type="image/png",
        width=4,
        height=4,
        prompt_hints=ForegroundPromptHints(
            box=(0.2, 0.2, 0.8, 0.8),
            positive_points=((0.5, 0.5),),
            negative_points=((0.05, 0.05),),
        ),
    )


def _staged_artifact(root: Path, name: str, model_type: str) -> Path:
    artifact = root / name
    artifact.mkdir(parents=True)
    files = {
        "model.safetensors": f"{name}-weights".encode(),
        "config.json": json.dumps({"model_type": model_type}).encode(),
        "LICENSE": (
            b"MIT License\nPermission is hereby granted, free of charge\n"
            if model_type == "birefnet"
            else b"Apache License\nVersion 2.0, January 2004\n"
        ),
    }
    if model_type == "birefnet":
        files.update(
            {
                "BiRefNet_config.py": b"class BiRefNetConfig: pass\n",
                "birefnet.py": b"class BiRefNet: pass\n",
            }
        )
    else:
        files.update(
            {
                "preprocessor_config.json": b'{"size":1024}',
                "processor_config.json": json.dumps(
                    {"processor_class": f"{name.title()}Processor"}
                ).encode(),
                "worker.py": f"{name}-worker".encode(),
            }
        )
    for filename, content in files.items():
        (artifact / filename).write_bytes(content)
    return artifact


def _artifact_names(root: Path) -> tuple[str, ...]:
    if (root / "BiRefNet_config.py").exists():
        return ("BiRefNet_config.py", "LICENSE", "birefnet.py", "config.json", "model.safetensors")
    return (
        "LICENSE",
        "config.json",
        "model.safetensors",
        "preprocessor_config.json",
        "processor_config.json",
        "worker.py",
    )


def _code_tree_sha256(root: Path) -> str:
    names = (
        ("BiRefNet_config.py", "birefnet.py", "config.json")
        if (root / "BiRefNet_config.py").exists()
        else ("config.json", "preprocessor_config.json", "processor_config.json", "worker.py")
    )
    payload = "".join(
        f"{name}\0{sha256((root / name).read_bytes()).hexdigest()}\n"
        for name in names
    ).encode()
    return sha256(payload).hexdigest()


def _installed_pipeline(root: Path) -> tuple[Path, Path, Path, str]:
    model_root = root / "pipeline"
    sam = _staged_artifact(model_root, "sam2", "sam2")
    birefnet = _staged_artifact(model_root, "birefnet", "birefnet")
    (model_root / "worker.py").write_bytes(b"pipeline-orchestrator-v1")
    executable = model_root / "runner"
    executable.write_bytes(b"pipeline-worker-v1")
    executable.chmod(0o700)
    artifacts = tuple(
        LocalModelArtifactManifest(
            role=role,  # type: ignore[arg-type]
            model_name=name,
            model_version=version,
            source_url=source_url,
            source_revision=revision,
            license_id="Apache-2.0" if role == "coarse_segmenter" else "MIT",
            license_sha256=sha256((path / "LICENSE").read_bytes()).hexdigest(),
            weights_sha256=sha256(
                (path / "model.safetensors").read_bytes()
            ).hexdigest(),
            code_tree_sha256=_code_tree_sha256(path),
            weights_source_url=f"{source_url}/resolve/{revision}/model.safetensors",
            weights_source_revision=revision,
            code_source_url=f"{source_url}/tree/{revision}",
            code_source_revision=revision,
            license_source_url=f"{source_url}/blob/{revision}/LICENSE",
            license_source_revision=revision,
            files_sha256=tuple(
                (filename, sha256((path / filename).read_bytes()).hexdigest())
                for filename in _artifact_names(path)
            ),
        )
        for role, name, version, source_url, revision, path in (
            (
                "coarse_segmenter",
                "sam2",
                "2.1-small",
                "https://github.com/facebookresearch/sam2",
                "a" * 40,
                sam,
            ),
            (
                "alpha_refiner",
                "birefnet",
                "1.0.0",
                "https://github.com/ZhengPeng7/BiRefNet",
                "b" * 40,
                birefnet,
            ),
        )
    )
    total_bytes = (
        executable.stat().st_size
        + (model_root / "worker.py").stat().st_size
        + sum(
        path.stat().st_size
        for artifact in (sam, birefnet)
        for path in artifact.iterdir()
        if path.is_file()
        )
    )
    manifest = LocalForegroundPipelineManifest(
        pipeline_version="sam2-birefnet-official-v1",
        artifacts=artifacts,
        executable_sha256=sha256(executable.read_bytes()).hexdigest(),
        worker_sha256=sha256((model_root / "worker.py").read_bytes()).hexdigest(),
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
        total_bytes=total_bytes,
    )
    manifest_path = model_root / "pipeline-manifest.json"
    manifest_path.write_text(manifest.canonical_json(), encoding="utf-8")
    return executable, model_root, manifest_path, pipeline_manifest_sha256(manifest)


def _successful_runner(prompt_capture: list[dict[str, object]] | None = None):
    mask, foreground = _outputs()

    def runner(command: list[str], **_: object) -> subprocess.CompletedProcess[bytes]:
        values = {command[index]: Path(command[index + 1]) for index in range(1, len(command) - 1, 2)}
        values["--mask-output"].write_bytes(mask)
        values["--foreground-output"].write_bytes(foreground)
        prompt = json.loads(values["--prompt-json"].read_text(encoding="utf-8"))
        if prompt_capture is not None:
            prompt_capture.append(prompt)
        receipt = {
            "source_sha256": sha256(values["--input"].read_bytes()).hexdigest(),
            "mask_sha256": sha256(mask).hexdigest(),
            "foreground_sha256": sha256(foreground).hexdigest(),
            "model_manifest_sha256": command[command.index("--model-manifest-sha256") + 1],
        }
        values["--receipt-output"].write_text(json.dumps(receipt), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, b"", b"")

    return runner


def _successful_pipeline_runner(*, omit_refiner: bool = False):
    mask, foreground = _outputs()

    def runner(command: list[str], **_: object) -> subprocess.CompletedProcess[bytes]:
        values = {
            command[index]: Path(command[index + 1])
            for index in range(1, len(command) - 1, 2)
        }
        values["--coarse-mask-output"].write_bytes(mask)
        values["--mask-output"].write_bytes(mask)
        values["--foreground-output"].write_bytes(foreground)
        prompt_bytes = values["--prompt-json"].read_bytes()
        manifest = json.loads(
            (values["--model-root"] / "pipeline-manifest.json").read_text(
                encoding="utf-8"
            )
        )
        artifact_digests = {
            item["role"]: item["weights_sha256"] for item in manifest["artifacts"]
        }
        stages = [
            {
                "role": "coarse_segmenter",
                "artifact_sha256": artifact_digests["coarse_segmenter"],
                "output_sha256": sha256(mask).hexdigest(),
                "parameters_sha256": sha256(prompt_bytes).hexdigest(),
            }
        ]
        if not omit_refiner:
            stages.append(
                {
                    "role": "alpha_refiner",
                    "artifact_sha256": artifact_digests["alpha_refiner"],
                    "output_sha256": sha256(mask).hexdigest(),
                    "parameters_sha256": sha256(
                        prompt_bytes + sha256(mask).hexdigest().encode()
                    ).hexdigest(),
                }
            )
        receipt = {
            "source_sha256": sha256(values["--input"].read_bytes()).hexdigest(),
            "coarse_mask_sha256": sha256(mask).hexdigest(),
            "mask_sha256": sha256(mask).hexdigest(),
            "foreground_sha256": sha256(foreground).hexdigest(),
            "model_manifest_sha256": command[
                command.index("--model-manifest-sha256") + 1
            ],
            "stage_receipts": stages,
        }
        values["--receipt-output"].write_text(json.dumps(receipt), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, b"", b"")

    return runner


def _worker(root: Path, *, runner=None, **candidate_options: object) -> LocalForegroundWorker:
    executable, model_root, manifest_path, digest = _installed_candidate(
        root, **candidate_options
    )
    return LocalForegroundWorker(
        executable_path=executable,
        model_root=model_root,
        manifest_path=manifest_path,
        expected_model_manifest_sha256=digest,
        runner=runner or _successful_runner(),
    )


def test_successful_worker_serializes_prompts_and_binds_model_digest(tmp_path: Path) -> None:
    prompts: list[dict[str, object]] = []
    worker = _worker(tmp_path, runner=_successful_runner(prompts))

    result = worker.extract(_request())

    assert prompts == [
        {
            "box": [0.2, 0.2, 0.8, 0.8],
            "negative_points": [[0.05, 0.05]],
            "positive_points": [[0.5, 0.5]],
        }
    ]
    assert result.extractor_version.startswith("local-sam2:")
    assert sha256(result.mask_png).hexdigest() == result.mask_sha256


def test_startup_attestation_digest_reuses_verified_startup_proof(
    tmp_path: Path,
) -> None:
    worker = _worker(tmp_path)

    startup_digest = worker.verify_installation()
    worker._manifest_path.write_text("{}", encoding="utf-8")

    assert worker.startup_attestation_digest() == startup_digest
    with pytest.raises(DomainRuleError):
        worker.extract(_request())


def test_installation_manifest_exposes_verified_pipeline_identity(tmp_path: Path) -> None:
    executable, model_root, manifest_path, digest = _installed_pipeline(tmp_path)
    worker = LocalForegroundWorker(
        executable_path=executable,
        model_root=model_root,
        manifest_path=manifest_path,
        expected_model_manifest_sha256=digest,
        runner=_successful_pipeline_runner(),
    )

    manifest = worker.installation_manifest()

    assert isinstance(manifest, LocalForegroundPipelineManifest)
    assert pipeline_manifest_sha256(manifest) == worker.verify_installation()


def test_worker_supplies_full_image_box_when_prompt_is_missing(tmp_path: Path) -> None:
    prompts: list[dict[str, object]] = []
    worker = _worker(tmp_path, runner=_successful_runner(prompts))

    worker.extract(replace(_request(), prompt_hints=None))

    assert prompts == [
        {
            "box": [0.0, 0.0, 1.0, 1.0],
            "negative_points": [],
            "positive_points": [],
        }
    ]


def test_rejects_missing_or_symlinked_executable(tmp_path: Path) -> None:
    worker = _worker(tmp_path)
    worker._executable_path.unlink()
    with pytest.raises(DomainRuleError) as missing:
        worker.extract(_request())
    assert missing.value.code == "foreground_candidate_unavailable"

    target = tmp_path / "real-worker"
    target.write_bytes(b"worker")
    target.chmod(0o700)
    worker._executable_path.symlink_to(target)
    with pytest.raises(DomainRuleError) as symlink:
        worker.extract(_request())
    assert symlink.value.code == "foreground_candidate_unavailable"


def test_rejects_model_digest_mismatch_and_incomplete_license(tmp_path: Path) -> None:
    worker = _worker(tmp_path / "digest")
    worker._expected_model_manifest_sha256 = "0" * 64
    with pytest.raises(DomainRuleError) as digest:
        worker.extract(_request())
    assert digest.value.code == "foreground_candidate_manifest_mismatch"

    noncommercial = _worker(tmp_path / "license", license_id="non-commercial")
    with pytest.raises(DomainRuleError) as license_error:
        noncommercial.extract(_request())
    assert license_error.value.code == "foreground_candidate_license_invalid"


def test_rejects_tampered_model_configuration(tmp_path: Path) -> None:
    worker = _worker(tmp_path)
    config = tmp_path / "model/config.json"
    config.write_text('{"model_type":"tampered"}', encoding="utf-8")

    with pytest.raises(DomainRuleError) as error:
        worker.extract(_request())

    assert error.value.code == "foreground_candidate_manifest_mismatch"


def test_rejects_tampered_worker_implementation(tmp_path: Path) -> None:
    worker = _worker(tmp_path)
    implementation = tmp_path / "model/worker.py"
    implementation.write_text("tampered-worker", encoding="utf-8")

    with pytest.raises(DomainRuleError) as error:
        worker.extract(_request())

    assert error.value.code == "foreground_candidate_manifest_mismatch"


def test_rejects_timeout_nonzero_and_never_leaks_private_error(tmp_path: Path) -> None:
    secret = "PRIVATE-IMAGE-CONTENT"

    def timeout(*_: object, **__: object) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired(secret, 20, output=secret.encode(), stderr=secret.encode())

    with pytest.raises(DomainRuleError) as timed_out:
        _worker(tmp_path / "timeout", runner=timeout).extract(_request())
    assert timed_out.value.code == "foreground_candidate_failed"
    assert secret not in str(timed_out.value)

    def nonzero(command: list[str], **_: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(command, 2, secret.encode(), secret.encode())

    with pytest.raises(DomainRuleError) as failed:
        _worker(tmp_path / "nonzero", runner=nonzero).extract(_request())
    assert failed.value.code == "foreground_candidate_failed"
    assert secret not in str(failed.value)


def test_rejects_concurrent_local_model_execution_as_retryable_busy_work(
    tmp_path: Path,
) -> None:
    entered = Event()
    release = Event()
    completed: list[object] = []
    successful_runner = _successful_runner()

    def blocking_runner(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        entered.set()
        assert release.wait(timeout=2)
        return successful_runner(command, **kwargs)

    worker = _worker(tmp_path, runner=blocking_runner)

    def execute_first() -> None:
        completed.append(worker.extract(_request()))

    thread = Thread(target=execute_first)
    thread.start()
    assert entered.wait(timeout=2)
    try:
        with pytest.raises(DomainRuleError) as busy:
            worker.extract(_request())
        assert busy.value.code == "foreground_candidate_busy"
    finally:
        release.set()
        thread.join(timeout=2)

    assert not thread.is_alive()
    assert len(completed) == 1


def test_rejects_oversized_malformed_output_and_wrong_source_digest(tmp_path: Path) -> None:
    mask, foreground = _outputs()

    def oversized(command: list[str], **_: object) -> subprocess.CompletedProcess[bytes]:
        values = {command[index]: Path(command[index + 1]) for index in range(1, len(command) - 1, 2)}
        values["--mask-output"].write_bytes(mask + b"x" * 200)
        values["--foreground-output"].write_bytes(foreground)
        values["--receipt-output"].write_text("{}", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, b"", b"")

    worker = _worker(tmp_path / "oversized", runner=oversized)
    worker._max_output_bytes = 100
    with pytest.raises(DomainRuleError) as too_large:
        worker.extract(_request())
    assert too_large.value.code == "foreground_candidate_failed"

    def malformed(command: list[str], **_: object) -> subprocess.CompletedProcess[bytes]:
        values = {command[index]: Path(command[index + 1]) for index in range(1, len(command) - 1, 2)}
        values["--mask-output"].write_bytes(b"not-a-png")
        values["--foreground-output"].write_bytes(b"not-a-png")
        values["--receipt-output"].write_text("{}", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, b"", b"")

    with pytest.raises(DomainRuleError) as bad_output:
        _worker(tmp_path / "malformed", runner=malformed).extract(_request())
    assert bad_output.value.code == "foreground_candidate_failed"

    with pytest.raises(DomainRuleError) as bad_source:
        _worker(tmp_path / "source").extract(_request(digest="f" * 64))
    assert bad_source.value.code == "foreground_source_incomplete"


def test_worker_help_does_not_require_model_dependencies() -> None:
    completed = subprocess.run(
        [sys.executable, ROOT / "tools/local_foreground_worker/main.py", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert "--model-root" in completed.stdout
    assert "--coarse-mask-output" in completed.stdout
    assert "No module named" not in completed.stderr


def test_worker_uses_static_image_sam2_model() -> None:
    source = (ROOT / "tools/local_foreground_worker/main.py").read_text(encoding="utf-8")

    assert "from transformers import AutoProcessor, Sam2Model" in source
    assert "AutoModel.from_pretrained" not in source
    assert "from transformers import AutoModelForImageSegmentation" in source
    assert "local_files_only=True" in source
    assert "dtype=model_dtype" in source
    assert ")[0, 0].float()" in source


def test_install_script_seals_staged_files_without_network(tmp_path: Path) -> None:
    weights = tmp_path / "model.safetensors"
    weights.write_bytes(b"pinned-weights")
    config = tmp_path / "config.json"
    config.write_text('{"model_type":"sam2"}', encoding="utf-8")
    preprocessor = tmp_path / "preprocessor_config.json"
    preprocessor.write_text('{"size":1024}', encoding="utf-8")
    processor = tmp_path / "processor_config.json"
    processor.write_text('{"processor_class":"Sam2Processor"}', encoding="utf-8")
    license_file = tmp_path / "LICENSE"
    license_file.write_text("Apache License\nVersion 2.0, January 2004\n", encoding="utf-8")
    destination = tmp_path / "installed"

    completed = subprocess.run(
        [
            "/bin/bash",
            ROOT / "scripts/install_local_foreground_candidate.sh",
            "--weights",
            weights,
            "--weights-sha256",
            sha256(weights.read_bytes()).hexdigest(),
            "--config",
            config,
            "--preprocessor-config",
            preprocessor,
            "--processor-config",
            processor,
            "--license",
            license_file,
            "--worker",
            ROOT / "tools/local_foreground_worker/main.py",
            "--python",
            sys.executable,
            "--destination",
            destination,
            "--source-url",
            "https://github.com/facebookresearch/sam2",
            "--model-version",
            "2.1-small",
            "--runtime-versions-json",
            '{"pillow":"11.3.0","python":"3.9.6","safetensors":"0.7.0","torch":"2.8.0","torchvision":"0.23.0","transformers":"4.57.6"}',
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    manifest = json.loads((destination / "model-manifest.json").read_text())
    assert manifest["license_id"] == "Apache-2.0"
    assert manifest["weights_sha256"] == sha256(weights.read_bytes()).hexdigest()
    assert dict(manifest["runtime_versions"])["torchvision"] == "0.23.0"
    assert len(manifest["model_tree_sha256"]) == 64
    assert (destination / "runner").stat().st_mode & 0o111
    assert "http" not in completed.stderr.lower()


def test_install_script_rejects_non_apache_license(tmp_path: Path) -> None:
    weights = tmp_path / "model.safetensors"
    weights.write_bytes(b"pinned-weights")
    config = tmp_path / "config.json"
    config.write_text('{"model_type":"sam2"}', encoding="utf-8")
    preprocessor = tmp_path / "preprocessor_config.json"
    preprocessor.write_text('{"size":1024}', encoding="utf-8")
    processor = tmp_path / "processor_config.json"
    processor.write_text('{"processor_class":"Sam2Processor"}', encoding="utf-8")
    license_file = tmp_path / "LICENSE"
    license_file.write_text("non-commercial only\n", encoding="utf-8")

    completed = subprocess.run(
        [
            "/bin/bash",
            ROOT / "scripts/install_local_foreground_candidate.sh",
            "--weights",
            weights,
            "--weights-sha256",
            sha256(weights.read_bytes()).hexdigest(),
            "--config",
            config,
            "--preprocessor-config",
            preprocessor,
            "--processor-config",
            processor,
            "--license",
            license_file,
            "--worker",
            ROOT / "tools/local_foreground_worker/main.py",
            "--python",
            sys.executable,
            "--destination",
            tmp_path / "installed",
            "--source-url",
            "https://github.com/facebookresearch/sam2",
            "--model-version",
            "2.1-small",
            "--runtime-versions-json",
            '{"pillow":"11.3.0","python":"3.9.6","safetensors":"0.7.0","torch":"2.8.0","torchvision":"0.23.0","transformers":"4.57.6"}',
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "license_invalid" in completed.stderr


def _run_pipeline_installer(
    sam: Path,
    birefnet: Path,
    destination: Path,
    *,
    birefnet_code_sha256: str | None = None,
) -> subprocess.CompletedProcess[str]:
    command = [
        "/bin/bash",
        ROOT / "scripts/install_local_foreground_candidate.sh",
        "--sam-root",
        sam,
        "--sam-weights-sha256",
        sha256((sam / "model.safetensors").read_bytes()).hexdigest(),
        "--sam-source-url",
        "https://github.com/facebookresearch/sam2",
        "--sam-source-revision",
        "a" * 40,
        "--sam-model-version",
        "2.1-small",
        "--birefnet-root",
        birefnet,
        "--birefnet-weights-sha256",
        sha256((birefnet / "model.safetensors").read_bytes()).hexdigest(),
        "--birefnet-code-sha256",
        birefnet_code_sha256 or _code_tree_sha256(birefnet),
        "--birefnet-source-url",
        "https://huggingface.co/ZhengPeng7/BiRefNet",
        "--birefnet-source-revision",
        "b" * 40,
        "--birefnet-model-version",
        "1.0.0",
        "--worker",
        ROOT / "tools/local_foreground_worker/main.py",
        "--python",
        sys.executable,
        "--destination",
        destination,
        "--runtime-versions-json",
        '{"einops":"0.8.2","kornia":"0.8.2","pillow":"11.3.0","python":"3.12.11","safetensors":"0.7.0","timm":"1.0.30","torch":"2.8.0","torchvision":"0.23.0","transformers":"4.57.6"}',
    ]
    return subprocess.run(command, check=False, capture_output=True, text=True)


def test_install_script_seals_sam_and_birefnet_without_network(
    tmp_path: Path,
) -> None:
    sam = _staged_artifact(tmp_path, "sam-stage", "sam2")
    birefnet = _staged_artifact(tmp_path, "birefnet-stage", "birefnet")
    destination = tmp_path / "installed"

    completed = subprocess.run(
        [
            "/bin/bash",
            ROOT / "scripts/install_local_foreground_candidate.sh",
            "--sam-root",
            sam,
            "--sam-weights-sha256",
            sha256((sam / "model.safetensors").read_bytes()).hexdigest(),
            "--sam-source-url",
            "https://github.com/facebookresearch/sam2",
            "--sam-source-revision",
            "a" * 40,
            "--sam-model-version",
            "2.1-small",
            "--birefnet-root",
            birefnet,
            "--birefnet-weights-sha256",
            sha256((birefnet / "model.safetensors").read_bytes()).hexdigest(),
            "--birefnet-code-sha256",
            _code_tree_sha256(birefnet),
            "--birefnet-source-url",
            "https://github.com/ZhengPeng7/BiRefNet",
            "--birefnet-source-revision",
            "b" * 40,
            "--birefnet-model-version",
            "1.0.0",
            "--worker",
            ROOT / "tools/local_foreground_worker/main.py",
            "--python",
            sys.executable,
            "--destination",
            destination,
            "--runtime-versions-json",
            '{"einops":"0.8.2","kornia":"0.8.2","pillow":"11.3.0","python":"3.12.11","safetensors":"0.7.0","timm":"1.0.30","torch":"2.8.0","torchvision":"0.23.0","transformers":"4.57.6"}',
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    manifest = json.loads((destination / "pipeline-manifest.json").read_text())
    assert [item["role"] for item in manifest["artifacts"]] == [
        "coarse_segmenter",
        "alpha_refiner",
    ]
    assert all(len(item["license_sha256"]) == 64 for item in manifest["artifacts"])
    assert all(len(item["weights_sha256"]) == 64 for item in manifest["artifacts"])
    assert len(manifest["executable_sha256"]) == 64
    assert len(manifest["worker_sha256"]) == 64
    assert (destination / "runner").stat().st_mode & 0o111
    assert "http" not in completed.stderr.lower()


def test_pipeline_installer_accepts_only_official_birefnet_layout(tmp_path: Path) -> None:
    sam = _staged_artifact(tmp_path, "sam-stage", "sam2")
    birefnet = _staged_artifact(tmp_path, "birefnet-stage", "birefnet")
    destination = tmp_path / "installed"

    completed = _run_pipeline_installer(sam, birefnet, destination)

    assert completed.returncode == 0, completed.stderr
    assert tuple(sorted(path.name for path in (destination / "birefnet").iterdir())) == (
        "BiRefNet_config.py",
        "LICENSE",
        "birefnet.py",
        "config.json",
        "model.safetensors",
    )
    artifact = json.loads((destination / "pipeline-manifest.json").read_text())["artifacts"][1]
    assert [item[0] for item in artifact["files_sha256"]] == list(_artifact_names(birefnet))


@pytest.mark.parametrize("failure", ["missing", "extra", "symlink", "tampered"])
def test_pipeline_installer_rejects_missing_extra_symlinked_or_tampered_custom_code(
    tmp_path: Path, failure: str
) -> None:
    sam = _staged_artifact(tmp_path, "sam-stage", "sam2")
    birefnet = _staged_artifact(tmp_path, "birefnet-stage", "birefnet")
    expected_code_sha256 = _code_tree_sha256(birefnet)
    if failure == "missing":
        (birefnet / "birefnet.py").unlink()
    elif failure == "extra":
        (birefnet / "unexpected.py").write_text("raise RuntimeError\n")
    elif failure == "symlink":
        target = birefnet / "actual.py"
        target.write_text("class BiRefNet: pass\n")
        (birefnet / "birefnet.py").unlink()
        (birefnet / "birefnet.py").symlink_to(target)
    else:
        (birefnet / "birefnet.py").write_text("raise RuntimeError('tampered')\n")

    completed = _run_pipeline_installer(
        sam,
        birefnet,
        tmp_path / "installed",
        birefnet_code_sha256=expected_code_sha256,
    )

    assert completed.returncode != 0
    assert "foreground_candidate_" in completed.stderr


def test_birefnet_loader_uses_only_sealed_local_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    birefnet = _staged_artifact(tmp_path, "birefnet", "birefnet")
    calls: list[tuple[object, dict[str, object]]] = []

    class FakeModel:
        def to(self, device: str) -> FakeModel:
            assert device == "cpu"
            return self

        def eval(self) -> FakeModel:
            return self

    def fake_from_pretrained(path: object, **kwargs: object) -> FakeModel:
        calls.append((path, kwargs))
        return FakeModel()

    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setitem(
        sys.modules,
        "transformers",
        SimpleNamespace(
            AutoModelForImageSegmentation=SimpleNamespace(
                from_pretrained=fake_from_pretrained
            )
        ),
    )

    _worker_module()._load_birefnet_model(birefnet, "cpu")

    assert calls == [
        (
            birefnet.resolve(),
            {"local_files_only": True, "trust_remote_code": True},
        )
    ]


def test_birefnet_loader_fails_when_network_or_cache_resolution_is_attempted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    birefnet = _staged_artifact(tmp_path, "birefnet", "birefnet")
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    monkeypatch.setenv("NO_PROXY", "*")

    with pytest.raises(RuntimeError, match="offline_environment_required"):
        _worker_module()._load_birefnet_model(birefnet, "cpu")


def test_install_script_rejects_unpinned_source_revision(tmp_path: Path) -> None:
    sam = _staged_artifact(tmp_path, "sam-stage", "sam2")
    birefnet = _staged_artifact(tmp_path, "birefnet-stage", "birefnet")
    completed = subprocess.run(
        [
            "/bin/bash",
            ROOT / "scripts/install_local_foreground_candidate.sh",
            "--sam-root",
            sam,
            "--sam-weights-sha256",
            sha256((sam / "model.safetensors").read_bytes()).hexdigest(),
            "--sam-source-url",
            "https://github.com/facebookresearch/sam2",
            "--sam-source-revision",
            "main",
            "--sam-model-version",
            "2.1-small",
            "--birefnet-root",
            birefnet,
            "--birefnet-weights-sha256",
            sha256((birefnet / "model.safetensors").read_bytes()).hexdigest(),
            "--birefnet-code-sha256",
            _code_tree_sha256(birefnet),
            "--birefnet-source-url",
            "https://github.com/ZhengPeng7/BiRefNet",
            "--birefnet-source-revision",
            "b" * 40,
            "--birefnet-model-version",
            "1.0.0",
            "--worker",
            ROOT / "tools/local_foreground_worker/main.py",
            "--python",
            sys.executable,
            "--destination",
            tmp_path / "installed",
            "--runtime-versions-json",
            '{"einops":"0.8.2","kornia":"0.8.2","pillow":"11.3.0","python":"3.12.11","safetensors":"0.7.0","timm":"1.0.30","torch":"2.8.0","torchvision":"0.23.0","transformers":"4.57.6"}',
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "source_revision_invalid" in completed.stderr


def test_worker_rejects_tampered_second_artifact(tmp_path: Path) -> None:
    executable, model_root, manifest_path, digest = _installed_pipeline(tmp_path)
    worker = LocalForegroundWorker(
        executable_path=executable,
        model_root=model_root,
        manifest_path=manifest_path,
        expected_model_manifest_sha256=digest,
        runner=_successful_runner(),
    )
    (model_root / "birefnet" / "config.json").write_text(
        '{"model_type":"tampered"}', encoding="utf-8"
    )

    with pytest.raises(DomainRuleError) as error:
        worker.extract(_request())

    assert error.value.code == "foreground_candidate_manifest_mismatch"


def test_worker_rejects_tampered_pipeline_orchestrator(tmp_path: Path) -> None:
    executable, model_root, manifest_path, digest = _installed_pipeline(tmp_path)
    worker = LocalForegroundWorker(
        executable_path=executable,
        model_root=model_root,
        manifest_path=manifest_path,
        expected_model_manifest_sha256=digest,
        runner=_successful_pipeline_runner(),
    )
    (model_root / "worker.py").write_bytes(b"tampered-orchestrator")

    with pytest.raises(DomainRuleError) as error:
        worker.extract(_request())

    assert error.value.code == "foreground_candidate_manifest_mismatch"


def test_worker_runs_sam_then_birefnet_and_binds_all_outputs(tmp_path: Path) -> None:
    executable, model_root, manifest_path, digest = _installed_pipeline(tmp_path)
    worker = LocalForegroundWorker(
        executable_path=executable,
        model_root=model_root,
        manifest_path=manifest_path,
        expected_model_manifest_sha256=digest,
        runner=_successful_pipeline_runner(),
    )

    result = worker.extract(_request())

    assert result.extractor_version.startswith("local-sam2-birefnet-official-v1:")
    assert result.pipeline_manifest_sha256 == digest
    assert result.coarse_mask_png is not None
    assert result.coarse_mask_sha256 == sha256(result.coarse_mask_png).hexdigest()
    assert [receipt.role for receipt in result.stage_receipts] == [
        "coarse_segmenter",
        "alpha_refiner",
    ]


def test_worker_blocks_valid_sam_with_missing_birefnet_receipt(
    tmp_path: Path,
) -> None:
    executable, model_root, manifest_path, digest = _installed_pipeline(tmp_path)
    worker = LocalForegroundWorker(
        executable_path=executable,
        model_root=model_root,
        manifest_path=manifest_path,
        expected_model_manifest_sha256=digest,
        runner=_successful_pipeline_runner(omit_refiner=True),
    )

    with pytest.raises(DomainRuleError) as error:
        worker.extract(_request())

    assert error.value.code == "foreground_candidate_failed"


def test_worker_blocks_private_error_from_either_pipeline_stage(
    tmp_path: Path,
) -> None:
    secret = "/private/customer/photo.png"
    executable, model_root, manifest_path, digest = _installed_pipeline(tmp_path)

    def failed(command: list[str], **_: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(command, 9, secret.encode(), secret.encode())

    worker = LocalForegroundWorker(
        executable_path=executable,
        model_root=model_root,
        manifest_path=manifest_path,
        expected_model_manifest_sha256=digest,
        runner=failed,
    )
    with pytest.raises(DomainRuleError) as error:
        worker.extract(_request())

    assert error.value.code == "foreground_candidate_failed"
    assert secret not in str(error.value)
