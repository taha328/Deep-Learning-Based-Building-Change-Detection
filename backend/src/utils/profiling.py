from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator


@dataclass
class StageTimings:
    values: dict[str, float] = field(default_factory=dict)

    @contextmanager
    def track(self, name: str) -> Iterator[None]:
        start = time.perf_counter()
        try:
            yield
        finally:
            self.values[name] = round(time.perf_counter() - start, 4)
