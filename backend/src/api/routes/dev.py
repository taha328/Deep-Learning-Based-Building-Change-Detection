from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, ConfigDict

from src.api.deps import get_app_settings


router = APIRouter()
logger = logging.getLogger(__name__)
_MAX_RELAY_PAYLOAD_BYTES = 20_000
_ALLOWED_EVENT_PREFIXES = (
    "TEMPORAL_REFERENCE_",
    "TEMPORAL_ADDED_",
    "TEMPORAL_OUTPUT_",
    "TEMPORAL_ACTIVE_",
    "TEMPORAL_VECTOR_",
    "TEMPORAL_VECTOR_TILE_",
    "TEMPORAL_GEOJSON_",
    "TEMPORAL_BASELINE_",
    "TEMPORAL_EMPTY_BASELINE_",
    "TEMPORAL_RENDER_",
    "TEMPORAL_STALE_PROJECT_",
    "TEMPORAL_SCREENSHOT_",
    "RUN_CACHE_POLL_",
    "REFERENCE_LAYER_PANEL_",
)


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
    if not body.event.startswith(_ALLOWED_EVENT_PREFIXES):
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    logger.info(
        "CLIENT_LOG event=%s payload=%s",
        body.event,
        _serialize_payload(body.payload),
    )
    try:
        log_dir = settings.runtime_cache_dir / "dev_client_logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / "client_log.ndjson").open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "received_at": datetime.now(UTC).isoformat(),
                        "event": body.event,
                        "payload": body.payload,
                        "timestamp": body.timestamp,
                        "source": body.source,
                    },
                    ensure_ascii=False,
                    default=str,
                    separators=(",", ":"),
                )
                + "\n"
            )
    except Exception:  # pragma: no cover - dev diagnostics must never break UI logging
        logger.debug("CLIENT_LOG_PERSIST_FAILED event=%s", body.event, exc_info=True)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
