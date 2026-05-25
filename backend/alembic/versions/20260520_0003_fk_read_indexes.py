"""add foreign key read indexes

Revision ID: 20260520_0003
Revises: 20260427_0002
Create Date: 2026-05-20
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import inspect


revision = "20260520_0003"
down_revision = "20260427_0002"
branch_labels = None
depends_on = None


INDEXES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("artifacts", "ix_artifacts_project_db_id", ("project_db_id",)),
    ("artifacts", "ix_artifacts_milestone_id", ("milestone_id",)),
    ("artifacts", "ix_artifacts_run_db_id", ("run_db_id",)),
    ("geometry_layers", "ix_geometry_layers_project_db_id", ("project_db_id",)),
    ("geometry_layers", "ix_geometry_layers_milestone_id", ("milestone_id",)),
    ("runs", "ix_runs_project_db_id", ("project_db_id",)),
    ("jobs", "ix_jobs_project_db_id", ("project_db_id",)),
)


def _index_exists(table_name: str, index_name: str) -> bool:
    inspector = inspect(op.get_bind())
    return any(index["name"] == index_name for index in inspector.get_indexes(table_name))


def upgrade() -> None:
    for table_name, index_name, columns in INDEXES:
        if not _index_exists(table_name, index_name):
            op.create_index(index_name, table_name, list(columns))


def downgrade() -> None:
    for table_name, index_name, _columns in reversed(INDEXES):
        if _index_exists(table_name, index_name):
            op.drop_index(index_name, table_name=table_name)
