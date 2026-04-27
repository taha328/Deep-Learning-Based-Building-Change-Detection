from __future__ import annotations

import uuid
from datetime import UTC, datetime

from geoalchemy2 import Geometry
from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.db.base import Base


def utc_now() -> datetime:
    return datetime.now(UTC)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)


class ProjectRecord(TimestampMixin, Base):
    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    semantics: Mapped[str] = mapped_column(String(64), nullable=False)
    project_dir: Mapped[str | None] = mapped_column(Text, nullable=True)
    aoi_geom: Mapped[object | None] = mapped_column(Geometry("MULTIPOLYGON", srid=4326, spatial_index=False), nullable=True)
    aoi_geojson: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    raw_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    milestones: Mapped[list[MilestoneRecord]] = relationship(back_populates="project", cascade="all, delete-orphan")
    runs: Mapped[list[RunRecord]] = relationship(back_populates="project")
    artifacts: Mapped[list[ArtifactRecord]] = relationship(back_populates="project")
    geometry_layers: Mapped[list[GeometryLayerRecord]] = relationship(back_populates="project", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_projects_aoi_geom_gist", "aoi_geom", postgresql_using="gist"),
        Index("ix_projects_created_at", "created_at"),
        Index("ix_projects_updated_at", "updated_at"),
    )


class MilestoneRecord(TimestampMixin, Base):
    __tablename__ = "milestones"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_db_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    release_identifier: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    release_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    source_mode: Mapped[str] = mapped_column(String(64), nullable=False)
    pair_request_hash: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    project: Mapped[ProjectRecord] = relationship(back_populates="milestones")
    metrics: Mapped[MilestoneMetricRecord | None] = relationship(
        back_populates="milestone",
        cascade="all, delete-orphan",
        uselist=False,
    )
    artifacts: Mapped[list[ArtifactRecord]] = relationship(back_populates="milestone")
    geometry_layers: Mapped[list[GeometryLayerRecord]] = relationship(back_populates="milestone")

    __table_args__ = (
        UniqueConstraint("project_db_id", "release_identifier", name="uq_milestones_project_release"),
        Index("ix_milestones_created_at", "created_at"),
        Index("ix_milestones_updated_at", "updated_at"),
    )


class MilestoneMetricRecord(TimestampMixin, Base):
    __tablename__ = "milestone_metrics"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    milestone_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("milestones.id", ondelete="CASCADE"), unique=True, nullable=False)
    added_area_m2: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    total_area_m2: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    additions_feature_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    effective_feature_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    building_level_available: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    added_block_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cumulative_block_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    added_block_area_m2: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    cumulative_block_area_m2: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    growth_envelope_area_m2: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    raw_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    milestone: Mapped[MilestoneRecord] = relationship(back_populates="metrics")


class RunRecord(TimestampMixin, Base):
    __tablename__ = "runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    project_db_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="SET NULL"), nullable=True)
    request_hash: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    run_kind: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    mode: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model_backend: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_request: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    raw_response: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    project: Mapped[ProjectRecord | None] = relationship(back_populates="runs")
    artifacts: Mapped[list[ArtifactRecord]] = relationship(back_populates="run")

    __table_args__ = (Index("ix_runs_created_at", "created_at"), Index("ix_runs_updated_at", "updated_at"))


class ArtifactRecord(Base):
    __tablename__ = "artifacts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_db_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=True)
    milestone_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("milestones.id", ondelete="CASCADE"), nullable=True)
    run_db_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=True)
    name: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    media_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    artifact_kind: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    checksum: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    project: Mapped[ProjectRecord | None] = relationship(back_populates="artifacts")
    milestone: Mapped[MilestoneRecord | None] = relationship(back_populates="artifacts")
    run: Mapped[RunRecord | None] = relationship(back_populates="artifacts")


class GeometryLayerRecord(TimestampMixin, Base):
    __tablename__ = "geometry_layers"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_db_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    milestone_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("milestones.id", ondelete="CASCADE"), nullable=True)
    layer_kind: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    geom: Mapped[object | None] = mapped_column(Geometry("MULTIPOLYGON", srid=4326, spatial_index=False), nullable=True)
    geojson: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    feature_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    area_m2: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str | None] = mapped_column(String(128), nullable=True)

    project: Mapped[ProjectRecord] = relationship(back_populates="geometry_layers")
    milestone: Mapped[MilestoneRecord | None] = relationship(back_populates="geometry_layers")

    __table_args__ = (
        Index("ix_geometry_layers_geom_gist", "geom", postgresql_using="gist"),
        Index("ix_geometry_layers_created_at", "created_at"),
        Index("ix_geometry_layers_updated_at", "updated_at"),
    )
