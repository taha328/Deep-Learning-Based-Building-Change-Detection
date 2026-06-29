import type { FeatureCollection } from "geojson";
import type { ReferenceLayer, TemporalMilestone } from "@/api/contracts";

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

export interface TemporalOutputArtifactPresentation {
  key: string;
  featureCount: number | null;
  sizeBytes: number | null;
  bbox: [number, number, number, number] | null;
  artifactUrl: string | null;
  tilejsonUrl: string | null;
  tilesUrlTemplate: string | null;
  vectorSourceLayer: string | null;
}

export interface TemporalAddedOverlayPresentation {
  releaseIdentifier: string;
  status: "pending" | "validated" | "complete" | "error" | null;
  artifacts: Record<string, TemporalOutputArtifactPresentation>;
  additions: FeatureCollection;
  buffer10m: FeatureCollection;
  buffer15m: FeatureCollection;
  buffer20m: FeatureCollection;
  cumulativeBuffer10m: FeatureCollection;
  cumulativeBuffer15m: FeatureCollection;
  cumulativeBuffer20m: FeatureCollection;
  automatedCandidate: FeatureCollection;
  automatedBuildingBlocks: FeatureCollection;
  effectiveBuildingBlocks: FeatureCollection;
  cumulativeUnion: FeatureCollection;
  cumulativeGrowthBlocks: FeatureCollection;
  cumulativeGrowthEnvelope: FeatureCollection;
  manualOverride: FeatureCollection;
}

export interface TemporalMapPresentation {
  projectId: string | null;
  projectUpdatedAt: string | null;
  isHydratingProject: boolean;
  availableMilestoneIds: string[];
  availableMilestones: Array<{
    releaseIdentifier: string;
    date: string | null;
  }>;
  selectedMilestoneIndex: number;
  selectedReleaseIdentifier: string | null;
  selectedMilestoneStatus: "pending" | "validated" | "complete" | "error" | null;
  selectedMilestone: TemporalMilestone | null;
  milestones: TemporalMilestone[];
  milestoneCount: number;
  referenceImagery: TemporalReferenceImageryPresentation | null;
  referenceImageryTimeline: TemporalReferenceImageryPresentation[];
  addedOverlayTimeline: TemporalAddedOverlayPresentation[];
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
  cumulativeGrowthBlocks: FeatureCollection;
  cumulativeGrowthEnvelope: FeatureCollection;
  manualOverride: FeatureCollection;
  referenceLayers: ReferenceLayerPresentation[];
}

export interface TemporalLayerControlEntryPresentation {
  key: string;
  label: string;
  enabled: boolean;
  checked: boolean;
  description?: string;
  swatch?: {
    color: string;
    opacity?: number;
  };
  onCheckedChange: (checked: boolean) => void;
}

export interface TemporalLayerControlsPresentation {
  satellite: TemporalLayerControlEntryPresentation[];
  buildingEvolution: TemporalLayerControlEntryPresentation[];
  manualReferenceLayers: Array<{
    id: string;
    name: string;
    geometryType: string | null;
    storageStrategy: string | null;
    opacity: number;
  }>;
  referenceWarning: string | null;
}
