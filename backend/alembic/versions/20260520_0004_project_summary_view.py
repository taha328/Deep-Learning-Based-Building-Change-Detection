"""add project summary view

Revision ID: 20260520_0004
Revises: 20260520_0003
Create Date: 2026-05-20
"""

from __future__ import annotations

from alembic import op


revision = "20260520_0004"
down_revision = "20260520_0003"
branch_labels = None
depends_on = None


PROJECT_SUMMARY_VIEW_SQL = """
CREATE OR REPLACE VIEW public.project_summary AS
SELECT
    p.id AS project_db_id,
    p.project_id,
    p.name,
    p.semantics,
    p.project_dir,
    p.created_at,
    p.updated_at,
    COALESCE(ms.milestone_count, 0)::integer AS milestone_count,
    COALESCE(ms.complete_milestone_count, 0)::integer AS complete_milestone_count,
    COALESCE(rs.run_count, 0)::integer AS run_count,
    COALESCE(ar.artifact_count, 0)::integer AS artifact_count,
    bundle.download_bundle_path,
    latest_run.status AS latest_run_status,
    latest_run.created_at AS latest_run_created_at,
    latest_job.status AS latest_job_status,
    latest_job.updated_at AS latest_job_updated_at
FROM public.projects AS p
LEFT JOIN LATERAL (
    SELECT
        count(*) AS milestone_count,
        count(*) FILTER (WHERE m.status = 'complete') AS complete_milestone_count
    FROM public.milestones AS m
    WHERE m.project_db_id = p.id
) AS ms ON true
LEFT JOIN LATERAL (
    SELECT count(*) AS run_count
    FROM public.runs AS r
    WHERE r.project_db_id = p.id
) AS rs ON true
LEFT JOIN LATERAL (
    SELECT count(*) AS artifact_count
    FROM public.artifacts AS a
    WHERE a.project_db_id = p.id
) AS ar ON true
LEFT JOIN LATERAL (
    SELECT a.path AS download_bundle_path
    FROM public.artifacts AS a
    WHERE a.project_db_id = p.id
      AND a.artifact_kind = 'bundle'
    ORDER BY a.created_at DESC
    LIMIT 1
) AS bundle ON true
LEFT JOIN LATERAL (
    SELECT r.status, r.created_at
    FROM public.runs AS r
    WHERE r.project_db_id = p.id
    ORDER BY r.created_at DESC
    LIMIT 1
) AS latest_run ON true
LEFT JOIN LATERAL (
    SELECT j.status, j.updated_at
    FROM public.jobs AS j
    WHERE j.project_db_id = p.id
       OR j.project_id = p.project_id
    ORDER BY j.updated_at DESC
    LIMIT 1
) AS latest_job ON true
"""


def upgrade() -> None:
    op.execute(PROJECT_SUMMARY_VIEW_SQL)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS public.project_summary")
