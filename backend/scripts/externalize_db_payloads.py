#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import orjson
from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from src.config import Settings, get_settings
from src.db.session import get_session_factory
from src.repositories.payload_storage import (
    build_payload_reference,
    compute_sha256,
    is_payload_reference,
    resolve_payload_reference,
    write_json_payload_to_file,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PayloadTarget:
    table: str
    column: str
    pk_column: str
    schema: str = "db_payload_externalized_v1"


@dataclass
class MigrationSummary:
    scanned_rows: int = 0
    skipped_rows: int = 0
    migrated_rows: int = 0
    failed_rows: int = 0
    bytes_externalized: int = 0
    files_written: int = 0

    def add(self, result: str, *, size_bytes: int = 0, file_written: bool = False) -> None:
        self.scanned_rows += 1
        if result in {"skipped", "dry_run"}:
            self.skipped_rows += 1
        elif result == "migrated":
            self.migrated_rows += 1
            self.bytes_externalized += size_bytes
            if file_written:
                self.files_written += 1
        elif result == "failed":
            self.failed_rows += 1


@dataclass(frozen=True)
class RowResult:
    result: str
    reason: str
    path: Path | None = None
    reference: dict[str, Any] | None = None
    size_bytes: int = 0
    file_written: bool = False


PAYLOAD_TARGETS: dict[str, PayloadTarget] = {
    "projects": PayloadTarget("projects", "raw_payload", "id"),
    "milestones": PayloadTarget("milestones", "raw_payload", "id"),
    "runs": PayloadTarget("runs", "raw_response", "id"),
    "jobs": PayloadTarget("jobs", "raw_result", "id"),
    "geometry_layers": PayloadTarget("geometry_layers", "geojson", "id"),
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Externalize large inline JSONB payloads from hot DB rows into files.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Scan and print candidates without writing files or DB rows.")
    mode.add_argument("--apply", action="store_true", help="Externalize candidates and update DB rows.")
    mode.add_argument("--verify-only", action="store_true", help="Validate existing file references without migrating inline payloads.")
    parser.add_argument("--table", choices=tuple(PAYLOAD_TARGETS), help="Limit work to one target table.")
    parser.add_argument("--limit", type=int, help="Maximum rows to scan per target table.")
    parser.add_argument("--min-bytes", type=int, default=256 * 1024, help="Minimum JSONB column size to externalize.")
    parser.add_argument("--batch-size", type=int, default=100, help="Commit interval for apply mode.")
    parser.add_argument("--resume-safe", action="store_true", help="Accepted for explicit resumable runs; script is always idempotent.")
    parser.add_argument("--database-url", help="Override DATABASE_URL.")
    return parser.parse_args(argv)


def _json_bytes(payload: Any) -> bytes:
    return orjson.dumps(payload, option=orjson.OPT_SORT_KEYS)


def _payload_sha256(payload: Any) -> str:
    import hashlib

    return hashlib.sha256(_json_bytes(payload)).hexdigest()


def _target_path(settings: Settings, target: PayloadTarget, pk: Any, *, suffix: str | None = None) -> Path:
    filename = f"{pk}.json" if suffix is None else f"{pk}-{suffix}.json"
    return settings.runtime_cache_dir / "db_payloads" / target.table / target.column / filename


def _reference_for_file(target: PayloadTarget, pk: Any, path: Path, size_bytes: int) -> dict[str, Any]:
    reference = build_payload_reference(path, compute_sha256(path), size_bytes, target.schema)
    reference.update(
        {
            "source_table": target.table,
            "source_column": target.column,
            "source_pk": str(pk),
        }
    )
    return reference


def _path_for_payload(settings: Settings, target: PayloadTarget, pk: Any, payload: Any) -> tuple[Path, bool]:
    target_path = _target_path(settings, target, pk)
    payload_sha = _payload_sha256(payload)
    if not target_path.exists():
        return target_path, True
    if compute_sha256(target_path) == payload_sha:
        return target_path, False
    return _target_path(settings, target, pk, suffix=payload_sha[:12]), True


def _validate_reference(raw_value: Any, target: PayloadTarget) -> None:
    resolve_payload_reference(raw_value, table=target.table, column=target.column)


def process_payload_row(
    *,
    target: PayloadTarget,
    pk: Any,
    raw_value: Any,
    size_bytes: int,
    settings: Settings,
    min_bytes: int,
    mode: str,
    update_reference: Callable[[dict[str, Any]], None] | None = None,
) -> RowResult:
    if raw_value is None:
        logger.info("DB_PAYLOAD_MIGRATION_SKIPPED table=%s column=%s pk=%s reason=null", target.table, target.column, pk)
        return RowResult("skipped", "null", size_bytes=size_bytes)

    if is_payload_reference(raw_value):
        try:
            _validate_reference(raw_value, target)
        except Exception as exc:
            logger.error(
                "DB_PAYLOAD_MIGRATION_FAILED table=%s column=%s pk=%s reason=%s",
                target.table,
                target.column,
                pk,
                exc,
            )
            return RowResult("failed", str(exc), size_bytes=size_bytes)
        logger.info(
            "DB_PAYLOAD_MIGRATION_SKIPPED table=%s column=%s pk=%s reason=already_externalized",
            target.table,
            target.column,
            pk,
        )
        return RowResult("skipped", "already_externalized", size_bytes=size_bytes)

    if mode == "verify_only":
        logger.info("DB_PAYLOAD_MIGRATION_SKIPPED table=%s column=%s pk=%s reason=inline_value", target.table, target.column, pk)
        return RowResult("skipped", "inline_value", size_bytes=size_bytes)

    if size_bytes < min_bytes:
        logger.info("DB_PAYLOAD_MIGRATION_SKIPPED table=%s column=%s pk=%s reason=below_threshold sizeBytes=%s", target.table, target.column, pk, size_bytes)
        return RowResult("skipped", "below_threshold", size_bytes=size_bytes)

    path, should_write = _path_for_payload(settings, target, pk, raw_value)
    if mode == "dry_run":
        logger.info(
            "DB_PAYLOAD_MIGRATION_DRY_RUN table=%s column=%s pk=%s sizeBytes=%s path=%s",
            target.table,
            target.column,
            pk,
            size_bytes,
            path,
        )
        return RowResult("dry_run", "would_migrate", path=path, size_bytes=size_bytes)

    if mode != "apply":
        raise ValueError(f"Unsupported migration mode: {mode}")

    if should_write:
        write_json_payload_to_file(raw_value, path)
        logger.info(
            "DB_PAYLOAD_MIGRATION_WRITE table=%s column=%s pk=%s sizeBytes=%s path=%s sha256=%s",
            target.table,
            target.column,
            pk,
            size_bytes,
            path,
            compute_sha256(path),
        )

    reference = _reference_for_file(target, pk, path, size_bytes)
    _validate_reference(reference, target)
    if update_reference is not None:
        update_reference(reference)
    logger.info(
        "DB_PAYLOAD_MIGRATION_UPDATED table=%s column=%s pk=%s sizeBytes=%s path=%s",
        target.table,
        target.column,
        pk,
        size_bytes,
        path,
    )
    return RowResult(
        "migrated",
        "externalized",
        path=path,
        reference=reference,
        size_bytes=size_bytes,
        file_written=should_write,
    )


def _select_rows(session: Session, target: PayloadTarget, *, limit: int | None, min_bytes: int, verify_only: bool):
    where_clause = f"{target.column} IS NOT NULL"
    if verify_only:
        where_clause += f" AND {target.column}->>'storage' = 'file'"
    else:
        where_clause += f" AND ({target.column}->>'storage' IS NULL OR {target.column}->>'storage' <> 'file')"
        where_clause += f" AND pg_column_size({target.column}) >= :min_bytes"
    limit_clause = " LIMIT :limit" if limit is not None else ""
    statement = text(
        f"""
        SELECT {target.pk_column} AS pk, {target.column} AS payload, pg_column_size({target.column}) AS size_bytes
        FROM public.{target.table}
        WHERE {where_clause}
        ORDER BY {target.pk_column}
        {limit_clause}
        """
    )
    params: dict[str, Any] = {"min_bytes": min_bytes}
    if limit is not None:
        params["limit"] = limit
    return session.execute(statement, params).all()


def _update_row(session: Session, target: PayloadTarget, pk: Any, reference: dict[str, Any]) -> None:
    statement = (
        text(f"UPDATE public.{target.table} SET {target.column} = :reference WHERE {target.pk_column} = :pk")
        .bindparams(bindparam("reference", type_=JSONB))
    )
    session.execute(statement, {"reference": reference, "pk": pk})


def _selected_targets(table: str | None) -> list[PayloadTarget]:
    if table:
        return [PAYLOAD_TARGETS[table]]
    return list(PAYLOAD_TARGETS.values())


def run(args: argparse.Namespace, *, settings: Settings | None = None) -> MigrationSummary:
    resolved_settings = settings or get_settings()
    if args.database_url:
        resolved_settings.database_url = args.database_url
    mode = "verify_only" if args.verify_only else "apply" if args.apply else "dry_run"
    summary = MigrationSummary()
    session = get_session_factory(resolved_settings)()
    processed_since_commit = 0
    try:
        for target in _selected_targets(args.table):
            rows = _select_rows(session, target, limit=args.limit, min_bytes=args.min_bytes, verify_only=args.verify_only)
            for row in rows:
                pk = row.pk

                def update_reference(reference: dict[str, Any], *, target=target, pk=pk) -> None:
                    _update_row(session, target, pk, reference)

                result = process_payload_row(
                    target=target,
                    pk=pk,
                    raw_value=row.payload,
                    size_bytes=int(row.size_bytes or 0),
                    settings=resolved_settings,
                    min_bytes=args.min_bytes,
                    mode=mode,
                    update_reference=update_reference if args.apply else None,
                )
                summary.add(result.result, size_bytes=result.size_bytes, file_written=result.file_written)
                if args.apply and result.result == "migrated":
                    processed_since_commit += 1
                    if processed_since_commit >= args.batch_size:
                        session.commit()
                        processed_since_commit = 0
        if args.apply:
            session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
    logger.info(
        "DB_PAYLOAD_MIGRATION_SUMMARY scannedRows=%s skippedRows=%s migratedRows=%s failedRows=%s bytesExternalized=%s filesWritten=%s",
        summary.scanned_rows,
        summary.skipped_rows,
        summary.migrated_rows,
        summary.failed_rows,
        summary.bytes_externalized,
        summary.files_written,
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    args = parse_args(argv)
    summary = run(args)
    print(
        "DB_PAYLOAD_MIGRATION_SUMMARY "
        f"scanned_rows={summary.scanned_rows} "
        f"skipped_rows={summary.skipped_rows} "
        f"migrated_rows={summary.migrated_rows} "
        f"failed_rows={summary.failed_rows} "
        f"bytes_externalized={summary.bytes_externalized} "
        f"files_written={summary.files_written}"
    )
    return 1 if summary.failed_rows else 0


if __name__ == "__main__":
    sys.exit(main())
