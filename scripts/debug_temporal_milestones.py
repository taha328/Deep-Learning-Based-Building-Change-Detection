#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


DEFAULT_PROJECT_ID = "temporal-tanger-mqnl74v1-azmvpg"
KNOWN_MARKERS = {
    DEFAULT_PROJECT_ID,
    "WB_2021_R04",
    "a424271fed0194d26445c325",
    "dc17aee16171d1cf88c7eeb7",
    "job-7b5f505167044816bcbda1601d2ed411",
}
TEXT_SUFFIXES = {".csv", ".json", ".jsonl", ".log", ".txt"}
SKIP_DIR_NAMES = {"tiles", "__pycache__", "node_modules", ".git", "dist", "vendor"}
MAX_TEXT_BYTES = 80 * 1024 * 1024
MILESTONE_RE = re.compile(r"\bWB_\d{4}_R\d+\b")
DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[T ][0-9:.+-]+Z?)?\b")


@dataclass(frozen=True)
class Evidence:
    category: str
    project_id: str
    milestone_id: str
    date: str
    label: str
    source_run_id: str
    request_hash: str
    found_in: str


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None


def as_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def first_str(data: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return as_str(value)
    return ""


def add_evidence(rows: list[Evidence], seen: set[tuple[str, str, str, str, str]], evidence: Evidence) -> None:
    key = (
        evidence.category,
        evidence.milestone_id,
        evidence.date,
        evidence.source_run_id or evidence.request_hash,
        evidence.found_in,
    )
    if evidence.milestone_id and key not in seen:
        seen.add(key)
        rows.append(evidence)


def normalize_found_in(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except Exception:
        return str(path)


def milestone_from_payload(
    *,
    payload: dict[str, Any],
    category: str,
    project_id: str,
    path: Path,
    root: Path,
    rows: list[Evidence],
    seen: set[tuple[str, str, str, str, str]],
    default_request_hash: str = "",
    default_source_run_id: str = "",
) -> None:
    milestone_id = first_str(
        payload,
        ("release_identifier", "releaseIdentifier", "identifier", "milestone", "milestoneId", "release_t2"),
    )
    if not MILESTONE_RE.fullmatch(milestone_id):
        return
    reference = payload.get("reference_imagery") if isinstance(payload.get("reference_imagery"), dict) else {}
    date = first_str(
        payload,
        ("release_date", "releaseDate", "date", "archive_date", "archiveDate", "capture_date", "captureDate"),
    )
    if not date and isinstance(reference, dict):
        date = first_str(reference, ("release_date", "releaseDate", "date", "archive_date", "capture_date"))
    add_evidence(
        rows,
        seen,
        Evidence(
            category=category,
            project_id=project_id,
            milestone_id=milestone_id,
            date=date or "UNKNOWN",
            label=first_str(payload, ("label", "display_label", "displayLabel", "name")) or "UNKNOWN",
            source_run_id=first_str(payload, ("sourceRunId", "source_run_id", "run_id", "runId")) or default_source_run_id,
            request_hash=first_str(payload, ("pair_request_hash", "pairRequestHash", "request_hash", "requestHash"))
            or default_request_hash,
            found_in=normalize_found_in(path, root),
        ),
    )


def scan_project_json(path: Path, root: Path, project_id: str, rows: list[Evidence], seen: set[tuple[str, str, str, str, str]]) -> None:
    payload = read_json(path)
    if not isinstance(payload, dict):
        return
    if payload.get("project_id") not in (project_id, None) and project_id not in json.dumps(payload, default=str):
        return
    for milestone in payload.get("milestones") or []:
        if isinstance(milestone, dict):
            milestone_from_payload(
                payload=milestone,
                category="project_definition",
                project_id=project_id,
                path=path,
                root=root,
                rows=rows,
                seen=seen,
            )


def scan_run_cache(path: Path, root: Path, project_id: str, rows: list[Evidence], seen: set[tuple[str, str, str, str, str]]) -> None:
    request_hash = path.parent.name
    payload = read_json(path)
    if not isinstance(payload, dict):
        return

    if path.name == "run_response.json":
        summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
        date_by_role = {
            "t1": first_str(summary, ("release_date_t1", "t1_release_date")),
            "t2": first_str(summary, ("release_date_t2", "t2_release_date")),
        }
        for row in (payload.get("tabular_metrics") or {}).get("summary_rows") or []:
            if isinstance(row, dict):
                role = first_str(row, ("label",))
                if role in date_by_role and not date_by_role[role]:
                    date_by_role[role] = first_str(row, ("release_date", "date"))
        change_rows = (payload.get("tabular_metrics") or {}).get("change_rows") or []
        if change_rows and isinstance(change_rows[0], dict):
            for key, role in (("release_t1", "t1"), ("release_t2", "t2")):
                milestone_id = first_str(change_rows[0], (key,))
                if MILESTONE_RE.fullmatch(milestone_id):
                    add_evidence(
                        rows,
                        seen,
                        Evidence(
                            category="individual_detection_run",
                            project_id=project_id,
                            milestone_id=milestone_id,
                            date=date_by_role.get(role) or "UNKNOWN",
                            label=role,
                            source_run_id=first_str(summary, ("run_id", "runId")) or request_hash,
                            request_hash=first_str(summary, ("request_hash", "requestHash")) or request_hash,
                            found_in=normalize_found_in(path, root),
                        ),
                    )

    if path.name == "manifest.json":
        source_run_id = first_str(payload, ("run_id", "runId")) or request_hash
        for source in (payload.get("imagery_sources") or {}).values():
            if isinstance(source, dict):
                milestone_from_payload(
                    payload=source,
                    category="individual_detection_run",
                    project_id=project_id,
                    path=path,
                    root=root,
                    rows=rows,
                    seen=seen,
                    default_request_hash=request_hash,
                    default_source_run_id=source_run_id,
                )


def scan_csv(path: Path, root: Path, project_id: str, rows: list[Evidence], seen: set[tuple[str, str, str, str, str]]) -> None:
    try:
        with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
            reader = csv.DictReader(handle)
            first = next(reader, None)
    except Exception:
        return
    if not first:
        return
    for key in ("release_t1", "release_t2", "t1_release", "t2_release"):
        milestone_id = first.get(key) or ""
        if MILESTONE_RE.fullmatch(milestone_id):
            add_evidence(
                rows,
                seen,
                Evidence(
                    category="individual_detection_run",
                    project_id=project_id,
                    milestone_id=milestone_id,
                    date=first.get(f"{key}_date") or "UNKNOWN",
                    label=key,
                    source_run_id=path.parent.name if len(path.parent.name) >= 16 else "",
                    request_hash=path.parent.name if len(path.parent.name) >= 16 else "",
                    found_in=normalize_found_in(path, root),
                ),
            )


def scan_text_for_summary(path: Path, root: Path, project_id: str, rows: list[Evidence], seen: set[tuple[str, str, str, str, str]]) -> None:
    try:
        if path.stat().st_size > MAX_TEXT_BYTES:
            return
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return
    if "TEMPORAL_SUMMARY_SOURCE" not in text and project_id not in text:
        return
    for line in text.splitlines():
        if "TEMPORAL_SUMMARY_SOURCE" not in line or project_id not in line:
            continue
        milestone = re.search(r"milestone=([A-Za-z0-9_:-]+)", line)
        source = re.search(r"sourceRunId=([A-Za-z0-9_:-]+)", line)
        date = DATE_RE.search(line)
        if milestone:
            add_evidence(
                rows,
                seen,
                Evidence(
                    category="completed_temporal_summary",
                    project_id=project_id,
                    milestone_id=milestone.group(1),
                    date=date.group(0) if date else "UNKNOWN",
                    label="UNKNOWN",
                    source_run_id=source.group(1) if source else "",
                    request_hash="",
                    found_in=normalize_found_in(path, root),
                ),
            )


def iter_candidate_files(runtime: Path) -> list[Path]:
    candidates: list[Path] = []
    for current, dirs, files in os.walk(runtime):
        dirs[:] = [d for d in dirs if d not in SKIP_DIR_NAMES]
        base = Path(current)
        for name in files:
            path = base / name
            if path.suffix.lower() in TEXT_SUFFIXES and not name.endswith(".bak-metadata_compaction_20260526T225057Z"):
                candidates.append(path)
    return candidates


def file_contains_marker(path: Path, markers: set[str]) -> bool:
    try:
        if path.stat().st_size > MAX_TEXT_BYTES:
            return False
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return False
    return any(marker in text for marker in markers)


def scan_files(root: Path, runtime: Path, project_id: str, rows: list[Evidence], seen: set[tuple[str, str, str, str, str]]) -> list[str]:
    checked: list[str] = []
    markers = set(KNOWN_MARKERS)
    markers.add(project_id)
    context_markers = {
        project_id,
        "a424271fed0194d26445c325",
        "dc17aee16171d1cf88c7eeb7",
        "job-7b5f505167044816bcbda1601d2ed411",
    }
    for path in iter_candidate_files(runtime):
        if not file_contains_marker(path, markers):
            continue
        checked.append(normalize_found_in(path, root))
        path_context = str(path)
        try:
            text_context = path.read_text(encoding="utf-8", errors="replace")[:MAX_TEXT_BYTES]
        except Exception:
            text_context = ""
        if not any(marker in path_context or marker in text_context for marker in context_markers):
            continue
        if path.name in {"project.json", "project_manifest.json", "project_summary.json"}:
            scan_project_json(path, root, project_id, rows, seen)
        if path.name in {"run_response.json", "manifest.json"}:
            scan_run_cache(path, root, project_id, rows, seen)
        if path.suffix.lower() == ".csv":
            scan_csv(path, root, project_id, rows, seen)
        if path.suffix.lower() in {".log", ".txt", ".json", ".jsonl"}:
            scan_text_for_summary(path, root, project_id, rows, seen)
    return checked


def postgres_url_for_psycopg(url: str) -> str:
    if url.startswith("postgresql+"):
        parts = urlsplit(url)
        scheme = "postgresql"
        return urlunsplit((scheme, parts.netloc, parts.path, parts.query, parts.fragment))
    return url


def scan_postgres(root: Path, project_id: str, rows: list[Evidence], seen: set[tuple[str, str, str, str, str]]) -> list[str]:
    database_url = os.getenv("APP_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not database_url:
        return ["Postgres skipped: no APP_DATABASE_URL or DATABASE_URL configured"]
    try:
        import psycopg  # type: ignore
    except Exception as exc:
        return [f"Postgres skipped: psycopg unavailable ({exc})"]

    warnings: list[str] = []
    try:
        with psycopg.connect(postgres_url_for_psycopg(database_url), connect_timeout=3) as conn:
            conn.read_only = True
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT table_name, column_name
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                    ORDER BY table_name, ordinal_position
                    """
                )
                columns = {(table, column) for table, column in cur.fetchall()}
                if {("projects", "project_id"), ("milestones", "release_identifier")} <= columns:
                    cur.execute(
                        """
                        SELECT m.release_identifier, m.release_date, m.status, m.pair_request_hash, m.raw_payload
                        FROM public.projects p
                        JOIN public.milestones m ON m.project_db_id = p.id
                        WHERE p.project_id = %s
                        ORDER BY m.release_date NULLS LAST, m.release_identifier
                        """,
                        (project_id,),
                    )
                    for milestone_id, release_date, status, request_hash, raw_payload in cur.fetchall():
                        payload = raw_payload if isinstance(raw_payload, dict) else {}
                        add_evidence(
                            rows,
                            seen,
                            Evidence(
                                category="project_definition",
                                project_id=project_id,
                                milestone_id=as_str(milestone_id),
                                date=release_date.isoformat() if release_date else first_str(payload, ("release_date", "releaseDate")) or "UNKNOWN",
                                label=first_str(payload, ("label", "display_label", "name")) or as_str(status) or "UNKNOWN",
                                source_run_id=first_str(payload, ("sourceRunId", "source_run_id")),
                                request_hash=as_str(request_hash),
                                found_in="postgres:public.projects/public.milestones",
                            ),
                        )
                if ("jobs", "job_id") in columns:
                    cur.execute(
                        """
                        SELECT job_id, request_hash, result_run_id, raw_request, raw_result
                        FROM public.jobs
                        WHERE project_id = %s OR job_id = %s OR result_run_id = %s
                        ORDER BY updated_at DESC
                        LIMIT 20
                        """,
                        (project_id, "job-7b5f505167044816bcbda1601d2ed411", "dc17aee16171d1cf88c7eeb7"),
                    )
                    for job_id, request_hash, result_run_id, raw_request, raw_result in cur.fetchall():
                        for payload in (raw_request, raw_result):
                            if isinstance(payload, dict):
                                for milestone in payload.get("milestones") or []:
                                    if isinstance(milestone, dict):
                                        milestone_from_payload(
                                            payload=milestone,
                                            category="project_definition",
                                            project_id=project_id,
                                            path=root / "postgres:public.jobs",
                                            root=root,
                                            rows=rows,
                                            seen=seen,
                                            default_request_hash=as_str(request_hash),
                                            default_source_run_id=as_str(result_run_id or job_id),
                                        )
    except Exception as exc:
        warnings.append(f"Postgres warning: {exc}")
    return warnings


def scan_redis(project_id: str) -> list[str]:
    redis_url = os.getenv("APP_CELERY_RESULT_BACKEND") or os.getenv("CELERY_RESULT_BACKEND") or os.getenv("APP_REDIS_URL")
    if not redis_url:
        return ["Redis skipped: no APP_CELERY_RESULT_BACKEND/CELERY_RESULT_BACKEND/APP_REDIS_URL configured"]
    try:
        import redis  # type: ignore
    except Exception as exc:
        return [f"Redis skipped: redis client unavailable ({exc})"]
    try:
        client = redis.Redis.from_url(redis_url, socket_connect_timeout=2, socket_timeout=2, decode_responses=True)
        keys = []
        for marker in ("job-7b5f505167044816bcbda1601d2ed411", "dc17aee16171d1cf88c7eeb7", project_id):
            keys.extend(client.keys(f"*{marker}*"))
        if not keys:
            return ["Redis checked: no matching keys"]
        return [f"Redis checked: matching keys={', '.join(sorted(set(keys))[:10])}"]
    except Exception as exc:
        return [f"Redis warning: {exc}"]


def find_frontend_persistence_sources(root: Path) -> list[str]:
    sources: list[str] = []
    frontend = root / "frontend" / "src"
    if not frontend.exists():
        return sources
    for path in frontend.rglob("*"):
        if path.suffix.lower() not in {".ts", ".tsx"}:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        if ("temporal-projects" in text or "temporalProject" in text) and (
            "getTemporalProject" in text or "project_id" in text or "persist" in text or "localStorage" in text
        ):
            sources.append(str(path.relative_to(root)))
    return sorted(sources)[:20]


def print_table(rows: list[Evidence], project_id: str) -> None:
    print(f"=== Selected Milestone Dates for {project_id} ===")
    print("category | projectId | milestoneId | date | label | sourceRunId | requestHash | foundIn")
    for row in sorted(rows, key=lambda item: (item.category, item.milestone_id, item.date, item.found_in)):
        print(
            " | ".join(
                [
                    row.category,
                    row.project_id,
                    row.milestone_id,
                    row.date or "UNKNOWN",
                    row.label or "UNKNOWN",
                    row.source_run_id or "UNKNOWN",
                    row.request_hash or "UNKNOWN",
                    row.found_in,
                ]
            )
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only local temporal milestone evidence report.")
    parser.add_argument("--project-id", default=DEFAULT_PROJECT_ID)
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    load_env_file(root / "backend" / ".env")
    load_env_file(root / "backend" / ".env.local")
    runtime = Path(os.getenv("APP_RUNTIME_CACHE_DIR", str(root / "backend" / "runtime_cache"))).expanduser()
    if not runtime.is_absolute():
        runtime = root / runtime

    rows: list[Evidence] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    warnings: list[str] = []

    checked_files = scan_files(root, runtime, args.project_id, rows, seen) if runtime.exists() else []
    warnings.extend(scan_postgres(root, args.project_id, rows, seen))
    warnings.extend(scan_redis(args.project_id))
    frontend_sources = find_frontend_persistence_sources(root)

    exact_project_json = runtime / "temporal_projects" / args.project_id / "project.json"
    registry_json = runtime / "temporal_projects_registry.json"

    print_table(rows, args.project_id)
    print()
    print("=== Project Load / 404 Evidence ===")
    print(f"exact project.json exists: {exact_project_json.exists()} ({normalize_found_in(exact_project_json, root)})")
    print(f"temporal registry exists: {registry_json.exists()} ({normalize_found_in(registry_json, root)})")
    print("frontend persistence/load source files:")
    for source in frontend_sources or ["NONE_FOUND"]:
        print(f"- {source}")
    print()
    print("=== Local Sources Checked ===")
    print(f"runtime root: {runtime}")
    print(f"matching local files: {len(checked_files)}")
    for source in checked_files[:30]:
        print(f"- {source}")
    if len(checked_files) > 30:
        print(f"- ... {len(checked_files) - 30} more")
    print()
    print("=== Warnings ===")
    for warning in warnings or ["NONE"]:
        print(f"- {warning}")
    if not rows:
        print()
        print("No exact selected milestone dates recovered from local sources.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
