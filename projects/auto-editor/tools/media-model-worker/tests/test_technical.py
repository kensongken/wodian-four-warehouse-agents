from subprocess_fixture import shell_overflow_executable
import hashlib
import json
import os
import platform
import subprocess
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
WORKER_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKER_ROOT))

import technical  # noqa: E402
from technical import TechnicalConfigurationError, analyze_scene, analyze_technical  # noqa: E402


FFMPEG_PATH = (
    REPOSITORY_ROOT
    / "node_modules/.pnpm/ffmpeg-static@5.2.0/node_modules/ffmpeg-static/ffmpeg"
).resolve()


def bundled_ffprobe_path() -> Path:
    system = {"Darwin": "darwin", "Linux": "linux", "Windows": "win32"}[platform.system()]
    machine = platform.machine().lower()
    architecture = "arm64" if machine in {"arm64", "aarch64"} else "x64"
    executable = "ffprobe.exe" if system == "win32" else "ffprobe"
    return (
        REPOSITORY_ROOT
        / "node_modules/.pnpm/ffprobe-static@3.1.0/node_modules/ffprobe-static/bin"
        / system
        / architecture
        / executable
    ).resolve()


FFPROBE_PATH = bundled_ffprobe_path()


def make_fixture(root: Path, cuts: list[tuple[str, int]]) -> Path:
    output = root / "fixture.mp4"
    arguments = [str(FFMPEG_PATH), "-hide_banner", "-loglevel", "error"]
    for color, seconds in cuts:
        arguments.extend([
            "-f", "lavfi", "-i", f"color=c={color}:s=160x120:r=10:d={seconds}",
        ])
    inputs = "".join(f"[{index}:v]" for index in range(len(cuts)))
    arguments.extend([
        "-filter_complex", f"{inputs}concat=n={len(cuts)}:v=1:a=0[out]",
        "-map", "[out]", "-c:v", "libx264", "-preset", "ultrafast",
        "-pix_fmt", "yuv420p", "-y", str(output),
    ])
    subprocess.run(arguments, check=True, capture_output=True, timeout=20)
    return output


def make_fake_ffprobe(root: Path, name: str, body: str) -> Path:
    executable = root / name
    executable.write_text(f"#!{sys.executable}\n{body}\n", encoding="utf-8")
    executable.chmod(0o700)
    return executable.resolve()


def valid_job(source: Path, **overrides):
    duration_ms = overrides.pop("sourceEndMs", 6000)
    job = {
        "schemaVersion": 1,
        "jobId": "job-technical-1",
        "customerId": "customer-a",
        "sessionId": "session-a",
        "sourceAssetId": "asset-a",
        "sourceDigest": hashlib.sha256(source.read_bytes()).hexdigest(),
        "sourcePath": str(source.resolve()),
        "sourceStartMs": 0,
        "sourceEndMs": duration_ms,
        "layer": "technical",
        "operationKey": "technical-fixture-v1",
        "requestedAt": "2026-09-12T05:00:00.000Z",
    }
    job.update(overrides)
    return job


class TechnicalAnalysisTest(unittest.TestCase):
    def test_reports_only_verifiable_technical_facts(self):
        with TemporaryDirectory() as directory:
            video = make_fixture(Path(directory), [("red", 2), ("blue", 2), ("green", 2)])
            job = valid_job(video)
            receipt = analyze_technical(job, str(FFPROBE_PATH))

        self.assertEqual(receipt["status"], "succeeded")
        self.assertEqual(receipt["providerMode"], "local")
        self.assertEqual(receipt["suggestedTags"], [])
        self.assertEqual(receipt["failure"], None)
        self.assertEqual(receipt["sourceStartMs"], 0)
        self.assertEqual(receipt["sourceEndMs"], 6000)
        self.assertEqual(
            receipt["inputDigest"],
            hashlib.sha256(json.dumps(
                job,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")).hexdigest(),
        )
        self.assertEqual(receipt["observations"], [
            "duration-ms:6000",
            "video-codec:h264",
            "audio-stream:absent",
        ])
        self.assertEqual(receipt["warnings"], [])
        self.assertEqual(set(receipt["qualitySignals"]), {
            "durationMs",
            "hasVideo",
            "hasAudio",
            "width",
            "height",
            "videoCodec",
            "audioCodec",
            "blackRatio",
            "freezeRatio",
            "blurScore",
        })
        self.assertEqual(receipt["qualitySignals"]["hasVideo"], True)
        self.assertEqual(receipt["qualitySignals"]["hasAudio"], False)
        self.assertEqual(receipt["qualitySignals"]["width"], 160)
        self.assertEqual(receipt["qualitySignals"]["height"], 120)
        self.assertEqual(receipt["qualitySignals"]["durationMs"], 6000)
        self.assertGreaterEqual(receipt["qualitySignals"]["blackRatio"], 0)
        self.assertGreaterEqual(receipt["qualitySignals"]["freezeRatio"], 0)
        self.assertGreaterEqual(receipt["qualitySignals"]["blurScore"], 0)

    def test_uses_half_up_millisecond_rounding_for_ffprobe_duration(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            video = make_fixture(root, [("red", 2)])
            payload = json.dumps({
                "format": {"duration": "1.2345"},
                "streams": [{
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 160,
                    "height": 120,
                }],
            })
            probe = make_fake_ffprobe(
                root,
                "rounding-ffprobe",
                f"import sys\nsys.stdout.write({payload!r})",
            )
            receipt = analyze_technical(
                valid_job(video, sourceEndMs=1235),
                str(probe),
            )

        self.assertEqual(receipt["status"], "succeeded")
        self.assertEqual(receipt["qualitySignals"]["durationMs"], 1235)

    def test_scene_receipt_binds_every_frame_to_the_exact_job_range(self):
        with TemporaryDirectory() as directory:
            video = make_fixture(Path(directory), [("red", 2), ("blue", 2), ("green", 2)])
            job = valid_job(
                video,
                sourceStartMs=1000,
                sourceEndMs=5000,
                layer="scene",
                jobId="job-scene-1",
            )
            receipt = analyze_scene(job, str(FFPROBE_PATH))
            scenes = receipt["qualitySignals"]["scenes"]

        self.assertEqual(receipt["status"], "succeeded")
        self.assertEqual(receipt["sourceStartMs"], 1000)
        self.assertEqual(receipt["sourceEndMs"], 5000)
        self.assertEqual(len(scenes), 3)
        self.assertTrue(all(
            1000 <= row["startMs"] < row["representativeFrameMs"] < row["endMs"] <= 5000
            for row in scenes
        ))
        self.assertTrue(all(
            row["endMs"] <= min(receipt["sourceEndMs"], receipt["qualitySignals"]["durationMs"])
            for row in scenes
        ))
        self.assertTrue(all(row["endMs"] - row["startMs"] >= 600 for row in scenes))

    def test_rejects_every_job_range_past_media_duration_for_both_layers(self):
        with TemporaryDirectory() as directory:
            video = make_fixture(Path(directory), [("red", 2), ("blue", 2), ("green", 2)])
            for excess_ms in (1, 49, 50):
                for layer, analyze in (("technical", analyze_technical), ("scene", analyze_scene)):
                    with self.subTest(excess_ms=excess_ms, layer=layer):
                        receipt = analyze(
                            valid_job(
                                video,
                                sourceEndMs=6000 + excess_ms,
                                layer=layer,
                                jobId=f"job-{layer}-over-{excess_ms}",
                            ),
                            str(FFPROBE_PATH),
                        )
                        self.assertEqual(receipt["status"], "failed-manual-review")
                        self.assertEqual(
                            receipt["failure"],
                            {"code": "studio-technical-source-range-invalid"},
                        )

    def test_generated_fixture_analysis_is_stable_across_two_runs(self):
        with TemporaryDirectory() as directory:
            video = make_fixture(Path(directory), [("red", 2), ("blue", 2), ("green", 2)])
            job = valid_job(video, layer="scene", jobId="job-scene-stable")
            first = analyze_scene(job, str(FFPROBE_PATH))
            second = analyze_scene(job, str(FFPROBE_PATH))

        self.assertEqual(first["qualitySignals"], second["qualitySignals"])
        self.assertEqual(first["observations"], second["observations"])
        self.assertEqual(first["warnings"], second["warnings"])

    def test_scene_range_shorter_than_minimum_fails_for_manual_review(self):
        with TemporaryDirectory() as directory:
            video = make_fixture(Path(directory), [("red", 1)])
            receipt = analyze_scene(
                valid_job(
                    video,
                    sourceEndMs=500,
                    layer="scene",
                    jobId="job-scene-too-short",
                ),
                str(FFPROBE_PATH),
            )

        self.assertEqual(receipt["status"], "failed-manual-review")
        self.assertEqual(
            receipt["failure"],
            {"code": "studio-technical-scene-interval-too-short"},
        )
        self.assertEqual(receipt["qualitySignals"], {})

    def test_corrupt_media_fails_manual_review_without_private_details(self):
        with TemporaryDirectory() as directory:
            broken = Path(directory) / "broken.mp4"
            broken.write_bytes(b"not-video")
            receipt = analyze_technical(
                valid_job(broken, sourceEndMs=1000),
                str(FFPROBE_PATH),
            )

        self.assertEqual(receipt["status"], "failed-manual-review")
        self.assertEqual(receipt["failure"], {"code": "studio-technical-media-invalid"})
        self.assertNotIn(str(broken), str(receipt["failure"]))
        self.assertNotIn("not-video", str(receipt["failure"]))
        self.assertNotIn(str(broken), str(receipt["warnings"]))

    def test_requires_an_absolute_configured_ffprobe_binary(self):
        with TemporaryDirectory() as directory:
            video = make_fixture(Path(directory), [("red", 2), ("blue", 2), ("green", 2)])
            with self.assertRaisesRegex(
                TechnicalConfigurationError,
                "^studio-technical-ffprobe-path-invalid$",
            ):
                analyze_technical(valid_job(video), "ffprobe")

    def test_kills_and_reaps_ffprobe_as_soon_as_stdout_exceeds_the_cap(self):
        self._assert_bounded_probe_failure(
            "import os, sys, time\n"
            "sys.stdout.buffer.write(b'x' * (70 * 1024))\n"
            "sys.stdout.flush()\n"
            "time.sleep(2)",
            "studio-technical-ffprobe-stdout-overflow",
        )

    def test_kills_and_reaps_ffprobe_as_soon_as_stderr_exceeds_the_cap(self):
        self._assert_bounded_probe_failure(
            "import os, sys, time\n"
            "sys.stderr.buffer.write(b'x' * (70 * 1024))\n"
            "sys.stderr.flush()\n"
            "time.sleep(2)",
            "studio-technical-ffprobe-stderr-overflow",
        )

    def test_kills_and_reaps_continuously_writing_ffprobe_on_timeout(self):
        self._assert_bounded_probe_failure(
            "import os, sys, time\n"
            "deadline = time.monotonic() + 2\n"
            "while time.monotonic() < deadline:\n"
            "    sys.stdout.write('x')\n"
            "    sys.stdout.flush()\n"
            "    time.sleep(0.01)",
            "studio-technical-ffprobe-timeout",
            timeout_seconds=1,
        )

    def _assert_bounded_probe_failure(
        self,
        body: str,
        expected_code: str,
        *,
        timeout_seconds: float = 1,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            video = make_fixture(root, [("red", 1)])
            probe = (shell_overflow_executable(root, "bounded-ffprobe", "stderr" if "stderr-overflow" in expected_code else "stdout") if expected_code.endswith("-overflow") else make_fake_ffprobe(root, "bounded-ffprobe", body))
            spawned_pids: list[int] = []
            real_popen = subprocess.Popen

            def observing_popen(*args, **kwargs):
                process = real_popen(*args, **kwargs)
                spawned_pids.append(process.pid)
                return process

            started = time.monotonic()
            with (
                patch.object(technical.subprocess, "Popen", observing_popen),
                patch.object(technical, "PROBE_TIMEOUT_SECONDS", timeout_seconds),
            ):
                receipt = analyze_technical(valid_job(video, sourceEndMs=1000), str(probe))
            elapsed = time.monotonic() - started

            self.assertEqual(len(spawned_pids), 1)
            self.assertEqual(receipt["status"], "failed-manual-review")
            self.assertEqual(receipt["failure"], {"code": expected_code})
            self.assertLess(elapsed, 1.5)
            self.assertNotIn(str(root), json.dumps(receipt["failure"]))
            self.assertNotIn(str(root), json.dumps(receipt["warnings"]))
            with self.assertRaises(ProcessLookupError):
                os.kill(spawned_pids[0], 0)


if __name__ == "__main__":
    unittest.main()
