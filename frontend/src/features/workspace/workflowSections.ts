export const WORKFLOW_SECTION_IDS = [
  "overview",
  "aoi",
  "releases",
  "progress",
  "downloads",
] as const;

export type WorkflowSectionId = (typeof WORKFLOW_SECTION_IDS)[number];
