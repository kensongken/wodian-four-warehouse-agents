from subprocess_fixture import shell_overflow_executable
import hashlib
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

WORKER_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKER_ROOT))

import transcription  # noqa: E402
from transcription import analyze_transcript  # noqa: E402


def executable(root: Path, name: str, body: str) -> Path:
    path = root / name
    path.write_text(f"#!{sys.executable}\n{body}\n", encoding="utf-8")
    path.chmod(0o700)
    return path.resolve()


def source_job(source: Path, **overrides):
    job = {
        "schemaVersion": 1,
        "jobId": "job-transcript-1",
        "customerId": "customer-a",
        "sessionId": "session-a",
        "sourceAssetId": "asset-a",
        "sourceDigest": hashlib.sha256(source.read_bytes()).hexdigest(),
        "sourcePath": str(source.resolve()),
        "sourceStartMs": 1000,
        "sourceEndMs": 2000,
        "layer": "transcript",
        "operationKey": "transcript-fixture-v1",
        "requestedAt": "2026-09-12T06:00:00.000Z",
    }
    job.update(overrides)
    return job


def fake_ffprobe(root: Path, *, has_audio: bool = True) -> Path:
    streams = [{"codec_type": "video", "codec_name": "h264", "width": 160, "height": 120}]
    if has_audio:
        streams.append({"codec_type": "audio", "codec_name": "aac"})
    payload = json.dumps({"format": {"duration": "3.000"}, "streams": streams})
    return executable(root, "ffprobe", f"import sys\nsys.stdout.write({payload!r})")


def fake_ffmpeg(root: Path, log_path: Path, wav_path_log: Path) -> Path:
    return executable(
        root,
        "ffmpeg",
        "import json, pathlib, sys\n"
        f"pathlib.Path({str(log_path)!r}).write_text(json.dumps(sys.argv[1:]), encoding='utf-8')\n"
        "wav = pathlib.Path(sys.argv[-1])\n"
        "wav.write_bytes(b'RIFF-fake-16k-mono')\n"
        f"pathlib.Path({str(wav_path_log)!r}).write_text(str(wav), encoding='utf-8')",
    )


def fake_whisper(root: Path, log_path: Path, payload: dict) -> Path:
    encoded = json.dumps(payload, ensure_ascii=False)
    return executable(
        root,
        "whisper-cli",
        "import json, pathlib, sys\n"
        f"pathlib.Path({str(log_path)!r}).write_text(json.dumps(sys.argv[1:]), encoding='utf-8')\n"
        "arguments = sys.argv[1:]\n"
        f"payload = {encoded!r}\n"
        "if '-of' in arguments and arguments[arguments.index('-of') + 1] == '-' and '-np' in arguments:\n"
        "    sys.stdout.write(payload)\n"
        "else:\n"
        "    wav = arguments[arguments.index('-f') + 1]\n"
        "    pathlib.Path(wav + '.json').write_text(payload, encoding='utf-8')",
    )


def approved_registry(root: Path, whisper: Path, model: Path, **overrides):
    revision = "a" * 40
    binary_id = "synthetic-whisper-cli"
    model_id = "synthetic-whisper-model"

    def artifact(artifact_id: str, version: str, kind: str, path: Path):
        relative_path = str(path.resolve().relative_to(root.resolve()))
        source_root = f"https://models.invalid/org/{artifact_id}"
        return {
            "id": artifact_id,
            "version": version,
            "artifactKind": "binary-model" if kind == "code" else "checkpoint",
            "sources": [{
                "id": kind,
                "kind": kind,
                "sourceUrl": source_root,
                "sourceRevision": revision,
                "inventoryVerified": True,
                "files": [{
                    "relativePath": relative_path,
                    "sourceUrl": f"{source_root}/raw/{revision}/{Path(relative_path).name}",
                    "required": True,
                    "executable": kind == "code",
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "byteLength": path.stat().st_size,
                }],
            }],
            "licenseEvidence": {
                "code": {
                    "applies": kind == "code",
                    "sourceId": kind if kind == "code" else None,
                    "sourceRevision": revision if kind == "code" else None,
                    "spdxExpression": "MIT" if kind == "code" else None,
                    "evidenceUrl": f"{source_root}/raw/{revision}/LICENSE" if kind == "code" else None,
                },
                "weights": {
                    "applies": kind == "weights",
                    "sourceId": kind if kind == "weights" else None,
                    "sourceRevision": revision if kind == "weights" else None,
                    "spdxExpression": "MIT" if kind == "weights" else None,
                    "evidenceUrl": f"{source_root}/raw/{revision}/LICENSE" if kind == "weights" else None,
                },
            },
            "licenseReview": "approved",
            "runtimeBackend": "synthetic",
            "quantization": None,
            "enabled": True,
            "approved": True,
            "approvedBy": "synthetic-reviewer",
            "approvedAt": "2026-09-12T06:00:00.000Z",
            "testedDevices": [],
            "testSetVersion": None,
        }

    lock_path = root / "synthetic-models.lock.json"
    lock_path.write_text(json.dumps({
        "schemaVersion": 1,
        "artifacts": [
            artifact(binary_id, "1.9.2-fixture", "code", whisper),
            artifact(model_id, "small-q5_0-fixture", "weights", model),
        ],
    }), encoding="utf-8")
    configuration = {
        "model_lock_path": str(lock_path.resolve()),
        "model_root": str(root.resolve()),
        "whisper_binary_artifact_id": binary_id,
        "whisper_model_artifact_id": model_id,
    }
    configuration.update(overrides)
    return configuration


class TranscriptionTest(unittest.TestCase):
    def test_extracts_interval_without_shell_and_maps_chinese_spans_to_source_time(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "名字包含 空格;$(bad).mp4"
            source.write_bytes(b"synthetic-media")
            model = root / "ggml-small-q5_0.bin"
            model.write_bytes(b"synthetic-model")
            ffmpeg_log = root / "ffmpeg-args.json"
            whisper_log = root / "whisper-args.json"
            wav_path_log = root / "wav-path.txt"
            process_calls = []
            real_popen = subprocess.Popen

            def observing_popen(*args, **kwargs):
                process_calls.append((args, kwargs))
                return real_popen(*args, **kwargs)

            with patch.object(transcription.subprocess, "Popen", observing_popen):
                receipt = analyze_transcript(
                    source_job(source),
                    str(fake_ffprobe(root)),
                    str(fake_ffmpeg(root, ffmpeg_log, wav_path_log)),
                    str(whisper := fake_whisper(root, whisper_log, {
                        "result": {"language": "zh"},
                        "transcription": [{
                            "offsets": {"from": 120, "to": 880},
                            "text": "介绍设计灵感",
                            "no_speech_prob": 0.03,
                        }],
                    })),
                    str(model),
                    **approved_registry(root, whisper, model),
                )
            ffmpeg_arguments = json.loads(ffmpeg_log.read_text(encoding="utf-8"))
            whisper_arguments = json.loads(whisper_log.read_text(encoding="utf-8"))
            temporary_wav = Path(wav_path_log.read_text(encoding="utf-8"))

            self.assertEqual(receipt["status"], "succeeded")
            self.assertEqual(receipt["suggestedTags"], [])
            self.assertEqual(receipt["warnings"], ["transcript-is-model-suggestion"])
            self.assertEqual(receipt["qualitySignals"]["spans"], [{
                "startMs": 1120,
                "endMs": 1880,
                "text": "介绍设计灵感",
                "noSpeechProbability": 0.03,
            }])
            self.assertEqual(receipt["qualitySignals"]["detectedLanguage"], "zh")
            self.assertEqual(receipt["adapterName"], "synthetic-whisper-cli")
            self.assertEqual(receipt["modelName"], "synthetic-whisper-model")
            self.assertEqual(receipt["modelVersion"], "small-q5_0-fixture")
            self.assertEqual(receipt["checkpointDigest"], hashlib.sha256(model.read_bytes()).hexdigest())
            self.assertEqual(receipt["speechState"], "speech")
            self.assertEqual(receipt["inputBinding"], {
                "sourceDigest": receipt["sourceDigest"],
                "sourceStartMs": 1_000,
                "sourceEndMs": 2_000,
            })
            self.assertEqual(receipt["runtimeMetrics"], {"runtimeMs": receipt["runtimeMs"]})
            self.assertEqual(receipt["artifactBinding"], {
                "adapter": {
                    "id": "synthetic-whisper-cli",
                    "version": "1.9.2-fixture",
                    "sha256": hashlib.sha256(whisper.read_bytes()).hexdigest(),
                },
                "model": {
                    "id": "synthetic-whisper-model",
                    "version": "small-q5_0-fixture",
                    "sha256": hashlib.sha256(model.read_bytes()).hexdigest(),
                },
            })
            self.assertEqual(receipt["qualitySignals"]["artifacts"], {
                "binary": {
                    "id": "synthetic-whisper-cli",
                    "version": "1.9.2-fixture",
                    "sha256": hashlib.sha256(whisper.read_bytes()).hexdigest(),
                },
                "model": {
                    "id": "synthetic-whisper-model",
                    "version": "small-q5_0-fixture",
                    "sha256": hashlib.sha256(model.read_bytes()).hexdigest(),
                },
            })
            self.assertNotIn("confirmedFacts", receipt)
            self.assertIn(str(source.resolve()), ffmpeg_arguments)
            self.assertEqual(ffmpeg_arguments[ffmpeg_arguments.index("-ss") + 1], "1.000")
            self.assertEqual(ffmpeg_arguments[ffmpeg_arguments.index("-t") + 1], "1.000")
            self.assertEqual(ffmpeg_arguments[ffmpeg_arguments.index("-ar") + 1], "16000")
            self.assertEqual(ffmpeg_arguments[ffmpeg_arguments.index("-ac") + 1], "1")
            self.assertEqual(whisper_arguments, [
                "-m", str(model.resolve()), "-f", str(temporary_wav),
                "-oj", "-of", "-", "-np", "-l", "auto",
            ])
            self.assertFalse(temporary_wav.exists())
            self.assertFalse((root / "bad").exists())
            self.assertEqual(len(process_calls), 3)
            self.assertTrue(all(isinstance(call[0][0], list) for call in process_calls))
            self.assertTrue(all(call[1].get("shell") is False for call in process_calls))

    def test_no_audio_returns_success_with_warning_without_invoking_audio_tools(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "silent.mp4"
            source.write_bytes(b"synthetic-silent-media")
            model = root / "model.bin"
            model.write_bytes(b"synthetic-model")
            invoked = root / "must-not-run"
            forbidden = executable(root, "forbidden", f"from pathlib import Path\nPath({str(invoked)!r}).write_text('ran')")
            receipt = analyze_transcript(
                source_job(source),
                str(fake_ffprobe(root, has_audio=False)),
                str(forbidden),
                str(forbidden),
                str(model),
                **approved_registry(root, forbidden, model),
            )

            self.assertEqual(receipt["status"], "succeeded")
            self.assertEqual(receipt["qualitySignals"], {
                "detectedLanguage": None,
                "noSpeechProbability": None,
                "spans": [],
                "artifacts": {
                    "binary": {
                        "id": "synthetic-whisper-cli",
                        "version": "1.9.2-fixture",
                        "sha256": hashlib.sha256(forbidden.read_bytes()).hexdigest(),
                    },
                    "model": {
                        "id": "synthetic-whisper-model",
                        "version": "small-q5_0-fixture",
                        "sha256": hashlib.sha256(model.read_bytes()).hexdigest(),
                    },
                },
            })
            self.assertEqual(receipt["warnings"], ["no-audio-track"])
            self.assertEqual(receipt["speechState"], "no-audio")
            self.assertFalse(invoked.exists())

    def test_audio_without_transcript_is_explicit_no_speech(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "no-speech.mp4"
            source.write_bytes(b"synthetic-media")
            model = root / "model.bin"
            model.write_bytes(b"synthetic-model")
            whisper = fake_whisper(root, root / "whisper.json", {
                "result": {"language": "zh"},
                "transcription": [],
                "no_speech_prob": 0.99,
            })
            receipt = analyze_transcript(
                source_job(source),
                str(fake_ffprobe(root)),
                str(fake_ffmpeg(root, root / "ffmpeg.json", root / "wav.txt")),
                str(whisper),
                str(model),
                **approved_registry(root, whisper, model),
            )

            self.assertEqual(receipt["status"], "succeeded")
            self.assertEqual(receipt["speechState"], "no-speech")
            self.assertEqual(receipt["qualitySignals"]["spans"], [])
            self.assertEqual(receipt["qualitySignals"]["noSpeechProbability"], 0.99)

    def test_source_digest_mismatch_fails_before_any_media_command(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            source.write_bytes(b"synthetic-media")
            model = root / "model.bin"
            model.write_bytes(b"synthetic-model")
            invoked = root / "must-not-run"
            forbidden = executable(
                root,
                "forbidden",
                f"from pathlib import Path\nPath({str(invoked)!r}).write_text('ran')",
            )
            receipt = analyze_transcript(
                source_job(source, sourceDigest="0" * 64),
                str(forbidden),
                str(forbidden),
                str(forbidden),
                str(model),
                **approved_registry(root, forbidden, model),
            )

            self.assertEqual(receipt["status"], "failed-manual-review")
            self.assertEqual(
                receipt["failure"],
                {"code": "studio-transcription-source-digest-mismatch"},
            )
            self.assertFalse(invoked.exists())

    def test_rejects_relative_or_out_of_range_spans_instead_of_clamping_them(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            source.write_bytes(b"synthetic-media")
            model = root / "model.bin"
            model.write_bytes(b"synthetic-model")
            whisper = fake_whisper(root, root / "whisper.json", {
                "result": {"language": "zh"},
                "transcription": [{"offsets": {"from": 900, "to": 1001}, "text": "越界"}],
            })
            receipt = analyze_transcript(
                source_job(source),
                str(fake_ffprobe(root)),
                str(fake_ffmpeg(root, root / "ffmpeg.json", root / "wav.txt")),
                str(whisper),
                str(model),
                **approved_registry(root, whisper, model),
            )

            self.assertEqual(receipt["status"], "failed-manual-review")
            self.assertEqual(receipt["failure"], {"code": "studio-transcription-span-range-invalid"})
            self.assertEqual(receipt["qualitySignals"], {})
            temporary_wav = Path((root / "wav.txt").read_text(encoding="utf-8"))
            self.assertFalse(temporary_wav.exists())

    def test_registry_rejects_unapproved_mismatch_and_outside_paths_before_media_commands(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            source.write_bytes(b"synthetic-media")
            model = root / "model.bin"
            model.write_bytes(b"synthetic-model")
            invoked = root / "media-command-ran"
            forbidden = executable(
                root,
                "forbidden",
                f"from pathlib import Path\nPath({str(invoked)!r}).write_text('ran')",
            )

            for case, expected_code in (
                ("unapproved", "studio-model-artifact-unapproved"),
                ("digest", "studio-model-artifact-digest-mismatch"),
                ("outside", "studio-model-artifact-path-outside-manifest"),
            ):
                with self.subTest(case=case):
                    registry = approved_registry(root, forbidden, model)
                    lock_path = Path(registry["model_lock_path"])
                    lock = json.loads(lock_path.read_text(encoding="utf-8"))
                    configured_whisper = forbidden
                    if case == "unapproved":
                        lock["artifacts"][0]["approved"] = False
                        lock["artifacts"][0]["enabled"] = False
                    elif case == "digest":
                        lock["artifacts"][1]["sources"][0]["files"][0]["sha256"] = "0" * 64
                    else:
                        configured_whisper = executable(root, "outside-whisper", "pass")
                    lock_path.write_text(json.dumps(lock), encoding="utf-8")

                    receipt = analyze_transcript(
                        source_job(source),
                        str(forbidden),
                        str(forbidden),
                        str(configured_whisper),
                        str(model),
                        **registry,
                    )
                    self.assertEqual(receipt["status"], "failed-manual-review")
                    self.assertEqual(receipt["failure"], {"code": expected_code})
                    self.assertFalse(invoked.exists())

    def test_registry_rejects_removed_binary_execute_permission_before_media_commands(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            source.write_bytes(b"synthetic-media")
            model = root / "model.bin"
            model.write_bytes(b"synthetic-model")
            invoked = root / "media-command-ran"
            forbidden = executable(
                root,
                "forbidden",
                f"from pathlib import Path\nPath({str(invoked)!r}).write_text('ran')",
            )
            whisper = executable(root, "whisper", "pass")
            registry = approved_registry(root, whisper, model)
            whisper.chmod(0o600)

            receipt = analyze_transcript(
                source_job(source),
                str(forbidden),
                str(forbidden),
                str(whisper),
                str(model),
                **registry,
            )

            self.assertEqual(receipt["status"], "failed-manual-review")
            self.assertEqual(
                receipt["failure"],
                {"code": "studio-model-artifact-permission-mismatch"},
            )
            self.assertFalse(invoked.exists())

    def test_kills_and_reaps_whisper_when_stdout_exceeds_the_incremental_cap(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            whisper = shell_overflow_executable(root, "overflow-whisper", "stdout")
            spawned: list[int] = []
            real_popen = subprocess.Popen

            def observing_popen(*args, **kwargs):
                process = real_popen(*args, **kwargs)
                spawned.append(process.pid)
                return process

            started = time.monotonic()
            with (
                patch.object(transcription.subprocess, "Popen", observing_popen),
                patch.object(transcription, "COMMAND_TIMEOUT_SECONDS", 1),
            ):
                with self.assertRaisesRegex(
                    transcription.MediaAnalysisError,
                    "^studio-transcription-whisper-stdout-overflow$",
                ):
                    transcription._run_bounded_command([str(whisper)], "whisper")
            elapsed = time.monotonic() - started

            self.assertLess(elapsed, 1.5)
            with self.assertRaises(ProcessLookupError):
                os.kill(spawned[-1], 0)

    def test_kills_and_reaps_whisper_when_stderr_exceeds_the_incremental_cap(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            whisper = shell_overflow_executable(root, "overflow-whisper-stderr", "stderr")
            spawned: list[int] = []
            real_popen = subprocess.Popen

            def observing_popen(*args, **kwargs):
                process = real_popen(*args, **kwargs)
                spawned.append(process.pid)
                return process

            with (
                patch.object(transcription.subprocess, "Popen", observing_popen),
                patch.object(transcription, "COMMAND_TIMEOUT_SECONDS", 1),
            ):
                with self.assertRaisesRegex(
                    transcription.MediaAnalysisError,
                    "^studio-transcription-whisper-stderr-overflow$",
                ):
                    transcription._run_bounded_command([str(whisper)], "whisper")
            with self.assertRaises(ProcessLookupError):
                os.kill(spawned[-1], 0)

    def test_kills_and_reaps_whisper_on_timeout(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            whisper = executable(root, "timeout-whisper", "import time\ntime.sleep(2)")
            spawned: list[int] = []
            real_popen = subprocess.Popen

            def observing_popen(*args, **kwargs):
                process = real_popen(*args, **kwargs)
                spawned.append(process.pid)
                return process

            started = time.monotonic()
            with (
                patch.object(transcription.subprocess, "Popen", observing_popen),
                patch.object(transcription, "COMMAND_TIMEOUT_SECONDS", 0.2),
            ):
                with self.assertRaisesRegex(
                    transcription.MediaAnalysisError,
                    "^studio-transcription-whisper-timeout$",
                ):
                    transcription._run_bounded_command([str(whisper)], "whisper")
            self.assertLess(time.monotonic() - started, 1.5)
            with self.assertRaises(ProcessLookupError):
                os.kill(spawned[-1], 0)


if __name__ == "__main__":
    unittest.main()
