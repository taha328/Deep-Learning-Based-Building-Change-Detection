from __future__ import annotations

from typing import Any, NoReturn

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict


class ApiError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    details: dict[str, Any] | None = None


def raise_api_error(status_code: int, code: str, message: str, details: dict[str, Any] | None = None) -> NoReturn:
    raise HTTPException(
        status_code=status_code,
        detail={"error": ApiError(code=code, message=message, details=details).model_dump(mode="json")},
    )
