import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


WORKER_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKER_ROOT))

from stage_telemetry import StageTelemetryWriter, VISION_STAGE_ORDER  # noqa: E402
import worker  # noqa: E402


EXPECTED_STAGES = (
    "runtime-preaccept",
    "frontstage-extraction",
    "model-load",
    "processor-prepare",
    "model-generation",
    "output-validation",
)


def capture_fd_three(action):
    saved_fd = None
    try:
        saved_fd = os.dup(3)
    except OSError:
        pass
    read_fd, write_fd = os.pipe()
    if read_fd == 3:
        replacement_read_fd = os.dup(read_fd)
        os.close(read_fd)
        read_fd = replacement_read_fd
    os.dup2(write_fd, 3)
    os.close(write_fd)
    try:
        action()
        os.close(3)
        chunks = []
        while True:
            chunk = os.read(read_fd, 4096)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        try:
            os.close(3)
        except OSError:
            pass
        os.close(read_fd)
        if saved_fd is not None:
            os.dup2(saved_fd, 3)
            os.close(saved_fd)


class StageTelemetryWriterTest(unittest.TestCase):
    def test_emits_exact_canonical_six_stage_sequence(self):
        def emit():
            writer = StageTelemetryWriter.from_fd(3)
            for stage in EXPECTED_STAGES:
                writer.started(stage)
                writer.completed(stage)

        payload = capture_fd_three(emit)
        lines = payload.splitlines(keepends=True)

        self.assertEqual(VISION_STAGE_ORDER, EXPECTED_STAGES)
        self.assertEqual(len(lines), 12)
        self.assertTrue(all(line.endswith(b"\n") for line in lines))
        self.assertEqual(
            lines[0],
            b'{"schemaVersion":1,"sequence":1,"stage":"runtime-preaccept","state":"started"}\n',
        )
        self.assertEqual(
            lines[-1],
            b'{"schemaVersion":1,"sequence":12,"stage":"output-validation","state":"completed"}\n',
        )
        events = [json.loads(line) for line in lines]
        self.assertEqual([event["sequence"] for event in events], list(range(1, 13)))
        self.assertEqual(
            [(event["stage"], event["state"]) for event in events],
            [
                pair
                for stage in EXPECTED_STAGES
                for pair in ((stage, "started"), (stage, "completed"))
            ],
        )

    def test_rejects_non_three_fd_and_unknown_duplicate_or_out_of_order_stage(self):
        with self.assertRaisesRegex(ValueError, "^studio-model-worker-telemetry-invalid$"):
            StageTelemetryWriter.from_fd(4)

        for invalid_value in ("4", "/Users/test-user/private.mov"):
            with self.subTest(invalid_value=invalid_value):
                stderr = __import__("io").StringIO()
                with (
                    patch.object(
                        sys,
                        "argv",
                        ["worker.py", "--telemetry-fd", invalid_value],
                    ),
                    patch.object(sys, "stdin", __import__("io").StringIO("")),
                    patch.object(sys, "stderr", stderr),
                ):
                    self.assertEqual(worker.main(), 2)
                self.assertEqual(
                    stderr.getvalue(),
                    "studio-model-worker-telemetry-invalid\n",
                )

        def assert_invalid(actions):
            def emit():
                writer = StageTelemetryWriter.from_fd(3)
                with self.assertRaisesRegex(
                    ValueError,
                    "^studio-model-worker-telemetry-invalid$",
                ):
                    actions(writer)

            capture_fd_three(emit)

        assert_invalid(lambda writer: writer.started("unknown-stage"))
        assert_invalid(lambda writer: writer.started("frontstage-extraction"))

        def duplicate(writer):
            writer.started("runtime-preaccept")
            writer.started("runtime-preaccept")

        assert_invalid(duplicate)

        def skipped_completion(writer):
            writer.started("runtime-preaccept")
            writer.completed("frontstage-extraction")

        assert_invalid(skipped_completion)

    def test_disabled_writer_emits_nothing(self):
        writer = StageTelemetryWriter.from_fd(None)
        for stage in EXPECTED_STAGES:
            writer.started(stage)
            writer.completed(stage)

        self.assertFalse(writer.enabled)

    def test_event_has_only_fixed_keys_and_never_accepts_content(self):
        def emit():
            writer = StageTelemetryWriter.from_fd(3)
            with self.assertRaises(TypeError):
                writer.started("runtime-preaccept", "/Users/test-user/private.mov")
            writer.started("runtime-preaccept")

        payload = capture_fd_three(emit)
        event = json.loads(payload)

        self.assertEqual(
            set(event),
            {"schemaVersion", "sequence", "stage", "state"},
        )
        self.assertNotIn(b"customer", payload)
        self.assertNotIn(b"private.mov", payload)


if __name__ == "__main__":
    unittest.main()
