import type { FeatureCollection } from "geojson";
import type { ReferenceLayer } from "@/api/contracts";

export interface ReferenceLayerPresentation extends ReferenceLayer {
  resolvedDisplayUrl: string | null;
  resolvedPmtilesUrl: string | null;
}

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
  cumulativeBuffer15m: FeatureCollection;
  cumulativeBuffer20m: FeatureCollection;
  cumulativeUnion: FeatureCollection;
  cumulativeConvexHull: FeatureCollection;
  cumulativeGrowthBlocks: FeatureCollection;
  cumulativeGrowthEnvelope: FeatureCollection;
  manualOverride: FeatureCollection;
  referenceLayers: ReferenceLayerPresentation[];
}
