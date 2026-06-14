from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

import orjson

from src.config import Settings


logger = logging.getLogger(__name__)

DB_INLINE_JSON_MAX_BYTES = 256 * 1024
PAYLOAD_REFERENCE_STORAGE = "file"


def _json_bytes(payload: Any) -> bytes:
    return orjson.dumps(payload, option=orjson.OPT_SORT_KEYS)


def payload_size_bytes(payload: Any) -> int:
    return len(_json_bytes(payload))


def write_json_payload_to_file(payload: Any, target_path: Path) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target_path.with_name(f".{target_path.name}.tmp")
    tmp_path.write_bytes(_json_bytes(payload))
    tmp_path.replace(target_path)


def compute_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_payload_reference(path: Path, sha256: str, size_bytes: int, schema: str) -> dict[str, Any]:
    return {
        "storage": PAYLOAD_REFERENCE_STORAGE,
        "path": str(path),
        "sha256": sha256,
        "size_bytes": size_bytes,
        "schema": schema,
    }


def is_payload_reference(raw_value: Any) -> bool:
    return (
        isinstance(raw_value, dict)
        and raw_value.get("storage") == PAYLOAD_REFERENCE_STORAGE
        and isinstance(raw_value.get("path"), str)
    )


def _reference_log_context(table: str | None, column: str | None) -> tuple[str, str]:
    return table or "unknown", column or "unknown"


def resolve_payload_reference(
    raw_value: Any,
    *,
    settings: Settings | None = None,
    table: str | None = None,
    column: str | None = None,
) -> Any:
    if not is_payload_reference(raw_value):
        return raw_value

    log_table, log_column = _reference_log_context(table, column)
    path = Path(raw_value["path"]).expanduser()
    if not path.is_absolute() and settings is not None:
        parts = path.parts
        if "runtime_cache" in parts:
            path = settings.runtime_cache_dir.joinpath(*parts[parts.index("runtime_cache") + 1 :])
        else:
            path = settings.runtime_cache_dir / path
    path = path.resolve()
    if not path.exists():
        logger.error(
            "DB_PAYLOAD_REFERENCE_FAILED table=%s column=%s reason=%s path=%s",
            log_table,
            log_column,
            "missing_file",
            path,
        )
        raise FileNotFoundError(f"Referenced DB payload file is missing: {path}")

    expected_sha256 = raw_value.get("sha256")
    if isinstance(expected_sha256, str):
        actual_sha256 = compute_sha256(path)
        if actual_sha256 != expected_sha256:
            logger.error(
                "DB_PAYLOAD_REFERENCE_FAILED table=%s column=%s reason=%s path=%s",
                log_table,
                log_column,
                "sha256_mismatch",
                path,
            )
            raise ValueError(f"Referenced DB payload checksum mismatch: {path}")

    logger.info("DB_PAYLOAD_REFERENCE_RESOLVED table=%s column=%s path=%s", log_table, log_column, path)
    return orjson.loads(path.read_bytes())


def payload_storage_path(
    settings: Settings,
    *,
    table: str,
    column: str,
    key: str,
    filename: str = "payload.json",
) -> Path:
    safe_key = "".join(character if character.isalnum() or character in {"-", "_"} else "_" for character in key)
    return settings.runtime_cache_dir / "db_payloads" / table / column / safe_key / filename


def externalize_payload_if_needed(
    payload: Any,
    *,
    settings: Settings,
    table: str,
    column: str,
    schema: str,
    target_path: Path,
    force_externalize: bool = False,
) -> Any:
    size_bytes = payload_size_bytes(payload)
    threshold = settings.db_inline_json_max_bytes or DB_INLINE_JSON_MAX_BYTES
    if not force_externalize and size_bytes <= threshold:
        logger.info("DB_PAYLOAD_INLINE table=%s column=%s sizeBytes=%s", table, column, size_bytes)
        return payload

    write_json_payload_to_file(payload, target_path)
    sha256 = compute_sha256(target_path)
    reference = build_payload_reference(target_path, sha256, size_bytes, schema)
    logger.info(
        "DB_PAYLOAD_EXTERNALIZED table=%s column=%s sizeBytes=%s path=%s sha256=%s",
        table,
        column,
        size_bytes,
        target_path,
        sha256,
    )
    return reference
