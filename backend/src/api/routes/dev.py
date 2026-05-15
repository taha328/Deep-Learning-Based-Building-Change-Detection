from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, ConfigDict

from src.api.deps import get_app_settings


router = APIRouter()
logger = logging.getLogger(__name__)
_MAX_RELAY_PAYLOAD_BYTES = 20_000
_ALLOWED_EVENT_PREFIX = "TEMPORAL_REFERENCE_"


class ClientLogRelayBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event: str
    payload: dict[str, Any]
    timestamp: str | None = None
    source: str | None = None


def _serialize_payload(payload: dict[str, Any]) -> str:
    serialized = json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":"))
    if len(serialized) <= _MAX_RELAY_PAYLOAD_BYTES:
        return serialized
    return serialized[: _MAX_RELAY_PAYLOAD_BYTES - 14] + '…"truncated"}'


@router.post("/client-log", status_code=status.HTTP_204_NO_CONTENT)
def relay_client_log(
    body: ClientLogRelayBody,
    settings=Depends(get_app_settings),
) -> Response:
    if not getattr(settings, "enable_client_log_relay", True):
        return Response(status_code=status.HTTP_404_NOT_FOUND)
    if not body.event.startswith(_ALLOWED_EVENT_PREFIX):
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    logger.info(
        "CLIENT_LOG event=%s payload=%s",
        body.event,
        _serialize_payload(body.payload),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
