"""Strict, content-free stage events for the dedicated formal-probe fd."""

from __future__ import annotations

import json
import os


VISION_STAGE_ORDER = (
    "runtime-preaccept",
    "frontstage-extraction",
    "model-load",
    "processor-prepare",
    "model-generation",
    "output-validation",
)

_INVALID = "studio-model-worker-telemetry-invalid"


class StageTelemetryError(ValueError):
    pass


class StageTelemetryWriter:
    def __init__(self, file_descriptor: int | None):
        self._file_descriptor = file_descriptor
        self._sequence = 0
        self._next_stage_index = 0
        self._active_stage: str | None = None

    @classmethod
    def from_fd(cls, value: int | None) -> "StageTelemetryWriter":
        if value is None:
            return cls(None)
        if type(value) is not int or value != 3:
            raise StageTelemetryError(_INVALID)
        try:
            os.fstat(value)
        except OSError as error:
            raise StageTelemetryError(_INVALID) from error
        return cls(value)

    @property
    def enabled(self) -> bool:
        return self._file_descriptor is not None

    def started(self, stage: str) -> None:
        if not self.enabled:
            return
        if (
            self._active_stage is not None
            or self._next_stage_index >= len(VISION_STAGE_ORDER)
            or stage != VISION_STAGE_ORDER[self._next_stage_index]
        ):
            raise StageTelemetryError(_INVALID)
        self._emit(stage, "started")
        self._active_stage = stage

    def completed(self, stage: str) -> None:
        if not self.enabled:
            return
        if self._active_stage != stage:
            raise StageTelemetryError(_INVALID)
        self._emit(stage, "completed")
        self._active_stage = None
        self._next_stage_index += 1

    def _emit(self, stage: str, state: str) -> None:
        self._sequence += 1
        event = {
            "schemaVersion": 1,
            "sequence": self._sequence,
            "stage": stage,
            "state": state,
        }
        payload = (
            json.dumps(event, ensure_ascii=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        offset = 0
        try:
            while offset < len(payload):
                written = os.write(self._file_descriptor, payload[offset:])
                if written <= 0:
                    raise OSError
                offset += written
        except OSError as error:
            raise StageTelemetryError(_INVALID) from error


DISABLED_STAGE_TELEMETRY = StageTelemetryWriter.from_fd(None)
