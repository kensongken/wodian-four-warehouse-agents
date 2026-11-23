"""Private local benchmark API and data-boundary regression tests."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from uuid import UUID

from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy.orm import Session, sessionmaker

from opc_style.api.dependencies import get_session
from opc_style.db import create_database_engine
from opc_style.domain.foreground_benchmark import (
    CriticalComponent,
    GoldSampleManifest,
    LocalForegroundPipelineManifest,
    LocalModelArtifactManifest,
    pipeline_manifest_sha256,
)
from opc_style.main import create_app
from opc_style.models import Base, Customer
from opc_style.services.foreground_benchmarks import (
    FOREGROUND_BENCHMARK_CONFIG_SHA256,
    ValidatedBenchmarkDataset,
    ValidatedBenchmarkSample,
)
from opc_style.settings import ForegroundBenchmarkSettings

CUSTOMER_A = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
CUSTOMER_B = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


def _png(mode: str, values: list[int]) -> bytes:
    image = Image.new(mode, (2, 2))
    image.putdata(values)
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _dataset() -> ValidatedBenchmarkDataset:
    source = _png("RGB", [255, 255, 255, 255])
    mask = _png("L", [255, 255, 0, 0])
    component = _png("L", [255, 0, 0, 0])
    manifest = GoldSampleManifest(
        sample_id="opaque-sample-1",
        source_sha256=sha256(source).hexdigest(),
        width=2,
        height=2,
        mime_type="image/png",
        category="bracelet",
        difficulty="hard",
        scene_tags=("complex_background",),
        material_tags=("fine_chain",),
        gold_mask_sha256=sha256(mask).hexdigest(),
        critical_components=(
            CriticalComponent(
                name="pendant", mask_sha256=sha256(component).hexdigest()
            ),
        ),
        human_reviewed=True,
        split="acceptance",
    )
    sample = ValidatedBenchmarkSample(
        manifest=manifest,
        source_content=source,
        gold_mask_content=mask,
        critical_component_masks=(("pendant", component),),
    )
    return ValidatedBenchmarkDataset(
        samples=(sample,),
        seal_sha256="d" * 64,
        acceptance_count=1,
        calibration_count=0,
        categories=("bracelet",),
    )


def _model() -> LocalForegroundPipelineManifest:
    return LocalForegroundPipelineManifest(
        pipeline_version="sam2-birefnet-v1",
        artifacts=(
            LocalModelArtifactManifest(
                role="coarse_segmenter",
                model_name="sam2",
                model_version="2.1-small",
                source_url="https://example.invalid/sam2",
                source_revision="1" * 40,
                license_id="Apache-2.0",
                license_sha256="1" * 64,
                weights_sha256="2" * 64,
                code_tree_sha256="3" * 64,
            ),
            LocalModelArtifactManifest(
                role="alpha_refiner",
                model_name="birefnet",
                model_version="1.0.0",
                source_url="https://example.invalid/birefnet",
                source_revision="4" * 40,
                license_id="MIT",
                license_sha256="5" * 64,
                weights_sha256="6" * 64,
                code_tree_sha256="7" * 64,
            ),
        ),
        executable_sha256="8" * 64,
        worker_sha256="9" * 64,
        runtime_versions=(
            ("pillow", "11.3.0"),
            ("python", "3.9.6"),
            ("safetensors", "0.7.0"),
            ("torch", "2.8.0"),
            ("torchvision", "0.23.0"),
            ("transformers", "4.57.6"),
        ),
        total_bytes=2,
    )


@contextmanager
def _client(tmp_path: Path, *, enabled: bool) -> Generator[TestClient, None, None]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'benchmark.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    with sessions() as session:
        session.add(Customer(id=CUSTOMER_A, display_name="Customer A"))
        session.commit()
    app = create_app()
    app.state.foreground_benchmark_settings = ForegroundBenchmarkSettings(
        enabled=enabled,
        root=tmp_path.resolve() if enabled else None,
    )
    dataset = _dataset()
    model = _model()
    if enabled:
        app.state.foreground_benchmark_dataset_loader = lambda root: dataset
        app.state.foreground_benchmark_model_manifest = model
        app.state.foreground_benchmark_mask_provider = lambda sample: (
            sample.gold_mask_content
        )

    def session_override() -> Generator[Session, None, None]:
        with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = session_override
    try:
        with TestClient(app) as client:
            yield client
    finally:
        engine.dispose()


def _headers(customer_id: UUID = CUSTOMER_A) -> dict[str, str]:
    return {"X-Customer-Id": str(customer_id)}


def _payload() -> dict[str, str]:
    return {
        "idempotency_key": "local-benchmark-1",
        "dataset_seal_sha256": "d" * 64,
        "model_manifest_sha256": pipeline_manifest_sha256(_model()),
        "config_sha256": FOREGROUND_BENCHMARK_CONFIG_SHA256,
    }


def test_disabled_benchmark_does_not_disclose_local_configuration(
    tmp_path: Path,
) -> None:
    with _client(tmp_path, enabled=False) as client:
        response = client.get(
            "/local/style-suite/api/foreground-benchmarks/validate",
            headers=_headers(),
        )
    assert response.status_code == 404
    assert response.json() == {
        "error": {"code": "foreground_benchmark_unavailable", "retryable": False}
    }
    assert str(tmp_path) not in response.text


def test_run_is_idempotent_scoped_and_reports_without_paths(tmp_path: Path) -> None:
    with _client(tmp_path, enabled=True) as client:
        validation = client.get(
            "/local/style-suite/api/foreground-benchmarks/validate",
            headers=_headers(),
        )
        created = client.post(
            "/local/style-suite/api/foreground-benchmarks/runs",
            headers=_headers(),
            json=_payload(),
        )
        replay = client.post(
            "/local/style-suite/api/foreground-benchmarks/runs",
            headers=_headers(),
            json=_payload(),
        )
        run_id = created.json()["id"]
        foreign = client.get(
            f"/local/style-suite/api/foreground-benchmarks/runs/{run_id}",
            headers=_headers(CUSTOMER_B),
        )
        report = client.get(
            f"/local/style-suite/api/foreground-benchmarks/runs/{run_id}/report",
            headers=_headers(),
        )
        markdown = client.get(
            f"/local/style-suite/api/foreground-benchmarks/runs/{run_id}/report.md",
            headers=_headers(),
        )
        thumbnail = client.get(
            f"/local/style-suite/api/foreground-benchmarks/runs/{run_id}/samples/opaque-sample-1/thumbnail",
            headers=_headers(),
        )
        foreign_thumbnail = client.get(
            f"/local/style-suite/api/foreground-benchmarks/runs/{run_id}/samples/opaque-sample-1/thumbnail",
            headers=_headers(CUSTOMER_B),
        )

    assert validation.status_code == 200
    assert created.status_code == 201
    assert replay.status_code == 200
    assert replay.json()["id"] == run_id
    assert foreign.status_code == 404
    assert report.status_code == 200
    assert report.json()["status"] == "FAIL"
    assert report.json()["hardest_sample_ids"] == ["opaque-sample-1"]
    assert report.json()["category_breakdown"]["bracelet"]["auto_eligible"] == 1
    assert markdown.status_code == 200
    assert "不可发布" in markdown.text
    assert thumbnail.status_code == 200
    assert thumbnail.headers["cache-control"] == "no-store"
    assert foreign_thumbnail.status_code == 404
    combined = validation.text + created.text + report.text + markdown.text
    assert str(tmp_path) not in combined
    assert "source_content" not in combined


def test_bad_dataset_seal_fails_before_candidate_execution(tmp_path: Path) -> None:
    calls: list[str] = []
    with _client(tmp_path, enabled=True) as client:
        client.app.state.foreground_benchmark_mask_provider = lambda sample: (
            calls.append(sample.manifest.sample_id)
        )
        payload = _payload()
        payload["dataset_seal_sha256"] = "e" * 64
        response = client.post(
            "/local/style-suite/api/foreground-benchmarks/runs",
            headers=_headers(),
            json=payload,
        )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "foreground_benchmark_digest_mismatch"
    assert calls == []
