"""FastAPI application factory for the OpC customer style agent."""

import os
import stat
import sys
from pathlib import Path
from secrets import token_bytes
from uuid import UUID, uuid5

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session, sessionmaker

from opc_style.api import routes_classification_bridge
from opc_style.api.routes_assets import router as asset_router
from opc_style.api.routes_authenticity import router as authenticity_router
from opc_style.api.routes_exports import router as export_router
from opc_style.api.routes_foreground_benchmark_workbench import (
    router as foreground_benchmark_workbench_router,
)
from opc_style.api.routes_foreground_benchmarks import (
    router as foreground_benchmark_router,
)
from opc_style.api.routes_image_jobs import router as image_job_router
from opc_style.api.routes_image_reviews import router as image_review_router
from opc_style.api.routes_local_demo import router as local_demo_router
from opc_style.api.routes_local_style_suite import (
    build_real_fidelity_assessor,
    build_real_product_identity_analyzer,
    build_real_style_analyzer,
)
from opc_style.api.routes_local_style_suite import router as local_style_suite_router
from opc_style.api.routes_mall_style_handoff_v1 import (
    router as mall_style_handoff_router,
)
from opc_style.api.routes_missing_part_completion import (
    router as missing_part_completion_router,
)
from opc_style.api.routes_real_photos import router as real_photo_router
from opc_style.api.routes_samples import router as sample_router
from opc_style.api.routes_style_suites import router as style_suite_router
from opc_style.api.routes_style_warehouse_v3 import router as style_warehouse_v3_router
from opc_style.api.routes_styles import router as style_router
from opc_style.api.routes_subject_masks import router as subject_mask_router
from opc_style.api.routes_video_outputs import local_router as local_video_output_router
from opc_style.api.routes_video_outputs import router as video_output_router
from opc_style.config import Settings
from opc_style.db import create_database_engine
from opc_style.domain.analyzer import DeterministicPixelStyleAnalyzer
from opc_style.domain.product_foreground import ForegroundExtractionRequest
from opc_style.production_runtime import (
    DeferredProductionStore,
    build_production_store,
    readiness,
)
from opc_style.production_settings import ProductionSettings
from opc_style.providers.local_foreground_worker import LocalForegroundWorker
from opc_style.providers.macos_vision_foreground import MacOSVisionForegroundExtractor
from opc_style.providers.macos_vision_subjects import MacOSVisionSubjectSegmenter
from opc_style.providers.openai_backgrounds import OpenAIBackgroundGenerator
from opc_style.providers.openai_missing_part_completion import (
    OPENAI_MISSING_PART_MODEL_VERSION,
    OPENAI_MISSING_PART_QUALITY,
    OPENAI_MISSING_PART_SELECTION_REASON,
    OpenAIMissingPartCompleter,
)
from opc_style.services.foreground_benchmark_workbench import (
    PrivateBenchmarkDraftStore,
)
from opc_style.services.foreground_benchmarks import ValidatedBenchmarkSample
from opc_style.services.local_demo_sandbox import (
    LocalDemoSandbox,
    canonical_sqlite_database_path,
    validate_isolated_runtime_roots,
)
from opc_style.services.missing_part_calibration_review import (
    MissingPartCalibrationReviewStore,
)
from opc_style.services.missing_part_completion import (
    MissingPartCompletionService,
    PrivateMissingPartCandidateStore,
)
from opc_style.services.missing_part_learning import MissingPartLearningStore
from opc_style.services.missing_part_reference_assets import (
    MissingPartReferenceInboxStore,
)
from opc_style.services.real_photos import LocalSyntheticAssetStore
from opc_style.services.s3_asset_store import S3AssetStore
from opc_style.services.video_outputs import VideoOutputStore
from opc_style.settings import (
    ForegroundBenchmarkSettings,
    ForegroundBenchmarkWorkbenchSettings,
    ForegroundExtractorSettings,
    ForegroundPipelineSettings,
    LocalBridgeSettings,
    MissingPartCompletionSettings,
    RealBackgroundProviderSettings,
    RealFidelitySettings,
    RealImageProviderSettings,
    RealProductIdentitySettings,
    RealStyleAnalysisSettings,
    StyleSuitePlanningSettings,
    StyleWarehouseV3Settings,
    resolve_local_style_asset_root,
)


def build_product_foreground_extractor(
    pipeline_settings: ForegroundPipelineSettings,
    baseline_settings: ForegroundExtractorSettings,
) -> LocalForegroundWorker | MacOSVisionForegroundExtractor | None:
    """Build the selected local engine without silently downgrading an enabled pipeline."""
    if pipeline_settings.enabled:
        assert pipeline_settings.executable_path is not None
        assert pipeline_settings.model_root is not None
        assert pipeline_settings.manifest_path is not None
        assert pipeline_settings.pipeline_manifest_sha256 is not None
        worker = LocalForegroundWorker(
            executable_path=pipeline_settings.executable_path,
            model_root=pipeline_settings.model_root,
            manifest_path=pipeline_settings.manifest_path,
            expected_model_manifest_sha256=(pipeline_settings.pipeline_manifest_sha256),
            timeout_seconds=pipeline_settings.timeout_seconds,
            max_output_bytes=pipeline_settings.max_output_bytes,
        )
        worker.verify_installation()
        return worker
    if baseline_settings.enabled and baseline_settings.binary_path is not None:
        return MacOSVisionForegroundExtractor(
            binary_path=baseline_settings.binary_path,
            timeout_seconds=baseline_settings.timeout_seconds,
            max_output_bytes=baseline_settings.max_output_bytes,
            extractor_version=baseline_settings.extractor_version,
        )
    return None


def create_app() -> FastAPI:
    """Create and configure the style agent API application."""
    runtime_settings = Settings()
    production = runtime_settings.app_env == "production"
    production_settings = ProductionSettings()
    app = FastAPI(
        title="OpC Customer Style Agent",
        version="0.1.0",
        docs_url=None if production else "/docs",
        redoc_url=None if production else "/redoc",
        openapi_url=None if production else "/openapi.json",
    )
    app.state.identity_content_secret = token_bytes(32)
    bridge_settings = LocalBridgeSettings()
    app.state.local_bridge_settings = bridge_settings
    # Production exposes the signed Mall SaaS intake routes, not the old local
    # classification-media pull route. Do not block the whole API on credentials
    # for a route that is deliberately not mounted in production.
    style_warehouse_v3_settings = (
        StyleWarehouseV3Settings(verify_source_images=False)
        if production
        else StyleWarehouseV3Settings()
    )
    style_warehouse_v3_engine = None
    if style_warehouse_v3_settings.enabled:
        style_warehouse_v3_engine = create_database_engine(
            runtime_settings.database_url
        )
        app.state.style_warehouse_v3_settings = style_warehouse_v3_settings
        app.state.style_warehouse_v3_session_factory = sessionmaker(
            bind=style_warehouse_v3_engine,
            expire_on_commit=False,
            class_=Session,
        )
        app.state.mall_style_subject_segmenter = (
            MacOSVisionSubjectSegmenter() if sys.platform == "darwin" else None
        )
        if not production:
            app.include_router(style_warehouse_v3_router)
        app.include_router(mall_style_handoff_router)

        @app.on_event("shutdown")
        def close_style_warehouse_v3() -> None:
            assert style_warehouse_v3_engine is not None
            style_warehouse_v3_engine.dispose()

    local_workspace_enabled = runtime_settings.app_env in {
        "development",
        "local",
        "test",
    }
    local_demo_sandbox: LocalDemoSandbox | None = None
    missing_part_http_client: httpx.Client | None = None
    foreground_benchmark_workbench_settings = ForegroundBenchmarkWorkbenchSettings()
    app.state.foreground_benchmark_workbench_settings = (
        foreground_benchmark_workbench_settings
    )
    app.state.foreground_benchmark_workbench_store = None
    app.state.video_output_store = None
    if local_workspace_enabled:
        asset_root = resolve_local_style_asset_root(
            bridge_settings, Path(__file__).resolve().parents[2]
        )
        asset_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        asset_root.chmod(0o700)
        asset_root_stat = asset_root.lstat()
        if (
            stat.S_ISLNK(asset_root_stat.st_mode)
            or not stat.S_ISDIR(asset_root_stat.st_mode)
            or asset_root_stat.st_uid != os.getuid()
        ):
            raise RuntimeError("unsafe_local_style_asset_root")
        demo_database_path = (asset_root / "demo-sandbox" / "demo.sqlite3").resolve(
            strict=False
        )
        runtime_database_path = canonical_sqlite_database_path(
            runtime_settings.database_url
        )
        if runtime_database_path == demo_database_path:
            raise RuntimeError("local_demo_database_conflicts_with_runtime_database")
        runtime_media_roots = {
            "demo_media": asset_root / "demo-sandbox" / "media",
        }
        if bridge_settings.local_classification_bridge:
            bridge_asset_root = bridge_settings.local_classification_bridge_asset_root
            if bridge_asset_root is None:
                raise RuntimeError("local_classification_bridge_asset_root_required")
            runtime_media_roots.update(
                {
                    "bridge_assets": bridge_asset_root,
                    "bridge_stage": (
                        bridge_settings.local_classification_bridge_stage_root
                    ),
                }
            )
        else:
            runtime_media_roots["real_media"] = asset_root / "media"
        validate_isolated_runtime_roots(runtime_media_roots)
        app.state.local_style_asset_root = asset_root
        app.state.video_output_store = VideoOutputStore(asset_root / "video-outputs")
        missing_part_settings = MissingPartCompletionSettings()
        app.state.missing_part_completion_settings = missing_part_settings
        missing_part_learning_store = MissingPartLearningStore(
            asset_root / "missing-part-learning"
        )
        missing_part_candidate_store = PrivateMissingPartCandidateStore(
            asset_root / "missing-part-completion"
        )
        app.state.missing_part_learning_store = missing_part_learning_store
        app.state.missing_part_candidate_store = missing_part_candidate_store
        app.state.missing_part_calibration_review_store = (
            MissingPartCalibrationReviewStore(
                asset_root / "missing-part-calibration-review"
            )
        )
        app.state.missing_part_reference_inbox_store = MissingPartReferenceInboxStore(
            asset_root / "missing-part-reference-inbox"
        )
        missing_part_completer = None
        if missing_part_settings.enabled:
            assert missing_part_settings.api_key is not None
            missing_part_http_client = httpx.Client()
            missing_part_completer = OpenAIMissingPartCompleter(
                api_key=missing_part_settings.api_key.get_secret_value(),
                transport=missing_part_http_client,
                model=missing_part_settings.model,
                timeout_seconds=missing_part_settings.timeout_seconds,
            )
        app.state.missing_part_completion_service = MissingPartCompletionService(
            learning_store=missing_part_learning_store,
            candidate_store=missing_part_candidate_store,
            completer=missing_part_completer,
            model_version=OPENAI_MISSING_PART_MODEL_VERSION,
            maximum_usd=missing_part_settings.max_run_usd,
            quality=OPENAI_MISSING_PART_QUALITY,
            selection_reason=OPENAI_MISSING_PART_SELECTION_REASON,
        )
        local_demo_sandbox = LocalDemoSandbox(asset_root)
        app.state.local_demo_sandbox = local_demo_sandbox
        app.state.local_style_suite_preview_secret = token_bytes(32)
        app.state.style_visual_analyzer = DeterministicPixelStyleAnalyzer()
        real_style_settings = RealStyleAnalysisSettings()
        app.state.real_style_analysis_settings = real_style_settings
        app.state.real_style_visual_analyzer = build_real_style_analyzer(
            real_style_settings
        )
        real_fidelity_settings = RealFidelitySettings()
        app.state.real_fidelity_settings = real_fidelity_settings
        app.state.real_fidelity_assessor = build_real_fidelity_assessor(
            real_fidelity_settings
        )
        real_product_identity_settings = RealProductIdentitySettings()
        app.state.real_product_identity_settings = real_product_identity_settings
        app.state.real_product_identity_analyzer = build_real_product_identity_analyzer(
            real_product_identity_settings
        )
        foreground_extractor_settings = ForegroundExtractorSettings()
        app.state.foreground_extractor_settings = foreground_extractor_settings
        foreground_pipeline_settings = ForegroundPipelineSettings()
        app.state.foreground_pipeline_settings = foreground_pipeline_settings
        app.state.foreground_benchmark_settings = ForegroundBenchmarkSettings()
        if foreground_benchmark_workbench_settings.enabled:
            assert foreground_benchmark_workbench_settings.root is not None
            app.state.foreground_benchmark_workbench_store = PrivateBenchmarkDraftStore(
                foreground_benchmark_workbench_settings.root
            )
        product_foreground_extractor = build_product_foreground_extractor(
            foreground_pipeline_settings,
            foreground_extractor_settings,
        )
        app.state.product_foreground_extractor = product_foreground_extractor
        app.state.foreground_benchmark_model_manifest = None
        app.state.foreground_benchmark_mask_provider = None
        if isinstance(product_foreground_extractor, LocalForegroundWorker):
            app.state.foreground_benchmark_model_manifest = (
                product_foreground_extractor.installation_manifest()
            )

            def benchmark_mask_provider(sample: ValidatedBenchmarkSample) -> bytes:
                namespace = UUID(int=0)
                result = product_foreground_extractor.extract(
                    ForegroundExtractionRequest(
                        customer_id=namespace,
                        asset_id=uuid5(namespace, sample.manifest.sample_id),
                        product_id=sample.manifest.sample_id,
                        source_content=sample.source_content,
                        source_content_sha256=sample.manifest.source_sha256,
                        mime_type=sample.manifest.mime_type,
                        width=sample.manifest.width,
                        height=sample.manifest.height,
                    )
                )
                return result.mask_png

            app.state.foreground_benchmark_mask_provider = benchmark_mask_provider
        real_background_settings = RealBackgroundProviderSettings()
        app.state.real_background_settings = real_background_settings
        app.state.background_http_client = None
        app.state.background_generator = None
        if real_background_settings.enabled:
            background_http_client = httpx.Client()
            app.state.background_http_client = background_http_client
            app.state.background_generator = OpenAIBackgroundGenerator(
                real_background_settings, transport=background_http_client
            )
    style_suite_planning_settings = StyleSuitePlanningSettings()
    real_image_provider_settings = RealImageProviderSettings()
    app.state.style_suite_planning_settings = style_suite_planning_settings
    app.state.real_image_provider_settings = real_image_provider_settings
    if not production:
        app.include_router(authenticity_router)
        app.include_router(asset_router)
        app.include_router(export_router)
        # Register the one generic status route with the generation endpoints.
        app.include_router(image_job_router)
        app.include_router(image_review_router)
        app.include_router(subject_mask_router)
        app.include_router(foreground_benchmark_workbench_router)
        if local_workspace_enabled:
            app.include_router(local_demo_router)
            app.include_router(local_style_suite_router)
            app.include_router(foreground_benchmark_router)
            app.include_router(missing_part_completion_router)
        app.include_router(real_photo_router)
        app.include_router(sample_router)
        app.include_router(style_router)
        app.include_router(style_suite_router)
        app.include_router(video_output_router)
        if local_workspace_enabled:
            app.include_router(local_video_output_router)
    if not production and bridge_settings.local_classification_bridge:
        app.include_router(routes_classification_bridge.router)
        routes_classification_bridge.configure_classification_bridge(
            app, bridge_settings, database_url=runtime_settings.database_url
        )
        app.state.mall_style_asset_store = app.state.classification_bridge_store

        @app.on_event("shutdown")
        def close_local_classification_bridge() -> None:
            routes_classification_bridge.close_classification_bridge(app)
            background_http_client = app.state.background_http_client
            if background_http_client is not None:
                background_http_client.close()
            if local_demo_sandbox is not None:
                local_demo_sandbox.close()
    elif local_workspace_enabled:
        media_root = app.state.local_style_asset_root / "media"
        local_store = LocalSyntheticAssetStore(media_root, reopen=media_root.exists())
        app.state.classification_bridge_store = local_store
        app.state.mall_style_asset_store = local_store

        @app.on_event("shutdown")
        def close_local_style_suite_workspace() -> None:
            configured_store = app.state.classification_bridge_store
            if configured_store is not local_store:
                configured_store.close()
            local_store.close()
            background_http_client = app.state.background_http_client
            if background_http_client is not None:
                background_http_client.close()
            if local_demo_sandbox is not None:
                local_demo_sandbox.close()

    production_store = None
    if production and production_settings.asset_backend == "s3":

        def open_production_store() -> S3AssetStore:
            store = build_production_store(production_settings)
            if store is None:
                raise RuntimeError("private_storage_required")
            return store

        # Do not exchange cloud credentials while importing the ASGI app.
        # `/health` must bind first; `/ready` and business requests still fail
        # closed until the real private store can be opened and probed.
        production_store = DeferredProductionStore(open_production_store)
    if production_store is not None:
        app.state.mall_style_asset_store = production_store

        @app.on_event("shutdown")
        def close_production_storage() -> None:
            production_store.close()

    if missing_part_http_client is not None:

        @app.on_event("shutdown")
        def close_missing_part_provider() -> None:
            missing_part_http_client.close()

    @app.get("/health")
    def health() -> dict[str, str]:
        return {
            "status": "ok",
            "service": "opc-style-agent",
            "sourceSha": production_settings.source_sha,
        }

    @app.get("/ready")
    def ready() -> JSONResponse:
        if not production:
            return JSONResponse(
                {"status": "local_only", "sourceSha": production_settings.source_sha},
                status_code=503,
            )
        ok, body = readiness(
            style_warehouse_v3_engine,
            production_store,
            production_settings,
            integration_enabled=style_warehouse_v3_settings.enabled,
        )
        return JSONResponse(body, status_code=200 if ok else 503)

    return app


app = create_app()
