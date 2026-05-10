import type { FeatureCollection } from "geojson";
import type { ReferenceLayer } from "@/api/contracts";

export interface ReferenceLayerPresentation extends ReferenceLayer {
  resolvedDisplayUrl: string | null;
  resolvedPmtilesUrl: string | null;
}

export interface TemporalReferenceImageryPresentation {
  releaseIdentifier: string;
  storageStrategy: "raster_tiles" | "cog" | "image_overlay";
  tilejsonUrl: string | null;
  tilesUrlTemplate: string | null;
  cogUrl: string | null;
  imageUrl: string | null;
  bounds: [number, number, number, number] | null;
  minzoom: number | null;
  maxzoom: number | null;
  tileSize: number;
}

export interface TemporalMapPresentation {
  projectId: string | null;
  projectUpdatedAt: string | null;
  isHydratingProject: boolean;
  availableMilestoneIds: string[];
  selectedMilestoneIndex: number;
  selectedReleaseIdentifier: string | null;
  selectedMilestoneStatus: "pending" | "validated" | "complete" | "error" | null;
  milestoneCount: number;
  referenceImagery: TemporalReferenceImageryPresentation | null;
  referenceImageryTimeline: TemporalReferenceImageryPresentation[];
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
