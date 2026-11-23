from subprocess_fixture import shell_overflow_executable
import math
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import numpy as np

WORKER_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKER_ROOT))

import technical
from technical import (
    MediaAnalysisError,
    classify_reference_boundaries,
    detect_audio_beat_evidence,
    detect_audio_beats,
)


class ReferenceEvidenceTests(unittest.TestCase):
    def test_reference_evidence_classifies_only_supported_transitions(self):
        evidence = classify_reference_boundaries(frame_fixture())

        self.assertEqual(
            [row["kind"] for row in evidence],
            ["cut", "fade", "crossfade", "unsupported"],
        )
        self.assertTrue(all(row["confidence"] >= 0.6 for row in evidence[:3]))

    def test_reference_audio_peaks_are_bounded_and_sorted(self):
        beats = detect_audio_beats(pcm_fixture(8), sample_rate=8000)

        self.assertEqual(beats, sorted(set(beats)))
        self.assertEqual(len(beats), 8)
        self.assertLessEqual(len(beats), 512)

    def test_reference_audio_peak_cap_is_fail_closed(self):
        beats, truncated = detect_audio_beat_evidence(pcm_fixture(700), sample_rate=8000)

        self.assertEqual(beats, sorted(set(beats)))
        self.assertEqual(len(beats), 512)
        self.assertTrue(truncated)

    def test_ffmpeg_pcm_decode_kills_and_reaps_on_timeout(self):
        self._assert_bounded_ffmpeg_failure(
            "import sys, time\n"
            "deadline = time.monotonic() + 10\n"
            "while time.monotonic() < deadline:\n"
            "    sys.stdout.write('x')\n"
            "    sys.stdout.flush()\n"
            "    time.sleep(0.01)",
            "studio-technical-ffmpeg-timeout",
            timeout_seconds=0.5,
        )

    def test_ffmpeg_pcm_decode_kills_and_reaps_on_output_overflow(self):
        self._assert_bounded_ffmpeg_failure(
            "import os, sys, time\n"
            "while True:\n"
            "    os.write(sys.stdout.fileno(), b'x' * 8192)\n"
            "    time.sleep(0.001)",
            "studio-technical-ffmpeg-stdout-overflow",
            max_pcm_bytes=128,
        )

    def _assert_bounded_ffmpeg_failure(
        self,
        body: str,
        expected_code: str,
        *,
        timeout_seconds: float = 2,
        max_pcm_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            executable = (shell_overflow_executable(root, "ffmpeg-bounded", "stdout") if expected_code.endswith("-overflow") else fake_executable(root, "ffmpeg-bounded", body))
            spawned_pids: list[int] = []
            real_popen = technical.subprocess.Popen

            def observing_popen(*args, **kwargs):
                process = real_popen(*args, **kwargs)
                spawned_pids.append(process.pid)
                return process

            with (
                patch.object(technical.subprocess, "Popen", observing_popen),
                patch.object(technical, "FFMPEG_TIMEOUT_SECONDS", timeout_seconds),
                patch.object(technical, "MAX_PCM_BYTES", max_pcm_bytes),
            ):
                with self.assertRaisesRegex(MediaAnalysisError, expected_code):
                    technical._run_bounded_ffmpeg([str(executable)])
            self.assertEqual(len(spawned_pids), 1)
            assert_reaped(spawned_pids[0])


def frame_fixture():
    grid = np.indices((24, 24)).sum(axis=0) % 2
    first = (grid * 180 + 30).astype(np.uint8)
    second = ((1 - grid) * 180 + 30).astype(np.uint8)
    blended = ((first.astype(np.uint16) + second.astype(np.uint16)) // 2).astype(np.uint8)
    return [
        {"atMs": 1000, "frames": [np.full((24, 24), 40, np.uint8), np.full((24, 24), 220, np.uint8)]},
        {"atMs": 2000, "frames": [
            np.full((24, 24), 200, np.uint8), np.full((24, 24), 90, np.uint8),
            np.full((24, 24), 5, np.uint8), np.full((24, 24), 100, np.uint8),
            np.full((24, 24), 210, np.uint8),
        ]},
        {"atMs": 3000, "frames": [first, ((3 * first + second) // 4).astype(np.uint8), blended,
                                      ((first + 3 * second) // 4).astype(np.uint8), second]},
        {"atMs": 4000, "frames": [np.full((24, 24), 100, np.uint8), np.full((24, 24), 102, np.uint8)]},
    ]


def pcm_fixture(count: int) -> bytes:
    sample_rate = 8000
    spacing = 0.3
    total_samples = math.ceil((count * spacing + 0.1) * sample_rate)
    samples = np.zeros(total_samples, dtype=np.int16)
    for index in range(count):
        start = round((index * spacing + 0.025) * sample_rate)
        samples[start:start + 100] = 24000
    return samples.astype("<i2", copy=False).tobytes()


def fake_executable(root: Path, name: str, body: str) -> Path:
    executable = root / name
    executable.write_text(f"#!{sys.executable}\n{body}\n", encoding="utf-8")
    executable.chmod(0o700)
    return executable.resolve()


def assert_reaped(pid: int) -> None:
    with unittest.TestCase().assertRaises(ProcessLookupError):
        os.kill(pid, 0)


if __name__ == "__main__":
    unittest.main()
