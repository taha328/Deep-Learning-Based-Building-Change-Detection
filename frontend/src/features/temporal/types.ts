import type { FeatureCollection } from "geojson";

export interface TemporalMapPresentation {
  selectedReleaseIdentifier: string | null;
  selectedMilestoneStatus: "pending" | "validated" | "complete" | "error" | null;
  milestoneCount: number;
  referenceImageryUrl: string | null;
  referenceImageryBounds: [number, number, number, number] | null;
  automatedCandidate: FeatureCollection;
  automatedBuildingBlocks: FeatureCollection;
  additions: FeatureCollection;
  effectiveBuildingBlocks: FeatureCollection;
  bufferLayers: Record<string, FeatureCollection>;
  cumulativeBuffer10m: FeatureCollection;
  cumulativeUnion: FeatureCollection;
  cumulativeConvexHull: FeatureCollection;
  cumulativeGrowthBlocks: FeatureCollection;
  cumulativeGrowthEnvelope: FeatureCollection;
  manualOverride: FeatureCollection;
}
