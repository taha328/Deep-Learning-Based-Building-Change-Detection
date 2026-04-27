from __future__ import annotations

from collections.abc import Sequence
from typing import Any


def model_json(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


def model_list_json(values: Sequence[Any]) -> list[Any]:
    return [model_json(value) for value in values]
