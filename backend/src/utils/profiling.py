from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator

from src.domain.stage_timing import StageTimingRecorder


@dataclass
class StageTimings:
    values: dict[str, float] = field(default_factory=dict)
    recorder: StageTimingRecorder | None = None
    stage_name_map: dict[str, str] = field(default_factory=dict)

    @contextmanager
    def track(self, name: str) -> Iterator[None]:
        start = time.perf_counter()
        mapped_name = self.stage_name_map.get(name, name)
        if self.recorder is None:
            try:
                yield
            finally:
                self.values[name] = round(time.perf_counter() - start, 4)
            return

        try:
            with self.recorder.stage(mapped_name):
                yield
        finally:
            self.values[name] = round(time.perf_counter() - start, 4)
