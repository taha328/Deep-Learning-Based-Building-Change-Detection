import {
  AlertTriangle,
  ArrowDown,
  ArrowUp,
  Minus,
} from "lucide-react";

import type { TemporalMilestone } from "@/api/contracts";
import { buildTimelineLabelsFromReleases, getTemporalMilestoneLabel } from "@/features/map/temporal-layer-colors";
import { cn, formatNumber } from "@/lib/utils";

type TranslateFn = (key: string) => string;

type MilestoneMetricCardsProps = {
  milestone: TemporalMilestone;
  milestones: TemporalMilestone[];
  selectedMilestoneId: string | null;
  onSelectMilestone: (releaseIdentifier: string) => void;
  t: TranslateFn;
  className?: string;
  variant?: "all" | "timeline" | "stats";
};

type DonutMetricProps = {
  value: number;
  label: string;
  centerLabel: string;
  toneColor?: "primary" | "accent" | "warning";
};

type ComparisonCardProps = {
  milestone: TemporalMilestone;
  previousMilestone: TemporalMilestone | null;
  t: TranslateFn;
};

type SpatialCompositionCardProps = {
  metrics: NonNullable<TemporalMilestone["metrics"]>;
  t: TranslateFn;
};

// ============================================================================
// Helper Functions
// ============================================================================

function safeRatio(numerator: number | undefined, denominator: number | undefined): number {
  if (!numerator || !denominator || denominator <= 0) return 0;
  return numerator / denominator;
}

function percentage(numerator: number | undefined, denominator: number | undefined): number {
  return safeRatio(numerator, denominator) * 100;
}

function clampPercent(value: number): number {
  if (!Number.isFinite(value)) return 0;
  return Math.max(0, Math.min(100, value));
}

function formatArea(areaM2: number | undefined | null, fallback = "—"): string {
  if (!areaM2 || areaM2 <= 0) return fallback;
  if (areaM2 >= 1_000_000) return `${formatNumber(areaM2 / 1_000_000, 2)} km²`;
  return `${formatNumber(areaM2, 0)} m²`;
}

function formatPercent(value: number, fractionDigits = 1): string {
  return `${formatNumber(value, fractionDigits)}%`;
}

function formatCompactNumber(value: number | undefined | null, fallback = "—"): string {
  if (value === undefined || value === null || !Number.isFinite(value)) return fallback;
  return formatNumber(value, 0);
}

function formatDelta(value: number | undefined | null, formatter: (v: number) => string): string {
  if (value === undefined || value === null) return "—";
  const sign = value > 0 ? "+" : "";
  return `${sign}${formatter(value)}`;
}

function growthIntensityLabel(addedSharePercent: number, t: TranslateFn): string {
  if (addedSharePercent >= 50) return t("temporal.metrics.major_expansion");
  if (addedSharePercent >= 20) return t("temporal.metrics.moderate_expansion");
  if (addedSharePercent > 0) return t("temporal.metrics.limited_expansion");
  return t("temporal.metrics.no_expansion");
}

function trendTone(delta: number | undefined): "up" | "stable" | "down" | "none" {
  if (delta === undefined || delta === null) return "none";
  if (delta > 0) return "up";
  if (delta < 0) return "down";
  return "stable";
}

function hasMetrics(milestone: TemporalMilestone): milestone is TemporalMilestone & { metrics: NonNullable<TemporalMilestone["metrics"]> } {
  return Boolean(milestone.metrics);
}

// ============================================================================
// UI Components
// ============================================================================

type TemporalBarChartProps = {
  milestones: TemporalMilestone[];
  selectedMilestoneId: string | null;
  onSelectMilestone: (id: string) => void;
  maxAddedArea: number;
  t: TranslateFn;
};

function TemporalBarChart({
  milestones,
  selectedMilestoneId,
  onSelectMilestone,
  maxAddedArea,
  t,
}: TemporalBarChartProps) {
  const completeMilestoneByReleaseIdentifier = new Map(
    milestones
      .filter((m) => m.status === "complete" && hasMetrics(m))
      .map((milestone) => [milestone.release_identifier, milestone] as const),
  );
  const timelineLabels = buildTimelineLabelsFromReleases(Array.from(completeMilestoneByReleaseIdentifier.values()), {
    before: t("temporal.before"),
  });
  const completedMilestones = timelineLabels
    .map((item) => completeMilestoneByReleaseIdentifier.get(item.releaseIdentifier) ?? null)
    .filter((milestone): milestone is TemporalMilestone & { metrics: NonNullable<TemporalMilestone["metrics"]> } => Boolean(milestone));
  const labelByReleaseIdentifier = new Map(timelineLabels.map((item) => [item.releaseIdentifier, item.label]));

  if (completedMilestones.length === 0) {
    return null;
  }

  return (
    <div className="rounded-lg border border-sidebar-border/60 bg-surface/40 p-3.5">
      <div className="space-y-2">
        {completedMilestones.map((milestone, index) => {
          const isSelected = milestone.release_identifier === selectedMilestoneId;
          const barWidth = maxAddedArea > 0 && milestone.metrics ? (milestone.metrics.added_area_m2 / maxAddedArea) * 100 : 0;
          const displayLabel = labelByReleaseIdentifier.get(milestone.release_identifier) ?? milestone.release_identifier;

          return (
            <button
              key={milestone.release_identifier}
              onClick={() => onSelectMilestone(milestone.release_identifier)}
              className={cn(
                "group w-full rounded-lg border p-2 text-left transition-all",
                isSelected
                  ? "border-primary/40 bg-primary/5"
                  : "border-sidebar-border/50 bg-surface/40 hover:border-primary/30 hover:bg-surface/60",
              )}
            >
              <div className="mb-1.5 flex items-center justify-between gap-2">
                <span className="flex min-w-0 items-center gap-2 text-label font-semibold text-foreground">
                  <span
                    aria-hidden="true"
                    className={cn(
                      "h-4 w-4 shrink-0 rounded-full border",
                      isSelected ? "border-primary bg-primary ring-2 ring-primary/20" : "border-muted-foreground bg-transparent",
                    )}
                  />
                  <span className="truncate">{displayLabel}</span>
                </span>
                <span className="text-caption font-mono text-muted-foreground">{formatArea(milestone.metrics?.added_area_m2, "—")}</span>
              </div>
              <div className="h-2 overflow-hidden rounded-full bg-muted">
                <div
                  className={cn("h-full rounded-full transition-all duration-500", isSelected ? "bg-primary" : "bg-muted-foreground/60")}
                  style={{ width: `${clampPercent(barWidth)}%` }}
                />
              </div>
            </button>
          );
        })}
      </div>
    </div>
  );
}

function DonutMetric({ value, label, centerLabel, toneColor = "primary" }: DonutMetricProps) {
  const normalizedValue = clampPercent(value);
  const circumference = 2 * Math.PI * 18;
  const strokeDashoffset = circumference - (normalizedValue / 100) * circumference;

  const toneStroke = {
    primary: "stroke-primary",
    accent: "stroke-accent",
    warning: "stroke-warning",
  }[toneColor];

  return (
    <div className="rounded-lg border border-sidebar-border/60 bg-surface/50 p-3">
      <p className="text-caption font-semibold uppercase tracking-wider text-muted-foreground">{label}</p>
      <div className="mt-3 flex items-center justify-center">
        <svg viewBox="0 0 44 44" className="h-24 w-24">
          <circle cx="22" cy="22" r="18" fill="none" stroke="currentColor" className="stroke-muted" strokeWidth="3" />
          <circle
            cx="22"
            cy="22"
            r="18"
            fill="none"
            className={toneStroke}
            strokeWidth="3"
            strokeDasharray={circumference}
            strokeDashoffset={strokeDashoffset}
            strokeLinecap="round"
            style={{
              transform: "rotate(-90deg)",
              transformOrigin: "22px 22px",
            }}
          />
          <text
            x="22"
            y="22"
            textAnchor="middle"
            dominantBaseline="middle"
            className="fill-foreground text-[0.6rem] font-semibold"
          >
            {centerLabel}
          </text>
        </svg>
      </div>
    </div>
  );
}


function ComparisonCard({ milestone, previousMilestone, t }: ComparisonCardProps) {
  if (!milestone.metrics) {
    return null;
  }

  if (!previousMilestone || !hasMetrics(previousMilestone)) {
    return (
      <div className="rounded-lg border border-sidebar-border/60 bg-surface/50 p-3">
        <p className="text-caption font-semibold text-muted-foreground">{t("temporal.metrics.baseline_date")}</p>
      </div>
    );
  }

  const addedAreaDelta = milestone.metrics.added_area_m2 - previousMilestone.metrics.added_area_m2;
  const totalAreaDelta = milestone.metrics.total_area_m2 - previousMilestone.metrics.total_area_m2;
  const additionsCountDelta = milestone.metrics.additions_feature_count - previousMilestone.metrics.additions_feature_count;
  const blockCountDelta = milestone.metrics.added_block_count - previousMilestone.metrics.added_block_count;
  const footprintGrowthPercent =
    previousMilestone.metrics.total_area_m2 > 0
      ? ((totalAreaDelta / previousMilestone.metrics.total_area_m2) * 100)
      : 0;

  const renderTrend = (delta: number | undefined, tone: "up" | "stable" | "down" | "none") => {
    if (tone === "up") return <ArrowUp className="h-3.5 w-3.5 text-primary" />;
    if (tone === "down") return <ArrowDown className="h-3.5 w-3.5 text-accent" />;
    if (tone === "stable") return <Minus className="h-3.5 w-3.5 text-muted-foreground" />;
    return null;
  };

  return (
    <div className="rounded-lg border border-sidebar-border/60 bg-surface/50 p-3.5">
      <p className="mb-3 text-caption font-semibold uppercase tracking-wider text-muted-foreground">
        {t("temporal.metrics.compared_with")} {getTemporalMilestoneLabel([previousMilestone], previousMilestone.release_identifier)}
      </p>
      <div className="space-y-2">
        <div className="flex items-center justify-between gap-2">
          <span className="text-caption text-muted-foreground">{t("temporal.metrics.footprint_growth")}</span>
          <div className="flex items-center gap-1.5">
            {renderTrend(totalAreaDelta, trendTone(totalAreaDelta))}
            <span className="text-label font-semibold font-mono text-foreground">{formatDelta(totalAreaDelta, (v) => formatArea(v))}</span>
          </div>
        </div>
        <div className="flex items-center justify-between gap-2">
          <span className="text-caption text-muted-foreground">{t("temporal.metrics.additions_count")}</span>
          <div className="flex items-center gap-1.5">
            {renderTrend(additionsCountDelta, trendTone(additionsCountDelta))}
            <span className="text-label font-semibold font-mono text-foreground">{formatDelta(additionsCountDelta, formatCompactNumber)}</span>
          </div>
        </div>
        <div className="flex items-center justify-between gap-2">
          <span className="text-caption text-muted-foreground">{t("temporal.metrics.blocks_added")}</span>
          <div className="flex items-center gap-1.5">
            {renderTrend(blockCountDelta, trendTone(blockCountDelta))}
            <span className="text-label font-semibold font-mono text-foreground">{formatDelta(blockCountDelta, formatCompactNumber)}</span>
          </div>
        </div>
        <div className="border-t border-sidebar-border/50 pt-2">
          <div className="flex items-center justify-between gap-2">
            <span className="text-caption font-semibold text-foreground">{t("temporal.metrics.footprint_growth_percent")}</span>
            <span className="text-label font-semibold font-mono text-primary">{formatPercent(footprintGrowthPercent, 1)}</span>
          </div>
        </div>
      </div>
    </div>
  );
}

function SpatialCompositionCard({ metrics, t }: SpatialCompositionCardProps) {
  const addedBlockPercent = percentage(metrics.added_block_area_m2, metrics.cumulative_block_area_m2);
  const footprintEnvelopePercent = percentage(metrics.total_area_m2, metrics.growth_envelope_area_m2);
  const addedBlocksRatio = percentage(metrics.added_block_count, metrics.cumulative_block_count);

  return (
    <div className="rounded-lg border border-sidebar-border/60 bg-surface/50 p-3.5">
      <p className="mb-3 text-caption font-semibold uppercase tracking-wider text-muted-foreground">{t("temporal.metrics.spatial_composition")}</p>
      <div className="space-y-3">
        <div>
          <div className="mb-1.5 flex items-center justify-between">
            <span className="text-caption text-muted-foreground">{t("temporal.metrics.added_vs_cumulative_blocks")}</span>
            <span className="text-caption font-semibold font-mono text-foreground">
              {formatArea(metrics.added_block_area_m2)} / {formatArea(metrics.cumulative_block_area_m2)}
            </span>
          </div>
          <div className="h-2 overflow-hidden rounded-full bg-muted">
            <div className="h-full rounded-full bg-primary transition-all duration-500" style={{ width: `${clampPercent(addedBlockPercent)}%` }} />
          </div>
        </div>

        <div>
          <div className="mb-1.5 flex items-center justify-between">
            <span className="text-caption text-muted-foreground">{t("temporal.metrics.footprint_vs_envelope")}</span>
            <span className="text-caption font-semibold font-mono text-foreground">
              {formatArea(metrics.total_area_m2)} / {formatArea(metrics.growth_envelope_area_m2)}
            </span>
          </div>
          <div className="h-2 overflow-hidden rounded-full bg-muted">
            <div className="h-full rounded-full bg-accent transition-all duration-500" style={{ width: `${clampPercent(footprintEnvelopePercent)}%` }} />
          </div>
        </div>

        <div>
          <div className="mb-1.5 flex items-center justify-between">
            <span className="text-caption text-muted-foreground">{t("temporal.metrics.block_count_ratio")}</span>
            <span className="text-caption font-semibold font-mono text-foreground">
              {formatCompactNumber(metrics.added_block_count)} / {formatCompactNumber(metrics.cumulative_block_count)}
            </span>
          </div>
          <div className="h-2 overflow-hidden rounded-full bg-muted">
            <div className="h-full rounded-full bg-warning transition-all duration-500" style={{ width: `${clampPercent(addedBlocksRatio)}%` }} />
          </div>
        </div>
      </div>
    </div>
  );
}

// ============================================================================
// Main Component
// ============================================================================

export function MilestoneMetricCards({
  milestone,
  milestones,
  selectedMilestoneId,
  onSelectMilestone,
  t,
  className,
  variant = "all",
}: MilestoneMetricCardsProps) {
  const metrics = milestone.metrics;

  if (!metrics) {
    return (
      <div className="rounded-xl border border-sidebar-border/80 bg-surface/70 p-3.5">
        <div className="flex items-start gap-2.5">
          <AlertTriangle className="h-5 w-5 shrink-0 text-accent" />
          <div>
            <p className="text-label font-semibold text-foreground">{t("temporal.metrics.unavailable_title")}</p>
            <p className="mt-1 text-caption leading-5 text-muted-foreground">{t("temporal.metrics.unavailable_description")}</p>
          </div>
        </div>
      </div>
    );
  }

  // Compute derived metrics
  const completedMilestones = milestones.filter((m) => m.status === "complete" && hasMetrics(m));
  const selectedIndex = completedMilestones.findIndex((m) => m.release_identifier === milestone.release_identifier);
  const previousMilestone = selectedIndex > 0 ? completedMilestones[selectedIndex - 1] : null;

  const addedSharePercent = percentage(metrics.added_area_m2, metrics.total_area_m2);
  const envelopeDensityPercent = percentage(metrics.total_area_m2, metrics.growth_envelope_area_m2);
  const growth = growthIntensityLabel(addedSharePercent, t);
  const maxAddedArea = Math.max(...completedMilestones.filter((m) => m.metrics).map((m) => m.metrics!.added_area_m2));

  return (
    <div className={cn("space-y-3", className)}>
      {variant !== "stats" && completedMilestones.length > 1 ? (
        <TemporalBarChart
          milestones={milestones}
          selectedMilestoneId={selectedMilestoneId}
          onSelectMilestone={onSelectMilestone}
          maxAddedArea={maxAddedArea}
          t={t}
        />
      ) : null}

      {variant === "timeline" ? null : (
        <>
      {/* Executive KPI card - primary visual emphasis */}
      <div className="rounded-2xl border border-primary/35 bg-gradient-to-br from-primary/12 via-surface to-surface p-4 shadow-md">
        <div className="mb-2 flex items-center justify-between">
          <p className="text-label font-semibold text-foreground">{t("temporal.metrics.growth_overview")}</p>
          <span className="inline-flex items-center gap-1 rounded-full border border-primary/30 bg-primary/10 px-2 py-0.5 text-caption font-medium text-foreground">
            {growth}
          </span>
        </div>
        <p className="text-lg font-semibold tracking-tight text-foreground font-mono tabular-nums">{formatArea(metrics.added_area_m2)}</p>
        <p className="mt-2 text-caption leading-5 text-muted-foreground">
          {formatCompactNumber(metrics.additions_feature_count)} {t("temporal.metrics.additions_plural")} {t("temporal.metrics.grouped_into")}{" "}
          {formatCompactNumber(metrics.added_block_count)} {t("temporal.metrics.blocks_plural")}
        </p>
        <p className="mt-1 text-caption leading-5 text-muted-foreground">
          {t("temporal.metrics.current_footprint_context")}: {formatArea(metrics.total_area_m2)}
        </p>
      </div>

      {/* Visual separation before supporting metrics */}
      <div className="mt-3.5" />

      {/* KPI Dashboard Grid - supporting metrics with reduced visual weight */}
      <div className="grid grid-cols-2 gap-2.5">
        <div className="rounded-lg border border-sidebar-border/60 bg-surface/40 p-3">
          <p className="text-caption font-semibold uppercase tracking-wider text-muted-foreground">{t("temporal.metrics.added_surface")}</p>
          <p className="mt-2 text-lg font-semibold text-foreground font-mono tabular-nums">{formatArea(metrics.added_area_m2)}</p>
          <p className="mt-1 text-caption text-muted-foreground">{formatPercent(addedSharePercent, 1)} {t("temporal.metrics.of_current")}</p>
        </div>

        <div className="rounded-lg border border-sidebar-border/60 bg-surface/40 p-3">
          <p className="text-caption font-semibold uppercase tracking-wider text-muted-foreground">{t("temporal.metrics.current_footprint")}</p>
          <p className="mt-2 text-lg font-semibold text-foreground font-mono tabular-nums">{formatArea(metrics.total_area_m2)}</p>
          <p className="mt-1 text-caption text-muted-foreground">
            {formatCompactNumber(metrics.cumulative_block_count)} {t("temporal.metrics.blocks")}
          </p>
        </div>

        <div className="rounded-lg border border-sidebar-border/60 bg-surface/40 p-3">
          <p className="text-caption font-semibold uppercase tracking-wider text-muted-foreground">{t("temporal.metrics.detected_additions")}</p>
          <p className="mt-2 text-lg font-semibold text-foreground font-mono tabular-nums">{formatCompactNumber(metrics.additions_feature_count)}</p>
          <p className="mt-1 text-caption text-muted-foreground">{formatCompactNumber(metrics.added_block_count)} {t("temporal.metrics.blocks_spatial")}</p>
        </div>

        <div className="rounded-lg border border-sidebar-border/60 bg-surface/40 p-3">
          <p className="text-caption font-semibold uppercase tracking-wider text-muted-foreground">{t("temporal.metrics.growth_density")}</p>
          <p className="mt-2 text-lg font-semibold text-foreground font-mono tabular-nums">{formatPercent(envelopeDensityPercent, 1)}</p>
          <p className="mt-1 text-caption text-muted-foreground">{t("temporal.metrics.density_desc")}</p>
        </div>
      </div>

      {/* Donut visualizations */}
      <div className="grid grid-cols-2 gap-2.5">
        <DonutMetric
          value={addedSharePercent}
          label={t("temporal.metrics.added_vs_current")}
          centerLabel={formatPercent(addedSharePercent, 0)}
          toneColor="primary"
        />
        <DonutMetric
          value={envelopeDensityPercent}
          label={t("temporal.metrics.footprint_vs_envelope")}
          centerLabel={formatPercent(envelopeDensityPercent, 0)}
          toneColor="accent"
        />
      </div>

      {/* Comparison with previous */}
      {previousMilestone || completedMilestones.length > 0 ? (
        <ComparisonCard milestone={milestone} previousMilestone={previousMilestone} t={t} />
      ) : null}

      {/* Spatial composition */}
      <SpatialCompositionCard metrics={metrics} t={t} />
        </>
      )}
    </div>
  );
}
