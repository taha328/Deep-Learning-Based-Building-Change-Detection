"""add async job tracking table

Revision ID: 20260427_0002
Revises: 20260427_0001
Create Date: 2026-04-27
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260427_0002"
down_revision = "20260427_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("job_id", sa.String(length=128), nullable=False),
        sa.Column("celery_task_id", sa.String(length=128), nullable=True),
        sa.Column("job_kind", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("project_db_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("projects.id", ondelete="SET NULL"), nullable=True),
        sa.Column("project_id", sa.String(length=128), nullable=True),
        sa.Column("request_hash", sa.String(length=128), nullable=True),
        sa.Column("progress", sa.Integer(), nullable=True),
        sa.Column("stage", sa.String(length=128), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("result_run_id", sa.String(length=128), nullable=True),
        sa.Column("raw_request", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("raw_result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("job_id"),
    )
    op.create_index("ix_jobs_job_id", "jobs", ["job_id"])
    op.create_index("ix_jobs_celery_task_id", "jobs", ["celery_task_id"])
    op.create_index("ix_jobs_job_kind", "jobs", ["job_kind"])
    op.create_index("ix_jobs_status", "jobs", ["status"])
    op.create_index("ix_jobs_project_id", "jobs", ["project_id"])
    op.create_index("ix_jobs_request_hash", "jobs", ["request_hash"])
    op.create_index("ix_jobs_result_run_id", "jobs", ["result_run_id"])
    op.create_index("ix_jobs_created_at", "jobs", ["created_at"])
    op.create_index("ix_jobs_updated_at", "jobs", ["updated_at"])


def downgrade() -> None:
    op.drop_table("jobs")
