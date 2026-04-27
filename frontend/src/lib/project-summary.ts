import type { TemporalProjectSummary } from "@/api/contracts";

type TranslateFn = (key: string) => string;

function isMeaningfulProjectName(name: string | undefined, untitledLabels: Set<string>): boolean {
  if (!name) {
    return false;
  }
  const trimmed = name.trim();
  return trimmed.length > 0 && !untitledLabels.has(trimmed);
}

export function getProjectKind(summary: Pick<TemporalProjectSummary, "project_id" | "project_kind">): "pairwise" | "temporal" {
  if (summary.project_kind) {
    return summary.project_kind;
  }
  return summary.project_id.startsWith("run-") ? "pairwise" : "temporal";
}

export function getProjectDisplayName(
  summary: Pick<TemporalProjectSummary, "project_id" | "name" | "project_kind" | "display_name" | "milestone_count">,
  t: TranslateFn,
): string {
  if (summary.display_name) {
    return summary.display_name;
  }

  const untitledLabels = new Set([t("temporal.untitled_project"), "Untitled Temporal Mosaic", "Mosaïque temporelle sans titre"]);
  const kind = getProjectKind(summary);
  if (kind === "pairwise") {
    return `${t("project.kind.pairwise")} · ${summary.name}`;
  }

  if (isMeaningfulProjectName(summary.name, untitledLabels)) {
    return `${t("project.kind.temporal")} · ${summary.name}`;
  }

  if (summary.milestone_count === 1) {
    return `${t("project.kind.temporal")} · 1 ${t("project.milestone")}`;
  }
  return `${t("project.kind.temporal")} · ${summary.milestone_count} ${t("project.milestones")}`;
}
