"""initial postgis persistence

Revision ID: 20260427_0001
Revises:
Create Date: 2026-04-27
"""

from __future__ import annotations

from alembic import op
import geoalchemy2
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260427_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")

    op.create_table(
        "projects",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("project_id", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("semantics", sa.String(length=64), nullable=False),
        sa.Column("project_dir", sa.Text(), nullable=True),
        sa.Column("aoi_geom", geoalchemy2.Geometry(geometry_type="MULTIPOLYGON", srid=4326, spatial_index=False), nullable=True),
        sa.Column("aoi_geojson", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("raw_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("project_id"),
    )
    op.create_index("ix_projects_project_id", "projects", ["project_id"])
    op.create_index("ix_projects_created_at", "projects", ["created_at"])
    op.create_index("ix_projects_updated_at", "projects", ["updated_at"])
    op.create_index("ix_projects_aoi_geom_gist", "projects", ["aoi_geom"], postgresql_using="gist")

    op.create_table(
        "milestones",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("project_db_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("release_identifier", sa.String(length=128), nullable=False),
        sa.Column("release_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("source_mode", sa.String(length=64), nullable=False),
        sa.Column("pair_request_hash", sa.String(length=128), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("raw_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("project_db_id", "release_identifier", name="uq_milestones_project_release"),
    )
    op.create_index("ix_milestones_release_identifier", "milestones", ["release_identifier"])
    op.create_index("ix_milestones_status", "milestones", ["status"])
    op.create_index("ix_milestones_pair_request_hash", "milestones", ["pair_request_hash"])
    op.create_index("ix_milestones_created_at", "milestones", ["created_at"])
    op.create_index("ix_milestones_updated_at", "milestones", ["updated_at"])

    op.create_table(
        "milestone_metrics",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("milestone_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("milestones.id", ondelete="CASCADE"), nullable=False),
        sa.Column("added_area_m2", sa.Float(), nullable=False),
        sa.Column("total_area_m2", sa.Float(), nullable=False),
        sa.Column("additions_feature_count", sa.Integer(), nullable=False),
        sa.Column("effective_feature_count", sa.Integer(), nullable=False),
        sa.Column("building_level_available", sa.Boolean(), nullable=False),
        sa.Column("added_block_count", sa.Integer(), nullable=False),
        sa.Column("cumulative_block_count", sa.Integer(), nullable=False),
        sa.Column("added_block_area_m2", sa.Float(), nullable=False),
        sa.Column("cumulative_block_area_m2", sa.Float(), nullable=False),
        sa.Column("growth_envelope_area_m2", sa.Float(), nullable=False),
        sa.Column("raw_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("milestone_id"),
    )

    op.create_table(
        "runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("run_id", sa.String(length=128), nullable=False),
        sa.Column("project_db_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("projects.id", ondelete="SET NULL"), nullable=True),
        sa.Column("request_hash", sa.String(length=128), nullable=True),
        sa.Column("run_kind", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("mode", sa.String(length=64), nullable=True),
        sa.Column("model_backend", sa.String(length=64), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("raw_request", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("raw_response", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("run_id"),
    )
    op.create_index("ix_runs_run_id", "runs", ["run_id"])
    op.create_index("ix_runs_request_hash", "runs", ["request_hash"])
    op.create_index("ix_runs_run_kind", "runs", ["run_kind"])
    op.create_index("ix_runs_status", "runs", ["status"])
    op.create_index("ix_runs_created_at", "runs", ["created_at"])
    op.create_index("ix_runs_updated_at", "runs", ["updated_at"])

    op.create_table(
        "artifacts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("project_db_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=True),
        sa.Column("milestone_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("milestones.id", ondelete="CASCADE"), nullable=True),
        sa.Column("run_db_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("runs.id", ondelete="CASCADE"), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("media_type", sa.String(length=255), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("artifact_kind", sa.String(length=128), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("checksum", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_artifacts_name", "artifacts", ["name"])
    op.create_index("ix_artifacts_artifact_kind", "artifacts", ["artifact_kind"])

    op.create_table(
        "geometry_layers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("project_db_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("milestone_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("milestones.id", ondelete="CASCADE"), nullable=True),
        sa.Column("layer_kind", sa.String(length=128), nullable=False),
        sa.Column("geom", geoalchemy2.Geometry(geometry_type="MULTIPOLYGON", srid=4326, spatial_index=False), nullable=True),
        sa.Column("geojson", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("feature_count", sa.Integer(), nullable=True),
        sa.Column("area_m2", sa.Float(), nullable=True),
        sa.Column("source", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_geometry_layers_layer_kind", "geometry_layers", ["layer_kind"])
    op.create_index("ix_geometry_layers_created_at", "geometry_layers", ["created_at"])
    op.create_index("ix_geometry_layers_updated_at", "geometry_layers", ["updated_at"])
    op.create_index("ix_geometry_layers_geom_gist", "geometry_layers", ["geom"], postgresql_using="gist")


def downgrade() -> None:
    op.drop_table("geometry_layers")
    op.drop_table("artifacts")
    op.drop_table("runs")
    op.drop_table("milestone_metrics")
    op.drop_table("milestones")
    op.drop_table("projects")

