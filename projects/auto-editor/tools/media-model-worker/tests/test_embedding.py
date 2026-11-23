import contextlib
import hashlib
import io
import json
import math
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

WORKER_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(WORKER_ROOT))

from embedding import analyze_embedding, normalize_vector  # noqa: E402

FFMPEG_PATH = (
    REPOSITORY_ROOT
    / "node_modules/.pnpm/ffmpeg-static@5.2.0/node_modules/ffmpeg-static/ffmpeg"
).resolve()


def source_job(source: Path, **overrides):
    job = {
        "schemaVersion": 1,
        "jobId": "job-embedding-1",
        "customerId": "customer-a",
        "sessionId": "session-a",
        "sourceAssetId": "asset-a",
        "sourceDigest": hashlib.sha256(source.read_bytes()).hexdigest(),
        "sourcePath": str(source.resolve()),
        "sourceStartMs": 0,
        "sourceEndMs": 3_000,
        "layer": "embedding",
        "operationKey": "embedding-fixture-v1",
        "requestedAt": "2026-09-12T08:00:00.000Z",
    }
    job.update(overrides)
    return job


def artifact(artifact_id: str, root: Path, code: Path, weights: Path):
    revision = "a" * 40
    source_root = "https://github.com/mlfoundations/open_clip"
    weights_root = "https://huggingface.co/laion/CLIP-ViT-B-32-laion2B-s34B-b79K"

    def source(source_id: str, kind: str, source_url: str, path: Path):
        relative = path.relative_to(root).as_posix()
        return {
            "id": source_id,
            "kind": kind,
            "sourceUrl": source_url,
            "sourceRevision": revision,
            "inventoryVerified": True,
            "files": [{
                "relativePath": relative,
                "sourceUrl": f"{source_url}/resolve/{revision}/{relative}",
                "required": True,
                "executable": False,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "byteLength": path.stat().st_size,
            }],
        }

    return {
        "id": artifact_id,
        "version": "3.3.0-vit-b-32-fixture",
        "artifactKind": "checkpoint",
        "sources": [
            source("code", "code", source_root, code),
            source("weights", "weights", weights_root, weights),
        ],
        "licenseEvidence": {
            "code": {
                "applies": True,
                "sourceId": "code",
                "sourceRevision": revision,
                "spdxExpression": "MIT",
                "evidenceUrl": f"{source_root}/raw/{revision}/LICENSE",
            },
            "weights": {
                "applies": True,
                "sourceId": "weights",
                "sourceRevision": revision,
                "spdxExpression": "MIT",
                "evidenceUrl": f"{weights_root}/raw/{revision}/LICENSE",
            },
        },
        "licenseReview": "approved",
        "runtimeBackend": "python-pytorch-cpu",
        "quantization": None,
        "enabled": True,
        "approved": True,
        "approvedBy": "fixture-reviewer",
        "approvedAt": "2026-09-12T08:00:00.000Z",
        "testedDevices": ["synthetic-fixture"],
        "testSetVersion": "fixture-v1",
    }


def ready_registry(directory: str):
    root = Path(directory) / "models"
    root.mkdir()
    code = root / "open_clip-3.3.0.tar.gz"
    weights = root / "open_clip_pytorch_model.bin"
    code.write_bytes(b"synthetic-openclip-code")
    weights.write_bytes(b"synthetic-openclip-weights")
    artifact_id = "openclip-3.3.0-vit-b-32-fixture"
    lock = Path(directory) / "models.lock.json"
    lock.write_text(json.dumps({
        "schemaVersion": 1,
        "artifacts": [artifact(artifact_id, root, code, weights)],
    }), encoding="utf-8")
    return {
        "model_lock_path": str(lock.resolve()),
        "model_root": str(root.resolve()),
        "openclip_artifact_id": artifact_id,
        "checkpoint_path": str(weights.resolve()),
    }


def representative_frames(_job):
    first_pixels = b"private-frame-one"
    second_pixels = b"private-frame-two"
    return [
        {
            "startMs": 0,
            "endMs": 1_000,
            "representativeFrameMs": 500,
            "representativeFrameDigest": hashlib.sha256(first_pixels).hexdigest(),
            "pixels": first_pixels,
        },
        {
            "startMs": 1_000,
            "endMs": 3_000,
            "representativeFrameMs": 2_000,
            "representativeFrameDigest": hashlib.sha256(second_pixels).hexdigest(),
            "pixels": second_pixels,
        },
    ]


def make_video(root: Path) -> Path:
    output = root / "synthetic-scenes.mp4"
    subprocess.run([
        str(FFMPEG_PATH), "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=c=red:s=160x120:r=10:d=1",
        "-f", "lavfi", "-i", "color=c=blue:s=160x120:r=10:d=1",
        "-f", "lavfi", "-i", "color=c=green:s=160x120:r=10:d=1",
        "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[out]",
        "-map", "[out]", "-c:v", "libx264", "-preset", "ultrafast",
        "-pix_fmt", "yuv420p", "-y", str(output),
    ], check=True, capture_output=True, timeout=20)
    return output


def fake_openclip_modules(root: Path) -> Path:
    modules = root / "fake-python-modules"
    modules.mkdir()
    (modules / "open_clip.py").write_text(
        """from pathlib import Path

class Encoded:
    def float(self): return self
    def mean(self, dim=0): return self
    def detach(self): return self
    def cpu(self): return self
    def tolist(self): return [3, 4]

class Model:
    def eval(self): return self
    def encode_image(self, batch): return Encoded()

def create_model_and_transforms(name, *, pretrained, device):
    if name != "ViT-B-32" or device != "cpu" or not Path(pretrained).is_file():
        raise RuntimeError("invalid fake model binding")
    return Model(), None, lambda image: image
""",
        encoding="utf-8",
    )
    (modules / "torch.py").write_text(
        """class Batch:
    def to(self, device): return self

class InferenceMode:
    def __enter__(self): return self
    def __exit__(self, *args): return False

def stack(values): return Batch()
def inference_mode(): return InferenceMode()
""",
        encoding="utf-8",
    )
    pillow = modules / "PIL"
    pillow.mkdir()
    (pillow / "__init__.py").write_text(
        """class Image:
    @staticmethod
    def fromarray(value): return value
""",
        encoding="utf-8",
    )
    return modules


def run_embedding_worker(job, registry, modules: Path):
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join((str(modules), str(WORKER_ROOT)))
    return subprocess.run(
        [
            sys.executable,
            str(WORKER_ROOT / "worker.py"),
            "--model-lock-path", registry["model_lock_path"],
            "--model-root", registry["model_root"],
            "--openclip-artifact-id", registry["openclip_artifact_id"],
            "--openclip-checkpoint-path", registry["checkpoint_path"],
        ],
        input=f"{json.dumps(job)}\n",
        text=True,
        capture_output=True,
        cwd=modules.parent,
        env=environment,
        timeout=20,
        check=False,
    )


class FakeModel:
    def __init__(self, vector):
        self.vector = vector
        self.calls = 0

    def encode_images(self, frames):
        self.calls += 1
        if len(frames) != 2:
            raise AssertionError("expected representative frames")
        return self.vector


class EmbeddingAnalysisTest(unittest.TestCase):
    def test_normalizes_vectors_and_rejects_nan_zero_or_dimensions_outside_bounds(self):
        self.assertEqual(normalize_vector([3, 4]), [0.6, 0.8])
        for vector, code in (
            ([math.nan, 1], "studio-embedding-vector-invalid"),
            ([math.inf, 1], "studio-embedding-vector-invalid"),
            ([0, 0], "studio-embedding-vector-zero"),
            ([1], "studio-embedding-vector-dimension-invalid"),
            ([1] * 2_049, "studio-embedding-vector-dimension-invalid"),
        ):
            with self.subTest(code=code, dimension=len(vector)):
                with self.assertRaisesRegex(ValueError, f"^{code}$"):
                    normalize_vector(vector)

    def test_ready_artifact_gate_precedes_frames_and_model_and_receipt_records_exact_identity(self):
        with TemporaryDirectory() as directory:
            registry = ready_registry(directory)
            checkpoint_digest = hashlib.sha256(
                Path(registry["checkpoint_path"]).read_bytes()
            ).hexdigest()
            source = Path(directory) / "source.mp4"
            source.write_bytes(b"synthetic-video")
            model = FakeModel([3, 4])
            loads = []

            def load_model(path):
                loads.append(path)
                return model

            first = analyze_embedding(
                source_job(source),
                **registry,
                frame_provider=representative_frames,
                model_loader=load_model,
            )
            second = analyze_embedding(
                source_job(source, jobId="job-embedding-2", operationKey="embedding-fixture-v2"),
                **registry,
                frame_provider=representative_frames,
                model_loader=load_model,
            )

        self.assertEqual(first["status"], "succeeded")
        self.assertEqual(first["qualitySignals"]["embedding"], [0.6, 0.8])
        self.assertEqual(first["qualitySignals"]["dimension"], 2)
        self.assertEqual(first["modelName"], registry["openclip_artifact_id"])
        self.assertEqual(first["modelVersion"], "3.3.0-vit-b-32-fixture")
        self.assertEqual(first["checkpointDigest"], checkpoint_digest)
        self.assertEqual(first["qualitySignals"]["artifact"], {
            "id": registry["openclip_artifact_id"],
            "version": "3.3.0-vit-b-32-fixture",
            "sha256": checkpoint_digest,
        })
        self.assertEqual(len(loads), 1)
        self.assertEqual(model.calls, 2)
        self.assertEqual(second["checkpointDigest"], checkpoint_digest)
        serialized = json.dumps(first)
        self.assertNotIn("private-frame-one", serialized)
        self.assertNotIn("private-frame-two", serialized)

    def test_default_adapter_consumes_task4_representative_frames_without_serializing_pixels(self):
        with TemporaryDirectory() as directory:
            registry = ready_registry(directory)
            video = make_video(Path(directory))

            class SceneModel:
                def encode_images(self, frames):
                    self.count = len(frames)
                    return [1, 1]

            model = SceneModel()
            receipt = analyze_embedding(
                source_job(video),
                **registry,
                model_loader=lambda _path: model,
            )
            import cv2

            actual_digests = []
            capture = cv2.VideoCapture(str(video))
            try:
                for frame in receipt["qualitySignals"]["representativeFrames"]:
                    capture.set(cv2.CAP_PROP_POS_MSEC, frame["representativeFrameMs"])
                    ok, pixels = capture.read()
                    self.assertTrue(ok)
                    actual_digests.append(hashlib.sha256(pixels.tobytes()).hexdigest())
            finally:
                capture.release()

        self.assertEqual(receipt["status"], "succeeded")
        self.assertGreaterEqual(model.count, 1)
        frames = receipt["qualitySignals"]["representativeFrames"]
        self.assertEqual(len(frames), model.count)
        self.assertTrue(all(
            0 <= frame["startMs"] < frame["representativeFrameMs"] < frame["endMs"] <= 3_000
            for frame in frames
        ))
        self.assertEqual(
            [frame["representativeFrameDigest"] for frame in frames],
            actual_digests,
        )
        self.assertNotIn("pixels", json.dumps(receipt))

    def test_rejects_frame_digest_that_does_not_match_pixels_before_loading_model(self):
        with TemporaryDirectory() as directory:
            registry = ready_registry(directory)
            source = Path(directory) / "source.mp4"
            source.write_bytes(b"synthetic-video")
            loads = []

            frames = representative_frames(None)
            frames[0]["representativeFrameDigest"] = "f" * 64
            receipt = analyze_embedding(
                source_job(source),
                **registry,
                frame_provider=lambda _job: frames,
                model_loader=lambda path: loads.append(path),
            )

        self.assertEqual(receipt["status"], "failed-manual-review")
        self.assertEqual(
            receipt["failure"],
            {"code": "studio-embedding-frame-digest-mismatch"},
        )
        self.assertEqual(loads, [])

    def test_rechecks_source_after_frame_provider_before_loading_or_calling_model(self):
        with TemporaryDirectory() as directory:
            registry = ready_registry(directory)
            source = Path(directory) / "source.mp4"
            source.write_bytes(b"original-synthetic-video")
            calls = []

            def replace_source(_job):
                source.write_bytes(b"replaced-during-frame-provider")
                return representative_frames(None)

            receipt = analyze_embedding(
                source_job(source),
                **registry,
                frame_provider=replace_source,
                model_loader=lambda path: calls.append(("load", path)),
            )

        self.assertEqual(receipt["status"], "failed-manual-review")
        self.assertEqual(
            receipt["failure"],
            {"code": "studio-embedding-source-digest-mismatch"},
        )
        self.assertEqual(calls, [])

    def test_unapproved_or_digest_mismatched_artifact_fails_before_frames_or_model(self):
        for mutation, expected in (
            ("unapproved", "studio-model-artifact-unapproved"),
            ("digest", "studio-model-artifact-digest-mismatch"),
        ):
            with self.subTest(mutation=mutation), TemporaryDirectory() as directory:
                registry = ready_registry(directory)
                source = Path(directory) / "source.mp4"
                source.write_bytes(b"synthetic-video")
                lock = Path(registry["model_lock_path"])
                payload = json.loads(lock.read_text(encoding="utf-8"))
                if mutation == "unapproved":
                    payload["artifacts"][0]["approved"] = False
                    payload["artifacts"][0]["enabled"] = False
                    lock.write_text(json.dumps(payload), encoding="utf-8")
                else:
                    Path(registry["checkpoint_path"]).write_bytes(b"changed-weights")
                calls = []
                receipt = analyze_embedding(
                    source_job(source),
                    **registry,
                    frame_provider=lambda _job: calls.append("frames"),
                    model_loader=lambda _path: calls.append("model"),
                )
                self.assertEqual(receipt["status"], "failed-manual-review")
                self.assertEqual(receipt["failure"], {"code": expected})
                self.assertEqual(calls, [])

    def test_exact_source_digest_is_checked_before_frames_or_model(self):
        with TemporaryDirectory() as directory:
            registry = ready_registry(directory)
            source = Path(directory) / "source.mp4"
            source.write_bytes(b"original-synthetic-video")
            job = source_job(source)
            source.write_bytes(b"changed-synthetic-video")
            calls = []

            receipt = analyze_embedding(
                job,
                **registry,
                frame_provider=lambda _job: calls.append("frames"),
                model_loader=lambda _path: calls.append("model"),
            )

        self.assertEqual(receipt["status"], "failed-manual-review")
        self.assertEqual(
            receipt["failure"],
            {"code": "studio-embedding-source-digest-mismatch"},
        )
        self.assertEqual(calls, [])

    def test_invalid_model_vectors_fail_closed_without_raw_frame_or_path_output(self):
        for vector, expected in (
            ([0, 0], "studio-embedding-vector-zero"),
            ([math.nan, 1], "studio-embedding-vector-invalid"),
            ([1], "studio-embedding-vector-dimension-invalid"),
        ):
            with self.subTest(expected=expected), TemporaryDirectory() as directory:
                registry = ready_registry(directory)
                source = Path(directory) / "private-source.mp4"
                source.write_bytes(b"synthetic-video")
                output = io.StringIO()
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                    receipt = analyze_embedding(
                        source_job(source),
                        **registry,
                        frame_provider=representative_frames,
                        model_loader=lambda _path: FakeModel(vector),
                    )
                self.assertEqual(receipt["status"], "failed-manual-review")
                self.assertEqual(receipt["failure"], {"code": expected})
                self.assertEqual(output.getvalue(), "")
                self.assertNotIn(str(source), json.dumps(receipt["failure"]))
                self.assertNotIn("private-frame", json.dumps(receipt))

    def test_real_worker_subprocess_emits_one_safe_receipt_for_ready_and_unapproved_artifacts(self):
        with TemporaryDirectory() as directory:
            registry = ready_registry(directory)
            source = make_video(Path(directory))
            job = source_job(source)
            modules = fake_openclip_modules(Path(directory))

            success = run_embedding_worker(job, registry, modules)
            self.assertEqual(success.returncode, 0)
            self.assertEqual(success.stderr, "")
            self.assertEqual(len(success.stdout.splitlines()), 1)
            success_receipt = json.loads(success.stdout)
            self.assertEqual(success_receipt["status"], "succeeded")
            self.assertEqual(success_receipt["qualitySignals"]["embedding"], [0.6, 0.8])
            self.assertEqual(success_receipt["modelName"], registry["openclip_artifact_id"])
            self.assertEqual(
                success_receipt["checkpointDigest"],
                hashlib.sha256(Path(registry["checkpoint_path"]).read_bytes()).hexdigest(),
            )
            self.assertNotIn("pixels", success.stdout)

            lock = Path(registry["model_lock_path"])
            payload = json.loads(lock.read_text(encoding="utf-8"))
            payload["artifacts"][0]["approved"] = False
            payload["artifacts"][0]["enabled"] = False
            lock.write_text(json.dumps(payload), encoding="utf-8")
            rejected = run_embedding_worker(job, registry, modules)

            self.assertEqual(rejected.returncode, 0)
            self.assertEqual(rejected.stderr, "")
            self.assertEqual(len(rejected.stdout.splitlines()), 1)
            rejected_receipt = json.loads(rejected.stdout)
            self.assertEqual(rejected_receipt["status"], "failed-manual-review")
            self.assertEqual(
                rejected_receipt["failure"],
                {"code": "studio-model-artifact-unapproved"},
            )
            self.assertNotIn("pixels", rejected.stdout)


if __name__ == "__main__":
    unittest.main()
