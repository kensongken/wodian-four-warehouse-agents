import hashlib
import io
import json
import math
import os
import sys
import threading
import time
import types
import unittest
from argparse import Namespace
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory

WORKER_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKER_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from protocol import ProtocolError  # noqa: E402
import vision  # noqa: E402
from vision import (  # noqa: E402
    MAX_OUTPUT_TOKENS,
    PROMPT_VERSION,
    analyze_vision,
)
from worker import _apply_scheduled_vision_restart  # noqa: E402
import worker  # noqa: E402
from test_runtime_integrity import runtime_fixture  # noqa: E402
from stage_telemetry import StageTelemetryWriter  # noqa: E402
from test_stage_telemetry import capture_fd_three  # noqa: E402


def source_job(source: Path, **overrides):
    job = {
        "schemaVersion": 1,
        "jobId": "job-vision-1",
        "customerId": "customer-a",
        "sessionId": "session-a",
        "sourceAssetId": "asset-a",
        "sourceDigest": hashlib.sha256(source.read_bytes()).hexdigest(),
        "sourcePath": str(source.resolve()),
        "sourceStartMs": 1_000,
        "sourceEndMs": 4_000,
        "layer": "vision",
        "operationKey": "vision-fixture-v1",
        "requestedAt": "2026-09-12T09:00:00.000Z",
    }
    job.update(overrides)
    return job


def ready_registry(directory: str):
    root = Path(directory) / "models"
    checkpoint_dir = root / "qwen"
    checkpoint_dir.mkdir(parents=True)
    runtime = root / "runtime"
    package = runtime / "mlx_vlm"
    distribution = runtime / "mlx_vlm-0.6.6.dist-info"
    package.mkdir(parents=True)
    distribution.mkdir()
    runtime_init = package / "__init__.py"
    runtime_impl = package / "runtime.py"
    runtime_structured = package / "structured.py"
    runtime_metadata = distribution / "METADATA"
    config = checkpoint_dir / "config.json"
    weights = checkpoint_dir / "model.safetensors"
    runtime_init.write_text(
        '__version__ = "0.6.6"\nfrom .runtime import generate, load\n',
        encoding="utf-8",
    )
    runtime_impl.write_text(
        """import json
class Processor:
    def apply_chat_template(self, messages, **kwargs):
        content = messages[0]["content"]
        image_tokens = "".join("<image>" for item in content if item["type"] == "image")
        text = next(item["text"] for item in content if item["type"] == "text")
        return image_tokens + text
def load(path): return object(), Processor()
def generate(model, processor, prompt, image, **kwargs):
    return json.dumps({
        "suggestedTags": ["synthetic"],
        "visualObservations": [{"kind": "visible-scene", "text": "synthetic", "frameId": "frame-1", "confidence": 0.8}],
        "demandEvidenceCandidates": [],
        "highlightCandidates": [],
        "forbiddenContentWarnings": [],
        "uncertainty": ["fixture only"],
        "confidence": 0.5,
    })
""",
        encoding="utf-8",
    )
    runtime_structured.write_text(
        """def build_json_schema_logits_processor(tokenizer, schema):
    return lambda input_ids, logits: logits
""",
        encoding="utf-8",
    )
    runtime_metadata.write_text("Name: mlx-vlm\nVersion: 0.6.6\n", encoding="utf-8")
    config.write_bytes(b"synthetic-config")
    weights.write_bytes(b"synthetic-qwen-weights")
    revision = "a" * 40
    source_root = "https://models.invalid/mlx-community/qwen"
    artifact_id = "synthetic-qwen3-vl"
    runtime_artifact_id = "synthetic-mlx-vlm"
    files = []
    for path in (config, weights):
        relative = path.relative_to(root).as_posix()
        files.append({
            "relativePath": relative,
            "sourceUrl": f"{source_root}/resolve/{revision}/{relative}",
            "required": True,
            "executable": False,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "byteLength": path.stat().st_size,
        })
    lock = Path(directory) / "models.lock.json"
    lock.write_text(json.dumps({
        "schemaVersion": 1,
        "artifacts": [{
            "id": runtime_artifact_id,
            "version": "0.6.6",
            "artifactKind": "source-package",
            "sources": [{
                "id": "code",
                "kind": "code",
                "sourceUrl": "https://code.invalid/mlx-vlm",
                "sourceRevision": revision,
                "inventoryVerified": True,
                "files": [{
                    "relativePath": path.relative_to(root).as_posix(),
                    "sourceUrl": (
                        f"https://code.invalid/mlx-vlm/resolve/{revision}/"
                        f"{path.relative_to(runtime).as_posix()}"
                    ),
                    "required": True,
                    "executable": False,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "byteLength": path.stat().st_size,
                } for path in (
                    runtime_init,
                    runtime_impl,
                    runtime_structured,
                    runtime_metadata,
                )],
            }],
            "licenseEvidence": {
                "code": {
                    "applies": True,
                    "sourceId": "code",
                    "sourceRevision": revision,
                    "spdxExpression": "MIT",
                    "evidenceUrl": f"https://code.invalid/mlx-vlm/raw/{revision}/LICENSE",
                },
                "weights": {
                    "applies": False,
                    "sourceId": None,
                    "sourceRevision": None,
                    "spdxExpression": None,
                    "evidenceUrl": None,
                },
            },
            "licenseReview": "approved",
            "runtimeBackend": "synthetic-mlx-vlm",
            "quantization": None,
            "enabled": True,
            "approved": True,
            "approvedBy": "fixture-reviewer",
            "approvedAt": "2026-09-12T09:00:00.000Z",
            "testedDevices": ["synthetic-fixture"],
            "testSetVersion": "fixture-v1",
        }, {
            "id": artifact_id,
            "version": "0.6.6-qwen3-vl-2b-instruct-4bit-fixture",
            "artifactKind": "checkpoint",
            "sources": [{
                "id": "weights",
                "kind": "weights",
                "sourceUrl": source_root,
                "sourceRevision": revision,
                "inventoryVerified": True,
                "files": files,
            }],
            "licenseEvidence": {
                "code": {
                    "applies": False,
                    "sourceId": None,
                    "sourceRevision": None,
                    "spdxExpression": None,
                    "evidenceUrl": None,
                },
                "weights": {
                    "applies": True,
                    "sourceId": "weights",
                    "sourceRevision": revision,
                    "spdxExpression": "MIT",
                    "evidenceUrl": f"{source_root}/raw/{revision}/LICENSE",
                },
            },
            "licenseReview": "approved",
            "runtimeBackend": "synthetic-mlx-vlm",
            "quantization": "4bit",
            "enabled": True,
            "approved": True,
            "approvedBy": "fixture-reviewer",
            "approvedAt": "2026-09-12T09:00:00.000Z",
            "testedDevices": ["synthetic-fixture"],
            "testSetVersion": "fixture-v1",
        }],
    }), encoding="utf-8")
    return {
        "model_lock_path": str(lock.resolve()),
        "model_root": str(root.resolve()),
        "runtime_artifact_id": runtime_artifact_id,
        "runtime_package_path": str(runtime.resolve()),
        "qwen_artifact_id": artifact_id,
        "checkpoint_path": str(weights.resolve()),
    }


def frame(job, marker=b"frame"):
    return {
        "sourceDigest": job["sourceDigest"],
        "startMs": 1_000,
        "endMs": 2_000,
        "representativeFrameMs": 1_500,
        "representativeFrameDigest": hashlib.sha256(marker).hexdigest(),
        "width": 2,
        "height": 2,
        "pixels": marker,
    }


def context(job, *, frames=None, transcripts=None, demand_text="需要戒指特写"):
    return {
        "sourceAssetId": job["sourceAssetId"],
        "sourceDigest": job["sourceDigest"],
        "sourceStartMs": job["sourceStartMs"],
        "sourceEndMs": job["sourceEndMs"],
        "frames": frames if frames is not None else [frame(job)],
        "transcriptSpans": transcripts if transcripts is not None else [{
            "sourceDigest": job["sourceDigest"],
            "startMs": 1_100,
            "endMs": 1_800,
            "text": "介绍设计灵感",
        }],
        "demand": None if demand_text is None else {
            "sourceDigest": job["sourceDigest"],
            "sourceStartMs": job["sourceStartMs"],
            "sourceEndMs": job["sourceEndMs"],
            "text": demand_text,
        },
    }


def valid_output(**overrides):
    value = {
        "suggestedTags": ["戒指特写"],
        "visualObservations": [{
            "kind": "visible-product",
            "text": "画面可能是珠宝特写",
            "frameId": "frame-1",
            "confidence": 0.8,
        }],
        "demandEvidenceCandidates": [{
            "demandText": "需要戒指特写",
            "observation": "画面可能接近所需特写",
            "frameId": "frame-1",
            "confidence": 0.7,
        }],
        "highlightCandidates": [{
            "reason": "主体更清楚",
            "frameId": "frame-1",
            "confidence": 0.8,
        }],
        "forbiddenContentWarnings": [],
        "uncertainty": ["材质未经人工确认"],
        "confidence": 0.75,
    }
    value.update(overrides)
    return value


class FakeModel:
    def __init__(self, output=None, *, delay=0):
        self.output = json.dumps(output if output is not None else valid_output(), ensure_ascii=False)
        self.delay = delay
        self.calls = []
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()

    def generate(self, **arguments):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            self.calls.append(arguments)
            if self.delay:
                time.sleep(self.delay)
            return self.output
        finally:
            with self.lock:
                self.active -= 1


class VisionAnalysisTest(unittest.TestCase):
    def test_keeps_general_and_pairwise_highlight_schemas_separate(self):
        general = vision.VISION_OUTPUT_JSON_SCHEMA["properties"]["highlightCandidates"]["items"]
        pairwise = vision.HIGHLIGHT_OUTPUT_JSON_SCHEMA["properties"]["highlightCandidates"]["items"]
        self.assertEqual(general["required"], ["reason", "frameId", "confidence"])
        self.assertNotIn("comparisonFrameId", general["properties"])
        self.assertEqual(pairwise["required"], [
            "preferredFrame", "subjectKind", "visibleAdvantage", "confidence",
        ])
        self.assertNotIn("frameId", pairwise["properties"])

    def test_allows_a_direct_spatial_from_left_to_right_description(self):
        vision._reject_highlight_temporal_claim({
            "subject": "两只机械臂",
            "action": "静态展示",
            "visualChange": "机械臂从左到右排列且间距更宽",
            "retentionValue": "机械臂轮廓更清楚",
        })

    def test_splits_one_long_scene_into_contiguous_local_evidence_windows(self):
        windows = vision._localized_scene_windows([{
            "startMs": 0,
            "endMs": 17_933,
            "representativeFrameMs": 8_966,
            "representativeFrameDigest": "a" * 64,
        }])

        self.assertEqual(len(windows), 5)
        self.assertEqual(windows[0]["startMs"], 0)
        self.assertEqual(windows[-1]["endMs"], 17_933)
        self.assertTrue(all(
            current["endMs"] == following["startMs"]
            for current, following in zip(windows, windows[1:])
        ))
        self.assertTrue(all(
            0 < window["endMs"] - window["startMs"] <= vision.MAX_FRAME_EVIDENCE_WINDOW_MS
            for window in windows
        ))
        self.assertTrue(all(window["representativeFrameDigest"] is None for window in windows))

    def test_splits_short_highlight_bucket_into_two_comparable_windows(self):
        windows = vision._localized_scene_windows([{
            "startMs": 0,
            "endMs": 4_000,
            "representativeFrameMs": 2_000,
            "representativeFrameDigest": "a" * 64,
        }], vision.HIGHLIGHT_FRAME_EVIDENCE_WINDOW_MS)

        self.assertEqual([(item["startMs"], item["endMs"]) for item in windows], [
            (0, 2_000), (2_000, 4_000),
        ])

    def test_ordinary_mode_preserves_the_declared_model_signature(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            source.write_bytes(b"synthetic-source")

            class StrictModel:
                def generate(
                    self,
                    *,
                    prompt,
                    frames,
                    max_tokens,
                    temperature,
                    timeout_seconds,
                ):
                    self.call = {
                        "prompt": prompt,
                        "frames": frames,
                        "max_tokens": max_tokens,
                        "temperature": temperature,
                        "timeout_seconds": timeout_seconds,
                    }
                    return json.dumps(valid_output(), ensure_ascii=False)

            model = StrictModel()
            receipt = self.call(directory, source, model)

        self.assertEqual(receipt["status"], "succeeded")
        self.assertEqual(model.call["frames"], [b"frame"])
        self.assertEqual(model.call["temperature"], 0)

    def test_marks_runtime_frontstage_load_prepare_generate_and_validation_boundaries(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            source.write_bytes(b"synthetic-source")
            order = []
            receipt_holder = []

            class TrackingWriter:
                def __init__(self, writer):
                    self.writer = writer

                def started(self, stage):
                    order.append(f"started:{stage}")
                    self.writer.started(stage)

                def completed(self, stage):
                    order.append(f"completed:{stage}")
                    self.writer.completed(stage)

            class TrackingLock:
                def acquire(self, *, timeout):
                    self.timeout = timeout
                    order.append("lock-acquire")
                    return True

                def release(self):
                    order.append("lock-release")

            class BoundaryModel(FakeModel):
                def generate(self, **arguments):
                    arguments["stage_boundary"]()
                    return super().generate(**arguments)

            def emit():
                writer = TrackingWriter(StageTelemetryWriter.from_fd(3))
                with patch.object(vision, "_MODEL_EXECUTION_LOCK", TrackingLock()):
                    receipt_holder.append(self.call(
                        directory,
                        source,
                        BoundaryModel(),
                        telemetry=writer,
                    ))

            payload = capture_fd_three(emit)

        events = [json.loads(line) for line in payload.splitlines()]
        self.assertEqual(receipt_holder[0]["status"], "succeeded")
        self.assertEqual(
            [(event["stage"], event["state"]) for event in events],
            [
                (stage, state)
                for stage in (
                    "runtime-preaccept",
                    "frontstage-extraction",
                    "model-load",
                    "processor-prepare",
                    "model-generation",
                    "output-validation",
                )
                for state in ("started", "completed")
            ],
        )
        self.assertLess(order.index("started:model-load"), order.index("lock-acquire"))
        self.assertLess(order.index("started:processor-prepare"), order.index("started:model-generation"))
        self.assertLess(order.index("started:output-validation"), order.index("lock-release"))

    def test_marks_active_stage_without_fabricating_completion_on_timeout(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            source.write_bytes(b"synthetic-source")
            receipt_holder = []

            class TimeoutModel(FakeModel):
                def generate(self, **arguments):
                    arguments["stage_boundary"]()
                    raise vision.VisionAnalysisError("studio-vision-model-timeout")

            def emit():
                receipt_holder.append(self.call(
                    directory,
                    source,
                    TimeoutModel(),
                    telemetry=StageTelemetryWriter.from_fd(3),
                ))

            payload = capture_fd_three(emit)

        events = [json.loads(line) for line in payload.splitlines()]
        self.assertEqual(receipt_holder[0]["status"], "failed-retryable")
        self.assertEqual(events[-1], {
            "schemaVersion": 1,
            "sequence": 9,
            "stage": "model-generation",
            "state": "started",
        })
        self.assertNotIn(
            ("model-generation", "completed"),
            [(event["stage"], event["state"]) for event in events],
        )

    def test_telemetry_does_not_change_receipt_or_original_frame_bytes(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            source.write_bytes(b"synthetic-source")
            registry = ready_registry(directory)
            job = source_job(source)
            pixels = bytearray(b"frame")
            raw_context = context(job, frames=[frame(job, pixels)])

            class BoundaryModel(FakeModel):
                def generate(self, **arguments):
                    boundary = arguments.get("stage_boundary")
                    if boundary is not None:
                        boundary()
                    return super().generate(**arguments)

            def run(model, telemetry):
                vision._MODEL_CACHE.clear()
                return analyze_vision(
                    job,
                    **registry,
                    context_provider=lambda _job: raw_context,
                    model_loader=lambda _path, _runtime, _validate: model,
                    telemetry=telemetry,
                )

            with (
                patch.object(vision, "_timestamp", return_value="2026-09-29T00:00:00.000Z"),
                patch.object(vision.time, "monotonic_ns", return_value=1_000_000),
            ):
                baseline = run(BoundaryModel(), StageTelemetryWriter.from_fd(None))
                receipt_holder = []

                def emit():
                    receipt_holder.append(run(
                        BoundaryModel(),
                        StageTelemetryWriter.from_fd(3),
                    ))

                capture_fd_three(emit)

        self.assertEqual(receipt_holder[0], baseline)
        self.assertEqual(bytes(pixels), b"frame")

    def test_mlx_adapter_formats_one_multimodal_image_item_per_frame(self):
        class Processor:
            def __init__(self):
                self.calls = []

            def apply_chat_template(self, messages, **kwargs):
                self.calls.append((messages, kwargs))
                return "formatted-multimodal-prompt"

        processor = Processor()
        generate_calls = []

        def generate(_model, _processor, prompt, image, **kwargs):
            generate_calls.append((prompt, image, kwargs))
            return "model-json"

        adapter = vision._MlxVisionModel(object(), processor, generate)
        adapter.structured_output_factory = lambda _tokenizer, _schema: (
            lambda _input_ids, logits: logits
        )
        frames = [b"frame-a", b"frame-b"]

        result = adapter.generate(
            prompt="raw-studio-prompt",
            frames=frames,
            max_tokens=512,
            temperature=0,
        )

        self.assertEqual(result, "model-json")
        self.assertEqual(processor.calls, [([{
            "role": "user",
            "content": [
                {"type": "image"},
                {"type": "image"},
                {"type": "text", "text": "raw-studio-prompt"},
            ],
        }], {
            "tokenize": False,
            "add_generation_prompt": True,
        })])
        self.assertEqual(generate_calls[0][0:2], (
            "formatted-multimodal-prompt",
            frames,
        ))
        self.assertEqual(
            {key: value for key, value in generate_calls[0][2].items() if key != "logits_processors"},
            {"max_tokens": 512, "temperature": 0, "verbose": False},
        )

    def test_mlx_adapter_passes_chw_copies_without_mutating_hwc_frame_evidence(self):
        import numpy as np

        class Processor:
            def apply_chat_template(self, _messages, **_kwargs):
                return "formatted-multimodal-prompt"

        frame = np.arange(24, dtype=np.uint8).reshape(2, 4, 3)
        original = frame.copy()
        original_digest = hashlib.sha256(frame.tobytes()).hexdigest()
        generated_images = []

        def generate(_model, _processor, _prompt, images, **_kwargs):
            generated_images.extend(images)
            return "model-json"

        adapter = vision._MlxVisionModel(object(), Processor(), generate)
        adapter.structured_output_factory = lambda _tokenizer, _schema: (
            lambda _input_ids, logits: logits
        )
        result = adapter.generate(
            prompt="raw-studio-prompt",
            frames=[frame],
            max_tokens=512,
            temperature=0,
        )

        self.assertEqual(result, "model-json")
        self.assertEqual(len(generated_images), 1)
        self.assertEqual(generated_images[0].shape, (3, 2, 4))
        np.testing.assert_array_equal(generated_images[0], original.transpose(2, 0, 1))
        np.testing.assert_array_equal(frame, original)
        self.assertEqual(frame.shape, (2, 4, 3))
        self.assertEqual(hashlib.sha256(frame.tobytes()).hexdigest(), original_digest)

    def test_mlx_adapter_returns_generation_result_text_only(self):
        class Processor:
            def apply_chat_template(self, _messages, **_kwargs):
                return "formatted-multimodal-prompt"

        result_object = types.SimpleNamespace(text='{"suggestedTags":[]}')
        adapter = vision._MlxVisionModel(
            object(),
            Processor(),
            lambda *_args, **_kwargs: result_object,
        )
        adapter.structured_output_factory = lambda _tokenizer, _schema: (
            lambda _input_ids, logits: logits
        )

        result = adapter.generate(
            prompt="raw-studio-prompt",
            frames=[b"frame"],
            max_tokens=512,
            temperature=0,
        )

        self.assertEqual(result, '{"suggestedTags":[]}')

    def test_mlx_adapter_constrains_each_generation_with_a_fresh_json_schema_processor(self):
        class Processor:
            tokenizer = object()

            def apply_chat_template(self, _messages, **_kwargs):
                return "formatted-multimodal-prompt"

        built_processors = []
        generated_kwargs = []

        def structured_output_factory(tokenizer, schema):
            processor = lambda _input_ids, logits: logits
            built_processors.append((tokenizer, schema, processor))
            return processor

        def generate(_model, _processor, _prompt, _images, **kwargs):
            generated_kwargs.append(kwargs)
            return "model-json"

        processor = Processor()
        adapter = vision._MlxVisionModel(object(), processor, generate)
        adapter.structured_output_factory = structured_output_factory

        for _ in range(2):
            adapter.generate(
                prompt="raw-studio-prompt",
                frames=[b"frame"],
                max_tokens=512,
                temperature=0,
            )

        self.assertEqual(len(built_processors), 2)
        self.assertTrue(all(call[0] is processor.tokenizer for call in built_processors))
        self.assertEqual(
            built_processors[0][1]["required"],
            [
                "suggestedTags",
                "visualObservations",
                "demandEvidenceCandidates",
                "highlightCandidates",
                "forbiddenContentWarnings",
                "uncertainty",
                "confidence",
            ],
        )
        self.assertFalse(built_processors[0][1]["additionalProperties"])
        self.assertEqual(
            built_processors[0][1]["properties"]["visualObservations"]["items"]
            ["properties"]["kind"]["enum"],
            [
                "visible-person",
                "visible-product",
                "visible-action",
                "visible-scene",
            ],
        )
        for field in ("visualObservations", "demandEvidenceCandidates", "highlightCandidates"):
            item_schema = built_processors[0][1]["properties"][field]["items"]
            self.assertIn("frameId", item_schema["required"])
            self.assertNotIn("startMs", item_schema["properties"])
            self.assertNotIn("endMs", item_schema["properties"])
        self.assertEqual(
            [kwargs.get("logits_processors") for kwargs in generated_kwargs],
            [[built_processors[0][2]], [built_processors[1][2]]],
        )
        self.assertIsNot(built_processors[0][2], built_processors[1][2])

    def test_mlx_adapter_fails_closed_when_structured_output_processor_is_unavailable(self):
        class Processor:
            tokenizer = object()

            def apply_chat_template(self, _messages, **_kwargs):
                return "formatted-multimodal-prompt"

        def unavailable_factory(_tokenizer, _schema):
            raise RuntimeError("synthetic structured-output setup failure")

        factories = (
            None,
            lambda _tokenizer, _schema: object(),
            unavailable_factory,
        )

        for factory in factories:
            with self.subTest(factory=factory):
                generate_calls = []

                def generate(*_args, **_kwargs):
                    generate_calls.append(True)
                    return "model-json"

                adapter = vision._MlxVisionModel(object(), Processor(), generate)
                adapter.structured_output_factory = factory

                with self.assertRaisesRegex(
                    vision.VisionAnalysisError,
                    "^studio-vision-structured-output-unavailable$",
                ):
                    adapter.generate(
                        prompt="raw-studio-prompt",
                        frames=[b"frame"],
                        max_tokens=512,
                        temperature=0,
                    )

                self.assertEqual(generate_calls, [])

    def test_default_context_converts_opencv_bgr_pixels_to_rgb_after_digest_check(self):
        class Pixels:
            shape = (1, 1, 3)

            def __init__(self, payload):
                self.payload = payload

            def tobytes(self):
                return self.payload

        original = Pixels(b"\x01\x02\x03")
        converted = Pixels(b"\x03\x02\x01")
        capture_calls = []

        class Capture:
            def isOpened(self):
                return True

            def set(self, property_id, value):
                capture_calls.append((property_id, value))

            def read(self):
                return True, original

            def release(self):
                capture_calls.append("released")

        color_calls = []
        fake_cv2 = types.SimpleNamespace(
            CAP_PROP_POS_MSEC=7,
            COLOR_BGR2RGB=42,
            INTER_AREA=3,
            VideoCapture=lambda _path: Capture(),
            resize=lambda pixels, _size, interpolation: pixels,
            cvtColor=lambda pixels, code: (
                color_calls.append((pixels, code)) or converted
            ),
        )
        job = {
            "sourcePath": "/synthetic/input.mp4",
            "sourceAssetId": "asset-a",
            "sourceDigest": "a" * 64,
            "sourceStartMs": 1_000,
            "sourceEndMs": 4_000,
        }
        scenes = [{
            "startMs": 1_000,
            "endMs": 2_000,
            "representativeFrameMs": 1_500,
            "representativeFrameDigest": hashlib.sha256(original.tobytes()).hexdigest(),
        }]

        with (
            patch.dict(sys.modules, {"cv2": fake_cv2}),
            patch.object(vision, "_scene_intervals", return_value=scenes),
        ):
            resolved = vision._default_context(job)

        self.assertEqual(color_calls, [(original, fake_cv2.COLOR_BGR2RGB)])
        self.assertIs(resolved["frames"][0]["pixels"], converted)
        self.assertEqual(
            resolved["frames"][0]["representativeFrameDigest"],
            hashlib.sha256(converted.tobytes()).hexdigest(),
        )
        self.assertEqual(capture_calls, [(fake_cv2.CAP_PROP_POS_MSEC, 1_500), "released"])

    def test_worker_builds_runtime_expectation_from_formal_runtime_flags(self):
        with TemporaryDirectory() as directory:
            fixture = runtime_fixture(Path(directory))
            arguments = Namespace(
                model_lock_path=str(fixture.lock_path.resolve()),
                model_root=str(fixture.model_root.resolve()),
                worker_runtime_root=str(fixture.runtime_root.resolve()),
                worker_runtime_artifact_id=fixture.runtime_id,
                worker_runtime_tree_sha256=fixture.tree_sha256,
            )

            self.assertTrue(hasattr(worker, "_runtime_expectation"))
            expectation = worker._runtime_expectation(arguments)

        self.assertEqual(expectation.artifact_id, fixture.runtime_id)
        self.assertEqual(expectation.runtime_root, str(fixture.runtime_root.resolve()))
        self.assertEqual(expectation.tree_sha256, fixture.tree_sha256)

    def test_partial_formal_runtime_flags_never_fall_back_to_legacy_runtime(self):
        arguments = Namespace(
            worker_runtime_root="/formal/runtime",
            worker_runtime_artifact_id=None,
            worker_runtime_tree_sha256=None,
            mlx_vlm_package_path="/legacy/runtime",
            mlx_vlm_artifact_id="legacy-runtime",
        )

        self.assertTrue(hasattr(worker, "_vision_runtime_mode"))
        self.assertIsNone(worker._vision_runtime_mode(arguments))

    def test_formal_runtime_tree_digest_is_the_adapter_receipt_binding(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            source.write_bytes(b"synthetic-source")
            fixture = runtime_fixture(root)
            job = source_job(source)

            receipt = analyze_vision(
                job,
                model_lock_path=str(fixture.lock_path.resolve()),
                model_root=str(fixture.model_root.resolve()),
                runtime_expectation=fixture.expectation(),
                qwen_artifact_id="synthetic-qwen3-vl",
                checkpoint_path=str(fixture.model_path.resolve()),
                context_provider=lambda _job: context(job),
                model_loader=lambda _path, _runtime, _validate: FakeModel(),
            )

        self.assertEqual(receipt["status"], "succeeded")
        self.assertEqual(receipt["artifactBinding"]["adapter"], {
            "id": fixture.runtime_id,
            "version": fixture.runtime_version,
            "sha256": fixture.tree_sha256,
        })

    def test_formal_runtime_and_checkpoint_are_revalidated_after_generate(self):
        for mutation in ("runtime", "checkpoint"):
            with self.subTest(mutation=mutation), TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "source.mp4"
                source.write_bytes(b"synthetic-source")
                fixture = runtime_fixture(root)
                job = source_job(source)
                target = (
                    fixture.runtime_root / "site-packages/mlx/__init__.py"
                    if mutation == "runtime"
                    else fixture.model_path
                )

                class MutatingModel(FakeModel):
                    def generate(self, **arguments):
                        output = super().generate(**arguments)
                        target.write_bytes(b"mutated-during-generate")
                        return output

                receipt = analyze_vision(
                    job,
                    model_lock_path=str(fixture.lock_path.resolve()),
                    model_root=str(fixture.model_root.resolve()),
                    runtime_expectation=fixture.expectation(),
                    qwen_artifact_id="synthetic-qwen3-vl",
                    checkpoint_path=str(fixture.model_path.resolve()),
                    context_provider=lambda _job: context(job),
                    model_loader=lambda _path, _runtime, _validate: MutatingModel(),
                )

            self.assertEqual(receipt["status"], "failed-manual-review")
            self.assertEqual(receipt["failure"], {"code": (
                "studio-runtime-tree-mismatch"
                if mutation == "runtime"
                else "studio-model-artifact-digest-mismatch"
            )})

    def test_worker_rejects_failed_formal_preaccept_without_calling_vision(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            source.write_bytes(b"synthetic-source")
            fixture = runtime_fixture(root)
            job = source_job(source)
            arguments = Namespace(
                ffprobe_path=None,
                ffmpeg_path=None,
                whisper_path=None,
                whisper_model_path=None,
                model_lock_path=str(fixture.lock_path.resolve()),
                model_root=str(fixture.model_root.resolve()),
                whisper_binary_artifact_id=None,
                whisper_model_artifact_id=None,
                openclip_checkpoint_path=None,
                openclip_artifact_id=None,
                qwen_checkpoint_path=str(fixture.model_path.resolve()),
                qwen_artifact_id="synthetic-qwen3-vl",
                mlx_vlm_package_path=None,
                mlx_vlm_artifact_id=None,
                worker_runtime_root=str(fixture.runtime_root.resolve()),
                worker_runtime_artifact_id=fixture.runtime_id,
                worker_runtime_tree_sha256=fixture.tree_sha256,
            )
            output = io.StringIO()
            calls = []

            with (
                patch.object(worker, "_arguments", return_value=arguments),
                patch.object(worker, "validate_runtime_process", side_effect=(
                    worker.RuntimeIntegrityError("studio-runtime-python-mismatch")
                )),
                patch.object(worker, "analyze_vision", side_effect=lambda *_args, **_kwargs: (
                    calls.append("analyze") or {
                        "status": "succeeded",
                        "failure": None,
                        "qualitySignals": {},
                    }
                )),
                patch.object(sys, "stdin", io.StringIO(json.dumps(job) + "\n")),
                patch.object(sys, "stdout", output),
            ):
                exit_code = worker.main()

            receipt = json.loads(output.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertEqual(calls, [])
        self.assertEqual(receipt["failure"], {"code": "studio-runtime-python-mismatch"})

    def test_trusted_environment_context_is_exactly_bound_and_strips_server_ids(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            source.write_bytes(b"synthetic source")
            job = source_job(source)
            envelope = {
                "customerId": job["customerId"],
                "sessionId": job["sessionId"],
                "sourceAssetId": job["sourceAssetId"],
                "sourceDigest": job["sourceDigest"],
                "sourceStartMs": job["sourceStartMs"],
                "sourceEndMs": job["sourceEndMs"],
                "transcriptSpans": [{
                    "sourceDigest": job["sourceDigest"],
                    "startMs": 1_100,
                    "endMs": 1_800,
                    "text": "介绍设计灵感",
                }],
                "demand": {
                    "demandId": "demand-a",
                    "matchAssessmentId": "match-a",
                    "sourceDigest": job["sourceDigest"],
                    "sourceStartMs": job["sourceStartMs"],
                    "sourceEndMs": job["sourceEndMs"],
                    "text": "需要戒指特写",
                },
            }
            frames_only = context(job, transcripts=[], demand_text=None)
            with patch.object(vision, "_default_context", return_value=frames_only):
                with patch.dict(os.environ, {
                    "STUDIO_VISION_CONTEXT_JSON": json.dumps(envelope, ensure_ascii=False),
                }):
                    resolved = vision.trusted_context_provider(job)
            self.assertEqual(resolved["transcriptSpans"], envelope["transcriptSpans"])
            self.assertEqual(resolved["demand"], {
                key: envelope["demand"][key]
                for key in ("sourceDigest", "sourceStartMs", "sourceEndMs", "text")
            })
            self.assertNotIn("demandId", resolved["demand"])

            for field, value in (
                ("customerId", "customer-b"),
                ("sourceAssetId", "asset-b"),
                ("sourceStartMs", 1_001),
            ):
                forged = {**envelope, field: value}
                with patch.object(vision, "_default_context", return_value=frames_only):
                    with patch.dict(os.environ, {
                        "STUDIO_VISION_CONTEXT_JSON": json.dumps(forged, ensure_ascii=False),
                    }):
                        with self.assertRaisesRegex(
                            vision.VisionAnalysisError,
                            "^studio-vision-context-binding-mismatch$",
                        ):
                            vision.trusted_context_provider(job)

    def call(self, directory, source, model, **overrides):
        registry = ready_registry(directory)
        job = source_job(source, **overrides.pop("job_overrides", {}))
        provider = overrides.pop("context_provider", lambda _job: context(job))
        return analyze_vision(
            job,
            **registry,
            context_provider=provider,
            model_loader=lambda _path, _runtime, _validate: model,
            **overrides,
        )

    def test_enforces_prompt_and_inference_resource_contract(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            source.write_bytes(b"synthetic-source")
            source_digest = hashlib.sha256(source.read_bytes()).hexdigest()
            model = FakeModel()
            receipt = self.call(directory, source, model)

        self.assertEqual(receipt["status"], "succeeded")
        self.assertTrue(receipt["synthetic"])
        self.assertEqual(receipt["promptVersion"], "studio-qwen3-vl-zh-json-v5")
        self.assertEqual(PROMPT_VERSION, "studio-qwen3-vl-zh-json-v5")
        self.assertEqual(receipt["inputBinding"], {
            "sourceDigest": receipt["sourceDigest"],
            "sourceStartMs": 1_000,
            "sourceEndMs": 4_000,
        })
        self.assertEqual(receipt["runtimeMetrics"], {"runtimeMs": receipt["runtimeMs"]})
        self.assertEqual(
            receipt["qualitySignals"]["inference"]["structuredOutputSchemaVersion"],
            "studio-vision-json-schema-v2",
        )
        self.assertEqual(receipt["qualitySignals"]["inference"]["timeoutSeconds"], 300)
        self.assertEqual(receipt["artifactBinding"]["adapter"]["id"], "synthetic-mlx-vlm")
        self.assertEqual(receipt["artifactBinding"]["model"]["id"], "synthetic-qwen3-vl")
        self.assertNotIn("speechState", receipt)
        self.assertEqual(len(model.calls), 1)
        call = model.calls[0]
        self.assertEqual(call["max_tokens"], MAX_OUTPUT_TOKENS)
        self.assertEqual(call["temperature"], 0)
        self.assertEqual(call["timeout_seconds"], 300)
        self.assertIn("仅返回严格 JSON", call["prompt"])
        self.assertIn(source_digest, call["prompt"])
        self.assertNotIn(str(source), call["prompt"])

    def test_preserves_only_allowlisted_visual_evidence_kinds(self):
        for kind in (
            "visible-person",
            "visible-product",
            "visible-action",
            "visible-scene",
        ):
            with self.subTest(kind=kind), TemporaryDirectory() as directory:
                source = Path(directory) / "source.mp4"
                source.write_bytes(b"synthetic-source")
                receipt = self.call(
                    directory,
                    source,
                    FakeModel(valid_output(visualObservations=[{
                        "kind": kind,
                        "text": "可观察的画面内容",
                        "frameId": "frame-1",
                        "confidence": 0.8,
                    }])),
                )

            self.assertEqual(receipt["status"], "succeeded")
            self.assertEqual(
                receipt["qualitySignals"]["visualObservations"][0]["kind"],
                kind,
            )

    def test_binds_explicit_highlight_candidates_to_representative_windows(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            source.write_bytes(b"synthetic-source")
            receipt = self.call(
                directory,
                source,
                FakeModel(valid_output(highlightCandidates=[{
                    "reason": "色彩更丰富且主体更清楚",
                    "frameId": "frame-1",
                    "confidence": 0.86,
                }])),
            )
        self.assertEqual(receipt["status"], "succeeded")
        self.assertEqual(receipt["qualitySignals"]["highlightCandidates"], [{
            "reason": "色彩更丰富且主体更清楚",
            "startMs": 1_000,
            "endMs": 2_000,
            "confidence": 0.86,
            "status": "pending-human-confirmation",
        }])

    def test_deduplicates_repeated_highlight_windows_without_discarding_other_evidence(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            source.write_bytes(b"synthetic-source")
            repeated = {
                "reason": "色彩更丰富且主体更清楚",
                "frameId": "frame-1",
                "confidence": 0.86,
            }
            receipt = self.call(
                directory,
                source,
                FakeModel(valid_output(highlightCandidates=[repeated, repeated])),
            )

        self.assertEqual(receipt["status"], "succeeded")
        self.assertEqual(len(receipt["qualitySignals"]["highlightCandidates"]), 1)
        self.assertIn("duplicate-highlight-candidates-deduplicated", receipt["warnings"])

    def test_highlight_only_mode_returns_frame_bound_candidates_with_small_output_budget(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            source.write_bytes(b"synthetic-source")
            model = FakeModel({"highlightCandidates": [{
                "preferredFrame": "first",
                "subjectKind": "aircraft",
                "visibleAdvantage": "sharper-outline",
                "confidence": 0.88,
            }]})
            receipt = self.call(
                directory,
                source,
                model,
                job_overrides={"operationKey": "editorial-highlight-v1-fixture"},
                context_provider=lambda job: context(job, frames=[
                    frame(job),
                    {
                        **frame(job, b"frame-2"),
                        "startMs": 2_000,
                        "endMs": 3_000,
                        "representativeFrameMs": 2_500,
                    },
                ]),
            )

        self.assertEqual(receipt["status"], "succeeded")
        self.assertEqual(receipt["promptVersion"], "studio-qwen3-vl-highlight-pair-json-v19")
        self.assertEqual(receipt["observations"], [])
        self.assertEqual(receipt["suggestedTags"], [])
        self.assertEqual(receipt["qualitySignals"]["inference"]["maxOutputTokens"], 384)
        self.assertEqual(
            receipt["qualitySignals"]["inference"]["structuredOutputSchemaVersion"],
            "studio-highlight-pair-json-schema-v8",
        )
        self.assertEqual(receipt["qualitySignals"]["highlightCandidates"][0], {
            "subject": "飞机",
            "action": "画面中可见飞机",
            "visualChange": "所选画面的主体轮廓更清楚",
            "retentionValue": "主体边缘更便于人工核对",
            "subjectKind": "aircraft",
            "visibleAdvantage": "sharper-outline",
            "modelPreferredFrame": "first",
            "reason": "主体：飞机；动作：画面中可见飞机；变化：所选画面的主体轮廓更清楚；保留：主体边缘更便于人工核对",
            "startMs": 1_000,
            "endMs": 2_000,
            "comparisonStartMs": 2_000,
            "comparisonEndMs": 3_000,
            "confidence": 0.88,
            "status": "pending-human-confirmation",
        })
        self.assertNotIn("transcriptSpans", model.calls[0]["prompt"])
        self.assertNotIn("demand", model.calls[0]["prompt"])

    def test_highlight_only_mode_accepts_one_exact_json_code_fence_but_not_prose(self):
        candidate = {
            "highlightCandidates": [{
                "preferredFrame": "first",
                "subjectKind": "bead-strands",
                "visibleAdvantage": "subject-larger",
                "confidence": 0.86,
            }],
        }
        for label, output, expected_status in (
            ("fence", "```json\n" + json.dumps(candidate, ensure_ascii=False) + "\n```", "succeeded"),
            ("prose", "建议如下：" + json.dumps(candidate, ensure_ascii=False), "failed-manual-review"),
            ("truncated", json.dumps(candidate, ensure_ascii=False)[:-1], "failed-manual-review"),
        ):
            with self.subTest(label=label), TemporaryDirectory() as directory:
                source = Path(directory) / "source.mp4"
                source.write_bytes(b"synthetic-source")
                model = FakeModel()
                model.output = output
                receipt = self.call(
                    directory,
                    source,
                    model,
                    job_overrides={"operationKey": "editorial-highlight-v16-format-" + label},
                    context_provider=lambda job: context(job, frames=[
                        frame(job),
                        {**frame(job, b"frame-2"), "startMs": 2_000, "endMs": 3_000,
                         "representativeFrameMs": 2_500},
                    ]),
                )
            self.assertEqual(receipt["status"], expected_status)
            if expected_status != "succeeded":
                self.assertEqual(receipt["failure"]["code"], "studio-vision-output-invalid-json")

    def test_highlight_only_mode_rejects_more_than_one_candidate(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            source.write_bytes(b"synthetic-source")
            repeated = {
                "preferredFrame": "first",
                "subjectKind": "aircraft",
                "visibleAdvantage": "sharper-outline",
                "confidence": 0.88,
            }
            receipt = self.call(
                directory,
                source,
                FakeModel({"highlightCandidates": [repeated, repeated]}),
                job_overrides={"operationKey": "editorial-highlight-v1-deduplicate"},
                context_provider=lambda job: context(job, frames=[
                    frame(job),
                    {**frame(job, b"frame-2"), "startMs": 2_000, "endMs": 3_000,
                     "representativeFrameMs": 2_500},
                ]),
            )

        self.assertEqual(receipt["status"], "failed-manual-review")
        self.assertEqual(
            receipt["failure"]["code"],
            "studio-vision-output-schema-highlight-candidate-shape",
        )

    def test_highlight_only_mode_binds_a_model_preferred_second_frame(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            source.write_bytes(b"synthetic-source")
            receipt = self.call(
                directory,
                source,
                FakeModel({"highlightCandidates": [{
                    "preferredFrame": "second",
                    "subjectKind": "bead-strands",
                    "visibleAdvantage": "subject-larger",
                    "confidence": 0.86,
                }]}),
                job_overrides={"operationKey": "editorial-highlight-v17-prefer-second"},
                context_provider=lambda job: context(job, frames=[
                    frame(job),
                    {**frame(job, b"frame-2"), "startMs": 2_000, "endMs": 3_000,
                     "representativeFrameMs": 2_500},
                ]),
            )
        self.assertEqual(receipt["status"], "succeeded")
        candidate = receipt["qualitySignals"]["highlightCandidates"][0]
        self.assertEqual((candidate["startMs"], candidate["endMs"]), (2_000, 3_000))
        pair = receipt["qualitySignals"]["highlightPair"]
        self.assertEqual(pair["modelPreferredFrame"], "second")
        self.assertEqual((pair["candidateStartMs"], pair["comparisonStartMs"]), (2_000, 1_000))

    def test_highlight_only_mode_rejects_generic_explanations(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            source.write_bytes(b"synthetic-source")
            receipt = self.call(
                directory,
                source,
                FakeModel({"highlightCandidates": [{
                    "preferredFrame": "first",
                    "subject": "标题文字",
                    "visibleState": "静态展示",
                    "visibleDifference": "画面内容",
                    "retentionValue": "精彩",
                    "confidence": 0.88,
                }]}),
                job_overrides={"operationKey": "editorial-highlight-v9-generic"},
                context_provider=lambda job: context(job, frames=[
                    frame(job),
                    {**frame(job, b"frame-2"), "startMs": 2_000, "endMs": 3_000,
                     "representativeFrameMs": 2_500},
                ]),
            )

        self.assertEqual(receipt["status"], "failed-manual-review")
        self.assertEqual(
            receipt["failure"]["code"],
            "studio-vision-output-schema-highlight-candidate-shape",
        )
        self.assertNotIn("highlightCandidates", receipt["qualitySignals"])

    def test_highlight_only_mode_rejects_overlay_text_as_the_subject(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            source.write_bytes(b"synthetic-source")
            receipt = self.call(
                directory,
                source,
                FakeModel({"highlightCandidates": [{
                    "preferredFrame": "first",
                    "subject": "岩石左下方的字幕",
                    "visibleState": "字幕显示一行白色文字",
                    "visibleDifference": "第一张的字幕内容更短",
                    "retentionValue": "白色字幕边缘更清楚",
                    "confidence": 0.88,
                }]}),
                job_overrides={"operationKey": "editorial-highlight-v13-overlay-text"},
                context_provider=lambda job: context(job, frames=[
                    frame(job),
                    {**frame(job, b"frame-2"), "startMs": 2_000, "endMs": 3_000,
                     "representativeFrameMs": 2_500},
                ]),
            )

        self.assertEqual(receipt["status"], "failed-manual-review")
        self.assertEqual(receipt["failure"]["code"], "studio-vision-output-schema-highlight-candidate-shape")
        self.assertNotIn("highlightCandidates", receipt["qualitySignals"])

    def test_highlight_only_mode_rejects_reason_that_favors_the_comparison_frame(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            source.write_bytes(b"synthetic-source")
            receipt = self.call(
                directory,
                source,
                FakeModel({"highlightCandidates": [{
                    "preferredFrame": "first",
                    "subject": "画面中央的灰黑色原石",
                    "visibleState": "第一张有红色斑块，第二张更均匀",
                    "visibleDifference": "第一张边缘碎裂，第二张表面平滑",
                    "retentionValue": "第二张的红色区域更完整",
                    "confidence": 0.8,
                }]}),
                job_overrides={"operationKey": "editorial-highlight-v15-direction"},
                context_provider=lambda job: context(job, frames=[
                    frame(job),
                    {**frame(job, b"frame-2"), "startMs": 2_000, "endMs": 3_000,
                     "representativeFrameMs": 2_500},
                ]),
            )

        self.assertEqual(receipt["status"], "failed-manual-review")
        self.assertEqual(
            receipt["failure"]["code"],
            "studio-vision-output-schema-highlight-candidate-shape",
        )
        self.assertNotIn("highlightCandidates", receipt["qualitySignals"])

    def test_highlight_only_mode_rejects_a_temporal_story_in_static_frames(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            source.write_bytes(b"synthetic-source")
            receipt = self.call(
                directory,
                source,
                FakeModel({"highlightCandidates": [{
                    "preferredFrame": "first",
                    "subject": "海面上方的飞机",
                    "visibleState": "机翼位于画面右侧",
                    "visibleDifference": "从海面过渡到大桥",
                    "retentionValue": "飞机轮廓更清楚",
                    "confidence": 0.88,
                }]}),
                job_overrides={"operationKey": "editorial-highlight-v12-temporal-overclaim"},
                context_provider=lambda job: context(job, frames=[
                    frame(job),
                    {**frame(job, b"frame-2"), "startMs": 2_000, "endMs": 3_000,
                     "representativeFrameMs": 2_500},
                ]),
            )

        self.assertEqual(receipt["status"], "failed-manual-review")
        self.assertEqual(receipt["failure"]["code"], "studio-vision-output-schema-highlight-candidate-shape")
        self.assertNotIn("highlightCandidates", receipt["qualitySignals"])

    def test_highlight_only_mode_requires_a_distinct_comparison_frame(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            source.write_bytes(b"synthetic-source")
            receipt = self.call(
                directory,
                source,
                FakeModel({"highlightCandidates": [{
                    "preferredFrame": "first",
                    "subjectKind": "aircraft",
                    "visibleAdvantage": "stronger-color-contrast",
                    "confidence": 0.88,
                }]}),
                job_overrides={"operationKey": "editorial-highlight-v12-missing-comparison"},
            )

        self.assertEqual(receipt["status"], "failed-manual-review")
        self.assertEqual(
            receipt["failure"]["code"],
            "studio-highlight-comparison-frame-unavailable",
        )
        self.assertNotIn("highlightCandidates", receipt["qualitySignals"])

    def test_highlight_only_mode_prefilters_temporally_distributed_salient_frames(self):
        import numpy as np

        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            source.write_bytes(b"synthetic-source")
            registry = ready_registry(directory)
            job = source_job(
                source,
                operationKey="editorial-highlight-v1-prefilter",
            )
            frames = []
            for index, red in enumerate((10, 80, 20, 100, 30, 120)):
                pixels = np.zeros((2, 2, 3), dtype=np.uint8)
                pixels[:, :, 0] = red
                start = 1_000 + index * 500
                frames.append({
                    "sourceDigest": job["sourceDigest"],
                    "startMs": start,
                    "endMs": start + 500,
                    "representativeFrameMs": start + 250,
                    "representativeFrameDigest": hashlib.sha256(pixels.tobytes()).hexdigest(),
                    "width": 2,
                    "height": 2,
                    "pixels": pixels,
                })
            model = FakeModel({"highlightCandidates": [{
                "preferredFrame": "first",
                "subjectKind": "natural-scene",
                "visibleAdvantage": "stronger-color-contrast",
                "confidence": 0.8,
            }]})
            receipt = analyze_vision(
                job,
                **registry,
                context_provider=lambda _job: context(job, frames=frames),
                model_loader=lambda _path, _runtime, _validate: model,
            )

        self.assertEqual(receipt["status"], "succeeded")
        self.assertEqual(receipt["qualitySignals"]["highlightPrefilter"], {
            "algorithm": "temporal-bucket-salience-v2",
            "inputFrameCount": 6,
            "selectedFrameCount": 5,
            "selectedFrameIds": ["frame-1", "frame-2", "frame-3", "frame-4", "frame-5"],
            "selectedSourceFrameIds": ["frame-1", "frame-2", "frame-3", "frame-4", "frame-6"],
        })
        self.assertEqual(len(model.calls[0]["frames"]), 2)
        self.assertEqual(receipt["qualitySignals"]["highlightPair"], {
            "algorithm": "maximum-visible-signature-distance-v1",
            "candidateFrameId": "frame-5",
            "comparisonFrameId": "frame-1",
            "candidateStartMs": 3_500,
            "candidateEndMs": 4_000,
            "candidateRepresentativeFrameMs": 3_750,
            "comparisonStartMs": 1_000,
            "comparisonEndMs": 1_500,
            "comparisonRepresentativeFrameMs": 1_250,
            "modelPreferredFrame": "first",
        })
        self.assertEqual(receipt["qualitySignals"]["highlightCandidates"][0]["startMs"], 3_500)

    def test_rejects_model_unknown_representative_frame_id(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            source.write_bytes(b"synthetic-source")
            receipt = self.call(
                directory,
                source,
                FakeModel(valid_output(visualObservations=[{
                    "kind": "visible-scene",
                    "text": "模型引用了不存在的帧编号",
                    "frameId": "frame-999",
                    "confidence": 0.9,
                }])),
            )

        self.assertEqual(receipt["status"], "failed-manual-review")
        self.assertEqual(receipt["failure"], {
            "code": "studio-vision-output-schema-visual-observation-interval",
        })

    def test_rejects_visual_observation_without_an_allowlisted_kind(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            source.write_bytes(b"synthetic-source")
            receipt = self.call(
                directory,
                source,
                FakeModel(valid_output(visualObservations=[{
                    "text": "没有明确证据类型",
                    "frameId": "frame-1",
                    "confidence": 0.8,
                }])),
            )

        self.assertEqual(receipt["status"], "failed-manual-review")
        self.assertEqual(receipt["failure"], {
            "code": "studio-vision-output-schema-visual-observation-shape-kind",
        })

    def test_classifies_structured_output_schema_failures_into_fixed_safe_families(self):
        cases = [
            (
                "top-level-object-keys",
                valid_output(unexpected=True),
                None,
            ),
            (
                "visual-observation-shape-kind",
                valid_output(visualObservations=[{
                    "kind": "identity",
                    "text": "不允许的观察类型",
                    "frameId": "frame-1",
                    "confidence": 0.8,
                }]),
                None,
            ),
            (
                "visual-observation-interval",
                valid_output(visualObservations=[{
                    "kind": "visible-scene",
                    "text": "引用不存在的代表帧",
                    "frameId": "frame-999",
                    "confidence": 0.8,
                }]),
                None,
            ),
            (
                "warning-uncertainty-text-arrays",
                valid_output(uncertainty="不是数组"),
                None,
            ),
            (
                "scalar-confidence-optional-claim-shape",
                valid_output(confidence="high"),
                None,
            ),
        ]
        for detail, output, provider in cases:
            with self.subTest(detail=detail), TemporaryDirectory() as directory:
                source = Path(directory) / "source.mp4"
                source.write_bytes(b"synthetic-source")
                arguments = {} if provider is None else {"context_provider": provider}
                receipt = self.call(directory, source, FakeModel(output), **arguments)

            self.assertEqual(receipt["status"], "failed-manual-review")
            self.assertEqual(receipt["failure"], {
                "code": f"studio-vision-output-schema-{detail}",
            })

    def test_discards_untrusted_demand_evidence_without_reading_candidate_fields(self):
        secret = "malicious-untrusted-demand-content"
        surrogate = chr(0xD800)
        candidate = {
            "demandText": surrogate,
            "observation": secret,
            "startMs": "not-an-integer",
            "endMs": {},
            "confidence": 10 ** 400,
        }
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            source.write_bytes(b"synthetic-source")
            model = FakeModel()
            model.output = json.dumps(
                valid_output(demandEvidenceCandidates=[candidate]),
                ensure_ascii=True,
            )
            receipt = self.call(
                directory,
                source,
                model,
                context_provider=lambda job: context(job, demand_text=None),
            )

        serialized = json.dumps(receipt, ensure_ascii=True)
        self.assertEqual(receipt["status"], "succeeded")
        self.assertIsNone(receipt["failure"])
        self.assertEqual(receipt["qualitySignals"]["demandEvidenceCandidates"], [])
        self.assertIn("untrusted-demand-evidence-discarded", receipt["warnings"])
        self.assertNotIn(secret, serialized)
        self.assertNotIn("\\ud800", serialized)
        self.assertNotIn(str(10 ** 400), serialized)

    def test_demand_present_mismatch_discards_only_untrusted_candidate(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            source.write_bytes(b"synthetic-source")
            receipt = self.call(
                directory,
                source,
                FakeModel(valid_output(demandEvidenceCandidates=[{
                    "demandText": "伪造需求",
                    "observation": "画面可能接近所需特写",
                    "startMs": 1_000,
                    "endMs": 2_000,
                    "confidence": 0.7,
                }])),
            )

        self.assertEqual(receipt["status"], "succeeded")
        self.assertIsNone(receipt["failure"])
        self.assertEqual(receipt["qualitySignals"]["demandEvidenceCandidates"], [])
        self.assertIn("untrusted-demand-evidence-discarded", receipt["warnings"])

    def test_native_schema_values_map_to_bounded_receipt_codes(self):
        surrogate = chr(0xD800)
        cases = [
            (
                "list-kind",
                json.dumps(valid_output(visualObservations=[{
                    "kind": [],
                    "text": "不可哈希的 kind",
                    "startMs": 1_000,
                    "endMs": 2_000,
                    "confidence": 0.8,
                }]), ensure_ascii=False),
                "studio-vision-output-schema-visual-observation-shape-kind",
                False,
            ),
            (
                "object-kind",
                json.dumps(valid_output(visualObservations=[{
                    "kind": {},
                    "text": "不可哈希的 kind",
                    "startMs": 1_000,
                    "endMs": 2_000,
                    "confidence": 0.8,
                }]), ensure_ascii=False),
                "studio-vision-output-schema-visual-observation-shape-kind",
                False,
            ),
            (
                "overflow-confidence",
                json.dumps(valid_output(confidence=10 ** 400), ensure_ascii=False),
                "studio-vision-output-invalid-number",
                True,
            ),
            (
                "escaped-surrogate-text",
                json.dumps(valid_output(suggestedTags=[surrogate]), ensure_ascii=True),
                "studio-vision-output-schema-suggested-tag-shape-content-duplication",
                False,
            ),
            (
                "raw-surrogate-text",
                json.dumps(valid_output(suggestedTags=[surrogate]), ensure_ascii=False),
                "studio-vision-output-invalid-json",
                False,
            ),
        ]
        for name, raw, expected_code, restart in cases:
            with self.subTest(name=name), TemporaryDirectory() as directory:
                source = Path(directory) / "source.mp4"
                source.write_bytes(b"synthetic-source")
                model = FakeModel()
                model.output = raw
                receipt = self.call(directory, source, model)

                self.assertEqual(receipt["status"], "failed-manual-review")
                self.assertEqual(receipt["failure"], {"code": expected_code})
                self.assertEqual(
                    receipt["qualitySignals"].get("workerRestart", {}).get("required", False),
                    restart,
                )

    def test_deduplicates_exact_suggested_tags_without_promoting_them_to_facts(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            source.write_bytes(b"synthetic-source")
            receipt = self.call(
                directory,
                source,
                FakeModel(valid_output(suggestedTags=["飞机舷窗", "飞机舷窗", "黄昏天空"])),
            )

        self.assertEqual(receipt["status"], "succeeded")
        self.assertEqual(receipt["suggestedTags"], ["飞机舷窗", "黄昏天空"])
        self.assertIn("duplicate-suggested-tags-deduplicated", receipt["warnings"])
        self.assertTrue(all(value.startswith("pending-") for value in receipt["observations"]))

    def test_json_decoder_limits_map_to_bounded_receipt_codes(self):
        ordinary = json.dumps(valid_output(), ensure_ascii=False)
        huge_integer = ordinary.replace(
            '"confidence": 0.75',
            f'"confidence": {"9" * 5_000}',
        )
        deeply_nested = "[" * 10_000 + "0" + "]" * 10_000
        cases = [
            (
                "huge-integer",
                huge_integer,
                "studio-vision-output-invalid-number",
                True,
            ),
            (
                "deeply-nested",
                deeply_nested,
                "studio-vision-output-invalid-json",
                False,
            ),
        ]
        for name, raw, expected_code, restart in cases:
            with self.subTest(name=name), TemporaryDirectory() as directory:
                source = Path(directory) / "source.mp4"
                source.write_bytes(b"synthetic-source")
                model = FakeModel()
                model.output = raw
                receipt = self.call(directory, source, model)

                self.assertEqual(receipt["status"], "failed-manual-review")
                self.assertEqual(receipt["failure"], {"code": expected_code})
                self.assertEqual(
                    receipt["qualitySignals"].get("workerRestart", {}).get("required", False),
                    restart,
                )

    def test_rejects_more_than_twelve_frames_and_oversize_images(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            source.write_bytes(b"synthetic-source")
            registry = ready_registry(directory)
            job = source_job(source)
            too_many = context(job, frames=[frame(job, str(index).encode()) for index in range(13)])
            with self.assertRaisesRegex(ProtocolError, "^studio-vision-frame-limit$"):
                analyze_vision(
                    job,
                    **registry,
                    context_provider=lambda _job: too_many,
                    model_loader=lambda _path, _runtime, _validate: FakeModel(),
                )
            oversize = context(job)
            oversize["frames"][0]["width"] = 449
            with self.assertRaisesRegex(ProtocolError, "^studio-vision-frame-size-limit$"):
                analyze_vision(
                    job,
                    **registry,
                    context_provider=lambda _job: oversize,
                    model_loader=lambda _path, _runtime, _validate: FakeModel(),
                )
            oversized_pixels = context(job, frames=[frame(job, b"x" * 17)])
            with self.assertRaisesRegex(ProtocolError, "^studio-vision-frame-byte-limit$"):
                analyze_vision(
                    job,
                    **registry,
                    context_provider=lambda _job: oversized_pixels,
                    model_loader=lambda _path, _runtime, _validate: FakeModel(),
                )

    def test_strictly_binds_frames_transcript_and_demand_to_source(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            source.write_bytes(b"synthetic-source")
            registry = ready_registry(directory)
            job = source_job(source)
            mutations = []
            wrong_frame_digest = context(job)
            wrong_frame_digest["frames"][0]["representativeFrameDigest"] = "f" * 64
            mutations.append(wrong_frame_digest)
            outside_transcript = context(job)
            outside_transcript["transcriptSpans"][0]["endMs"] = 4_001
            mutations.append(outside_transcript)
            wrong_demand = context(job)
            wrong_demand["demand"]["sourceDigest"] = "e" * 64
            mutations.append(wrong_demand)
            extra_field = context(job)
            extra_field["unexpected"] = True
            mutations.append(extra_field)
            for invalid in mutations:
                with self.subTest(invalid=list(invalid)):
                    with self.assertRaisesRegex(ProtocolError, "^studio-vision-input-invalid$"):
                        analyze_vision(
                            job,
                            **registry,
                            context_provider=lambda _job, value=invalid: value,
                            model_loader=lambda _path, _runtime, _validate: FakeModel(),
                        )

    def test_input_text_totals_are_bounded_before_model_load(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            source.write_bytes(b"synthetic-source")
            registry = ready_registry(directory)
            job = source_job(source)
            loads = []
            too_much_transcript = context(job, transcripts=[{
                "sourceDigest": job["sourceDigest"],
                "startMs": 1_000,
                "endMs": 2_000,
                "text": "中" * 666,
            } for _ in range(9)])
            with self.assertRaisesRegex(ProtocolError, "^studio-vision-transcript-limit$"):
                analyze_vision(
                    job,
                    **registry,
                    context_provider=lambda _job: too_much_transcript,
                    model_loader=lambda path, _runtime, _validate: loads.append(path),
                )
            self.assertEqual(loads, [])

    def test_input_surrogate_text_fails_with_the_fixed_protocol_code(self):
        for field in ("transcript", "demand"):
            with self.subTest(field=field), TemporaryDirectory() as directory:
                source = Path(directory) / "source.mp4"
                source.write_bytes(b"synthetic-source")
                registry = ready_registry(directory)
                job = source_job(source)
                invalid = context(job)
                if field == "transcript":
                    invalid["transcriptSpans"][0]["text"] = chr(0xD800)
                else:
                    invalid["demand"]["text"] = chr(0xD800)

                with self.assertRaisesRegex(
                    ProtocolError,
                    "^studio-vision-input-invalid$",
                ):
                    analyze_vision(
                        job,
                        **registry,
                        context_provider=lambda _job: invalid,
                        model_loader=lambda _path, _runtime, _validate: FakeModel(),
                    )

    def test_unknown_nonfinite_and_oversize_outputs_fail_manual_review(self):
        cases = [
            (json.dumps({**valid_output(), "unknown": True}), "studio-vision-output-schema-top-level-object-keys", False),
            (json.dumps(valid_output(visualObservations=[{
                "kind": "visible-scene",
                "text": "引用不存在的代表帧",
                "frameId": "frame-999",
                "confidence": 0.5,
            }])), "studio-vision-output-schema-visual-observation-interval", False),
            (json.dumps(valid_output(confidence=math.nan)), "studio-vision-output-invalid-number", True),
            ("not-json", "studio-vision-output-invalid-json", False),
            (" " * 65_537, "studio-vision-output-too-large", False),
            (json.dumps(valid_output(
                visualObservations=[{
                    "kind": "visible-scene",
                    "text": "x" * 1_000,
                    "frameId": "frame-1",
                    "confidence": 0.5,
                } for _ in range(32)],
                uncertainty=["y" * 500 for _ in range(32)],
            )), "studio-vision-output-too-large", False),
        ]
        for raw, code, restart in cases:
            with self.subTest(code=code), TemporaryDirectory() as directory:
                source = Path(directory) / "source.mp4"
                source.write_bytes(b"synthetic-source")
                model = FakeModel()
                model.output = raw
                receipt = self.call(directory, source, model)
                self.assertEqual(receipt["status"], "failed-manual-review")
                self.assertEqual(receipt["failure"], {"code": code})
                self.assertEqual(
                    receipt["qualitySignals"].get("workerRestart", {}).get("required", False),
                    restart,
                )

    def test_sensitive_claims_are_only_pending_and_warned(self):
        claims = {
            "personIdentity": "张三",
            "productMaterial": "18K金",
            "productPrice": 9_999,
            "ownership": "归客户所有",
            "permission": "可以公开发布",
        }
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            source.write_bytes(b"synthetic-source")
            receipt = self.call(directory, source, FakeModel(valid_output(**claims)))

        serialized = json.dumps(receipt, ensure_ascii=False)
        for field in claims:
            self.assertNotIn(field, receipt)
        for warning in (
            "unsupported-identity-claim",
            "unsupported-material-claim",
            "unsupported-price-claim",
            "unsupported-ownership-claim",
            "unsupported-permission-claim",
        ):
            self.assertIn(warning, receipt["warnings"])
        self.assertEqual(
            {item["status"] for item in receipt["qualitySignals"]["pendingObservations"]},
            {"pending-human-confirmation"},
        )
        self.assertNotIn("confirmed", serialized.lower())

    def test_repeated_demand_text_counts_toward_the_output_budget(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            source.write_bytes(b"synthetic-source")
            registry = ready_registry(directory)
            job = source_job(source)
            demand_text = "中" * 1_000
            candidate = {
                "demandText": demand_text,
                "observation": "可能匹配",
                "frameId": "frame-1",
                "confidence": 0.5,
            }
            receipt = analyze_vision(
                job,
                **registry,
                context_provider=lambda _job: context(job, demand_text=demand_text),
                model_loader=lambda _path, _runtime, _validate: FakeModel(valid_output(
                    demandEvidenceCandidates=[candidate for _ in range(12)],
                )),
            )
        self.assertEqual(receipt["status"], "failed-manual-review")
        self.assertEqual(receipt["failure"], {"code": "studio-vision-output-too-large"})

    def test_registry_and_source_digest_gates_precede_context_and_model(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            source.write_bytes(b"original-source")
            registry = ready_registry(directory)
            lock = Path(registry["model_lock_path"])
            payload = json.loads(lock.read_text(encoding="utf-8"))
            payload["artifacts"][1]["approved"] = False
            payload["artifacts"][1]["enabled"] = False
            lock.write_text(json.dumps(payload), encoding="utf-8")
            calls = []
            receipt = analyze_vision(
                source_job(source),
                **registry,
                context_provider=lambda _job: calls.append("context"),
                model_loader=lambda _path, _runtime, _validate: calls.append("model"),
            )
            self.assertEqual(receipt["failure"], {"code": "studio-model-artifact-unapproved"})
            self.assertEqual(calls, [])

        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            source.write_bytes(b"original-source")
            registry = ready_registry(directory)
            metadata = Path(registry["runtime_package_path"]) / "mlx_vlm-0.6.6.dist-info/METADATA"
            metadata.write_text("Name: mlx-vlm\nVersion: 0.6.5\n", encoding="utf-8")
            lock = Path(registry["model_lock_path"])
            payload = json.loads(lock.read_text(encoding="utf-8"))
            manifest = payload["artifacts"][0]["sources"][0]["files"]
            locked = next(item for item in manifest if item["relativePath"].endswith("METADATA"))
            locked["sha256"] = hashlib.sha256(metadata.read_bytes()).hexdigest()
            locked["byteLength"] = metadata.stat().st_size
            lock.write_text(json.dumps(payload), encoding="utf-8")
            job = source_job(source)
            source.unlink()
            calls = []
            receipt = analyze_vision(
                job,
                **registry,
                context_provider=lambda _job: calls.append("context"),
                model_loader=lambda _path, _runtime, _validate: calls.append("model"),
            )
            self.assertEqual(
                receipt["failure"],
                {"code": "studio-vision-runtime-version-mismatch"},
            )
            self.assertEqual(calls, [])

        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            source.write_bytes(b"original-source")
            registry = ready_registry(directory)
            runtime = Path(registry["runtime_package_path"])
            job = source_job(source)
            source.unlink()
            (runtime / "mlx_vlm/runtime.py").write_text("raise RuntimeError('tampered')\n")
            calls = []
            receipt = analyze_vision(
                job,
                **registry,
                context_provider=lambda _job: calls.append("context"),
                model_loader=lambda *_paths: calls.append("model"),
            )
            self.assertEqual(
                receipt["failure"],
                {"code": "studio-model-artifact-digest-mismatch"},
            )
            self.assertEqual(calls, [])

        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            source.write_bytes(b"original-source")
            registry = ready_registry(directory)
            runtime_file = Path(registry["runtime_package_path"]) / "mlx_vlm/runtime.py"
            job = source_job(source)
            loads = []

            def tampering_context(_job):
                runtime_file.write_text("raise RuntimeError('replaced')\n", encoding="utf-8")
                return context(job)

            receipt = analyze_vision(
                job,
                **registry,
                context_provider=tampering_context,
                model_loader=lambda _path, _runtime, _validate: loads.append("model"),
            )
            self.assertEqual(
                receipt["failure"],
                {"code": "studio-model-artifact-digest-mismatch"},
            )
            self.assertEqual(loads, [])

        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            source.write_bytes(b"original-source")
            registry = ready_registry(directory)
            runtime_extra = Path(registry["runtime_package_path"]) / "mlx_vlm/unlocked.py"
            job = source_job(source)
            loads = []

            def adding_context(_job):
                runtime_extra.write_text("unlocked", encoding="utf-8")
                return context(job)

            receipt = analyze_vision(
                job,
                **registry,
                context_provider=adding_context,
                model_loader=lambda _path, _runtime, _validate: loads.append("model"),
            )
            self.assertEqual(
                receipt["failure"],
                {"code": "studio-model-runtime-manifest-incomplete"},
            )
            self.assertEqual(loads, [])

        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            source.write_bytes(b"original-source")
            registry = ready_registry(directory)
            runtime = Path(registry["runtime_package_path"])
            job = source_job(source)
            source.unlink()
            (runtime / "mlx_vlm/unlocked.py").write_text("raise RuntimeError('ambient')\n")
            calls = []
            receipt = analyze_vision(
                job,
                **registry,
                context_provider=lambda _job: calls.append("context"),
                model_loader=lambda *_paths: calls.append("model"),
            )
            self.assertEqual(
                receipt["failure"],
                {"code": "studio-model-runtime-manifest-incomplete"},
            )
            self.assertEqual(calls, [])

        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            source.write_bytes(b"original-source")
            registry = ready_registry(directory)
            lock = Path(registry["model_lock_path"])
            payload = json.loads(lock.read_text(encoding="utf-8"))
            payload["artifacts"][0]["approved"] = False
            payload["artifacts"][0]["enabled"] = False
            lock.write_text(json.dumps(payload), encoding="utf-8")
            calls = []
            receipt = analyze_vision(
                source_job(source),
                **registry,
                context_provider=lambda _job: calls.append("context"),
                model_loader=lambda _path, _runtime, _validate: calls.append("model"),
            )
            self.assertEqual(receipt["failure"], {"code": "studio-model-artifact-unapproved"})
            self.assertEqual(calls, [])

        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            source.write_bytes(b"original-source")
            registry = ready_registry(directory)
            job = source_job(source)
            source.write_bytes(b"changed-source")
            calls = []
            receipt = analyze_vision(
                job,
                **registry,
                context_provider=lambda _job: calls.append("context"),
                model_loader=lambda _path, _runtime, _validate: calls.append("model"),
            )
            self.assertEqual(receipt["failure"], {"code": "studio-vision-source-digest-mismatch"})
            self.assertEqual(calls, [])

    def test_model_execution_is_serial_and_model_load_is_cached(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            source.write_bytes(b"synthetic-source")
            registry = ready_registry(directory)
            model = FakeModel(delay=0.05)
            loads = []

            def run(position):
                job = source_job(
                    source,
                    jobId=f"job-vision-{position}",
                    operationKey=f"vision-fixture-{position}",
                )
                return analyze_vision(
                    job,
                    **registry,
                    context_provider=lambda _job: context(job),
                    model_loader=lambda path, _runtime, _validate: loads.append(path) or model,
                )

            with ThreadPoolExecutor(max_workers=2) as pool:
                receipts = list(pool.map(run, (1, 2)))

        self.assertTrue(all(receipt["status"] == "succeeded" for receipt in receipts))
        self.assertEqual(model.max_active, 1)
        self.assertEqual(len(loads), 1)

    def test_ambient_runtime_is_rejected_before_source_or_context_read(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            source.write_bytes(b"synthetic-source")
            registry = ready_registry(directory)
            job = source_job(source)
            source.unlink()
            ambient = types.ModuleType("mlx_vlm")
            ambient.__file__ = "/tmp/ambient/mlx_vlm.py"
            ambient.load = lambda _path: (_ for _ in ()).throw(RuntimeError("ambient-used"))
            ambient.generate = ambient.load
            previous = sys.modules.get("mlx_vlm")
            sys.modules["mlx_vlm"] = ambient
            calls = []
            try:
                receipt = analyze_vision(
                    job,
                    **registry,
                    context_provider=lambda _job: calls.append("context"),
                    model_loader=lambda *_arguments: calls.append("model"),
                )
            finally:
                sys.modules.pop("mlx_vlm", None)
                if previous is not None:
                    sys.modules["mlx_vlm"] = previous

        self.assertEqual(receipt["status"], "failed-manual-review")
        self.assertEqual(receipt["failure"], {
            "code": "studio-vision-runtime-origin-invalid",
        })
        self.assertEqual(calls, [])

    def test_default_loader_uses_the_locked_runtime_tree(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            source.write_bytes(b"synthetic-source")
            registry = ready_registry(directory)
            job = source_job(source)
            previous = {
                name: module for name, module in tuple(sys.modules.items())
                if name == "mlx_vlm" or name.startswith("mlx_vlm.")
            }
            for name in previous:
                sys.modules.pop(name, None)
            try:
                receipt = analyze_vision(
                    job,
                    **registry,
                    context_provider=lambda _job: context(job, demand_text=None),
                )
                loaded = sys.modules.get("mlx_vlm")
                self.assertEqual(receipt["status"], "succeeded")
                self.assertTrue(
                    Path(loaded.__file__).resolve().is_relative_to(
                        Path(registry["runtime_package_path"]).resolve()
                    )
                )
            finally:
                for name in tuple(sys.modules):
                    if name == "mlx_vlm" or name.startswith("mlx_vlm."):
                        sys.modules.pop(name, None)
                sys.modules.update(previous)

    def test_import_time_runtime_mutation_or_addition_fails_before_model_load(self):
        for mutation, expected_code in (
            (
                'Path(__file__).with_name("runtime.py").write_text("tampered", encoding="utf-8")',
                "studio-model-artifact-digest-mismatch",
            ),
            (
                'Path(__file__).with_name("unlocked.py").write_text("unlocked", encoding="utf-8")',
                "studio-model-runtime-manifest-incomplete",
            ),
        ):
            with self.subTest(expected_code=expected_code), TemporaryDirectory() as directory:
                source = Path(directory) / "source.mp4"
                source.write_bytes(b"synthetic-source")
                registry = ready_registry(directory)
                runtime_root = Path(registry["runtime_package_path"])
                runtime_init = runtime_root / "mlx_vlm/__init__.py"
                marker = runtime_root / "model-load-called"
                runtime_init.write_text(
                    f'''from pathlib import Path
from .runtime import generate
from .runtime import load as runtime_load
{mutation}
def load(path):
    Path({str(marker)!r}).write_text("called", encoding="utf-8")
    return runtime_load(path)
''',
                    encoding="utf-8",
                )
                lock_path = Path(registry["model_lock_path"])
                lock = json.loads(lock_path.read_text(encoding="utf-8"))
                manifest = lock["artifacts"][0]["sources"][0]["files"]
                locked = next(
                    item for item in manifest if item["relativePath"].endswith("__init__.py")
                )
                locked["sha256"] = hashlib.sha256(runtime_init.read_bytes()).hexdigest()
                locked["byteLength"] = runtime_init.stat().st_size
                lock_path.write_text(json.dumps(lock), encoding="utf-8")
                previous = {
                    name: module for name, module in tuple(sys.modules.items())
                    if name == "mlx_vlm" or name.startswith("mlx_vlm.")
                }
                for name in previous:
                    sys.modules.pop(name, None)
                try:
                    receipt = analyze_vision(
                        source_job(source),
                        **registry,
                        context_provider=lambda job: context(job, demand_text=None),
                    )
                finally:
                    for name in tuple(sys.modules):
                        if name == "mlx_vlm" or name.startswith("mlx_vlm."):
                            sys.modules.pop(name, None)
                    sys.modules.update(previous)

                self.assertEqual(receipt["status"], "failed-manual-review")
                self.assertEqual(receipt["failure"], {"code": expected_code})
                self.assertFalse(marker.exists())

    def test_default_loader_rejects_loaded_runtime_submodule_outside_tree(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            source.write_bytes(b"synthetic-source")
            registry = ready_registry(directory)
            runtime_root = Path(registry["runtime_package_path"])
            runtime_impl = runtime_root / "mlx_vlm/runtime.py"
            runtime_impl.write_text(
                """import json
import sys
import types
def load(path):
    escaped = types.ModuleType("mlx_vlm.escaped")
    escaped.__file__ = "/tmp/ambient/escaped.py"
    sys.modules["mlx_vlm.escaped"] = escaped
    return object(), object()
def generate(model, processor, prompt, image, **kwargs): return json.dumps({
    "suggestedTags": [], "visualObservations": [], "demandEvidenceCandidates": [], "highlightCandidates": [],
    "forbiddenContentWarnings": [], "uncertainty": [], "confidence": 0.5,
})
""",
                encoding="utf-8",
            )
            lock_path = Path(registry["model_lock_path"])
            lock = json.loads(lock_path.read_text(encoding="utf-8"))
            manifest = lock["artifacts"][0]["sources"][0]["files"]
            locked = next(item for item in manifest if item["relativePath"].endswith("runtime.py"))
            locked["sha256"] = hashlib.sha256(runtime_impl.read_bytes()).hexdigest()
            locked["byteLength"] = runtime_impl.stat().st_size
            lock_path.write_text(json.dumps(lock), encoding="utf-8")
            try:
                receipt = analyze_vision(
                    source_job(source),
                    **registry,
                    context_provider=lambda job: context(job, demand_text=None),
                )
            finally:
                sys.modules.pop("mlx_vlm.escaped", None)

        self.assertEqual(receipt["status"], "failed-manual-review")
        self.assertEqual(receipt["failure"], {
            "code": "studio-vision-runtime-origin-invalid",
        })

    def test_invalid_numeric_requests_immediate_worker_restart(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            source.write_bytes(b"synthetic-source")
            model = FakeModel()
            model.output = json.dumps(valid_output(confidence=math.nan))
            receipt = self.call(directory, source, model, vision_job_count=1)
            self.assertEqual(receipt["status"], "failed-manual-review")
            self.assertEqual(receipt["qualitySignals"]["workerRestart"], {
                "required": True,
                "reason": "invalid-numeric-output",
                "completedVisionJobs": 1,
            })
            self.assertIn("vision-worker-restart-required", receipt["warnings"])

    def test_worker_scheduled_restart_evidence_is_status_independent(self):
        for status in (
            "succeeded",
            "failed-retryable",
            "failed-manual-review",
        ):
            with self.subTest(status=status):
                receipt = _apply_scheduled_vision_restart({
                    "status": status,
                    "qualitySignals": {},
                    "warnings": [],
                }, 50)
                self.assertEqual(receipt["qualitySignals"]["workerRestart"], {
                    "required": True,
                    "reason": "scheduled-job-limit",
                    "completedVisionJobs": 50,
                })
                self.assertIn("vision-worker-restart-required", receipt["warnings"])

    def test_timeout_is_bounded_and_retryable_without_model_output(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            source.write_bytes(b"synthetic-source")
            receipt = self.call(
                directory,
                source,
                FakeModel(delay=0.05),
                inference_timeout_seconds=0.01,
            )
        self.assertEqual(receipt["status"], "failed-retryable")
        self.assertEqual(receipt["failure"], {"code": "studio-vision-model-timeout"})
        self.assertNotIn("workerRestart", receipt["qualitySignals"])


if __name__ == "__main__":
    unittest.main()
