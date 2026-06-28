import { featureCollection } from "@turf/helpers";
import type { Feature, FeatureCollection, GeoJsonProperties, MultiPolygon, Polygon } from "geojson";
import maplibregl, { type GeoJSONSource, type Map as MapLibreMap } from "maplibre-gl";
import { BarChart3, Check, Layers3, Loader2, Maximize2, Minus, Plus, Search, X } from "lucide-react";
import { Protocol } from "pmtiles";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { useAppStore } from "@/app/store";
import { useI18n } from "@/lib/i18n";
import { Input } from "@/components/ui/input";
import { createOpenStreetMapStyle, mapboxProvider } from "@/lib/basemap";
import { buildBackendFileUrl } from "@/lib/backend-files";
import { isDevClientLogEnabled } from "@/lib/client-log-config";
import { relayClientLog } from "@/lib/client-log-relay";
import { cn } from "@/lib/utils";
import { MilestoneMetricCards } from "@/features/temporal/MilestoneMetricCards";
import { formatReferenceLayerKindLabel } from "@/features/temporal/display-labels";
import {
  buildTemporalLayerLabels,
  getIncludedAdditionReleasesForCumulativeLayer,
  getIncludedTemporalMilestones,
  getTemporalLayerExpectedReleases,
  getMilestoneColorMap,
  getTemporalLayerPaint,
  temporalAdditionVisibilityReason,
  usesGeneratedMilestoneColors,
  type IncludedTemporalMilestone,
  type TemporalLayerPlanningKey,
  type TemporalMilestoneColorInput,
  type TemporalStyledLayerKind,
} from "@/features/map/temporal-layer-colors";
import {
  dedupeStable,
  shouldApplyMapValue,
  shouldSkipPostVisibilityLayerWork,
  shouldSkipReferenceRegistration,
  stableHash,
} from "@/features/map/temporal-map-performance";
import {
  AOI_DRAW_CLOSE_TARGET_RADIUS,
  AOI_DRAW_PREVIEW_FILL_OPACITY,
  AOI_DRAW_STROKE_COLOR,
  AOI_DRAW_STROKE_WIDTH,
  AOI_DRAW_VERTEX_FILL,
  AOI_DRAW_VERTEX_RADIUS,
  drawingHelperMessage,
  drawingKeyboardAction,
  drawingPreviewFeatureCollection,
  isNearFirstVertex,
  resolveDrawingClick,
} from "@/features/map/map-drawing";
import type {
  TemporalAddedOverlayPresentation,
  TemporalLayerControlsPresentation,
  ReferenceLayerPresentation,
  TemporalMapPresentation,
  TemporalReferenceImageryPresentation,
} from "@/features/temporal/types";

const EMPTY_FEATURE_COLLECTION: FeatureCollection = {
  type: "FeatureCollection",
  features: [],
};

const BUILDING_CHANGE_BUFFER_FILL_COLORS = {
  "10m": "#dc2626",
  "15m": "#facc15",
  "20m": "#2563eb",
} as const;

const NON_CUMULATIVE_BUFFER_FILL_OPACITY = 1;
const TEMPORAL_BUFFER_FILL_OPACITY = 0.5;

type SearchResult = {
  id: string;
  label: string;
  subtitle: string | null;
  center: [number, number];
  bbox: [number, number, number, number] | null;
};

type LayerToggleKey =
  | "t1Preview"
  | "t2Preview"
  | "temporalReferenceImagery"
  | "changeProbability"
  | "changeOverlay"
  | "detectedPolygons"
  | "buildingBlocks"
  | "buffers"
  | "buffer10m"
  | "buffer15m"
  | "buffer20m"
  | "temporalAdditions"
  | "selectedMilestoneAdditions"
  | "temporalCumulativeBuffer10m"
  | "temporalCumulativeBuffer15m"
  | "temporalCumulativeBuffer20m"
  | "temporalAutomated"
  | "temporalAutomatedBuildingBlocks"
  | "temporalEffectiveBuildingBlocks"
  | "temporalCumulative"
  | "temporalCumulativeGrowthBlocks"
  | "temporalCumulativeGrowthEnvelope"
  | "temporalManualOverride"
  | "labels";

type LayerToggleState = Record<LayerToggleKey, boolean>;

type OverlaySources = {
  t1Preview: string | null;
  t2Preview: string | null;
  changeProbability: string | null;
  changeOverlay: string | null;
};

type WorkflowMode = "pairwise" | "temporal";

type TemporalVectorSources = {
  temporalAdditions: FeatureCollection;
  temporalCumulativeBuffer10m: FeatureCollection;
  temporalCumulativeBuffer15m: FeatureCollection;
  temporalCumulativeBuffer20m: FeatureCollection;
  temporalAutomated: FeatureCollection;
  temporalAutomatedBuildingBlocks: FeatureCollection;
  temporalEffectiveBuildingBlocks: FeatureCollection;
  temporalCumulative: FeatureCollection;
  temporalCumulativeGrowthBlocks: FeatureCollection;
  temporalCumulativeGrowthEnvelope: FeatureCollection;
  temporalManualOverride: FeatureCollection;
};

type PairwiseBufferSources = {
  buffer10m: FeatureCollection;
  buffer15m: FeatureCollection;
  buffer20m: FeatureCollection;
};

type LayerEntry = {
  key: LayerToggleKey;
  label: string;
  enabled: boolean;
  description?: string;
  swatch?: {
    color: string;
    opacity?: number;
  };
};

type ReferenceLayerGeoJsonData = Record<string, FeatureCollection>;

type TemporalReferenceSourceLifecycleMode = "create" | "reuse" | "recreate";
type TemporalReferenceSwitchMode = TemporalReferenceSourceLifecycleMode | "ready_wait";
type TemporalReferenceReadinessSource =
  | "idle"
  | "sourcedata"
  | "already_loaded"
  | "already_registered"
  | "source_reuse"
  | "layer_reuse"
  | "render_frame"
  | "timeout_fallback"
  | "missing_layer_fallback";

type TemporalReferenceVisualReadyResult = {
  visualReadyMs: number;
  readinessSource: TemporalReferenceReadinessSource;
};

type TemporalReferenceLayerLifecycle = {
  layerId: string;
  sourceId: string;
  signature: string | null;
  previousSignature: string | null;
  mode: TemporalReferenceSourceLifecycleMode;
  firstTileMs?: number;
};

type TemporalReferenceTilejsonPayload = {
  tiles?: string[];
  minzoom?: number;
  maxzoom?: number;
  bounds?: [number, number, number, number];
};

type TemporalAddedLayerKind =
  | "additions"
  | "buffer10m"
  | "buffer15m"
  | "buffer20m"
  | "cumulativeBuffer10m"
  | "cumulativeBuffer15m"
  | "cumulativeBuffer20m"
  | "automated"
  | "automatedBuildingBlocks"
  | "effectiveBuildingBlocks"
  | "cumulative"
  | "cumulativeGrowthBlocks"
  | "cumulativeGrowthEnvelope"
  | "manualOverride";

type TemporalAddedLayerDefinition = {
  kind: TemporalAddedLayerKind;
  toggleKey: LayerToggleKey;
  paint: NonNullable<maplibregl.FillLayerSpecification["paint"]>;
  data: (overlay: TemporalAddedOverlayPresentation) => FeatureCollection;
};

type TemporalAddedLayerLifecycle = {
  sourceId: string;
  layerId: string;
  lineLayerId: string | null;
  signature: string;
  previousSignature: string | null;
  mode: "create" | "reuse" | "update" | "recreate";
  featureCount: number;
  payloadBytes: number;
};

type TemporalOutputLayerPlan = {
  projectId: string;
  releaseIdentifier: string;
  layerKey: LayerToggleKey;
  artifactKey: string;
  layerIds: string[];
  sourceId: string;
  sourceType: "geojson" | "vector";
  renderStrategy: "geojson" | "vector_tiles";
  featureCount: number | null;
  sizeBytes: number | null;
  isBaseline: boolean;
  isEmpty: boolean;
  enabled: boolean;
  availabilityReason: string;
  tilejsonUrl?: string | null;
  sourceLayer?: string | null;
  overlay: TemporalAddedOverlayPresentation;
  definition: TemporalAddedLayerDefinition;
};

type TemporalOutputLayerSyncResult = {
  appliedCount: number;
  hiddenCount: number;
  skippedCount: number;
  missingLayerCount: number;
  visibleLayerIds: string[];
  hiddenLayerIds: string[];
};

type TemporalReleaseSetByKind = Partial<Record<TemporalAddedLayerKind, string[]>>;

type TemporalLayerContractSnapshot = {
  uiLabel: string;
  layerKey: LayerToggleKey | "allNewBuildings" | "selectedAdditions";
  artifactKey: string;
  mode: "selected" | "cumulative" | "selected_source_cumulative_artifact";
  expectedReleases: string[];
  registeredSources: string[];
  registeredLayers: string[];
  visibleLayers: string[];
  unexpectedVisibleReleases: string[];
  unexpectedRegisteredReleases: string[];
};

type TemporalRuntimeDebugSnapshot = {
  projectId: string;
  selectedRelease: string;
  enabledLayerKeys: LayerToggleKey[];
  layerContracts: TemporalLayerContractSnapshot[];
  bufferLayers: Array<{
    layerId: string;
    lineLayerId: string;
    sourceId: string;
    layerKey: LayerToggleKey;
    artifactKey: string;
    releaseIdentifier: string;
    expectedColor: string;
    actualFillColor: unknown;
    actualLineColor: unknown;
    visibility: string | null;
    lineVisibility: string | null;
    expectedVisible: boolean;
  }>;
  layerIdCollisions: string[];
  sourceIdCollisions: string[];
  allNewBuildingsEnabled: boolean;
  selectedAdditionsEnabled: boolean;
  includedAdditionReleases: string[];
  additionRegistrationPlans: string[];
  registeredAdditionSources: string[];
  registeredAdditionLayers: string[];
  visibleAdditionLayers: string[];
  hiddenFutureAdditionLayers: string[];
  missing: string[];
  releases: Record<
    string,
    {
      sourceId: string;
      layerId: string;
      lineLayerId: string;
      sourceExists: boolean;
      layerExists: boolean;
      lineLayerExists: boolean;
      visibility: string | null;
      lineVisibility: string | null;
      filter: unknown;
      color: unknown;
      lineColor: unknown;
      orderIndex: number;
      aboveReferenceRaster: boolean | null;
    }
  >;
};

declare global {
  interface Window {
    __SATMONITOR_TEMPORAL_DEBUG__?: {
      latest: TemporalRuntimeDebugSnapshot | null;
      history: TemporalRuntimeDebugSnapshot[];
      getLatest: () => TemporalRuntimeDebugSnapshot | null;
    };
    __BUILDING_CHANGE_REFERENCE_DEBUG__?: {
      getState: () => {
        sources: string[];
        layers: Array<{ id: string; visibility: string | null; opacity: unknown; orderIndex: number }>;
        context: {
          workflowMode: WorkflowMode;
          projectId: string | null;
          selectedReleaseIdentifier: string | null;
          referenceImagery: TemporalReferenceImageryPresentation | null;
          referenceImageryAvailable: boolean;
          referenceLayerEnabled: boolean;
          mapStyleRevision: number;
          mapStyleLoaded: boolean;
        };
      };
    };
  }
}

let pmtilesProtocolRegistered = false;
let pmtilesProtocol: Protocol | null = null;
const DEV_LOGGING = isDevClientLogEnabled(import.meta.env);
let temporalRenderAuditModeLogged = false;

function isTemporalRenderAuditEnabled(): boolean {
  if (import.meta.env.VITE_TEMPORAL_RENDER_AUDIT === "true") {
    return true;
  }
  if (typeof window === "undefined") {
    return false;
  }
  const params = new URLSearchParams(window.location.search);
  return params.get("debugRenderAudit") === "1" || params.has("validation");
}

function devLog(event: string, payload: Record<string, unknown>) {
  if (!DEV_LOGGING) {
    return;
  }
  if (
    event.startsWith("TEMPORAL_REFERENCE_") ||
    event.startsWith("TEMPORAL_ADDED_") ||
    event.startsWith("TEMPORAL_OUTPUT_") ||
    event.startsWith("TEMPORAL_ACTIVE_") ||
    event.startsWith("TEMPORAL_VECTOR_") ||
    event.startsWith("TEMPORAL_VECTOR_TILE_") ||
    event.startsWith("TEMPORAL_GEOJSON_") ||
    event.startsWith("TEMPORAL_BASELINE_") ||
    event.startsWith("TEMPORAL_EMPTY_BASELINE_") ||
    event.startsWith("TEMPORAL_RENDER_") ||
    event.startsWith("TEMPORAL_STALE_PROJECT_") ||
    event.startsWith("TEMPORAL_SCREENSHOT_")
  ) {
    if (
      event.startsWith("TEMPORAL_OUTPUT_") ||
      event.startsWith("TEMPORAL_ACTIVE_") ||
      event.startsWith("TEMPORAL_VECTOR_") ||
      event.startsWith("TEMPORAL_VECTOR_TILE_") ||
      event.startsWith("TEMPORAL_GEOJSON_") ||
      event.startsWith("TEMPORAL_BASELINE_") ||
      event.startsWith("TEMPORAL_EMPTY_BASELINE_") ||
      event.startsWith("TEMPORAL_RENDER_") ||
      event.startsWith("TEMPORAL_STALE_PROJECT_") ||
      event.startsWith("TEMPORAL_SCREENSHOT_")
    ) {
      console.debug(event, payload);
    }
    relayClientLog(event, payload);
    return;
  }
  console.info(event, payload);
}

function ensurePmtilesProtocol() {
  if (pmtilesProtocolRegistered) {
    return;
  }
  pmtilesProtocol = new Protocol();
  maplibregl.addProtocol("pmtiles", pmtilesProtocol.tile);
  pmtilesProtocolRegistered = true;
}

function polygonFeatureCollection(polygon: Polygon | null): FeatureCollection {
  if (!polygon) {
    return EMPTY_FEATURE_COLLECTION;
  }
  return {
    type: "FeatureCollection",
    features: [
      {
        type: "Feature",
        geometry: polygon,
        properties: {},
      },
    ],
  };
}

function mergeBuffers(layers: Record<string, Record<string, unknown>>): FeatureCollection {
  const features = Object.values(layers).flatMap((layer) =>
    Array.isArray(layer.features) ? (layer.features as FeatureCollection["features"]) : [],
  );
  return {
    type: "FeatureCollection",
    features,
  };
}

function bufferFeatureCollection(
  layers: Record<string, Record<string, unknown>> | null | undefined,
  key: string,
): FeatureCollection {
  return ensureFeatureCollection(layers?.[key] as FeatureCollection | null | undefined);
}

function ensureFeatureCollection(value: FeatureCollection | null | undefined): FeatureCollection {
  if (value && value.type === "FeatureCollection" && Array.isArray(value.features)) {
    return value;
  }
  return EMPTY_FEATURE_COLLECTION;
}

function isPolygonFeature(feature: Feature): feature is Feature<Polygon | MultiPolygon> {
  return feature.geometry?.type === "Polygon" || feature.geometry?.type === "MultiPolygon";
}

function isDerivedTemporalGeometry(properties: GeoJsonProperties | null | undefined): boolean {
  if (!properties) {
    return false;
  }

  const kind = typeof properties.kind === "string" ? properties.kind.toLowerCase() : "";
  const type = typeof properties.type === "string" ? properties.type.toLowerCase() : "";
  const changeType = typeof properties.change_type === "string" ? properties.change_type.toLowerCase() : "";
  const changeTypeCamel = typeof properties.changeType === "string" ? properties.changeType.toLowerCase() : "";

  if (properties.manualReplacement === true || properties.manual_override === true || properties.manualOverride === true) {
    return true;
  }
  if (properties.cumulative === true) {
    return true;
  }
  if (properties.group === true || properties.grouped === true || properties.group_id != null || properties.block_id != null) {
    return true;
  }
  return (
    kind.includes("buffer") ||
    kind.includes("block") ||
    kind.includes("footprint") ||
    kind.includes("union") ||
    kind.includes("envelope") ||
    kind.includes("manual") ||
    kind.includes("override") ||
    type.includes("buffer") ||
    type.includes("union") ||
    type.includes("manual") ||
    changeType.includes("buffer") ||
    changeTypeCamel.includes("buffer")
  );
}

function isExplicitAddition(properties: GeoJsonProperties | null | undefined): boolean {
  if (!properties) {
    return false;
  }
  const changeType = typeof properties.change_type === "string" ? properties.change_type.toLowerCase() : "";
  const changeTypeCamel = typeof properties.changeType === "string" ? properties.changeType.toLowerCase() : "";
  const status = typeof properties.status === "string" ? properties.status.toLowerCase() : "";
  const type = typeof properties.type === "string" ? properties.type.toLowerCase() : "";
  const kind = typeof properties.kind === "string" ? properties.kind.toLowerCase() : "";
  return (
    changeType === "addition" ||
    changeTypeCamel === "addition" ||
    status === "added" ||
    type === "addition" ||
    kind === "addition"
  );
}

function isExplicitNonAddition(properties: GeoJsonProperties | null | undefined): boolean {
  if (!properties) {
    return false;
  }
  const markers = [
    typeof properties.change_type === "string" ? properties.change_type.toLowerCase() : "",
    typeof properties.changeType === "string" ? properties.changeType.toLowerCase() : "",
    typeof properties.status === "string" ? properties.status.toLowerCase() : "",
    typeof properties.type === "string" ? properties.type.toLowerCase() : "",
    typeof properties.kind === "string" ? properties.kind.toLowerCase() : "",
  ].filter(Boolean);
  return markers.length > 0 && markers.every((marker) => marker !== "addition" && marker !== "added");
}

function getRawAdditionFeatures(features: FeatureCollection["features"]): FeatureCollection["features"] {
  return features.filter((feature) => {
    if (!isPolygonFeature(feature)) {
      return false;
    }
    const properties = feature.properties ?? {};
    if (isDerivedTemporalGeometry(properties)) {
      return false;
    }
    if (isExplicitAddition(properties)) {
      return true;
    }
    if (isExplicitNonAddition(properties)) {
      return false;
    }
    return true;
  });
}

function buildRawAdditionFeatureCollection(source: FeatureCollection | null | undefined): FeatureCollection {
  const normalized = ensureFeatureCollection(source);
  return featureCollection(getRawAdditionFeatures(normalized.features)) as FeatureCollection;
}

function sourceData(map: maplibregl.Map, sourceId: string, data: FeatureCollection) {
  const source = map.getSource(sourceId) as GeoJSONSource | undefined;
  if (source) {
    source.setData(data);
  }
}

function syncAoiMapSource(map: maplibregl.Map, polygon: Polygon | null) {
  if (!map.isStyleLoaded()) {
    devLog("AOI_RENDER_DEFERRED_MAP_NOT_READY", {
      hasAoi: Boolean(polygon),
    });
    return;
  }
  ensureOperationalLayers(map);
  devLog("AOI_MAP_LAYER_READY", {
    hasFillLayer: Boolean(map.getLayer("aoi-fill")),
    hasLineLayer: Boolean(map.getLayer("aoi-line")),
  });
  const data = polygonFeatureCollection(polygon);
  sourceData(map, "aoi", data);
  devLog(polygon ? "AOI_MAP_SOURCE_UPDATED" : "AOI_MAP_SOURCE_CLEARED", {
    featureCount: data.features.length,
    geometryType: polygon?.type ?? null,
  });
}

function browserSupportsWebGL(): boolean {
  try {
    const canvas = document.createElement("canvas");
    return Boolean(canvas.getContext("webgl2") || canvas.getContext("webgl") || canvas.getContext("experimental-webgl"));
  } catch {
    return false;
  }
}

function fitPolygon(map: MapLibreMap, polygon: Polygon | null) {
  if (!polygon) {
    return;
  }

  try {
    const bounds = new maplibregl.LngLatBounds();
    
    // Extract coordinates and validate each point
    const coords = polygon.coordinates[0];
    if (!Array.isArray(coords) || coords.length === 0) {
      console.warn("Invalid polygon coordinates: empty or non-array");
      return;
    }

    let hasValidCoord = false;
    for (const [lng, lat] of coords) {
      // Validate each coordinate is a valid number
      if (!Number.isFinite(lng) || !Number.isFinite(lat)) {
        console.warn(`Skipping invalid coordinate: [${lng}, ${lat}]`);
        continue;
      }
      
      // Validate coordinate is within WGS84 range
      if (lng < -180 || lng > 180 || lat < -90 || lat > 90) {
        console.warn(`Coordinate out of WGS84 range: [${lng}, ${lat}]`);
        continue;
      }
      
      bounds.extend([lng, lat]);
      hasValidCoord = true;
    }

    // Only fit bounds if we have at least one valid coordinate
    if (!hasValidCoord) {
      console.warn("No valid coordinates found in polygon");
      return;
    }

    // Validate bounds before applying
    const ne = bounds.getNorthEast();
    const sw = bounds.getSouthWest();
    
    if (!Number.isFinite(ne.lng) || !Number.isFinite(ne.lat) || 
        !Number.isFinite(sw.lng) || !Number.isFinite(sw.lat)) {
      console.warn("Invalid bounds computed from polygon coordinates");
      return;
    }

    map.fitBounds(bounds, { padding: 80, duration: 600 });
  } catch (error) {
    console.error("Error fitting polygon to bounds:", error);
    // Silently fail - don't break the map
  }
}

function applyLabelVisibility(map: MapLibreMap, showLabels: boolean) {
  const layers = map.getStyle()?.layers ?? [];
  layers.forEach((layer) => {
    if (layer.type === "symbol") {
      setLayerLayoutPropertyIfChanged(map, layer.id, "visibility", showLabels ? "visible" : "none");
    }
  });
}

function createEditMarker(): HTMLDivElement {
  const element = document.createElement("div");
  element.className = "map-edit-marker";
  return element;
}

function imageCoordinatesFromBBox(bbox: [number, number, number, number] | null): [[number, number], [number, number], [number, number], [number, number]] | null {
  if (!bbox) {
    return null;
  }
  const [west, south, east, north] = bbox;
  return [
    [west, north],
    [east, north],
    [east, south],
    [west, south],
  ];
}

function hasValidRasterBounds(bounds: number[] | null | undefined): bounds is [number, number, number, number] {
  return Array.isArray(bounds) && bounds.length >= 4 && bounds.every((value) => Number.isFinite(value));
}

function syncImageOverlay(
  map: MapLibreMap,
  sourceId: string,
  layerId: string,
  url: string | null,
  coordinates: [[number, number], [number, number], [number, number], [number, number]] | null,
  opacity: number,
  visible: boolean,
) {
  const existingLayer = map.getLayer(layerId);
  const existingSource = map.getSource(sourceId) as (maplibregl.Source & { updateImage?: (options: { url: string; coordinates: [[number, number], [number, number], [number, number], [number, number]] }) => void }) | undefined;

  if (!url || !coordinates) {
    if (existingLayer) {
      map.removeLayer(layerId);
    }
    if (existingSource) {
      map.removeSource(sourceId);
    }
    return;
  }

  if (existingSource?.updateImage) {
    existingSource.updateImage({ url, coordinates });
  } else {
    if (existingLayer) {
      map.removeLayer(layerId);
    }
    if (existingSource) {
      map.removeSource(sourceId);
    }
    map.addSource(sourceId, {
      type: "image",
      url,
      coordinates,
    });
    map.addLayer(
      {
        id: layerId,
        type: "raster",
        source: sourceId,
        layout: { visibility: visible ? "visible" : "none" },
        paint: { "raster-opacity": opacity },
      },
      "detected-polygons-fill",
    );
  }

  setPaintPropertyIfChanged(map, layerId, "raster-opacity", opacity);
  setLayerLayoutPropertyIfChanged(map, layerId, "visibility", visible ? "visible" : "none");
}

function sanitizeMapLibreId(value: string): string {
  return value.replace(/[^a-zA-Z0-9_-]+/g, "-");
}

function lngLatToTile(lng: number, lat: number, zoom: number): { x: number; y: number } {
  const latRad = (Math.max(Math.min(lat, 85.05112878), -85.05112878) * Math.PI) / 180;
  const n = 2 ** zoom;
  const x = Math.floor(((lng + 180) / 360) * n);
  const y = Math.floor(((1 - Math.asinh(Math.tan(latRad)) / Math.PI) / 2) * n);
  return {
    x: Math.max(0, Math.min(n - 1, x)),
    y: Math.max(0, Math.min(n - 1, y)),
  };
}

function visibleTileCoordinates(
  map: MapLibreMap,
  zoom: number,
  maxTiles = 64,
): Array<{ z: number; x: number; y: number }> {
  const bounds = map.getBounds();
  if (!bounds) {
    return [];
  }
  const northWest = lngLatToTile(bounds.getWest(), bounds.getNorth(), zoom);
  const southEast = lngLatToTile(bounds.getEast(), bounds.getSouth(), zoom);
  const minX = Math.min(northWest.x, southEast.x);
  const maxX = Math.max(northWest.x, southEast.x);
  const minY = Math.min(northWest.y, southEast.y);
  const maxY = Math.max(northWest.y, southEast.y);
  const coords: Array<{ z: number; x: number; y: number }> = [];
  for (let x = minX; x <= maxX; x += 1) {
    for (let y = minY; y <= maxY; y += 1) {
      coords.push({ z: zoom, x, y });
      if (coords.length >= maxTiles) {
        return coords;
      }
    }
  }
  return coords;
}

function temporalReferenceSourceId(projectId: string | null, releaseIdentifier: string): string {
  return `temporal-reference-source-${sanitizeMapLibreId(projectId ?? "unknown")}-${sanitizeMapLibreId(releaseIdentifier)}`;
}

function temporalReferenceLayerId(projectId: string | null, releaseIdentifier: string): string {
  return `temporal-reference-layer-${sanitizeMapLibreId(projectId ?? "unknown")}-${sanitizeMapLibreId(releaseIdentifier)}`;
}

function temporalAddedSourceId(projectId: string | null, releaseIdentifier: string, kind: TemporalAddedLayerKind): string {
  return `temporal-added-source-${sanitizeMapLibreId(projectId ?? "unknown")}-${sanitizeMapLibreId(releaseIdentifier)}-${kind}`;
}

function temporalAddedLayerId(projectId: string | null, releaseIdentifier: string, kind: TemporalAddedLayerKind): string {
  return `temporal-added-layer-${sanitizeMapLibreId(projectId ?? "unknown")}-${sanitizeMapLibreId(releaseIdentifier)}-${kind}`;
}

function temporalAddedLineLayerId(projectId: string | null, releaseIdentifier: string, kind: TemporalAddedLayerKind): string {
  return `${temporalAddedLayerId(projectId, releaseIdentifier, kind)}-line`;
}

function isMilestoneStyledTemporalLayer(kind: TemporalAddedLayerKind): kind is TemporalStyledLayerKind {
  return (
    kind === "additions" ||
    kind === "buffer10m" ||
    kind === "buffer15m" ||
    kind === "buffer20m" ||
    kind === "cumulativeBuffer10m" ||
    kind === "cumulativeBuffer15m" ||
    kind === "cumulativeBuffer20m"
  );
}

function isCumulativeBufferLayerKind(
  kind: TemporalAddedLayerKind,
): kind is "cumulativeBuffer10m" | "cumulativeBuffer15m" | "cumulativeBuffer20m" {
  return kind === "cumulativeBuffer10m" || kind === "cumulativeBuffer15m" || kind === "cumulativeBuffer20m";
}

function temporalAddedLayerIds(projectId: string | null, releaseIdentifier: string, kind: TemporalAddedLayerKind): string[] {
  const fillLayerId = temporalAddedLayerId(projectId, releaseIdentifier, kind);
  return isMilestoneStyledTemporalLayer(kind) ? [fillLayerId, temporalAddedLineLayerId(projectId, releaseIdentifier, kind)] : [fillLayerId];
}

const TEMPORAL_ADDED_LAYER_DEFINITIONS: TemporalAddedLayerDefinition[] = [
  {
    kind: "additions",
    toggleKey: "temporalAdditions",
    paint: { "fill-color": "#B91C1C", "fill-opacity": 0.88, "fill-outline-color": "#B91C1C" },
    data: (overlay) => buildRawAdditionFeatureCollection(overlay.additions),
  },
  {
    kind: "buffer10m",
    toggleKey: "buffer10m",
    paint: { "fill-color": "#B91C1C", "fill-opacity": TEMPORAL_BUFFER_FILL_OPACITY, "fill-outline-color": "rgba(0, 0, 0, 0)" },
    data: (overlay) => ensureFeatureCollection(overlay.buffer10m),
  },
  {
    kind: "buffer15m",
    toggleKey: "buffer15m",
    paint: { "fill-color": BUILDING_CHANGE_BUFFER_FILL_COLORS["15m"], "fill-opacity": TEMPORAL_BUFFER_FILL_OPACITY, "fill-outline-color": "rgba(0, 0, 0, 0)" },
    data: (overlay) => ensureFeatureCollection(overlay.buffer15m),
  },
  {
    kind: "buffer20m",
    toggleKey: "buffer20m",
    paint: { "fill-color": BUILDING_CHANGE_BUFFER_FILL_COLORS["20m"], "fill-opacity": TEMPORAL_BUFFER_FILL_OPACITY, "fill-outline-color": "rgba(0, 0, 0, 0)" },
    data: (overlay) => ensureFeatureCollection(overlay.buffer20m),
  },
  {
    kind: "cumulativeBuffer10m",
    toggleKey: "temporalCumulativeBuffer10m",
    paint: { "fill-color": BUILDING_CHANGE_BUFFER_FILL_COLORS["10m"], "fill-opacity": TEMPORAL_BUFFER_FILL_OPACITY },
    data: (overlay) => ensureFeatureCollection(overlay.cumulativeBuffer10m),
  },
  {
    kind: "cumulativeBuffer15m",
    toggleKey: "temporalCumulativeBuffer15m",
    paint: { "fill-color": BUILDING_CHANGE_BUFFER_FILL_COLORS["15m"], "fill-opacity": TEMPORAL_BUFFER_FILL_OPACITY },
    data: (overlay) => ensureFeatureCollection(overlay.cumulativeBuffer15m),
  },
  {
    kind: "cumulativeBuffer20m",
    toggleKey: "temporalCumulativeBuffer20m",
    paint: { "fill-color": BUILDING_CHANGE_BUFFER_FILL_COLORS["20m"], "fill-opacity": TEMPORAL_BUFFER_FILL_OPACITY },
    data: (overlay) => ensureFeatureCollection(overlay.cumulativeBuffer20m),
  },
  {
    kind: "automatedBuildingBlocks",
    toggleKey: "temporalAutomatedBuildingBlocks",
    paint: { "fill-color": "#2563eb", "fill-opacity": 0.9 },
    data: (overlay) => ensureFeatureCollection(overlay.automatedBuildingBlocks),
  },
];

function temporalAddedDataStats(data: FeatureCollection): { featureCount: number; payloadBytes: number; signature: string } {
  const serialized = JSON.stringify(data);
  return {
    featureCount: data.features.length,
    payloadBytes: new Blob([serialized]).size,
    signature: `${data.features.length}:${serialized.length}:${hashString(serialized)}`,
  };
}

function temporalAddedArtifactKey(kind: TemporalAddedLayerKind): string {
  switch (kind) {
    case "additions":
      return "additions";
    case "buffer10m":
      return "building_change_buffer_10m";
    case "buffer15m":
      return "building_change_buffer_15m";
    case "buffer20m":
      return "building_change_buffer_20m";
    case "cumulativeBuffer10m":
      return "cumulative_building_change_buffer_10m";
    case "cumulativeBuffer15m":
      return "cumulative_building_change_buffer_15m";
    case "cumulativeBuffer20m":
      return "cumulative_building_change_buffer_20m";
    case "automated":
      return "additions";
    case "automatedBuildingBlocks":
      return "automated_building_blocks";
    case "effectiveBuildingBlocks":
      return "additions";
    case "cumulative":
      return "additions";
    case "cumulativeGrowthBlocks":
      return "additions";
    case "cumulativeGrowthEnvelope":
      return "additions";
    case "manualOverride":
      return "manual_override";
  }
}

function temporalPlanningKeyForKind(kind: TemporalAddedLayerKind): TemporalLayerPlanningKey {
  switch (kind) {
    case "additions":
      return "allNewBuildings";
    case "buffer10m":
      return "buffer10m";
    case "buffer15m":
      return "buffer15m";
    case "buffer20m":
      return "buffer20m";
    case "cumulativeBuffer10m":
      return "temporalCumulativeBuffer10m";
    case "cumulativeBuffer15m":
      return "temporalCumulativeBuffer15m";
    case "cumulativeBuffer20m":
      return "temporalCumulativeBuffer20m";
    case "cumulative":
      return "cumulativeUnion";
    case "manualOverride":
      return "manualOverride";
    case "automated":
    case "automatedBuildingBlocks":
    case "effectiveBuildingBlocks":
    case "cumulativeGrowthBlocks":
    case "cumulativeGrowthEnvelope":
      return "selectedAdditions";
  }
}

function createEmptyTemporalReleaseSetByKind(): Record<TemporalAddedLayerKind, string[]> {
  const empty = {} as Record<TemporalAddedLayerKind, string[]>;
  for (const definition of TEMPORAL_ADDED_LAYER_DEFINITIONS) {
    empty[definition.kind] = [];
  }
  return empty;
}

function buildAvailableReleaseIdentifiersByKind(
  overlays: TemporalAddedOverlayPresentation[],
): Record<TemporalAddedLayerKind, string[]> {
  const availableByKind = createEmptyTemporalReleaseSetByKind();
  for (const definition of TEMPORAL_ADDED_LAYER_DEFINITIONS) {
    availableByKind[definition.kind] = overlays
      .filter((overlay) => temporalAddedLayerAvailability(overlay, definition).available)
      .map((overlay) => overlay.releaseIdentifier);
  }
  return availableByKind;
}

function buildExpectedTemporalReleaseSets(params: {
  definitions: TemporalAddedLayerDefinition[];
  milestones: TemporalMilestoneColorInput[];
  selectedReleaseIdentifier: string | null | undefined;
  availableByKind: Record<TemporalAddedLayerKind, string[]>;
  includedAdditionReleaseIdentifiers: string[];
  layerState: LayerToggleState;
}): Record<TemporalAddedLayerKind, string[]> {
  const releaseSets = createEmptyTemporalReleaseSetByKind();
  const { definitions, milestones, selectedReleaseIdentifier, availableByKind, includedAdditionReleaseIdentifiers, layerState } =
    params;
  for (const definition of definitions) {
    if (definition.kind === "additions") {
      const releases = new Set<string>();
      if (layerState.temporalAdditions) {
        for (const releaseIdentifier of includedAdditionReleaseIdentifiers) {
          releases.add(releaseIdentifier);
        }
      }
      if (layerState.selectedMilestoneAdditions) {
        for (const releaseIdentifier of getTemporalLayerExpectedReleases({
          layerKey: "selectedAdditions",
          milestones,
          selectedReleaseIdentifier,
          availableReleaseIdentifiers: availableByKind.additions,
        })) {
          releases.add(releaseIdentifier);
        }
      }
      releaseSets.additions = Array.from(releases);
      continue;
    }

    if (!layerState[definition.toggleKey]) {
      releaseSets[definition.kind] = [];
      continue;
    }

    releaseSets[definition.kind] = getTemporalLayerExpectedReleases({
      layerKey: temporalPlanningKeyForKind(definition.kind),
      milestones,
      selectedReleaseIdentifier,
      availableReleaseIdentifiers: availableByKind[definition.kind],
    });
  }
  return releaseSets;
}

function temporalExpectedReleaseSetForKind(
  releaseSetsByKind: TemporalReleaseSetByKind | null | undefined,
  kind: TemporalAddedLayerKind,
): Set<string> {
  return new Set(releaseSetsByKind?.[kind] ?? []);
}

function shouldUseVectorTileArtifact(overlay: TemporalAddedOverlayPresentation, kind: TemporalAddedLayerKind): boolean {
  const artifact = overlay.artifacts[temporalAddedArtifactKey(kind)];
  return Boolean(artifact?.tilejsonUrl || artifact?.tilesUrlTemplate);
}

function normalizeTileUrlTemplate(template: string | null | undefined): string | null {
  if (!template) {
    return null;
  }
  return template.replace(/%7B/gi, "{").replace(/%7D/gi, "}");
}

function vectorTileMetadataPayloadBytes(artifact: TemporalAddedOverlayPresentation["artifacts"][string] | null | undefined): number {
  if (!artifact) {
    return 0;
  }
  return new Blob([
    JSON.stringify({
      key: artifact.key,
      featureCount: artifact.featureCount ?? 0,
      tilejsonUrl: artifact.tilejsonUrl ?? null,
      tilesUrlTemplate: artifact.tilesUrlTemplate ?? null,
      vectorSourceLayer: artifact.vectorSourceLayer ?? "results",
      bbox: artifact.bbox ?? null,
    }),
  ]).size;
}

function temporalAddedLayerAvailability(
  overlay: TemporalAddedOverlayPresentation,
  definition: TemporalAddedLayerDefinition,
): {
  available: boolean;
  reason: "vector_tile_artifact" | "inline_geojson" | "artifact_metadata_pending" | "empty_geojson" | "missing_artifact";
  featureCount: number;
  payloadBytes: number;
  useVectorTiles: boolean;
} {
  const artifact = overlay.artifacts[temporalAddedArtifactKey(definition.kind)] ?? null;
  const useVectorTiles = shouldUseVectorTileArtifact(overlay, definition.kind);
  if (useVectorTiles) {
    return {
      available: true,
      reason: "vector_tile_artifact",
      featureCount: artifact?.featureCount ?? 0,
      payloadBytes: vectorTileMetadataPayloadBytes(artifact),
      useVectorTiles: true,
    };
  }
  const data = definition.data(overlay);
  const stats = temporalAddedDataStats(data);
  if (data.features.length === 0 && artifact) {
    const artifactFeatureCount = artifact.featureCount ?? null;
    const artifactSizeBytes = artifact.sizeBytes ?? 0;
    return {
      available: (artifactFeatureCount ?? 0) > 0 && Boolean(artifact.artifactUrl),
      reason: artifactFeatureCount === 0 ? "empty_geojson" : "artifact_metadata_pending",
      featureCount: artifactFeatureCount ?? 0,
      payloadBytes: artifactSizeBytes,
      useVectorTiles: false,
    };
  }
  return {
    available: data.features.length > 0,
    reason: data.features.length > 0 ? "inline_geojson" : artifact ? "empty_geojson" : "missing_artifact",
    featureCount: stats.featureCount,
    payloadBytes: stats.payloadBytes,
    useVectorTiles: false,
  };
}

function buildTemporalOutputLayerPlan(params: {
  projectId: string;
  overlay: TemporalAddedOverlayPresentation;
  definition: TemporalAddedLayerDefinition;
  baselineReleaseIdentifier: string | null;
  layerState: LayerToggleState;
}): { plan: TemporalOutputLayerPlan | null; availability: ReturnType<typeof temporalAddedLayerAvailability> } {
  const { projectId, overlay, definition, baselineReleaseIdentifier, layerState } = params;
  const artifactKey = temporalAddedArtifactKey(definition.kind);
  const artifact = overlay.artifacts[artifactKey] ?? null;
  const availability = temporalAddedLayerAvailability(overlay, definition);
  if (!availability.available) {
    return { plan: null, availability };
  }
  const useVectorTiles = availability.useVectorTiles;
  const enabledFromUi = Boolean(
    definition.kind === "additions"
      ? layerState.temporalAdditions || layerState.selectedMilestoneAdditions
      : layerState[definition.toggleKey],
  );
  if (definition.toggleKey === "buffer10m") {
    devLog("TEMPORAL_OUTPUT_LAYER_CHECKBOX_STATE", {
      projectId,
      releaseIdentifier: overlay.releaseIdentifier,
      layerKey: definition.toggleKey,
      checked: enabledFromUi,
    });
    devLog("TEMPORAL_OUTPUT_LAYER_PLAN_ENABLED_FROM_UI", {
      projectId,
      releaseIdentifier: overlay.releaseIdentifier,
      layerKey: definition.toggleKey,
      enabled: enabledFromUi,
    });
  }
  return {
    availability,
    plan: {
      projectId,
      releaseIdentifier: overlay.releaseIdentifier,
      layerKey: definition.toggleKey,
      artifactKey,
      layerIds: temporalAddedLayerIds(projectId, overlay.releaseIdentifier, definition.kind),
      sourceId: temporalAddedSourceId(projectId, overlay.releaseIdentifier, definition.kind),
      sourceType: useVectorTiles ? "vector" : "geojson",
      renderStrategy: useVectorTiles ? "vector_tiles" : "geojson",
      featureCount: artifact?.featureCount ?? availability.featureCount,
      sizeBytes: artifact?.sizeBytes ?? availability.payloadBytes,
      isBaseline: baselineReleaseIdentifier === overlay.releaseIdentifier,
      isEmpty: availability.featureCount === 0 && !useVectorTiles,
      enabled: enabledFromUi,
      availabilityReason: availability.reason,
      tilejsonUrl: artifact?.tilejsonUrl ?? null,
      sourceLayer: artifact?.vectorSourceLayer ?? null,
      overlay,
      definition,
    },
  };
}

function hashString(value: string): string {
  let hash = 0;
  for (let index = 0; index < value.length; index += 1) {
    hash = (hash * 31 + value.charCodeAt(index)) | 0;
  }
  return String(hash >>> 0);
}

function applyTemporalAddedLayerStyle(
  map: MapLibreMap,
  projectId: string,
  releaseIdentifier: string,
  kind: TemporalAddedLayerKind,
  milestoneColor: string,
) {
  if (!isMilestoneStyledTemporalLayer(kind)) {
    return;
  }
  const fillLayerId = temporalAddedLayerId(projectId, releaseIdentifier, kind);
  const lineLayerId = temporalAddedLineLayerId(projectId, releaseIdentifier, kind);
  const temporalLayerPaint = getTemporalLayerPaint(kind, milestoneColor);
  if (map.getLayer(fillLayerId)) {
    for (const [property, value] of Object.entries(temporalLayerPaint.fillPaint)) {
      setPaintPropertyIfChanged(map, fillLayerId, property, value);
    }
  }
  if (map.getLayer(lineLayerId)) {
    for (const [property, value] of Object.entries(temporalLayerPaint.linePaint)) {
      setPaintPropertyIfChanged(map, lineLayerId, property, value);
    }
  }
  devLog("TEMPORAL_OUTPUT_LAYER_STYLE_APPLY", {
    projectId,
    releaseIdentifier,
    layerKind: kind,
    color: milestoneColor,
    fillOpacity: temporalLayerPaint.fillOpacity,
    lineOpacity: temporalLayerPaint.lineOpacity,
  });
}

function temporalLayerOrderIndex(map: MapLibreMap, layerId: string): number {
  return map.getStyle()?.layers?.findIndex((layer) => layer.id === layerId) ?? -1;
}

function publishTemporalRuntimeDebugSnapshot(params: {
  map: MapLibreMap;
  projectId: string;
  selectedReleaseIdentifier: string;
  layerState: LayerToggleState;
  includedAdditionReleaseIdentifiers: string[];
  availableAdditionReleaseIdentifiers: string[];
  colorByReleaseIdentifier: Record<string, string>;
  additionRegistrationPlans: string[];
  referenceReleaseIdentifier: string | null;
  expectedReleaseIdentifiersByKind: TemporalReleaseSetByKind;
  availableReleaseIdentifiersByKind: TemporalReleaseSetByKind;
  labelByLayerKey: Partial<Record<LayerToggleKey | "allNewBuildings" | "selectedAdditions", string>>;
}): TemporalRuntimeDebugSnapshot | null {
  if (typeof window === "undefined") {
    return null;
  }
  const debugEnabled =
    import.meta.env.DEV ||
    import.meta.env.VITE_TEMPORAL_RENDER_AUDIT === "true" ||
    new URLSearchParams(window.location.search).has("debugRenderAudit");
  if (!debugEnabled) {
    return null;
  }
  const {
    map,
    projectId,
    selectedReleaseIdentifier,
    layerState,
    includedAdditionReleaseIdentifiers,
    availableAdditionReleaseIdentifiers,
    colorByReleaseIdentifier,
    additionRegistrationPlans,
    referenceReleaseIdentifier,
    expectedReleaseIdentifiersByKind,
    availableReleaseIdentifiersByKind,
    labelByLayerKey,
  } = params;
  const includedSet = new Set(includedAdditionReleaseIdentifiers);
  const referenceLayerId = referenceReleaseIdentifier ? temporalReferenceLayerId(projectId, referenceReleaseIdentifier) : null;
  const referenceLayerOrder = referenceLayerId ? temporalLayerOrderIndex(map, referenceLayerId) : -1;
  const registeredAdditionSources: string[] = [];
  const registeredAdditionLayers: string[] = [];
  const visibleAdditionLayers: string[] = [];
  const hiddenFutureAdditionLayers: string[] = [];
  const missing: string[] = [];
  const releases: TemporalRuntimeDebugSnapshot["releases"] = {};
  const mapWithOptionalFilter = map as MapLibreMap & { getFilter?: (layerId: string) => unknown };

  for (const releaseIdentifier of availableAdditionReleaseIdentifiers) {
    const sourceId = temporalAddedSourceId(projectId, releaseIdentifier, "additions");
    const layerId = temporalAddedLayerId(projectId, releaseIdentifier, "additions");
    const lineLayerId = temporalAddedLineLayerId(projectId, releaseIdentifier, "additions");
    const sourceExists = Boolean(map.getSource(sourceId));
    const layerExists = Boolean(map.getLayer(layerId));
    const lineLayerExists = Boolean(map.getLayer(lineLayerId));
    const visibility =
      layerExists && map.getLayoutProperty(layerId, "visibility") === "visible"
        ? "visible"
        : layerExists
          ? "none"
          : null;
    const lineVisibility =
      lineLayerExists && map.getLayoutProperty(lineLayerId, "visibility") === "visible"
        ? "visible"
        : lineLayerExists
          ? "none"
          : null;
    const orderIndex = layerExists ? temporalLayerOrderIndex(map, layerId) : -1;
    const aboveReferenceRaster =
      layerExists && referenceLayerOrder >= 0 ? orderIndex > referenceLayerOrder : layerExists ? true : null;

    if (sourceExists) {
      registeredAdditionSources.push(releaseIdentifier);
    }
    if (layerExists) {
      registeredAdditionLayers.push(releaseIdentifier);
    }
    if (visibility === "visible") {
      visibleAdditionLayers.push(releaseIdentifier);
    }
    if (!includedSet.has(releaseIdentifier) && layerExists && visibility !== "visible") {
      hiddenFutureAdditionLayers.push(releaseIdentifier);
    }
    if (includedSet.has(releaseIdentifier)) {
      if (!sourceExists) {
        missing.push(`${releaseIdentifier}:source`);
      }
      if (!layerExists) {
        missing.push(`${releaseIdentifier}:layer`);
      }
      if (layerExists && visibility !== "visible" && layerState.temporalAdditions) {
        missing.push(`${releaseIdentifier}:visible`);
      }
    }

    releases[releaseIdentifier] = {
      sourceId,
      layerId,
      lineLayerId,
      sourceExists,
      layerExists,
      lineLayerExists,
      visibility,
      lineVisibility,
      filter:
        layerExists && typeof mapWithOptionalFilter.getFilter === "function"
          ? mapWithOptionalFilter.getFilter(layerId) ?? null
          : null,
      color: layerExists ? map.getPaintProperty(layerId, "fill-color") ?? null : null,
      lineColor: lineLayerExists ? map.getPaintProperty(lineLayerId, "line-color") ?? null : null,
      orderIndex,
      aboveReferenceRaster,
    };
  }

  const enabledLayerKeys: LayerToggleKey[] = dedupeStable([
    ...TEMPORAL_ADDED_LAYER_DEFINITIONS.filter((definition) =>
      definition.kind === "additions"
        ? layerState.temporalAdditions || layerState.selectedMilestoneAdditions
        : layerState[definition.toggleKey],
    ).map((definition) => definition.toggleKey),
    ...(layerState.selectedMilestoneAdditions ? (["selectedMilestoneAdditions"] as const) : []),
  ]);
  const makeContract = (params: {
    uiLabel: string;
    layerKey: LayerToggleKey | "allNewBuildings" | "selectedAdditions";
    kind: TemporalAddedLayerKind;
    mode: TemporalLayerContractSnapshot["mode"];
    expectedReleases: string[];
    availableReleases: string[];
  }): TemporalLayerContractSnapshot => {
    const expectedReleaseSet = new Set(params.expectedReleases);
    const registeredSources = params.availableReleases.filter((releaseIdentifier) =>
      Boolean(map.getSource(temporalAddedSourceId(projectId, releaseIdentifier, params.kind))),
    );
    const registeredLayers = params.availableReleases.filter((releaseIdentifier) =>
      temporalAddedLayerIds(projectId, releaseIdentifier, params.kind).some((layerId) => Boolean(map.getLayer(layerId))),
    );
    const visibleLayers = params.availableReleases.filter((releaseIdentifier) =>
      temporalAddedLayerIds(projectId, releaseIdentifier, params.kind).some(
        (layerId) => map.getLayer(layerId) && map.getLayoutProperty(layerId, "visibility") === "visible",
      ),
    );
    return {
      uiLabel: params.uiLabel,
      layerKey: params.layerKey,
      artifactKey: temporalAddedArtifactKey(params.kind),
      mode: params.mode,
      expectedReleases: params.expectedReleases,
      registeredSources,
      registeredLayers,
      visibleLayers,
      unexpectedVisibleReleases: visibleLayers.filter((releaseIdentifier) => !expectedReleaseSet.has(releaseIdentifier)),
      unexpectedRegisteredReleases: registeredLayers.filter((releaseIdentifier) => !expectedReleaseSet.has(releaseIdentifier)),
    };
  };
  const layerIdCounts = new Map<string, number>();
  const sourceIdCounts = new Map<string, number>();
  const bufferLayers: TemporalRuntimeDebugSnapshot["bufferLayers"] = [];
  for (const definition of TEMPORAL_ADDED_LAYER_DEFINITIONS.filter(
    (candidate) =>
      candidate.kind === "buffer10m" ||
      candidate.kind === "buffer15m" ||
      candidate.kind === "buffer20m" ||
      candidate.kind === "cumulativeBuffer10m" ||
      candidate.kind === "cumulativeBuffer15m" ||
      candidate.kind === "cumulativeBuffer20m",
  )) {
    const expectedReleaseSet = temporalExpectedReleaseSetForKind(expectedReleaseIdentifiersByKind, definition.kind);
    const availableReleases = availableReleaseIdentifiersByKind[definition.kind] ?? [];
    for (const releaseIdentifier of availableReleases) {
      const sourceId = temporalAddedSourceId(projectId, releaseIdentifier, definition.kind);
      const layerId = temporalAddedLayerId(projectId, releaseIdentifier, definition.kind);
      const lineLayerId = temporalAddedLineLayerId(projectId, releaseIdentifier, definition.kind);
      layerIdCounts.set(layerId, (layerIdCounts.get(layerId) ?? 0) + 1);
      layerIdCounts.set(lineLayerId, (layerIdCounts.get(lineLayerId) ?? 0) + 1);
      sourceIdCounts.set(sourceId, (sourceIdCounts.get(sourceId) ?? 0) + 1);
      const expectedColor = colorByReleaseIdentifier[releaseIdentifier] ?? "#B91C1C";
      bufferLayers.push({
        layerId,
        lineLayerId,
        sourceId,
        layerKey: definition.toggleKey,
        artifactKey: temporalAddedArtifactKey(definition.kind),
        releaseIdentifier,
        expectedColor,
        actualFillColor: map.getLayer(layerId) ? map.getPaintProperty(layerId, "fill-color") ?? null : null,
        actualLineColor: map.getLayer(lineLayerId) ? map.getPaintProperty(lineLayerId, "line-color") ?? null : null,
        visibility: map.getLayer(layerId) ? (map.getLayoutProperty(layerId, "visibility") as string | null) : null,
        lineVisibility: map.getLayer(lineLayerId)
          ? (map.getLayoutProperty(lineLayerId, "visibility") as string | null)
          : null,
        expectedVisible: expectedReleaseSet.has(releaseIdentifier) && Boolean(layerState[definition.toggleKey]),
      });
    }
  }
  const layerIdCollisions = Array.from(layerIdCounts.entries())
    .filter(([, count]) => count > 1)
    .map(([layerId]) => layerId);
  const sourceIdCollisions = Array.from(sourceIdCounts.entries())
    .filter(([, count]) => count > 1)
    .map(([sourceId]) => sourceId);
  const selectedAdditionExpectedReleases =
    layerState.selectedMilestoneAdditions && selectedReleaseIdentifier && availableAdditionReleaseIdentifiers.includes(selectedReleaseIdentifier)
      ? [selectedReleaseIdentifier]
      : [];
  const layerContracts: TemporalLayerContractSnapshot[] = [
    makeContract({
      uiLabel: labelByLayerKey.allNewBuildings ?? "All new buildings",
      layerKey: "allNewBuildings",
      kind: "additions",
      mode: "cumulative",
      expectedReleases: layerState.temporalAdditions ? includedAdditionReleaseIdentifiers : [],
      availableReleases: availableAdditionReleaseIdentifiers,
    }),
    makeContract({
      uiLabel: labelByLayerKey.selectedAdditions ?? "Added building",
      layerKey: "selectedAdditions",
      kind: "additions",
      mode: "selected",
      expectedReleases: selectedAdditionExpectedReleases,
      availableReleases: availableAdditionReleaseIdentifiers,
    }),
    ...TEMPORAL_ADDED_LAYER_DEFINITIONS.filter((definition) => definition.kind !== "additions").map((definition) =>
      makeContract({
        uiLabel: labelByLayerKey[definition.toggleKey] ?? definition.toggleKey,
        layerKey: definition.toggleKey,
        kind: definition.kind,
        mode:
          definition.kind === "cumulative"
            ? "selected_source_cumulative_artifact"
            : isCumulativeBufferLayerKind(definition.kind)
              ? "cumulative"
              : "selected",
        expectedReleases: expectedReleaseIdentifiersByKind[definition.kind] ?? [],
        availableReleases: availableReleaseIdentifiersByKind[definition.kind] ?? [],
      }),
    ),
  ];

  const snapshot: TemporalRuntimeDebugSnapshot = {
    projectId,
    selectedRelease: selectedReleaseIdentifier,
    enabledLayerKeys,
    layerContracts,
    bufferLayers,
    layerIdCollisions,
    sourceIdCollisions,
    allNewBuildingsEnabled: layerState.temporalAdditions,
    selectedAdditionsEnabled: layerState.selectedMilestoneAdditions,
    includedAdditionReleases: includedAdditionReleaseIdentifiers,
    additionRegistrationPlans,
    registeredAdditionSources,
    registeredAdditionLayers,
    visibleAdditionLayers,
    hiddenFutureAdditionLayers,
    missing,
    releases,
  };
  const existing = window.__SATMONITOR_TEMPORAL_DEBUG__;
  const history = [...(existing?.history ?? []), snapshot].slice(-25);
  window.__SATMONITOR_TEMPORAL_DEBUG__ = {
    latest: snapshot,
    history,
    getLatest: () => window.__SATMONITOR_TEMPORAL_DEBUG__?.latest ?? null,
  };
  console.debug("SATMONITOR_TEMPORAL_DEBUG_SNAPSHOT", snapshot);
  return snapshot;
}

function temporalVectorTileLayerMinzoom(
  kind: TemporalAddedLayerKind,
  layerType: "fill" | "line",
  _sizeBytes: number | null | undefined,
  _featureCount: number | null | undefined,
): number {
  if (layerType === "line") {
    return isMilestoneStyledTemporalLayer(kind) ? 14 : 15;
  }
  return isMilestoneStyledTemporalLayer(kind) ? 11 : 12;
}

function setLayerZoomRangeIfSupported(map: MapLibreMap, layerId: string, minzoom: number, maxzoom = 24): void {
  const zoomRangeMap = map as MapLibreMap & { setLayerZoomRange?: (layerId: string, minzoom: number, maxzoom: number) => void };
  if (typeof zoomRangeMap.setLayerZoomRange === "function") {
    zoomRangeMap.setLayerZoomRange(layerId, minzoom, maxzoom);
  }
}

function ensureTemporalAddedLayer(
  map: MapLibreMap,
  projectId: string,
  overlay: TemporalAddedOverlayPresentation,
  definition: TemporalAddedLayerDefinition,
  sourceSignatures: Record<string, string>,
  milestoneColor: string,
): TemporalAddedLayerLifecycle {
  const sourceId = temporalAddedSourceId(projectId, overlay.releaseIdentifier, definition.kind);
  const layerId = temporalAddedLayerId(projectId, overlay.releaseIdentifier, definition.kind);
  const lineLayerId = isMilestoneStyledTemporalLayer(definition.kind)
    ? temporalAddedLineLayerId(projectId, overlay.releaseIdentifier, definition.kind)
    : null;
  const temporalLayerPaint = isMilestoneStyledTemporalLayer(definition.kind)
    ? getTemporalLayerPaint(definition.kind, milestoneColor)
    : null;
  const fillPaint = temporalLayerPaint?.fillPaint ?? definition.paint;
  const vectorArtifact = overlay.artifacts[temporalAddedArtifactKey(definition.kind)] ?? null;
  const useVectorTiles = shouldUseVectorTileArtifact(overlay, definition.kind);
  const vectorSourceLayer = vectorArtifact?.vectorSourceLayer ?? "results";
  const vectorTilesUrlTemplate = normalizeTileUrlTemplate(vectorArtifact?.tilesUrlTemplate);
  const vectorFillMinzoom = useVectorTiles
    ? temporalVectorTileLayerMinzoom(definition.kind, "fill", vectorArtifact?.sizeBytes, vectorArtifact?.featureCount)
    : 0;
  const vectorLineMinzoom = useVectorTiles
    ? temporalVectorTileLayerMinzoom(definition.kind, "line", vectorArtifact?.sizeBytes, vectorArtifact?.featureCount)
    : 0;
  const inlineData = useVectorTiles ? EMPTY_FEATURE_COLLECTION : definition.data(overlay);
  const geojsonArtifactUrl =
    !useVectorTiles && inlineData.features.length === 0 && vectorArtifact?.artifactUrl ? vectorArtifact.artifactUrl : null;
  const data = inlineData;
  const dataStats = useVectorTiles
    ? {
        featureCount: vectorArtifact?.featureCount ?? 0,
        payloadBytes: vectorTileMetadataPayloadBytes(vectorArtifact),
        signature: `vector:explicit_tiles:${vectorArtifact?.tilejsonUrl ?? ""}:${vectorTilesUrlTemplate ?? ""}:${vectorArtifact?.featureCount ?? 0}:${vectorArtifact?.sizeBytes ?? 0}`,
      }
    : geojsonArtifactUrl
      ? {
          featureCount: vectorArtifact?.featureCount ?? 0,
          payloadBytes: vectorArtifact?.sizeBytes ?? 0,
          signature: `geojson:url:${geojsonArtifactUrl}:${vectorArtifact?.featureCount ?? 0}:${vectorArtifact?.sizeBytes ?? 0}`,
        }
    : temporalAddedDataStats(data);
  const { featureCount, payloadBytes, signature } = dataStats;
  const previousSignature = sourceSignatures[sourceId] ?? null;
  const existingSource = map.getSource(sourceId) as GeoJSONSource | undefined;
  let mode: TemporalAddedLayerLifecycle["mode"] = "reuse";

  devLog("TEMPORAL_ADDED_DATA_FETCH_START", {
    projectId,
    releaseIdentifier: overlay.releaseIdentifier,
    kind: definition.kind,
    sourceId,
    source: useVectorTiles ? "vector_tile_artifact" : geojsonArtifactUrl ? "geojson_artifact_url" : "inline_project_payload",
    artifactKey: temporalAddedArtifactKey(definition.kind),
    tilejsonUrl: vectorArtifact?.tilejsonUrl ?? null,
    artifactUrl: geojsonArtifactUrl,
  });
  if (useVectorTiles) {
    devLog("TEMPORAL_GEOJSON_FETCH_SKIPPED_HUGE_ARTIFACT", {
      projectId,
      releaseIdentifier: overlay.releaseIdentifier,
      kind: definition.kind,
      artifactKey: temporalAddedArtifactKey(definition.kind),
      sourceId,
      featureCount,
      artifactBytes: vectorArtifact?.sizeBytes ?? 0,
      payloadBytes,
      tilejsonUrl: vectorArtifact?.tilejsonUrl ?? null,
      tilesUrlTemplate: vectorTilesUrlTemplate,
      reason: "vector_tile_artifact",
    });
    devLog("TEMPORAL_GEOJSON_FETCH_BLOCKED_VECTOR_TILE_ARTIFACT", {
      projectId,
      releaseIdentifier: overlay.releaseIdentifier,
      kind: definition.kind,
      artifactKey: temporalAddedArtifactKey(definition.kind),
      sourceId,
      reason: "vector_tile_artifact",
      tilejsonUrl: vectorArtifact?.tilejsonUrl ?? null,
      tilesUrlTemplate: vectorTilesUrlTemplate,
    });
    if (vectorArtifact?.tilejsonUrl) {
      devLog("TEMPORAL_VECTOR_TILE_TILEJSON_FETCH_START", {
        projectId,
        releaseIdentifier: overlay.releaseIdentifier,
        kind: definition.kind,
        artifactKey: temporalAddedArtifactKey(definition.kind),
        tilejsonUrl: vectorArtifact.tilejsonUrl,
      });
      void fetch(vectorArtifact.tilejsonUrl)
        .then(async (response) => {
          const tilejson = (await response.json()) as {
            vector_layers?: Array<{ id?: string; fields?: Record<string, unknown> }>;
            tiles?: string[];
            bounds?: number[];
            minzoom?: number;
            maxzoom?: number;
          };
          const tilejsonSourceLayer = tilejson.vector_layers?.[0]?.id ?? vectorSourceLayer;
          devLog("TEMPORAL_VECTOR_TILE_TILEJSON_FETCH_DONE", {
            projectId,
            releaseIdentifier: overlay.releaseIdentifier,
            kind: definition.kind,
            status: response.status,
            ok: response.ok,
            tilejsonUrl: vectorArtifact.tilejsonUrl,
            vectorLayerIds: tilejson.vector_layers?.map((layer) => layer.id).filter(Boolean) ?? [],
            sourceLayer: tilejsonSourceLayer,
            tileCount: tilejson.tiles?.length ?? 0,
            bounds: tilejson.bounds ?? null,
          });
          devLog("TEMPORAL_VECTOR_TILE_SOURCE_LAYER_CONFIRMED", {
            projectId,
            releaseIdentifier: overlay.releaseIdentifier,
            kind: definition.kind,
            sourceId,
            sourceLayer: tilejsonSourceLayer,
            metadataSourceLayer: vectorSourceLayer,
            matchesMetadata: tilejsonSourceLayer === vectorSourceLayer,
          });
        })
        .catch((error) => {
          if (error instanceof DOMException && error.name === "AbortError") {
            devLog("TEMPORAL_VECTOR_TILE_TILEJSON_FETCH_CANCELLED", {
              projectId,
              releaseIdentifier: overlay.releaseIdentifier,
              kind: definition.kind,
              tilejsonUrl: vectorArtifact.tilejsonUrl,
              reason: "request_cancelled",
            });
            return;
          }
          devLog("TEMPORAL_VECTOR_TILE_TILEJSON_FETCH_DONE", {
            projectId,
            releaseIdentifier: overlay.releaseIdentifier,
            kind: definition.kind,
            ok: false,
            tilejsonUrl: vectorArtifact.tilejsonUrl,
            error: error instanceof Error ? error.message : String(error),
          });
        });
    }
  }
  devLog("TEMPORAL_ADDED_DATA_FETCH_DONE", {
    projectId,
    releaseIdentifier: overlay.releaseIdentifier,
    kind: definition.kind,
    sourceId,
    featureCount,
    payloadBytes,
    fetchMs: 0,
    parseMs: 0,
    source: useVectorTiles ? "vector_tile_artifact" : geojsonArtifactUrl ? "geojson_artifact_url" : "inline_project_payload",
  });

  const existingVectorSourceSpec = useVectorTiles
    ? (map.getStyle()?.sources?.[sourceId] as { tiles?: string[]; url?: string } | undefined)
    : null;
  const existingVectorSourceNeedsExplicitTiles =
    Boolean(useVectorTiles && existingSource && vectorTilesUrlTemplate && !existingVectorSourceSpec?.tiles?.length);

  if (useVectorTiles && existingSource && (previousSignature !== signature || existingVectorSourceNeedsExplicitTiles)) {
    for (const candidateLayerId of [lineLayerId, layerId]) {
      if (candidateLayerId && map.getLayer(candidateLayerId)) {
        map.removeLayer(candidateLayerId);
      }
    }
    map.removeSource(sourceId);
    mode = "recreate";
    devLog("TEMPORAL_GEOJSON_FETCH_SKIPPED_HUGE_ARTIFACT", {
      projectId,
      releaseIdentifier: overlay.releaseIdentifier,
      kind: definition.kind,
      artifactKey: temporalAddedArtifactKey(definition.kind),
      sourceId,
      featureCount,
      payloadBytes,
      tilejsonUrl: vectorArtifact?.tilejsonUrl ?? null,
    });
    devLog("TEMPORAL_GEOJSON_FETCH_BLOCKED_VECTOR_TILE_ARTIFACT", {
      projectId,
      releaseIdentifier: overlay.releaseIdentifier,
      kind: definition.kind,
      artifactKey: temporalAddedArtifactKey(definition.kind),
      sourceId,
      reason: existingVectorSourceNeedsExplicitTiles ? "vector_tile_source_requires_explicit_tiles" : "vector_tile_signature_changed",
      tilejsonUrl: vectorArtifact?.tilejsonUrl ?? null,
      tilesUrlTemplate: vectorTilesUrlTemplate,
    });
  }

  const sourceAfterModeCheck = map.getSource(sourceId) as GeoJSONSource | undefined;
  if (useVectorTiles && !sourceAfterModeCheck) {
    mode = mode === "recreate" ? "recreate" : "create";
    const vectorSourceSpec: maplibregl.VectorSourceSpecification = vectorTilesUrlTemplate
      ? {
          type: "vector",
          tiles: [vectorTilesUrlTemplate],
          bounds: vectorArtifact.bbox ?? undefined,
          minzoom: 0,
          maxzoom: 18,
          scheme: "xyz",
        }
      : {
          type: "vector",
          url: vectorArtifact?.tilejsonUrl ?? undefined,
        };
    map.addSource(sourceId, vectorSourceSpec);
    if (useVectorTiles && (vectorFillMinzoom > 0 || vectorLineMinzoom > 0)) {
      devLog("TEMPORAL_VECTOR_LOW_ZOOM_STYLE_APPLIED", {
        projectId,
        releaseIdentifier: overlay.releaseIdentifier,
        kind: definition.kind,
        sourceId,
        fillMinzoom: vectorFillMinzoom,
        lineMinzoom: vectorLineMinzoom,
        artifactBytes: vectorArtifact?.sizeBytes ?? 0,
        featureCount: vectorArtifact?.featureCount ?? 0,
      });
    }
    devLog("TEMPORAL_ADDED_SOURCE_CREATE", {
      projectId,
      releaseIdentifier: overlay.releaseIdentifier,
      kind: definition.kind,
      sourceId,
      layerId,
      featureCount,
      payloadBytes,
      source: "vector_tile_artifact",
      tilejsonUrl: vectorArtifact?.tilejsonUrl ?? null,
      tilesUrlTemplate: vectorTilesUrlTemplate,
      registrationMode: vectorTilesUrlTemplate ? "explicit_tiles" : "tilejson_url",
      sourceLayer: vectorSourceLayer,
    });
    devLog("TEMPORAL_VECTOR_TILE_SOURCE_REGISTERED", {
      projectId,
      releaseIdentifier: overlay.releaseIdentifier,
      kind: definition.kind,
      sourceId,
      artifactKey: temporalAddedArtifactKey(definition.kind),
      tilejsonUrl: vectorArtifact?.tilejsonUrl ?? null,
      tilesUrlTemplate: vectorTilesUrlTemplate,
      sourceLayer: vectorSourceLayer,
      registrationMode: vectorTilesUrlTemplate ? "explicit_tiles" : "tilejson_url",
      mode,
      featureCount,
      payloadBytes,
      artifactBytes: vectorArtifact?.sizeBytes ?? 0,
    });
    sourceSignatures[sourceId] = signature;
  } else if (!useVectorTiles && !sourceAfterModeCheck) {
    mode = "create";
    devLog("TEMPORAL_ADDED_SOURCE_CREATE", {
      projectId,
      releaseIdentifier: overlay.releaseIdentifier,
      kind: definition.kind,
      sourceId,
      layerId,
      featureCount,
      payloadBytes,
      source: geojsonArtifactUrl ? "geojson_artifact_url" : "inline_project_payload",
      artifactUrl: geojsonArtifactUrl,
    });
    const setDataStartedAt = performance.now();
    devLog("TEMPORAL_ADDED_SETDATA_START", {
      projectId,
      releaseIdentifier: overlay.releaseIdentifier,
      kind: definition.kind,
      sourceId,
      layerId,
      featureCount,
      payloadBytes,
      reason: "source_create",
    });
    map.addSource(sourceId, { type: "geojson", data: geojsonArtifactUrl ?? data });
    devLog("TEMPORAL_ADDED_SETDATA_DONE", {
      projectId,
      releaseIdentifier: overlay.releaseIdentifier,
      kind: definition.kind,
      sourceId,
      layerId,
      featureCount,
      payloadBytes,
      setDataMs: Math.round(performance.now() - setDataStartedAt),
    });
    sourceSignatures[sourceId] = signature;
  } else if (!useVectorTiles && previousSignature !== signature && sourceAfterModeCheck) {
    mode = "update";
    const setDataStartedAt = performance.now();
    devLog("TEMPORAL_ADDED_SETDATA_START", {
      projectId,
      releaseIdentifier: overlay.releaseIdentifier,
      kind: definition.kind,
      sourceId,
      layerId,
      featureCount,
      payloadBytes,
      reason: "signature_changed",
    });
    sourceAfterModeCheck.setData(geojsonArtifactUrl ?? data);
    devLog("TEMPORAL_ADDED_SETDATA_DONE", {
      projectId,
      releaseIdentifier: overlay.releaseIdentifier,
      kind: definition.kind,
      sourceId,
      layerId,
      featureCount,
      payloadBytes,
      setDataMs: Math.round(performance.now() - setDataStartedAt),
    });
    sourceSignatures[sourceId] = signature;
  } else {
    devLog("TEMPORAL_ADDED_SOURCE_REUSE", {
      projectId,
      releaseIdentifier: overlay.releaseIdentifier,
      kind: definition.kind,
      sourceId,
      layerId,
      featureCount,
      payloadBytes,
      source: useVectorTiles ? "vector_tile_artifact" : "inline_project_payload",
    });
  }

  if (!map.getLayer(layerId)) {
    map.addLayer({
      id: layerId,
      type: "fill",
      source: sourceId,
      ...(useVectorTiles ? { "source-layer": vectorSourceLayer } : {}),
      ...(useVectorTiles && vectorFillMinzoom > 0 ? { minzoom: vectorFillMinzoom } : {}),
      paint: fillPaint,
      layout: { visibility: "none" },
    } as maplibregl.FillLayerSpecification);
    devLog("TEMPORAL_ADDED_LAYER_CREATE", {
      projectId,
      releaseIdentifier: overlay.releaseIdentifier,
      kind: definition.kind,
      sourceId,
      layerId,
      featureCount,
      payloadBytes,
    });
    if (useVectorTiles) {
      devLog("TEMPORAL_VECTOR_TILE_LAYER_REGISTERED", {
        projectId,
        releaseIdentifier: overlay.releaseIdentifier,
        kind: definition.kind,
        sourceId,
        layerId,
        sourceLayer: vectorSourceLayer,
        layerType: "fill",
        featureCount,
        payloadBytes,
        mode: "create",
      });
    }
  } else {
    for (const [property, value] of Object.entries(fillPaint)) {
      setPaintPropertyIfChanged(map, layerId, property, value);
    }
    devLog("TEMPORAL_ADDED_LAYER_REUSE", {
      projectId,
      releaseIdentifier: overlay.releaseIdentifier,
      kind: definition.kind,
      sourceId,
      layerId,
      featureCount,
      payloadBytes,
    });
  }
  if (useVectorTiles && vectorFillMinzoom > 0) {
    setLayerZoomRangeIfSupported(map, layerId, vectorFillMinzoom);
  }

  if (lineLayerId && temporalLayerPaint) {
    if (!map.getLayer(lineLayerId)) {
      map.addLayer({
        id: lineLayerId,
        type: "line",
        source: sourceId,
        ...(useVectorTiles ? { "source-layer": vectorSourceLayer } : {}),
        ...(useVectorTiles && vectorLineMinzoom > 0 ? { minzoom: vectorLineMinzoom } : {}),
        paint: temporalLayerPaint.linePaint,
        layout: { visibility: "none" },
      } as maplibregl.LineLayerSpecification);
      devLog("TEMPORAL_ADDED_LAYER_CREATE", {
        projectId,
        releaseIdentifier: overlay.releaseIdentifier,
        kind: `${definition.kind}:line`,
        sourceId,
        layerId: lineLayerId,
        featureCount,
        payloadBytes,
      });
      if (useVectorTiles) {
        devLog("TEMPORAL_VECTOR_TILE_LAYER_REGISTERED", {
          projectId,
          releaseIdentifier: overlay.releaseIdentifier,
          kind: `${definition.kind}:line`,
          sourceId,
          layerId: lineLayerId,
          sourceLayer: vectorSourceLayer,
          layerType: "line",
          featureCount,
          payloadBytes,
          mode: "create",
        });
      }
    } else {
      for (const [property, value] of Object.entries(temporalLayerPaint.linePaint)) {
        setPaintPropertyIfChanged(map, lineLayerId, property, value);
      }
      devLog("TEMPORAL_ADDED_LAYER_REUSE", {
        projectId,
        releaseIdentifier: overlay.releaseIdentifier,
        kind: `${definition.kind}:line`,
        sourceId,
        layerId: lineLayerId,
        featureCount,
        payloadBytes,
      });
    }
    if (useVectorTiles && vectorLineMinzoom > 0) {
      setLayerZoomRangeIfSupported(map, lineLayerId, vectorLineMinzoom);
    }
    applyTemporalAddedLayerStyle(map, projectId, overlay.releaseIdentifier, definition.kind, milestoneColor);
  }

  return { sourceId, layerId, lineLayerId, signature, previousSignature, mode, featureCount, payloadBytes };
}

function syncTemporalOutputLayers(params: {
  map: MapLibreMap | null;
  projectId: string | null;
  activeReleaseIdentifier: string | null;
  availableReleaseIdentifiers: string[];
  includedAdditionReleaseIdentifiers: string[];
  colorByReleaseIdentifier: Record<string, string>;
  layerState: LayerToggleState;
  registeredLayerIds: Set<string>;
  expectedReleaseIdentifiersByKind?: TemporalReleaseSetByKind;
  expectedActiveLayerIds?: string[];
}): TemporalOutputLayerSyncResult {
  const {
    map,
    projectId,
    activeReleaseIdentifier,
    availableReleaseIdentifiers,
    includedAdditionReleaseIdentifiers,
    colorByReleaseIdentifier,
    layerState,
    registeredLayerIds,
    expectedReleaseIdentifiersByKind,
    expectedActiveLayerIds: expectedActiveLayerIdsFromPlans,
  } = params;
  const emptyResult = {
    appliedCount: 0,
    hiddenCount: 0,
    skippedCount: registeredLayerIds.size,
    missingLayerCount: 0,
    visibleLayerIds: [],
    hiddenLayerIds: [],
  };
  const styleLoaded = Boolean(map?.isStyleLoaded());
  const styleReady = isMapStyleReadyForLayerMutation(map);
  const layerStateKeys = Object.keys(layerState);
  const enabledLayerKeys: LayerToggleKey[] = dedupeStable([
    ...TEMPORAL_ADDED_LAYER_DEFINITIONS.filter((definition) => layerState[definition.toggleKey]).map(
      (definition) => definition.toggleKey,
    ),
    ...(layerState.selectedMilestoneAdditions ? (["selectedMilestoneAdditions"] as const) : []),
  ]);
  const includedAdditionReleaseIdentifierSet = new Set(includedAdditionReleaseIdentifiers);
  const expectedCumulativeBufferReleaseSets = {
    cumulativeBuffer10m: temporalExpectedReleaseSetForKind(expectedReleaseIdentifiersByKind, "cumulativeBuffer10m"),
    cumulativeBuffer15m: temporalExpectedReleaseSetForKind(expectedReleaseIdentifiersByKind, "cumulativeBuffer15m"),
    cumulativeBuffer20m: temporalExpectedReleaseSetForKind(expectedReleaseIdentifiersByKind, "cumulativeBuffer20m"),
  };
  const temporalLayerCount = registeredLayerIds.size;
  const expectedOutputLayerCount = registeredLayerIds.size;
  const expectedActiveLayerIds =
    expectedActiveLayerIdsFromPlans ??
    (projectId && activeReleaseIdentifier
      ? TEMPORAL_ADDED_LAYER_DEFINITIONS.flatMap((definition) =>
          temporalAddedLayerIds(projectId, activeReleaseIdentifier, definition.kind),
        )
      : []);
  const missingLayerIds = expectedActiveLayerIds.filter((layerId) => !registeredLayerIds.has(layerId));
  const startPayload = {
    projectId,
    activeReleaseIdentifier,
    releaseIdentifier: activeReleaseIdentifier,
    hasMap: Boolean(map),
    styleLoaded,
    styleReady,
    layerStateKeys,
    enabledLayerKeys,
    includedAdditionReleaseIdentifiers,
    expectedReleaseIdentifiersByKind,
    temporalLayerCount,
    expectedOutputLayerCount,
    missingLayerIds,
  };
  if (DEV_LOGGING) {
    console.debug("TEMPORAL_OUTPUT_LAYER_SYNC_START", startPayload);
  }
  devLog("TEMPORAL_OUTPUT_LAYER_SYNC_START", startPayload);

  if (!map) {
    const skippedPayload = {
      projectId,
      activeReleaseIdentifier,
      releaseIdentifier: activeReleaseIdentifier,
      reason: "missing_map",
      hasMap: false,
      styleLoaded,
      styleReady,
      temporalLayerCount,
      expectedOutputLayerCount,
      missingLayerIds,
    };
    if (DEV_LOGGING) {
      console.debug("TEMPORAL_OUTPUT_LAYER_SYNC_SKIPPED", skippedPayload);
    }
    devLog("TEMPORAL_OUTPUT_LAYER_SYNC_SKIPPED", skippedPayload);
    return emptyResult;
  }
  if (!styleReady) {
    const skippedPayload = {
      projectId,
      activeReleaseIdentifier,
      releaseIdentifier: activeReleaseIdentifier,
      reason: "style_not_loaded",
      hasMap: true,
      styleLoaded,
      styleReady: false,
      temporalLayerCount,
      expectedOutputLayerCount,
      missingLayerIds,
    };
    if (DEV_LOGGING) {
      console.debug("TEMPORAL_OUTPUT_LAYER_SYNC_SKIPPED", skippedPayload);
    }
    devLog("TEMPORAL_OUTPUT_LAYER_SYNC_SKIPPED", skippedPayload);
    return emptyResult;
  }
  if (!projectId) {
    const skippedPayload = {
      projectId,
      activeReleaseIdentifier,
      releaseIdentifier: activeReleaseIdentifier,
      reason: "missing_project",
      hasMap: true,
      styleLoaded,
      styleReady,
      temporalLayerCount,
      expectedOutputLayerCount,
      missingLayerIds,
    };
    if (DEV_LOGGING) {
      console.debug("TEMPORAL_OUTPUT_LAYER_SYNC_SKIPPED", skippedPayload);
    }
    devLog("TEMPORAL_OUTPUT_LAYER_SYNC_SKIPPED", skippedPayload);
    return emptyResult;
  }
  if (!activeReleaseIdentifier) {
    const skippedPayload = {
      projectId,
      activeReleaseIdentifier,
      releaseIdentifier: activeReleaseIdentifier,
      reason: "missing_active_release",
      hasMap: true,
      styleLoaded,
      styleReady,
      temporalLayerCount,
      expectedOutputLayerCount,
      missingLayerIds,
    };
    if (DEV_LOGGING) {
      console.debug("TEMPORAL_OUTPUT_LAYER_SYNC_SKIPPED", skippedPayload);
    }
    devLog("TEMPORAL_OUTPUT_LAYER_SYNC_SKIPPED", skippedPayload);
    return emptyResult;
  }
  if (temporalLayerCount === 0) {
    const skippedPayload = {
      projectId,
      activeReleaseIdentifier,
      releaseIdentifier: activeReleaseIdentifier,
      reason: "no_temporal_output_layers",
      hasMap: true,
      styleLoaded,
      styleReady,
      temporalLayerCount,
      expectedOutputLayerCount,
      enabledLayerKeys,
      missingLayerIds,
    };
    if (DEV_LOGGING) {
      console.debug("TEMPORAL_OUTPUT_LAYER_SYNC_SKIPPED", skippedPayload);
    }
    devLog("TEMPORAL_OUTPUT_LAYER_SYNC_SKIPPED", skippedPayload);
    return emptyResult;
  }
  if (enabledLayerKeys.length === 0) {
    devLog("TEMPORAL_OUTPUT_LAYER_SYNC_SKIPPED", {
      projectId,
      activeReleaseIdentifier,
      releaseIdentifier: activeReleaseIdentifier,
      reason: "no_enabled_layer_keys",
      hasMap: true,
      styleLoaded,
      styleReady,
      temporalLayerCount,
      expectedOutputLayerCount,
      missingLayerIds,
    });
  }
  if (missingLayerIds.length > 0) {
    devLog("TEMPORAL_OUTPUT_LAYER_SYNC_SKIPPED", {
      projectId,
      activeReleaseIdentifier,
      releaseIdentifier: activeReleaseIdentifier,
      reason: "missing_layer_ids",
      hasMap: true,
      styleLoaded,
      styleReady,
      temporalLayerCount,
      expectedOutputLayerCount,
      missingLayerIds,
    });
  }

  let appliedCount = 0;
  let hiddenCount = 0;
  let skippedCount = 0;
  let missingLayerCount = 0;
  const visibleLayerIds: string[] = [];
  const hiddenLayerIds: string[] = [];
  for (const layerId of registeredLayerIds) {
    const activeDefinition = TEMPORAL_ADDED_LAYER_DEFINITIONS.find(
      (definition) =>
        activeReleaseIdentifier &&
        temporalAddedLayerIds(projectId, activeReleaseIdentifier, definition.kind).includes(layerId),
    );
    const includedAdditionReleaseIdentifier = includedAdditionReleaseIdentifiers.find((releaseIdentifier) =>
      temporalAddedLayerIds(projectId, releaseIdentifier, "additions").includes(layerId),
    );
    const cumulativeBufferDefinitions = TEMPORAL_ADDED_LAYER_DEFINITIONS.filter(
      (
        definition,
      ): definition is TemporalAddedLayerDefinition & {
        kind: "cumulativeBuffer10m" | "cumulativeBuffer15m" | "cumulativeBuffer20m";
      } => isCumulativeBufferLayerKind(definition.kind),
    );
    const includedCumulativeBufferMatch = cumulativeBufferDefinitions
      .flatMap((definition) =>
        Array.from(expectedCumulativeBufferReleaseSets[definition.kind]).map((releaseIdentifier) => ({
          releaseIdentifier,
          definition,
        })),
      )
      .find(({ releaseIdentifier, definition }) => temporalAddedLayerIds(projectId, releaseIdentifier, definition.kind).includes(layerId));
    const matchingReleaseIdentifier =
      includedAdditionReleaseIdentifier ??
      includedCumulativeBufferMatch?.releaseIdentifier ??
      (activeReleaseIdentifier && activeDefinition
        ? activeReleaseIdentifier
        : availableReleaseIdentifiers.find((releaseIdentifier) =>
            TEMPORAL_ADDED_LAYER_DEFINITIONS.some(
              (definition) => temporalAddedLayerIds(projectId, releaseIdentifier, definition.kind).includes(layerId),
            ),
          ));
    const includedAdditionVisible = Boolean(
      includedAdditionReleaseIdentifier &&
        temporalAdditionVisibilityReason({
          releaseIdentifier: includedAdditionReleaseIdentifier,
          selectedReleaseIdentifier: activeReleaseIdentifier,
          includedAdditionReleaseIdentifiers: includedAdditionReleaseIdentifierSet,
          allNewBuildingsEnabled: layerState.temporalAdditions,
          selectedAdditionsEnabled: false,
        }) === "allNewBuildings",
    );
    const includedCumulativeBufferVisible = Boolean(
      includedCumulativeBufferMatch &&
        expectedCumulativeBufferReleaseSets[includedCumulativeBufferMatch.definition.kind].has(
          includedCumulativeBufferMatch.releaseIdentifier,
        ) &&
        layerState[includedCumulativeBufferMatch.definition.toggleKey],
    );
    const selectedMilestoneAdditionVisible = Boolean(
      activeReleaseIdentifier &&
        temporalAddedLayerIds(projectId, activeReleaseIdentifier, "additions").includes(layerId) &&
        temporalAdditionVisibilityReason({
          releaseIdentifier: activeReleaseIdentifier,
          selectedReleaseIdentifier: activeReleaseIdentifier,
          includedAdditionReleaseIdentifiers: includedAdditionReleaseIdentifierSet,
          allNewBuildingsEnabled: false,
          selectedAdditionsEnabled: layerState.selectedMilestoneAdditions,
        }) === "selectedMilestoneAdditions",
    );
    const mapLayerExists = Boolean(map.getLayer(layerId));
    const visible = Boolean(
      mapLayerExists &&
        (includedAdditionVisible ||
          includedCumulativeBufferVisible ||
          selectedMilestoneAdditionVisible ||
          (activeDefinition && activeDefinition.kind !== "additions" && layerState[activeDefinition.toggleKey])),
    );

    if (!mapLayerExists) {
      skippedCount += 1;
      missingLayerCount += 1;
      devLog("TEMPORAL_OUTPUT_LAYER_SYNC_APPLY", {
        projectId,
        releaseIdentifier: matchingReleaseIdentifier ?? activeReleaseIdentifier,
        layerId,
        visible: false,
        reason: "missing_layer",
      });
      continue;
    }

    const visibilityChanged = setLayerVisibility(map, layerId, visible);
    appliedCount += 1;
    if (shouldSkipPostVisibilityLayerWork(visible)) {
      hiddenCount += 1;
      hiddenLayerIds.push(layerId);
      if (visibilityChanged) {
        devLog("TEMPORAL_OUTPUT_LAYER_SYNC_APPLY", {
          projectId,
          releaseIdentifier: matchingReleaseIdentifier ?? activeReleaseIdentifier,
          layerKey: includedAdditionReleaseIdentifier
            ? "temporalAdditions"
            : includedCumulativeBufferMatch
              ? includedCumulativeBufferMatch.definition.toggleKey
              : activeDefinition?.toggleKey ?? "non_active_release",
          layerId,
          visible,
          reason: "hidden_once",
        });
      }
      continue;
    } else {
      visibleLayerIds.push(layerId);
    }

    if (includedAdditionReleaseIdentifier || includedCumulativeBufferMatch || activeDefinition) {
      const styledReleaseIdentifier =
        includedAdditionReleaseIdentifier ?? includedCumulativeBufferMatch?.releaseIdentifier ?? activeReleaseIdentifier ?? "";
      const styledDefinition = includedAdditionReleaseIdentifier
        ? TEMPORAL_ADDED_LAYER_DEFINITIONS.find((definition) => definition.kind === "additions")
        : includedCumulativeBufferMatch
          ? includedCumulativeBufferMatch.definition
          : activeDefinition;
      const color = colorByReleaseIdentifier[styledReleaseIdentifier] ?? "#B91C1C";
      if (styledDefinition && layerId === temporalAddedLayerId(projectId, styledReleaseIdentifier, styledDefinition.kind)) {
        applyTemporalAddedLayerStyle(map, projectId, styledReleaseIdentifier, styledDefinition.kind, color);
      }
      const activeSourceId = styledDefinition ? temporalAddedSourceId(projectId, styledReleaseIdentifier, styledDefinition.kind) : "";
      const sourceSpec = map.getStyle()?.sources?.[activeSourceId] as { type?: string } | undefined;
      if (sourceSpec?.type === "vector") {
        devLog("TEMPORAL_VECTOR_TILE_RELEASE_FILTER_SKIPPED", {
          projectId,
          layerId,
          sourceId: activeSourceId,
          layerKey: styledDefinition?.toggleKey,
          releaseIdentifier: styledReleaseIdentifier,
          reason: "release_specific_vector_tile_source",
          visible,
        });
        if (visible) {
          devLog("TEMPORAL_VECTOR_TILE_LAYER_VISIBLE", {
            projectId,
            layerId,
            sourceId: activeSourceId,
            layerKey: styledDefinition?.toggleKey,
            releaseIdentifier: styledReleaseIdentifier,
          });
          devLog("TEMPORAL_VECTOR_TILE_FILTER_CONFIRMED", {
            projectId,
            layerId,
            sourceId: activeSourceId,
            layerKey: styledDefinition?.toggleKey,
            releaseIdentifier: styledReleaseIdentifier,
            filterApplied: false,
            reason: "release_specific_vector_tile_source",
          });
          devLog("TEMPORAL_VECTOR_TILE_PAINT_CONFIRMED", {
            projectId,
            layerId,
            sourceId: activeSourceId,
            layerKey: styledDefinition?.toggleKey,
            releaseIdentifier: styledReleaseIdentifier,
            ...getTemporalLayerPaintDiagnostics(map, layerId),
          });
        }
      } else {
        devLog("TEMPORAL_OUTPUT_LAYER_FILTER_APPLIED", {
          projectId,
          layerId,
          layerKey: styledDefinition?.toggleKey,
          releaseIdentifier: styledReleaseIdentifier,
          filter: { mode: "separate_release_layer", releaseIdentifier: styledReleaseIdentifier },
          visible,
        });
      }
    } else {
      devLog("TEMPORAL_OUTPUT_LAYER_FILTER_APPLIED", {
        projectId,
        layerId,
        releaseIdentifier: matchingReleaseIdentifier ?? "unknown",
        activeReleaseIdentifier,
        filter: { mode: "hide_non_active_release", releaseIdentifier: matchingReleaseIdentifier ?? "unknown" },
        visible: false,
      });
    }

    devLog("TEMPORAL_OUTPUT_LAYER_SYNC_APPLY", {
      projectId,
      releaseIdentifier: matchingReleaseIdentifier ?? activeReleaseIdentifier,
      layerKey: selectedMilestoneAdditionVisible
        ? "selectedMilestoneAdditions"
        : includedAdditionReleaseIdentifier
          ? "temporalAdditions"
          : includedCumulativeBufferMatch
            ? includedCumulativeBufferMatch.definition.toggleKey
          : activeDefinition?.toggleKey ?? "non_active_release",
      layerId,
      visible,
      reason: selectedMilestoneAdditionVisible
        ? "selected_milestone_addition"
        : includedAdditionReleaseIdentifier
          ? "included_previous_addition"
          : includedCumulativeBufferMatch
            ? "included_previous_cumulative_buffer"
          : activeDefinition
            ? "active_release_state"
            : "non_active_release",
    });

    if (visible) {
      const moved = moveLayerToTopIfNeeded(map, layerId);
      const visibleSourceId = includedAdditionReleaseIdentifier
        ? temporalAddedSourceId(projectId, includedAdditionReleaseIdentifier, "additions")
        : includedCumulativeBufferMatch
          ? temporalAddedSourceId(projectId, includedCumulativeBufferMatch.releaseIdentifier, includedCumulativeBufferMatch.definition.kind)
        : activeDefinition
          ? temporalAddedSourceId(projectId, activeReleaseIdentifier ?? "", activeDefinition.kind)
        : null;
      const visibleSourceSpec = visibleSourceId ? (map.getStyle()?.sources?.[visibleSourceId] as { type?: string } | undefined) : null;
      devLog(visibleSourceSpec?.type === "vector" ? "TEMPORAL_VECTOR_TILE_LAYER_ORDER" : "TEMPORAL_OUTPUT_LAYER_ORDER_APPLIED", {
        projectId,
        layerId,
        releaseIdentifier: matchingReleaseIdentifier ?? activeReleaseIdentifier,
        layerKey: selectedMilestoneAdditionVisible
          ? "selectedMilestoneAdditions"
          : includedAdditionReleaseIdentifier
            ? "temporalAdditions"
            : includedCumulativeBufferMatch
              ? includedCumulativeBufferMatch.definition.toggleKey
            : activeDefinition?.toggleKey ?? "non_active_release",
        movedToTop: true,
        moved,
        aboveReferenceRaster: true,
      });
    }
  }

  map.triggerRepaint();
  const donePayload = {
    projectId,
    activeReleaseIdentifier,
    releaseIdentifier: activeReleaseIdentifier,
    appliedCount,
    hiddenCount,
    skippedCount,
    registeredLayerCount: registeredLayerIds.size,
    enabledLayerKeys,
    includedAdditionReleaseIdentifiers,
    visibleLayerIds,
    hiddenLayerIds,
    missingLayerIds,
  };
  if (DEV_LOGGING) {
    console.debug("TEMPORAL_OUTPUT_LAYER_SYNC_DONE", donePayload);
  }
  devLog("TEMPORAL_OUTPUT_LAYER_SYNC_DONE", donePayload);
  return { appliedCount, hiddenCount, skippedCount, missingLayerCount, visibleLayerIds, hiddenLayerIds };
}

function queryTemporalRenderedFeatureCount(map: MapLibreMap, layerIds: string[]): number {
  const existingLayerIds = layerIds.filter((layerId) => Boolean(map.getLayer(layerId)));
  if (!existingLayerIds.length) {
    return 0;
  }
  try {
    const query = map.queryRenderedFeatures.bind(map) as (
      geometryOrOptions?: unknown,
      options?: { layers?: string[] },
    ) => Array<unknown>;
    return query(undefined, { layers: existingLayerIds }).length;
  } catch {
    try {
      const query = map.queryRenderedFeatures.bind(map) as (options?: { layers?: string[] }) => Array<unknown>;
      return query({ layers: existingLayerIds }).length;
    } catch {
      return 0;
    }
  }
}

function scheduleTemporalOutputRenderConfirmation(params: {
  map: MapLibreMap;
  projectId: string;
  releaseIdentifier: string;
  plans: TemporalOutputLayerPlan[];
  visibleLayerIds: string[];
}) {
  const { map, projectId, releaseIdentifier, plans, visibleLayerIds } = params;
  if (!isTemporalRenderAuditEnabled()) {
    devLog("TEMPORAL_RENDER_CHECK_SKIPPED_NORMAL_MODE", {
      projectId,
      releaseIdentifier,
      reason: "render_audit_disabled",
      check: "temporal_output_render_confirmation",
      visibleLayerCount: visibleLayerIds.length,
      plannedLayerCount: plans.length,
    });
    return;
  }
  const visibleLayerIdSet = new Set(visibleLayerIds);
  const confirm = () => {
    for (const plan of plans) {
      if (!plan.enabled || !plan.layerIds.some((layerId) => visibleLayerIdSet.has(layerId))) {
        continue;
      }
      const renderedFeatureCount = queryTemporalRenderedFeatureCount(map, plan.layerIds);
      const existingLayerIds = plan.layerIds.filter((layerId) => Boolean(map.getLayer(layerId)));
      const firstLayerId = existingLayerIds[0] ?? plan.layerIds[0] ?? null;
      const sourceSpec = map.getStyle()?.sources?.[plan.sourceId] as { tiles?: string[]; url?: string } | undefined;
      const payload = {
        projectId,
        releaseIdentifier,
        layerKey: plan.layerKey,
        artifactKey: plan.artifactKey,
        sourceId: plan.sourceId,
        sourceExists: Boolean(map.getSource(plan.sourceId)),
        sourceType: plan.sourceType,
        sourceLayer: plan.sourceLayer ?? "results",
        renderStrategy: plan.renderStrategy,
        layerIds: plan.layerIds,
        layerExists: existingLayerIds.length > 0,
        visibility: firstLayerId ? map.getLayoutProperty(firstLayerId, "visibility") : null,
        paint: firstLayerId ? getTemporalLayerPaintDiagnostics(map, firstLayerId) : null,
        filter: firstLayerId ? map.getFilter(firstLayerId) ?? null : null,
        tilejsonUrl: plan.tilejsonUrl ?? null,
        tilesTemplate: sourceSpec?.tiles?.[0] ?? null,
        mapBounds: map.getBounds().toArray(),
        mapZoom: map.getZoom(),
        renderedFeatureCount,
      };
      if (plan.sourceType === "vector") {
        devLog(
          renderedFeatureCount > 0 ? "TEMPORAL_VECTOR_TILE_RENDER_CONFIRMED" : "TEMPORAL_VECTOR_TILE_RENDER_EMPTY",
          payload,
        );
      } else {
        devLog(renderedFeatureCount > 0 ? "TEMPORAL_OUTPUT_LAYER_RENDER_CONFIRMED" : "TEMPORAL_OUTPUT_LAYER_RENDER_EMPTY", payload);
      }
    }
  };
  if (typeof map.once === "function") {
    map.once("idle", confirm);
  }
  window.setTimeout(confirm, 400);
}

function temporalReferenceSourceSignature(
  projectId: string | null,
  imagery: TemporalReferenceImageryPresentation,
): string {
  return stableHash({
    projectId,
    releaseIdentifier: imagery.releaseIdentifier,
    storageStrategy: imagery.storageStrategy,
    tilejsonUrl: imagery.tilejsonUrl,
    tilesUrlTemplate: imagery.tilesUrlTemplate,
    cogUrl: imagery.cogUrl,
    imageUrl: imagery.imageUrl,
    bounds: imagery.bounds,
    minzoom: imagery.minzoom,
    maxzoom: imagery.maxzoom,
    tileSize: imagery.tileSize,
  });
}

function getTemporalReferenceInsertionLayerId(map: MapLibreMap): string | undefined {
  const candidates = [
    "reference-layer-fill",
    "aoi-fill",
    "detected-polygons-fill",
    "temporal-additions-fill",
    "buffer-10m-fill",
  ];
  return candidates.find((layerId) => Boolean(map.getLayer(layerId)));
}

function ensureTemporalReferenceRasterLayer(
  map: MapLibreMap,
  imagery: TemporalReferenceImageryPresentation,
  options?: {
    projectId: string | null;
    sourceSignatures: Record<string, string>;
    resolvedTilejson?: TemporalReferenceTilejsonPayload | null;
  },
): TemporalReferenceLayerLifecycle {
  const sourceId = temporalReferenceSourceId(options?.projectId ?? null, imagery.releaseIdentifier);
  const layerId = temporalReferenceLayerId(options?.projectId ?? null, imagery.releaseIdentifier);
  const nextSignature = options
    ? temporalReferenceSourceSignature(options.projectId, imagery)
    : null;
  const previousSignature = options?.sourceSignatures[sourceId] ?? null;
  if (
    shouldSkipReferenceRegistration({
      previousSignature,
      nextSignature,
      sourceExists: Boolean(map.getSource(sourceId)),
      layerExists: Boolean(map.getLayer(layerId)),
    })
  ) {
    devLog("TEMPORAL_REFERENCE_REGISTRATION_SKIPPED", {
      projectId: options?.projectId ?? null,
      releaseIdentifier: imagery.releaseIdentifier,
      sourceId,
      layerId,
      signatureHash: nextSignature,
      changed: false,
      mode: "reuse",
    });
    return {
      layerId,
      sourceId,
      signature: nextSignature,
      previousSignature,
      mode: "reuse",
    };
  }
  const directTilesUrlTemplate = normalizeTileUrlTemplate(imagery.tilesUrlTemplate);
  const resolvedTileTemplate = normalizeTileUrlTemplate(
    Array.isArray(options?.resolvedTilejson?.tiles) && typeof options?.resolvedTilejson?.tiles?.[0] === "string"
      ? options.resolvedTilejson.tiles[0]
      : null,
  );
  const confirmedTilesUrlTemplate = resolvedTileTemplate ?? directTilesUrlTemplate;
  if (confirmedTilesUrlTemplate) {
    devLog("TEMPORAL_REFERENCE_TILE_TEMPLATE_CONFIRMED", {
      projectId: options?.projectId ?? null,
      releaseIdentifier: imagery.releaseIdentifier,
      sourceId,
      layerId,
      tilesUrlTemplate: confirmedTilesUrlTemplate,
      encodedPlaceholders:
        confirmedTilesUrlTemplate.includes("%7Bz%7D") ||
        confirmedTilesUrlTemplate.includes("%7Bx%7D") ||
        confirmedTilesUrlTemplate.includes("%7By%7D"),
      hasRawPlaceholders:
        confirmedTilesUrlTemplate.includes("{z}") &&
        confirmedTilesUrlTemplate.includes("{x}") &&
        confirmedTilesUrlTemplate.includes("{y}"),
      bounds: options?.resolvedTilejson?.bounds ?? imagery.bounds ?? null,
      minzoom: options?.resolvedTilejson?.minzoom ?? imagery.minzoom ?? null,
      maxzoom: options?.resolvedTilejson?.maxzoom ?? imagery.maxzoom ?? null,
      tileSize: imagery.tileSize ?? 256,
    });
  }
  let mode: TemporalReferenceSourceLifecycleMode = "reuse";
  if (nextSignature && map.getSource(sourceId) && previousSignature !== nextSignature) {
    mode = "recreate";
    devLog("TEMPORAL_REFERENCE_SOURCE_RECREATE", {
      projectId: options?.projectId ?? null,
      releaseIdentifier: imagery.releaseIdentifier,
      sourceId,
      layerId,
      previousSignatureHash: previousSignature,
      nextSignatureHash: nextSignature,
      signatureHash: nextSignature,
      changed: true,
      tileVersion:
        imagery.tilesUrlTemplate ??
        imagery.tilejsonUrl ??
        imagery.cogUrl ??
        null,
      recreateReason: "signature_changed",
      tilejsonUrl: imagery.tilejsonUrl,
      tilesUrlTemplate: imagery.tilesUrlTemplate,
      cogUrl: imagery.cogUrl,
    });
    if (map.getLayer(layerId)) {
      map.removeLayer(layerId);
    }
    if (map.getSource(sourceId)) {
      map.removeSource(sourceId);
    }
  }
  if (!map.getSource(sourceId)) {
    if (mode !== "recreate") {
      mode = "create";
    }
    devLog("TEMPORAL_REFERENCE_SOURCE_CREATE", {
      projectId: options?.projectId ?? null,
      releaseIdentifier: imagery.releaseIdentifier,
      sourceId,
      layerId,
      signatureHash: nextSignature,
      changed: true,
      tileVersion:
        imagery.tilesUrlTemplate ??
        imagery.tilejsonUrl ??
        imagery.cogUrl ??
        null,
      tilejsonUrl: imagery.tilejsonUrl,
      tilesUrlTemplate: imagery.tilesUrlTemplate,
      cogUrl: imagery.cogUrl,
      reason: "missing_source",
    });
    if (imagery.tilejsonUrl) {
      map.addSource(sourceId, {
        type: "raster",
        ...(resolvedTileTemplate
          ? {
              tiles: [resolvedTileTemplate],
              bounds: options?.resolvedTilejson?.bounds ?? imagery.bounds ?? undefined,
              minzoom: options?.resolvedTilejson?.minzoom ?? imagery.minzoom ?? 0,
              maxzoom: options?.resolvedTilejson?.maxzoom ?? imagery.maxzoom ?? 22,
            }
          : { url: imagery.tilejsonUrl }),
        tileSize: imagery.tileSize ?? 256,
      });
    } else if (imagery.tilesUrlTemplate) {
      map.addSource(sourceId, {
        type: "raster",
        tiles: [directTilesUrlTemplate ?? imagery.tilesUrlTemplate],
        tileSize: imagery.tileSize ?? 256,
        bounds: imagery.bounds ?? undefined,
        minzoom: imagery.minzoom ?? 0,
        maxzoom: imagery.maxzoom ?? 22,
      });
    } else {
      throw new Error(`Raster-tile imagery for ${imagery.releaseIdentifier} is missing a TileJSON or tiles template URL.`);
    }
    if (nextSignature && options) {
      options.sourceSignatures[sourceId] = nextSignature;
    }
  } else {
    devLog("TEMPORAL_REFERENCE_SOURCE_REUSE", {
      projectId: options?.projectId ?? null,
      releaseIdentifier: imagery.releaseIdentifier,
      sourceId,
      layerId,
      signatureHash: nextSignature,
      changed: false,
      mode: "reuse",
      tileVersion:
        imagery.tilesUrlTemplate ??
        imagery.tilejsonUrl ??
        imagery.cogUrl ??
        null,
      tilejsonUrl: imagery.tilejsonUrl,
      tilesUrlTemplate: imagery.tilesUrlTemplate,
      cogUrl: imagery.cogUrl,
    });
    if (nextSignature && options) {
      options.sourceSignatures[sourceId] = nextSignature;
    }
  }
  if (!map.getLayer(layerId)) {
    map.addLayer(
      {
        id: layerId,
        type: "raster",
        source: sourceId,
        paint: {
          "raster-opacity": 1,
          "raster-fade-duration": 0,
        },
        layout: { visibility: "none" },
      },
      getTemporalReferenceInsertionLayerId(map),
    );
    devLog("TEMPORAL_REFERENCE_LAYER_CREATE", {
      projectId: options?.projectId ?? null,
      releaseIdentifier: imagery.releaseIdentifier,
      sourceId,
      layerId,
    });
  } else {
    devLog("TEMPORAL_REFERENCE_LAYER_REUSE", {
      projectId: options?.projectId ?? null,
      releaseIdentifier: imagery.releaseIdentifier,
      sourceId,
      layerId,
    });
  }
  return {
    layerId,
    sourceId,
    signature: nextSignature,
    previousSignature,
    mode,
  };
}

function ensureTemporalReferenceImageLayer(
  map: MapLibreMap,
  imagery: TemporalReferenceImageryPresentation,
  options?: { projectId: string | null; sourceSignatures: Record<string, string> },
): TemporalReferenceLayerLifecycle {
  const sourceId = temporalReferenceSourceId(options?.projectId ?? null, imagery.releaseIdentifier);
  const layerId = temporalReferenceLayerId(options?.projectId ?? null, imagery.releaseIdentifier);
  const nextSignature = options
    ? temporalReferenceSourceSignature(options.projectId, imagery)
    : null;
  const previousSignature = options?.sourceSignatures[sourceId] ?? null;
  let mode: TemporalReferenceSourceLifecycleMode = "reuse";
  if (
    shouldSkipReferenceRegistration({
      previousSignature,
      nextSignature,
      sourceExists: Boolean(map.getSource(sourceId)),
      layerExists: Boolean(map.getLayer(layerId)),
    })
  ) {
    devLog("TEMPORAL_REFERENCE_REGISTRATION_SKIPPED", {
      projectId: options?.projectId ?? null,
      releaseIdentifier: imagery.releaseIdentifier,
      sourceId,
      layerId,
      signatureHash: nextSignature,
      changed: false,
      mode: "reuse",
    });
    return {
      layerId,
      sourceId,
      signature: nextSignature,
      previousSignature,
      mode: "reuse",
    };
  }
  if (nextSignature && map.getSource(sourceId) && previousSignature !== nextSignature) {
    mode = "recreate";
    devLog("TEMPORAL_REFERENCE_SOURCE_RECREATE", {
      projectId: options?.projectId ?? null,
      releaseIdentifier: imagery.releaseIdentifier,
      sourceId,
      layerId,
      previousSignatureHash: previousSignature,
      nextSignatureHash: nextSignature,
      signatureHash: nextSignature,
      changed: true,
      recreateReason: "signature_changed",
      tilejsonUrl: imagery.tilejsonUrl,
      tilesUrlTemplate: imagery.tilesUrlTemplate,
      cogUrl: imagery.cogUrl,
    });
    removeTemporalReferenceLayer(map, options?.projectId ?? null, imagery.releaseIdentifier);
  }
  syncImageOverlay(
    map,
    sourceId,
    layerId,
    imagery.imageUrl,
    imagery.bounds ? imageCoordinatesFromBBox(imagery.bounds) : null,
    1,
    false,
  );
  if (nextSignature && options) {
    options.sourceSignatures[sourceId] = nextSignature;
  }
  if (mode === "recreate") {
    devLog("TEMPORAL_REFERENCE_SOURCE_CREATE", {
      projectId: options?.projectId ?? null,
      releaseIdentifier: imagery.releaseIdentifier,
      sourceId,
      layerId,
      signatureHash: nextSignature,
      changed: true,
      tileVersion:
        imagery.tilesUrlTemplate ??
        imagery.tilejsonUrl ??
        imagery.cogUrl ??
        null,
      tilejsonUrl: imagery.tilejsonUrl,
      tilesUrlTemplate: imagery.tilesUrlTemplate,
      cogUrl: imagery.cogUrl,
      reason: "recreated_source",
    });
  } else if (previousSignature === nextSignature && map.getSource(sourceId)) {
    devLog("TEMPORAL_REFERENCE_SOURCE_REUSE", {
      projectId: options?.projectId ?? null,
      releaseIdentifier: imagery.releaseIdentifier,
      sourceId,
      layerId,
      signatureHash: nextSignature,
      changed: false,
      mode: "reuse",
      tileVersion:
        imagery.tilesUrlTemplate ??
        imagery.tilejsonUrl ??
        imagery.cogUrl ??
        null,
      tilejsonUrl: imagery.tilejsonUrl,
      tilesUrlTemplate: imagery.tilesUrlTemplate,
      cogUrl: imagery.cogUrl,
    });
  } else {
    mode = "create";
    devLog("TEMPORAL_REFERENCE_SOURCE_CREATE", {
      projectId: options?.projectId ?? null,
      releaseIdentifier: imagery.releaseIdentifier,
      sourceId,
      layerId,
      signatureHash: nextSignature,
      changed: true,
      tileVersion:
        imagery.tilesUrlTemplate ??
        imagery.tilejsonUrl ??
        imagery.cogUrl ??
        null,
      tilejsonUrl: imagery.tilejsonUrl,
      tilesUrlTemplate: imagery.tilesUrlTemplate,
      cogUrl: imagery.cogUrl,
      reason: "missing_source",
    });
  }
  return {
    layerId,
    sourceId,
    signature: nextSignature,
    previousSignature,
    mode,
  };
}

function setTemporalReferenceLayerVisibility(map: MapLibreMap, layerId: string, visible: boolean) {
  if (map.getLayer(layerId)) {
    const nextVisibility = visible ? "visible" : "none";
    if (map.getLayoutProperty(layerId, "visibility") === nextVisibility) {
      return;
    }
    setLayerLayoutPropertyIfChanged(map, layerId, "visibility", nextVisibility);
    devLog("TEMPORAL_REFERENCE_LAYER_VISIBILITY", {
      layerId,
      visibility: nextVisibility,
    });
  }
}

function removeTemporalReferenceLayer(map: MapLibreMap, projectId: string | null, releaseIdentifier: string) {
  const layerId = temporalReferenceLayerId(projectId, releaseIdentifier);
  const sourceId = temporalReferenceSourceId(projectId, releaseIdentifier);
  devLog("TEMPORAL_REFERENCE_SOURCE_REMOVE", {
    releaseIdentifier,
    sourceId,
    layerId,
  });
  if (map.getLayer(layerId)) {
    map.removeLayer(layerId);
  }
  if (map.getSource(sourceId)) {
    map.removeSource(sourceId);
  }
}

function moveReferenceOverlaysAboveTemporalImagery(map: MapLibreMap, activeReferenceLayerId: string, referenceLayers: ReferenceLayerPresentation[]) {
  if (!map.getLayer(activeReferenceLayerId)) {
    return;
  }
  const overlayLayerIds = [
    "aoi-fill",
    "aoi-line",
    "aoi-draft-line",
    "rectangle-preview-fill",
    "rectangle-preview-line",
    "rectangle-vertices-line",
    "detected-polygons-fill",
    "detected-polygons-line",
    "building-blocks-line",
    "buffer-layers-fill",
    "buffer-layers-line",
    "buffer-10m-fill",
    "buffer-10m-line",
    "buffer-15m-fill",
    "buffer-15m-line",
    "buffer-20m-fill",
    "buffer-20m-line",
    "temporal-additions-fill",
    "temporal-cumulative-buffer-20m-fill",
    "temporal-cumulative-buffer-15m-fill",
    "temporal-cumulative-buffer-10m-fill",
    "temporal-automated-fill",
    "temporal-automated-building-blocks-fill",
    "temporal-effective-building-blocks-fill",
    "temporal-cumulative-fill",
    "temporal-cumulative-growth-blocks-fill",
    "temporal-cumulative-growth-envelope-fill",
    "temporal-manual-override-fill",
  ];
  for (const layerId of overlayLayerIds) {
    if (map.getLayer(layerId)) {
      moveLayerToTopIfNeeded(map, layerId);
    }
  }
  for (const referenceLayer of referenceLayers) {
    for (const suffix of ["fill", "line", "circle"]) {
      const layerId = `${referenceSourceId(referenceLayer.layer_id)}-${suffix}`;
      if (map.getLayer(layerId)) {
        moveLayerToTopIfNeeded(map, layerId);
      }
    }
  }
}

function waitForTemporalRasterSource(map: MapLibreMap, sourceId: string): Promise<void> {
  if (typeof map.isSourceLoaded === "function" && map.isSourceLoaded(sourceId)) {
    devLog("TEMPORAL_REFERENCE_READY", {
      sourceId,
      readiness: "already_loaded",
    });
    return Promise.resolve();
  }
  return new Promise((resolve) => {
    const handleSourceData = (event: maplibregl.MapSourceDataEvent) => {
      if (event.sourceId !== sourceId) {
        return;
      }
      if (typeof map.isSourceLoaded === "function" && map.isSourceLoaded(sourceId)) {
        map.off("sourcedata", handleSourceData);
        devLog("TEMPORAL_REFERENCE_READY", {
          sourceId,
          readiness: "sourcedata_loaded",
        });
        resolve();
      }
    };
    map.on("sourcedata", handleSourceData);
  });
}

function preloadImage(url: string): Promise<void> {
  return new Promise((resolve, reject) => {
    const image = new Image();
    image.decoding = "async";
    image.onload = () => resolve();
    image.onerror = () => reject(new Error(`Failed to preload image: ${url}`));
    image.src = url;
  });
}

function scheduleIdle(task: () => void, timeoutMs: number): () => void {
  const idleWindow = window as Window & {
    requestIdleCallback?: (callback: IdleRequestCallback, options?: IdleRequestOptions) => number;
    cancelIdleCallback?: (handle: number) => void;
  };
  if (typeof idleWindow.requestIdleCallback === "function" && typeof idleWindow.cancelIdleCallback === "function") {
    const idleId = idleWindow.requestIdleCallback(() => task(), { timeout: timeoutMs });
    return () => idleWindow.cancelIdleCallback?.(idleId);
  }
  const timer = window.setTimeout(task, Math.min(timeoutMs, 750));
  return () => window.clearTimeout(timer);
}

function setPaintPropertyIfChanged(map: MapLibreMap, layerId: string, property: string, value: unknown): boolean {
  if (!map.getLayer(layerId)) {
    return false;
  }
  if (!shouldApplyMapValue(map.getPaintProperty(layerId, property), value)) {
    return false;
  }
  map.setPaintProperty(layerId, property, value);
  return true;
}

function setLayerLayoutPropertyIfChanged(map: MapLibreMap, layerId: string, property: string, value: unknown): boolean {
  if (!map.getLayer(layerId)) {
    return false;
  }
  if (!shouldApplyMapValue(map.getLayoutProperty(layerId, property), value)) {
    return false;
  }
  map.setLayoutProperty(layerId, property, value);
  return true;
}

function setLayerFilterIfChanged(map: MapLibreMap, layerId: string, filter: unknown[] | null): boolean {
  if (!map.getLayer(layerId)) {
    return false;
  }
  if (!shouldApplyMapValue(map.getFilter(layerId) ?? null, filter)) {
    return false;
  }
  map.setFilter(layerId, filter as maplibregl.FilterSpecification | null);
  return true;
}

function moveLayerToTopIfNeeded(map: MapLibreMap, layerId: string): boolean {
  const layers = map.getStyle()?.layers ?? [];
  if (!map.getLayer(layerId) || layers[layers.length - 1]?.id === layerId) {
    return false;
  }
  map.moveLayer(layerId);
  return true;
}

function moveLayerBeforeIfNeeded(map: MapLibreMap, layerId: string, beforeLayerId?: string): boolean {
  if (!map.getLayer(layerId)) {
    return false;
  }
  if (!beforeLayerId || !map.getLayer(beforeLayerId)) {
    return moveLayerToTopIfNeeded(map, layerId);
  }
  const layerIds = (map.getStyle()?.layers ?? []).map((layer) => layer.id);
  const targetIndex = layerIds.indexOf(layerId);
  const beforeIndex = layerIds.indexOf(beforeLayerId);
  if (beforeIndex > 0 && targetIndex === beforeIndex - 1) {
    return false;
  }
  map.moveLayer(layerId, beforeLayerId);
  return true;
}

function moveDrawingLayersToTop(map: MapLibreMap) {
  [
    "aoi-fill",
    "aoi-line",
    "export-perimeter-fill",
    "export-perimeter-line",
    "aoi-draft-fill",
    "aoi-draft-line",
    "rectangle-preview-fill",
    "rectangle-preview-line",
    "drawing-vertices-circle",
    "rectangle-vertices-line",
  ].forEach((layerId) => moveLayerToTopIfNeeded(map, layerId));
}

function setLayerVisibility(map: MapLibreMap, layerId: string, visible: boolean): boolean {
  if (!map.getLayer(layerId)) {
    return false;
  }
  return setLayerLayoutPropertyIfChanged(map, layerId, "visibility", visible ? "visible" : "none");
}

function getTemporalLayerPaintDiagnostics(map: MapLibreMap, layerId: string): Record<string, unknown> {
  const layer = map.getLayer(layerId) as { type?: string } | undefined;
  if (!layer) {
    return { layerType: null };
  }
  try {
    if (layer.type === "line") {
      return {
        layerType: layer.type,
        lineColor: map.getPaintProperty(layerId, "line-color"),
        lineOpacity: map.getPaintProperty(layerId, "line-opacity"),
      };
    }
    if (layer.type === "fill") {
      return {
        layerType: layer.type,
        fillColor: map.getPaintProperty(layerId, "fill-color"),
        fillOpacity: map.getPaintProperty(layerId, "fill-opacity"),
      };
    }
    return { layerType: layer.type ?? null };
  } catch (error) {
    devLog("TEMPORAL_VECTOR_TILE_PAINT_DIAGNOSTIC_SKIPPED", {
      layerId,
      layerType: layer.type ?? null,
      reason: error instanceof Error ? error.message : String(error),
    });
    return { layerType: layer.type ?? null, paintDiagnosticSkipped: true };
  }
}

function isMapStyleReadyForLayerMutation(map: MapLibreMap | null): boolean {
  if (!map) {
    return false;
  }
  try {
    return Array.isArray(map.getStyle()?.layers);
  } catch {
    return false;
  }
}

function referenceSourceId(layerId: string) {
  return `reference-layer-${layerId}`;
}

function syncReferenceLayers(map: MapLibreMap, layers: ReferenceLayerPresentation[], data: ReferenceLayerGeoJsonData) {
  const activeSourceIds = new Set(layers.map((layer) => referenceSourceId(layer.layer_id)));
  const styleLayers = map.getStyle()?.layers ?? [];
  styleLayers
    .filter((layer) => layer.id.startsWith("reference-layer-"))
    .forEach((layer) => {
      const sourceId = "source" in layer && typeof layer.source === "string" ? layer.source : "";
      if (!activeSourceIds.has(sourceId) && map.getLayer(layer.id)) {
        map.removeLayer(layer.id);
      }
    });
  Object.keys((map.getStyle()?.sources ?? {}) as Record<string, unknown>)
    .filter((sourceId) => sourceId.startsWith("reference-layer-") && !activeSourceIds.has(sourceId))
    .forEach((sourceId) => {
      if (map.getSource(sourceId)) {
        map.removeSource(sourceId);
      }
    });

  layers.forEach((layer) => {
    if (layer.layer_kind !== "vector") {
      return;
    }
    const sourceId = referenceSourceId(layer.layer_id);
    const isGeojson = layer.storage_strategy === "geojson";
    const isPmtiles = layer.storage_strategy === "pmtiles" && Boolean(layer.resolvedPmtilesUrl) && Boolean(layer.source_layer);
    if (!isGeojson && !isPmtiles) {
      return;
    }

    if (isGeojson) {
      const sourceDataValue = data[layer.layer_id] ?? EMPTY_FEATURE_COLLECTION;
      if (!map.getSource(sourceId)) {
        map.addSource(sourceId, { type: "geojson", data: sourceDataValue });
      } else {
        sourceData(map, sourceId, sourceDataValue);
      }
    } else if (!map.getSource(sourceId)) {
      map.addSource(sourceId, {
        type: "vector",
        url: `pmtiles://${layer.resolvedPmtilesUrl as string}`,
      });
    }

    const visible = layer.visible ? "visible" : "none";
    const opacity = Math.max(0, Math.min(1, layer.opacity));
    const fillLayerId = `${sourceId}-fill`;
    const lineLayerId = `${sourceId}-line`;
    const circleLayerId = `${sourceId}-circle`;
    const beforeLayer = map.getLayer("detected-polygons-fill") ? "detected-polygons-fill" : undefined;
    const vectorSourceLayer = isPmtiles ? layer.source_layer ?? undefined : undefined;

    if (!map.getLayer(fillLayerId)) {
      map.addLayer(
        {
          id: fillLayerId,
          type: "fill",
          source: sourceId,
          ...(vectorSourceLayer ? { "source-layer": vectorSourceLayer } : {}),
          layout: { visibility: visible },
          paint: {
            "fill-color": layer.style.fill_color,
            "fill-opacity": layer.style.fill_opacity * opacity,
            "fill-outline-color": layer.style.outline_color,
          },
          filter: ["in", ["geometry-type"], ["literal", ["Polygon", "MultiPolygon"]]],
        },
        beforeLayer,
      );
    }
    if (!map.getLayer(lineLayerId)) {
      map.addLayer(
        {
          id: lineLayerId,
          type: "line",
          source: sourceId,
          ...(vectorSourceLayer ? { "source-layer": vectorSourceLayer } : {}),
          layout: { visibility: visible },
          paint: {
            "line-color": layer.style.color,
            "line-opacity": opacity,
            "line-width": layer.style.line_width,
          },
          filter: ["in", ["geometry-type"], ["literal", ["LineString", "MultiLineString", "Polygon", "MultiPolygon"]]],
        },
        beforeLayer,
      );
    }
    if (!map.getLayer(circleLayerId)) {
      map.addLayer(
        {
          id: circleLayerId,
          type: "circle",
          source: sourceId,
          ...(vectorSourceLayer ? { "source-layer": vectorSourceLayer } : {}),
          layout: { visibility: visible },
          paint: {
            "circle-color": layer.style.color,
            "circle-opacity": opacity,
            "circle-radius": layer.style.point_radius,
          },
          filter: ["in", ["geometry-type"], ["literal", ["Point", "MultiPoint"]]],
        },
        beforeLayer,
      );
    }

    [fillLayerId, lineLayerId, circleLayerId].forEach((layerId) => setLayerVisibility(map, layerId, layer.visible));
    if (map.getLayer(fillLayerId)) {
      setPaintPropertyIfChanged(map, fillLayerId, "fill-color", layer.style.fill_color);
      setPaintPropertyIfChanged(map, fillLayerId, "fill-opacity", layer.style.fill_opacity * opacity);
      setPaintPropertyIfChanged(map, fillLayerId, "fill-outline-color", layer.style.outline_color);
    }
    if (map.getLayer(lineLayerId)) {
      setPaintPropertyIfChanged(map, lineLayerId, "line-color", layer.style.color);
      setPaintPropertyIfChanged(map, lineLayerId, "line-opacity", opacity);
      setPaintPropertyIfChanged(map, lineLayerId, "line-width", layer.style.line_width);
    }
    if (map.getLayer(circleLayerId)) {
      setPaintPropertyIfChanged(map, circleLayerId, "circle-color", layer.style.color);
      setPaintPropertyIfChanged(map, circleLayerId, "circle-opacity", opacity);
      setPaintPropertyIfChanged(map, circleLayerId, "circle-radius", layer.style.point_radius);
    }
  });
}

function ensureGeoJsonSource(map: MapLibreMap, sourceId: string) {
  if (map.getSource(sourceId)) {
    return;
  }
  map.addSource(sourceId, { type: "geojson", data: EMPTY_FEATURE_COLLECTION });
}

function ensureLineLayer(
  map: MapLibreMap,
  layerId: string,
  sourceId: string,
  paint: maplibregl.LineLayerSpecification["paint"],
) {
  if (map.getLayer(layerId)) {
    return;
  }
  map.addLayer({
    id: layerId,
    type: "line",
    source: sourceId,
    paint,
  });
}

function ensureFillLayer(
  map: MapLibreMap,
  layerId: string,
  sourceId: string,
  paint: maplibregl.FillLayerSpecification["paint"],
) {
  if (map.getLayer(layerId)) {
    return;
  }
  map.addLayer({
    id: layerId,
    type: "fill",
    source: sourceId,
    paint,
  });
}

function ensureCircleLayer(
  map: MapLibreMap,
  layerId: string,
  sourceId: string,
  paint: maplibregl.CircleLayerSpecification["paint"],
) {
  if (map.getLayer(layerId)) {
    return;
  }
  map.addLayer({
    id: layerId,
    type: "circle",
    source: sourceId,
    paint,
  });
}

function ensureOperationalLayers(map: MapLibreMap) {
  ensureGeoJsonSource(map, "aoi");
  ensureGeoJsonSource(map, "export-perimeter");
  ensureGeoJsonSource(map, "aoi-draft");
  ensureGeoJsonSource(map, "drawing-vertices");
  ensureGeoJsonSource(map, "rectangle-preview");
  ensureGeoJsonSource(map, "rectangle-vertices");
  ensureGeoJsonSource(map, "detected-polygons");
  ensureGeoJsonSource(map, "building-blocks");
  ensureGeoJsonSource(map, "buffer-layers");
  ensureGeoJsonSource(map, "buffer-10m");
  ensureGeoJsonSource(map, "buffer-15m");
  ensureGeoJsonSource(map, "buffer-20m");
  ensureGeoJsonSource(map, "temporal-additions");
  ensureGeoJsonSource(map, "temporal-cumulative-buffer-20m");
  ensureGeoJsonSource(map, "temporal-cumulative-buffer-15m");
  ensureGeoJsonSource(map, "temporal-cumulative-buffer-10m");
  ensureGeoJsonSource(map, "temporal-automated");
  ensureGeoJsonSource(map, "temporal-automated-building-blocks");
  ensureGeoJsonSource(map, "temporal-effective-building-blocks");
  ensureGeoJsonSource(map, "temporal-cumulative");
  ensureGeoJsonSource(map, "temporal-cumulative-growth-blocks");
  ensureGeoJsonSource(map, "temporal-cumulative-growth-envelope");
  ensureGeoJsonSource(map, "temporal-manual-override");

  ensureFillLayer(map, "aoi-fill", "aoi", { "fill-color": AOI_DRAW_STROKE_COLOR, "fill-opacity": 0.22 });
  ensureLineLayer(map, "aoi-line", "aoi", { "line-color": AOI_DRAW_STROKE_COLOR, "line-width": AOI_DRAW_STROKE_WIDTH });
  ensureFillLayer(map, "export-perimeter-fill", "export-perimeter", { "fill-color": AOI_DRAW_STROKE_COLOR, "fill-opacity": 0.22 });
  ensureLineLayer(map, "export-perimeter-line", "export-perimeter", { "line-color": AOI_DRAW_STROKE_COLOR, "line-width": AOI_DRAW_STROKE_WIDTH });
  ensureFillLayer(map, "aoi-draft-fill", "aoi-draft", {
    "fill-color": AOI_DRAW_STROKE_COLOR,
    "fill-opacity": AOI_DRAW_PREVIEW_FILL_OPACITY,
  });
  ensureLineLayer(map, "aoi-draft-line", "aoi-draft", {
    "line-color": AOI_DRAW_STROKE_COLOR,
    "line-width": AOI_DRAW_STROKE_WIDTH,
  });
  ensureCircleLayer(map, "drawing-vertices-circle", "drawing-vertices", {
    "circle-color": ["case", ["boolean", ["get", "closeTarget"], false], AOI_DRAW_STROKE_COLOR, AOI_DRAW_VERTEX_FILL],
    "circle-radius": ["case", ["boolean", ["get", "closeTarget"], false], AOI_DRAW_CLOSE_TARGET_RADIUS, AOI_DRAW_VERTEX_RADIUS],
    "circle-stroke-color": AOI_DRAW_STROKE_COLOR,
    "circle-stroke-width": 2,
  });
  ensureFillLayer(map, "rectangle-preview-fill", "rectangle-preview", {
    "fill-color": "#fbbf24",
    "fill-opacity": 0.12,
  });
  ensureLineLayer(map, "rectangle-preview-line", "rectangle-preview", {
    "line-color": "#fbbf24",
    "line-width": 2.5,
  });
  ensureLineLayer(map, "rectangle-vertices-line", "rectangle-vertices", {
    "line-color": "#fff",
    "line-width": 4,
  });
  ensureFillLayer(map, "detected-polygons-fill", "detected-polygons", {
    "fill-color": "#ef4444",
    "fill-opacity": 0.9,
  });
  ensureLineLayer(map, "detected-polygons-line", "detected-polygons", {
    "line-color": "#f87171",
    "line-width": 1.6,
  });
  ensureLineLayer(map, "building-blocks-line", "building-blocks", {
    "line-color": "#93c5fd",
    "line-width": 1.5,
  });
  ensureFillLayer(map, "temporal-cumulative-buffer-20m-fill", "temporal-cumulative-buffer-20m", {
    "fill-color": BUILDING_CHANGE_BUFFER_FILL_COLORS["20m"],
    "fill-opacity": 1,
  });
  ensureFillLayer(map, "temporal-cumulative-buffer-15m-fill", "temporal-cumulative-buffer-15m", {
    "fill-color": BUILDING_CHANGE_BUFFER_FILL_COLORS["15m"],
    "fill-opacity": 1,
  });
  ensureFillLayer(map, "temporal-cumulative-buffer-10m-fill", "temporal-cumulative-buffer-10m", {
    "fill-color": BUILDING_CHANGE_BUFFER_FILL_COLORS["10m"],
    "fill-opacity": 1,
  });
  ensureFillLayer(map, "temporal-additions-fill", "temporal-additions", {
    "fill-color": "#dc2626",
    "fill-opacity": 0.9,
  });
  ensureFillLayer(map, "temporal-automated-fill", "temporal-automated", {
    "fill-color": "#1d4ed8",
    "fill-opacity": 0.9,
  });
  ensureFillLayer(map, "temporal-automated-building-blocks-fill", "temporal-automated-building-blocks", {
    "fill-color": "#2563eb",
    "fill-opacity": 0.9,
  });
  ensureFillLayer(map, "temporal-effective-building-blocks-fill", "temporal-effective-building-blocks", {
    "fill-color": "#eab308",
    "fill-opacity": 0.9,
  });
  ensureFillLayer(map, "temporal-cumulative-fill", "temporal-cumulative", {
    "fill-color": "#dc2626",
    "fill-opacity": 0.9,
  });
  ensureFillLayer(map, "temporal-cumulative-growth-blocks-fill", "temporal-cumulative-growth-blocks", {
    "fill-color": "#2563eb",
    "fill-opacity": 0.9,
  });
  ensureFillLayer(map, "temporal-cumulative-growth-envelope-fill", "temporal-cumulative-growth-envelope", {
    "fill-color": "#1d4ed8",
    "fill-opacity": 0.9,
  });
  ensureFillLayer(map, "temporal-manual-override-fill", "temporal-manual-override", {
    "fill-color": "#dc2626",
    "fill-opacity": 0.9,
  });
  ensureFillLayer(map, "buffer-layers-fill", "buffer-layers", {
    "fill-color": "#c084fc",
    "fill-opacity": 0.35,
    "fill-outline-color": "rgba(0, 0, 0, 0)",
  });
  ensureLineLayer(map, "buffer-layers-line", "buffer-layers", {
    "line-color": "#7c3aed",
    "line-opacity": 0,
    "line-width": 2.5,
    "line-dasharray": [4, 3],
  });
  ensureFillLayer(map, "buffer-10m-fill", "buffer-10m", {
    "fill-color": BUILDING_CHANGE_BUFFER_FILL_COLORS["10m"],
    "fill-opacity": NON_CUMULATIVE_BUFFER_FILL_OPACITY,
    "fill-outline-color": "rgba(0, 0, 0, 0)",
  });
  ensureLineLayer(map, "buffer-10m-line", "buffer-10m", {
    "line-color": "#16a34a",
    "line-opacity": 0,
    "line-width": 3,
    "line-dasharray": [4, 3],
  });
  ensureFillLayer(map, "buffer-15m-fill", "buffer-15m", {
    "fill-color": BUILDING_CHANGE_BUFFER_FILL_COLORS["15m"],
    "fill-opacity": NON_CUMULATIVE_BUFFER_FILL_OPACITY,
    "fill-outline-color": "rgba(0, 0, 0, 0)",
  });
  ensureLineLayer(map, "buffer-15m-line", "buffer-15m", {
    "line-color": "#d97706",
    "line-opacity": 0,
    "line-width": 3.25,
    "line-dasharray": [5, 3],
  });
  ensureFillLayer(map, "buffer-20m-fill", "buffer-20m", {
    "fill-color": BUILDING_CHANGE_BUFFER_FILL_COLORS["20m"],
    "fill-opacity": NON_CUMULATIVE_BUFFER_FILL_OPACITY,
    "fill-outline-color": "rgba(0, 0, 0, 0)",
  });
  ensureLineLayer(map, "buffer-20m-line", "buffer-20m", {
    "line-color": "#a855f7",
    "line-opacity": 0,
    "line-width": 3.5,
    "line-dasharray": [6, 3],
  });
    if (map.getLayer("temporal-additions-fill")) {
    ["buffer-10m-fill", "buffer-10m-line", "buffer-15m-fill", "buffer-15m-line", "buffer-20m-fill", "buffer-20m-line"].forEach(
      (layerId) => {
        if (map.getLayer(layerId)) {
          moveLayerBeforeIfNeeded(map, layerId, "temporal-additions-fill");
        }
      },
    );
  }
}

function syncMapPresentation(
  map: MapLibreMap,
  params: {
    aoi: Polygon | null;
    exportGeometry: Polygon | null;
    draftVertices: [number, number][];
    detectedPolygons: FeatureCollection;
    buildingBlocks: FeatureCollection;
    bufferLayers: FeatureCollection;
    pairwiseBuffers: PairwiseBufferSources;
    temporalVectors: TemporalVectorSources;
    overlayBounds: [[number, number], [number, number], [number, number], [number, number]] | null;
    overlaySources: OverlaySources;
    referenceLayers: ReferenceLayerPresentation[];
    referenceLayerData: ReferenceLayerGeoJsonData;
    layerState: LayerToggleState;
    workflowMode: WorkflowMode;
  },
) {
  if (!map.isStyleLoaded()) {
    return;
  }

  syncAoiMapSource(map, useAppStore.getState().aoi);
  sourceData(map, "export-perimeter", polygonFeatureCollection(params.exportGeometry));
  sourceData(map, "detected-polygons", params.detectedPolygons);
  sourceData(map, "building-blocks", params.buildingBlocks);
  sourceData(map, "buffer-layers", params.bufferLayers);
  if (params.workflowMode === "pairwise") {
    sourceData(map, "buffer-10m", params.pairwiseBuffers.buffer10m);
    sourceData(map, "buffer-15m", params.pairwiseBuffers.buffer15m);
    sourceData(map, "buffer-20m", params.pairwiseBuffers.buffer20m);
  }
  syncReferenceLayers(map, params.referenceLayers, params.referenceLayerData);

  if (params.workflowMode === "temporal") {
    if (params.overlaySources.t1Preview || params.overlaySources.t2Preview) {
      devLog("LEGACY_PAIRWISE_PREVIEW_BLOCKED_IN_TEMPORAL_MODE", {
        t1PreviewPresent: Boolean(params.overlaySources.t1Preview),
        t2PreviewPresent: Boolean(params.overlaySources.t2Preview),
      });
    }
    setLayerVisibility(map, "overlay-t1-preview-layer", false);
    setLayerVisibility(map, "overlay-t2-preview-layer", false);
    setLayerVisibility(map, "overlay-change-probability-layer", false);
    setLayerVisibility(map, "overlay-change-overlay-layer", false);
  } else {
    syncImageOverlay(
      map,
      "overlay-t1-preview",
      "overlay-t1-preview-layer",
      params.overlaySources.t1Preview,
      params.overlayBounds,
      0.9,
      params.layerState.t1Preview,
    );
    syncImageOverlay(
      map,
      "overlay-t2-preview",
      "overlay-t2-preview-layer",
      params.overlaySources.t2Preview,
      params.overlayBounds,
      0.9,
      params.layerState.t2Preview,
    );
    syncImageOverlay(
      map,
      "overlay-change-probability",
      "overlay-change-probability-layer",
      params.overlaySources.changeProbability,
      params.overlayBounds,
      0.75,
      params.layerState.changeProbability,
    );
    syncImageOverlay(
      map,
      "overlay-change-overlay",
      "overlay-change-overlay-layer",
      params.overlaySources.changeOverlay,
      params.overlayBounds,
      0.85,
      params.layerState.changeOverlay,
    );
  }

  applyLabelVisibility(map, params.layerState.labels);
  setLayerVisibility(map, "overlay-t1-preview-layer", params.workflowMode === "pairwise" && params.layerState.t1Preview);
  setLayerVisibility(map, "overlay-t2-preview-layer", params.workflowMode === "pairwise" && params.layerState.t2Preview);
  setLayerVisibility(map, "overlay-change-probability-layer", params.workflowMode === "pairwise" && params.layerState.changeProbability);
  setLayerVisibility(map, "overlay-change-overlay-layer", params.workflowMode === "pairwise" && params.layerState.changeOverlay);
  setLayerVisibility(map, "detected-polygons-fill", params.layerState.detectedPolygons);
  setLayerVisibility(map, "detected-polygons-line", params.layerState.detectedPolygons);
  setLayerVisibility(map, "building-blocks-line", params.layerState.buildingBlocks);
  setLayerVisibility(map, "buffer-layers-fill", params.layerState.buffers);
  setLayerVisibility(map, "buffer-layers-line", params.layerState.buffers);
  setLayerVisibility(map, "buffer-10m-fill", params.workflowMode === "pairwise" && params.layerState.buffer10m);
  setLayerVisibility(map, "buffer-10m-line", params.workflowMode === "pairwise" && params.layerState.buffer10m);
  setLayerVisibility(map, "buffer-15m-fill", params.workflowMode === "pairwise" && params.layerState.buffer15m);
  setLayerVisibility(map, "buffer-15m-line", params.workflowMode === "pairwise" && params.layerState.buffer15m);
  setLayerVisibility(map, "buffer-20m-fill", params.workflowMode === "pairwise" && params.layerState.buffer20m);
  setLayerVisibility(map, "buffer-20m-line", params.workflowMode === "pairwise" && params.layerState.buffer20m);
  setLayerVisibility(map, "temporal-additions-fill", false);
  setLayerVisibility(map, "temporal-cumulative-buffer-20m-fill", false);
  setLayerVisibility(map, "temporal-cumulative-buffer-15m-fill", false);
  setLayerVisibility(map, "temporal-cumulative-buffer-10m-fill", false);
  setLayerVisibility(map, "temporal-automated-fill", false);
  setLayerVisibility(map, "temporal-automated-building-blocks-fill", false);
  setLayerVisibility(map, "temporal-effective-building-blocks-fill", false);
  setLayerVisibility(map, "temporal-cumulative-fill", false);
  setLayerVisibility(map, "temporal-cumulative-growth-blocks-fill", false);
  setLayerVisibility(map, "temporal-cumulative-growth-envelope-fill", false);
  setLayerVisibility(map, "temporal-manual-override-fill", false);
  moveDrawingLayersToTop(map);
}

function applyLayerVisibilityState(map: MapLibreMap, layerState: LayerToggleState, workflowMode: WorkflowMode = "pairwise") {
  applyLabelVisibility(map, layerState.labels);
  setLayerVisibility(map, "overlay-t1-preview-layer", layerState.t1Preview);
  setLayerVisibility(map, "overlay-t2-preview-layer", layerState.t2Preview);
  setLayerVisibility(map, "overlay-change-probability-layer", layerState.changeProbability);
  setLayerVisibility(map, "overlay-change-overlay-layer", layerState.changeOverlay);
  setLayerVisibility(map, "detected-polygons-fill", layerState.detectedPolygons);
  setLayerVisibility(map, "detected-polygons-line", layerState.detectedPolygons);
  setLayerVisibility(map, "building-blocks-line", layerState.buildingBlocks);
  setLayerVisibility(map, "buffer-layers-fill", layerState.buffers);
  setLayerVisibility(map, "buffer-layers-line", layerState.buffers);
  setLayerVisibility(map, "buffer-10m-fill", workflowMode === "pairwise" && layerState.buffer10m);
  setLayerVisibility(map, "buffer-10m-line", workflowMode === "pairwise" && layerState.buffer10m);
  setLayerVisibility(map, "buffer-15m-fill", workflowMode === "pairwise" && layerState.buffer15m);
  setLayerVisibility(map, "buffer-15m-line", workflowMode === "pairwise" && layerState.buffer15m);
  setLayerVisibility(map, "buffer-20m-fill", workflowMode === "pairwise" && layerState.buffer20m);
  setLayerVisibility(map, "buffer-20m-line", workflowMode === "pairwise" && layerState.buffer20m);
  setLayerVisibility(map, "temporal-additions-fill", false);
  setLayerVisibility(map, "temporal-cumulative-buffer-20m-fill", false);
  setLayerVisibility(map, "temporal-cumulative-buffer-15m-fill", false);
  setLayerVisibility(map, "temporal-cumulative-buffer-10m-fill", false);
  setLayerVisibility(map, "temporal-automated-fill", false);
  setLayerVisibility(map, "temporal-automated-building-blocks-fill", false);
  setLayerVisibility(map, "temporal-effective-building-blocks-fill", false);
  setLayerVisibility(map, "temporal-cumulative-fill", false);
  setLayerVisibility(map, "temporal-cumulative-growth-blocks-fill", false);
  setLayerVisibility(map, "temporal-cumulative-growth-envelope-fill", false);
  setLayerVisibility(map, "temporal-manual-override-fill", false);
}

function defaultLayerState(workflowMode: WorkflowMode, hasPairResult: boolean): LayerToggleState {
  const state: LayerToggleState = {
    t1Preview: workflowMode === "pairwise" ? false : false,
    t2Preview: workflowMode === "pairwise" ? false : false,
    temporalReferenceImagery: workflowMode === "temporal",
    changeProbability: workflowMode === "pairwise" ? false : false,
    changeOverlay: workflowMode === "pairwise" ? hasPairResult : false,
    detectedPolygons: workflowMode === "pairwise" ? hasPairResult : false,
    buildingBlocks: false,
    buffers: false,
    buffer10m: workflowMode === "temporal",
    buffer15m: false,
    buffer20m: false,
    temporalAdditions: workflowMode === "temporal",
    selectedMilestoneAdditions: workflowMode === "temporal",
    temporalCumulativeBuffer10m: false,
    temporalCumulativeBuffer15m: false,
    temporalCumulativeBuffer20m: false,
    temporalAutomated: false,
    temporalAutomatedBuildingBlocks: false,
    temporalEffectiveBuildingBlocks: false,
    temporalCumulative: false,
    temporalCumulativeGrowthBlocks: false,
    temporalCumulativeGrowthEnvelope: false,
    temporalManualOverride: workflowMode === "temporal",
    labels: true,
  };
  if (workflowMode !== "temporal" || typeof window === "undefined" || !isTemporalRenderAuditEnabled()) {
    return state;
  }
  const forcedLayerKeys = new URLSearchParams(window.location.search)
    .get("temporalLayerKeys")
    ?.split(",")
    .map((value) => value.trim())
    .filter(Boolean) as LayerToggleKey[] | undefined;
  if (!forcedLayerKeys?.length) {
    return state;
  }
  const temporalOutputKeys: LayerToggleKey[] = [
    "temporalAdditions",
    "selectedMilestoneAdditions",
    "buffer10m",
    "buffer15m",
    "buffer20m",
    "temporalCumulativeBuffer10m",
    "temporalCumulativeBuffer15m",
    "temporalCumulativeBuffer20m",
    "temporalAutomated",
    "temporalAutomatedBuildingBlocks",
    "temporalEffectiveBuildingBlocks",
    "temporalCumulative",
    "temporalCumulativeGrowthBlocks",
    "temporalCumulativeGrowthEnvelope",
    "temporalManualOverride",
  ];
  for (const key of temporalOutputKeys) {
    state[key] = false;
  }
  for (const key of forcedLayerKeys) {
    if (key in state) {
      state[key] = true;
    }
  }
  return state;
}

function normalizeNominatimResults(payload: unknown, unnamedLocationLabel: string): SearchResult[] {
  if (!Array.isArray(payload)) {
    return [];
  }

  return payload
    .map((entry, index) => {
      if (!entry || typeof entry !== "object") {
        return null;
      }

      const record = entry as Record<string, unknown>;
      const lat = Number(record.lat);
      const lon = Number(record.lon);
      if (!Number.isFinite(lat) || !Number.isFinite(lon)) {
        return null;
      }

      const bbox =
        Array.isArray(record.boundingbox) && record.boundingbox.length >= 4
          ? [
              Number(record.boundingbox[2]),
              Number(record.boundingbox[0]),
              Number(record.boundingbox[3]),
              Number(record.boundingbox[1]),
            ] as [number, number, number, number]
          : null;

      const type = typeof record.type === "string" ? record.type : null;
      const category = typeof record.class === "string" ? record.class : null;

      return {
        id: String(record.place_id ?? index),
        label: String(record.display_name ?? unnamedLocationLabel),
        subtitle: [type, category].filter(Boolean).join(" · ") || null,
        center: [lon, lat] as [number, number],
        bbox,
      };
    })
    .filter((feature): feature is SearchResult => Boolean(feature));
}

async function fetchSearchResults(
  query: string,
  apiKey: string,
  signal: AbortSignal,
  unnamedLocationLabel: string,
): Promise<SearchResult[]> {
  if (apiKey) {
    const url = new URL("https://api.mapbox.com/search/geocode/v6/forward");
    url.searchParams.set("q", query);
    url.searchParams.set("access_token", apiKey);
    url.searchParams.set("limit", "6");
    url.searchParams.set("language", "en");

    const response = await fetch(url.toString(), { signal });
    if (response.ok) {
      const data = await response.json();
      // Mapbox API v6 returns features array
      if (data.features && Array.isArray(data.features)) {
        return data.features
          .map((feature: Record<string, unknown>, index: number) => {
            const geometry = feature.geometry as Record<string, unknown> | undefined;
            const coords = geometry?.coordinates as number[] | undefined;
            if (!coords || coords.length < 2) {
              return null;
            }
            const [lon, lat] = coords;
            const bbox = (feature.bbox as number[] | undefined) || null;
            const name = (feature.properties as Record<string, unknown>)?.["name"] as string | undefined || "";
            const type = (feature.properties as Record<string, unknown>)?.["feature_type"] as string | undefined || "";

            return {
              id: String(feature.id ?? index),
              label: name || String(feature.id ?? index),
              subtitle: type ? String(type) : null,
              center: [lon, lat] as [number, number],
              bbox: bbox && bbox.length >= 4 ? [bbox[0], bbox[1], bbox[2], bbox[3]] as [number, number, number, number] : null,
            } as SearchResult;
          })
          .filter((r: SearchResult | null): r is SearchResult => r !== null);
      }
    }

    if (![401, 403, 429].includes(response.status)) {
      return [];
    }
  }

  // Fallback to Nominatim if Mapbox fails or key missing
  const fallbackUrl = new URL("https://nominatim.openstreetmap.org/search");
  fallbackUrl.searchParams.set("q", query);
  fallbackUrl.searchParams.set("format", "jsonv2");
  fallbackUrl.searchParams.set("limit", "6");
  fallbackUrl.searchParams.set("addressdetails", "1");
  fallbackUrl.searchParams.set("accept-language", "en,fr");

  const fallbackResponse = await fetch(fallbackUrl.toString(), {
    signal,
    headers: {
      Accept: "application/json",
    },
  });
  if (!fallbackResponse.ok) {
    throw new Error(`Search failed with HTTP ${fallbackResponse.status}.`);
  }
  return normalizeNominatimResults(await fallbackResponse.json(), unnamedLocationLabel);
}

export function MapView({
  apiKey,
  backendUrl,
  workflowMode,
  temporalPresentation,
  onTemporalLayerControlsChange,
}: {
  apiKey: string;
  backendUrl: string;
  workflowMode: WorkflowMode;
  temporalPresentation: TemporalMapPresentation | null;
  onTemporalLayerControlsChange?: (controls: TemporalLayerControlsPresentation | null) => void;
}) {
  const { t } = useI18n();
  const containerRef = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const markersRef = useRef<maplibregl.Marker[]>([]);
  const [mapError, setMapError] = useState<string | null>(null);
  const [mapStyleRevision, setMapStyleRevision] = useState(0);
  const [layersOpen, setLayersOpen] = useState(false);
  const [statisticsOpen, setStatisticsOpen] = useState(false);
  const [layerState, setLayerState] = useState<LayerToggleState>(() => defaultLayerState("pairwise", false));
  const [searchValue, setSearchValue] = useState("");
  const [searchResults, setSearchResults] = useState<SearchResult[]>([]);
  const [searchLoading, setSearchLoading] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [highlightedResultIndex, setHighlightedResultIndex] = useState(-1);
  const [drawingInstruction, setDrawingInstruction] = useState<string | null>(null);
  const [drawingPointer, setDrawingPointer] = useState<[number, number] | null>(null);
  const [drawingCursorCoordinate, setDrawingCursorCoordinate] = useState<[number, number] | null>(null);
  const [firstVertexCloseTarget, setFirstVertexCloseTarget] = useState(false);
  const [temporalReferenceLoading, setTemporalReferenceLoading] = useState(false);
  const [referenceLayerData, setReferenceLayerData] = useState<ReferenceLayerGeoJsonData>({});
  const temporalReferenceLayerIdsRef = useRef<Set<string>>(new Set());
  const temporalReferenceSourceIdsRef = useRef<Set<string>>(new Set());
  const temporalReferenceSourceSignaturesRef = useRef<Record<string, string>>({});
  const temporalAddedLayerIdsRef = useRef<Set<string>>(new Set());
  const temporalAddedSourceIdsRef = useRef<Set<string>>(new Set());
  const temporalAddedSourceSignaturesRef = useRef<Record<string, string>>({});
  const temporalAddedRegistrationKeyRef = useRef<string | null>(null);
  const temporalAddedRegistrationStatsRef = useRef<Record<string, { featureCount: number; payloadBytes: number }>>({});
  const temporalAddedSyncRetryKeyRef = useRef<string | null>(null);
  const activeTemporalAddedProjectIdRef = useRef<string | null>(null);
  const activeTemporalAddedReleaseIdentifierRef = useRef<string | null>(null);
  const activeTemporalAddedSwitchKeyRef = useRef<string | null>(null);
  const visualReadyTemporalAddedSwitchKeysRef = useRef<Set<string>>(new Set());
  const temporalRenderAuditSkippedSwitchKeysRef = useRef<Set<string>>(new Set());
  const temporalReferenceLoadedLayerIdsRef = useRef<Set<string>>(new Set());
  const temporalReferenceTilejsonCacheRef = useRef<Partial<Record<string, TemporalReferenceTilejsonPayload>>>({});
  const temporalReferenceTilejsonPendingRef = useRef<
    Partial<Record<string, Promise<TemporalReferenceTilejsonPayload | null>>>
  >({});
  const temporalReferencePrefetchedViewportKeysRef = useRef<Set<string>>(new Set());
  const temporalReferencePrewarmAbortRef = useRef<AbortController | null>(null);
  const preloadedImageUrlsRef = useRef<Set<string>>(new Set());
  const temporalLayerStateByProjectRef = useRef<Record<string, LayerToggleState>>({});
  const lastTemporalControlsSignatureRef = useRef<string | null>(null);
  const activeLayerStateScopeRef = useRef<string | null>(null);
  const activeTemporalReferenceLayerIdRef = useRef<string | null>(null);
  const activeTemporalReferenceReleaseIdentifierRef = useRef<string | null>(null);
  const activeTemporalProjectIdRef = useRef<string | null>(null);
  const activeTemporalReferenceSwitchKeyRef = useRef<string | null>(null);
  const committedTemporalReferenceSwitchKeyRef = useRef<string | null>(null);
  const visualReadyTemporalReferenceSwitchKeysRef = useRef<Set<string>>(new Set());
  const committedTemporalReferenceSwitchKeysRef = useRef<Set<string>>(new Set());
  const loggedTemporalReferenceDuplicateSkipKeysRef = useRef<Set<string>>(new Set());
  const pendingTemporalReferenceReadyCleanupRef = useRef<(() => void) | null>(null);
  const temporalReferenceDebugContextRef = useRef<{
    workflowMode: WorkflowMode;
    projectId: string | null;
    selectedReleaseIdentifier: string | null;
    referenceImagery: TemporalReferenceImageryPresentation | null;
    referenceImageryAvailable: boolean;
    referenceLayerEnabled: boolean;
    mapStyleRevision: number;
  }>({
    workflowMode,
    projectId: null,
    selectedReleaseIdentifier: null,
    referenceImagery: null,
    referenceImageryAvailable: false,
    referenceLayerEnabled: false,
    mapStyleRevision: 0,
  });
  const latestPresentationRef = useRef<{
    aoi: Polygon | null;
    exportGeometry: Polygon | null;
    draftVertices: [number, number][];
    detectedPolygons: FeatureCollection;
    buildingBlocks: FeatureCollection;
    bufferLayers: FeatureCollection;
    pairwiseBuffers: PairwiseBufferSources;
    temporalVectors: TemporalVectorSources;
    overlayBounds: [[number, number], [number, number], [number, number], [number, number]] | null;
    overlaySources: OverlaySources;
    referenceLayers: ReferenceLayerPresentation[];
    referenceLayerData: ReferenceLayerGeoJsonData;
    layerState: LayerToggleState;
    workflowMode: WorkflowMode;
  } | null>(null);

  const aoi = useAppStore((state) => state.aoi);
  const exportGeometry = useAppStore((state) => state.exportGeometry);
  const draftVertices = useAppStore((state) => state.draftVertices);
  const mapFocusRequestId = useAppStore((state) => state.mapFocusRequestId);
  const referenceLayerFocus = useAppStore((state) => state.referenceLayerFocus);
  const drawingMode = useAppStore((state) => state.drawingMode);
  const drawingSubMode = useAppStore((state) => state.drawingSubMode);
  const isRunning = useAppStore((state) => state.isRunning);
  const setDraftVertices = useAppStore((state) => state.setDraftVertices);
  const finishDrawing = useAppStore((state) => state.finishDrawing);
  const completeDrawing = useAppStore((state) => state.completeDrawing);
  const stopDrawing = useAppStore((state) => state.stopDrawing);
  const updateDraftVertex = useAppStore((state) => state.updateDraftVertex);
  const result = useAppStore((state) => state.result);
  const temporalAddedOverlayTimeline =
    workflowMode === "temporal" ? temporalPresentation?.addedOverlayTimeline ?? [] : [];
  const activeTemporalAddedOverlay =
    workflowMode === "temporal" && temporalPresentation?.selectedReleaseIdentifier
      ? temporalAddedOverlayTimeline.find(
          (overlay) => overlay.releaseIdentifier === temporalPresentation.selectedReleaseIdentifier,
        ) ?? null
      : null;
  const temporalRawAdditions = useMemo(
    () => buildRawAdditionFeatureCollection(activeTemporalAddedOverlay?.additions ?? temporalPresentation?.additions),
    [activeTemporalAddedOverlay?.additions, temporalPresentation?.additions],
  );
  const temporalVectors = useMemo<TemporalVectorSources>(
    () => ({
      temporalAdditions: temporalRawAdditions,
      temporalCumulativeBuffer10m: ensureFeatureCollection(temporalPresentation?.cumulativeBuffer10m),
      temporalCumulativeBuffer15m: ensureFeatureCollection(temporalPresentation?.cumulativeBuffer15m),
      temporalCumulativeBuffer20m: ensureFeatureCollection(temporalPresentation?.cumulativeBuffer20m),
      temporalAutomated: ensureFeatureCollection(temporalPresentation?.automatedCandidate),
      temporalAutomatedBuildingBlocks: ensureFeatureCollection(temporalPresentation?.automatedBuildingBlocks),
      temporalEffectiveBuildingBlocks: ensureFeatureCollection(temporalPresentation?.effectiveBuildingBlocks),
      temporalCumulative: ensureFeatureCollection(temporalPresentation?.cumulativeUnion),
      temporalCumulativeGrowthBlocks: ensureFeatureCollection(temporalPresentation?.cumulativeGrowthBlocks),
      temporalCumulativeGrowthEnvelope: ensureFeatureCollection(temporalPresentation?.cumulativeGrowthEnvelope),
      temporalManualOverride: ensureFeatureCollection(temporalPresentation?.manualOverride),
    }),
    [temporalPresentation, temporalRawAdditions],
  );
  const selectedTemporalMilestoneReady =
    temporalPresentation?.selectedMilestoneStatus === "complete" ||
    temporalPresentation?.selectedMilestoneStatus === "validated";
  const hasPairwiseLayerContext = workflowMode === "pairwise" && Boolean(result?.success);
  const hasTemporalMosaicLayerContext =
    workflowMode === "temporal" && Boolean(temporalPresentation) && (temporalPresentation?.milestoneCount ?? 0) >= 2;
  const detectedPolygons = useMemo(
    () =>
      workflowMode === "pairwise"
        ? (((result?.change_polygons_geojson ?? result?.new_buildings_geojson) as FeatureCollection | undefined) ??
          EMPTY_FEATURE_COLLECTION)
        : EMPTY_FEATURE_COLLECTION,
    [result?.change_polygons_geojson, result?.new_buildings_geojson, workflowMode],
  );
  const buildingBlocks = useMemo(
    () =>
      workflowMode === "pairwise"
        ? ((result?.building_blocks_geojson as FeatureCollection | undefined) ?? EMPTY_FEATURE_COLLECTION)
        : EMPTY_FEATURE_COLLECTION,
    [result?.building_blocks_geojson, workflowMode],
  );
  const bufferLayers = useMemo(
    () => (workflowMode === "pairwise" ? mergeBuffers(result?.buffer_layers_geojson ?? {}) : EMPTY_FEATURE_COLLECTION),
    [result?.buffer_layers_geojson, workflowMode],
  );
  const pairwiseBuffers = useMemo<PairwiseBufferSources>(
    () => ({
      buffer10m:
        workflowMode === "pairwise"
          ? bufferFeatureCollection(result?.buffer_layers_geojson, "10m")
          : ensureFeatureCollection(activeTemporalAddedOverlay?.buffer10m),
      buffer15m:
        workflowMode === "pairwise"
          ? bufferFeatureCollection(result?.buffer_layers_geojson, "15m")
          : ensureFeatureCollection(activeTemporalAddedOverlay?.buffer15m),
      buffer20m:
        workflowMode === "pairwise"
          ? bufferFeatureCollection(result?.buffer_layers_geojson, "20m")
          : ensureFeatureCollection(activeTemporalAddedOverlay?.buffer20m),
    }),
    [activeTemporalAddedOverlay, result?.buffer_layers_geojson, workflowMode],
  );
  const temporalReferenceImagery =
    workflowMode === "temporal" ? temporalPresentation?.referenceImagery ?? null : null;
  const temporalReferenceImageryTimeline =
    workflowMode === "temporal" ? temporalPresentation?.referenceImageryTimeline ?? [] : [];
  const activeTemporalArtifactAvailable = useCallback(
    (artifactKey: string) => {
      const artifact = activeTemporalAddedOverlay?.artifacts[artifactKey];
      return Boolean(
        artifact &&
          ((artifact.featureCount ?? 0) > 0 ||
            (artifact.sizeBytes ?? 0) > 0 ||
            artifact.tilejsonUrl ||
            artifact.tilesUrlTemplate),
      );
    },
    [activeTemporalAddedOverlay?.artifacts],
  );
  const temporalAvailableMilestoneIds =
    workflowMode === "temporal" ? temporalPresentation?.availableMilestoneIds ?? [] : [];
  const temporalAvailableMilestones =
    workflowMode === "temporal"
      ? (temporalPresentation?.availableMilestones?.length
          ? temporalPresentation.availableMilestones
          : temporalAvailableMilestoneIds.map((releaseIdentifier) => ({ releaseIdentifier, date: null })))
      : [];
  const temporalMilestoneColorByReleaseIdentifier = useMemo(
    () => (workflowMode === "temporal" ? getMilestoneColorMap(temporalAvailableMilestones) : {}),
    [temporalAvailableMilestones, workflowMode],
  );
  const temporalLayerLabels = useMemo(
    () =>
      buildTemporalLayerLabels(
        temporalAvailableMilestones,
        workflowMode === "temporal" ? temporalPresentation?.selectedReleaseIdentifier : null,
        {
          allNewBuildings: t("map.all_new_buildings"),
          addedBuildingIn: t("map.added_building_in"),
          buffer10m: t("map.buffer_10m"),
          buffer15m: t("map.buffer_15m"),
          buffer20m: t("map.buffer_20m"),
          rangeSeparator: "→",
        },
      ),
    [temporalAvailableMilestones, temporalPresentation?.selectedReleaseIdentifier, t, workflowMode],
  );
  const includedTemporalAdditionMilestones = useMemo<IncludedTemporalMilestone[]>(
    () =>
      workflowMode === "temporal"
        ? getIncludedTemporalMilestones(temporalAvailableMilestones, temporalPresentation?.selectedReleaseIdentifier)
        : [],
    [temporalAvailableMilestones, temporalPresentation?.selectedReleaseIdentifier, workflowMode],
  );
  const availableAdditionReleaseIdentifiers = useMemo(() => {
    if (workflowMode !== "temporal") {
      return [];
    }
    const additionsDefinition = TEMPORAL_ADDED_LAYER_DEFINITIONS.find((definition) => definition.kind === "additions");
    if (!additionsDefinition) {
      return [];
    }
    return temporalAddedOverlayTimeline
      .filter((overlay) => temporalAddedLayerAvailability(overlay, additionsDefinition).available)
      .map((overlay) => overlay.releaseIdentifier);
  }, [temporalAddedOverlayTimeline, workflowMode]);
  const availableTemporalOutputReleaseIdentifiersByKind = useMemo(
    () =>
      workflowMode === "temporal"
        ? buildAvailableReleaseIdentifiersByKind(temporalAddedOverlayTimeline)
        : createEmptyTemporalReleaseSetByKind(),
    [temporalAddedOverlayTimeline, workflowMode],
  );
  const includedTemporalAdditionReleaseIdentifiers = useMemo(
    () =>
      workflowMode === "temporal"
        ? getIncludedAdditionReleasesForCumulativeLayer(
            temporalAvailableMilestones,
            temporalPresentation?.selectedReleaseIdentifier,
            availableAdditionReleaseIdentifiers,
          )
        : [],
    [
      availableAdditionReleaseIdentifiers,
      temporalAvailableMilestones,
      temporalPresentation?.selectedReleaseIdentifier,
      workflowMode,
    ],
  );
  const expectedTemporalOutputReleaseIdentifiersByKind = useMemo(
    () =>
      workflowMode === "temporal"
        ? buildExpectedTemporalReleaseSets({
            definitions: TEMPORAL_ADDED_LAYER_DEFINITIONS,
            milestones: temporalAvailableMilestones,
            selectedReleaseIdentifier: temporalPresentation?.selectedReleaseIdentifier,
            availableByKind: availableTemporalOutputReleaseIdentifiersByKind,
            includedAdditionReleaseIdentifiers: includedTemporalAdditionReleaseIdentifiers,
            layerState,
          })
        : createEmptyTemporalReleaseSetByKind(),
    [
      availableTemporalOutputReleaseIdentifiersByKind,
      includedTemporalAdditionReleaseIdentifiers,
      layerState,
      temporalAvailableMilestones,
      temporalPresentation?.selectedReleaseIdentifier,
      workflowMode,
    ],
  );
  const activeTemporalMilestoneColor =
    temporalPresentation?.selectedReleaseIdentifier
      ? temporalMilestoneColorByReleaseIdentifier[temporalPresentation.selectedReleaseIdentifier] ?? "#B91C1C"
      : "#B91C1C";
  const availableAdditionsByDateEntries = useMemo(
    () => {
      if (workflowMode !== "temporal") {
        return [];
      }
      const additionsDefinition = TEMPORAL_ADDED_LAYER_DEFINITIONS.find((definition) => definition.kind === "additions");
      if (!additionsDefinition) {
        return [];
      }
      const overlayByRelease = new Map(
        temporalAddedOverlayTimeline.map((overlay) => [overlay.releaseIdentifier, overlay] as const),
      );
      return includedTemporalAdditionMilestones
        .filter((milestone) => {
          const overlay = overlayByRelease.get(milestone.releaseIdentifier);
          return Boolean(overlay && temporalAddedLayerAvailability(overlay, additionsDefinition).available);
        })
        .map((milestone) => ({
          ...milestone,
          color: temporalMilestoneColorByReleaseIdentifier[milestone.releaseIdentifier] ?? "#B91C1C",
        }));
    },
    [
      includedTemporalAdditionMilestones,
      temporalAddedOverlayTimeline,
      temporalMilestoneColorByReleaseIdentifier,
      workflowMode,
    ],
  );
  const selectedMilestoneAdditionsEntry =
    temporalPresentation?.selectedReleaseIdentifier
      ? (availableAdditionsByDateEntries.find(
          (entry) => entry.releaseIdentifier === temporalPresentation.selectedReleaseIdentifier,
        ) ?? null)
      : null;
  const visibleAdditionsLegendEntries = useMemo(() => {
    const visibleEntries = new Map<string, (typeof availableAdditionsByDateEntries)[number]>();
    if (layerState.temporalAdditions) {
      for (const entry of availableAdditionsByDateEntries) {
        visibleEntries.set(entry.releaseIdentifier, entry);
      }
    }
    if (layerState.selectedMilestoneAdditions && selectedMilestoneAdditionsEntry) {
      visibleEntries.set(selectedMilestoneAdditionsEntry.releaseIdentifier, selectedMilestoneAdditionsEntry);
    }
    return Array.from(visibleEntries.values());
  }, [
    availableAdditionsByDateEntries,
    layerState.selectedMilestoneAdditions,
    layerState.temporalAdditions,
    selectedMilestoneAdditionsEntry,
  ]);
  const allPreviousAdditionsAvailable = availableAdditionsByDateEntries.length > 0;
  const selectedMilestoneAdditionsAvailable = Boolean(selectedMilestoneAdditionsEntry);
  const selectedMilestoneAdditionsLabel = temporalLayerLabels.selectedAdditions;
  const temporalSelectedMilestoneIndex =
    workflowMode === "temporal" ? temporalPresentation?.selectedMilestoneIndex ?? -1 : -1;
  const temporalHydratingProject =
    workflowMode === "temporal" ? temporalPresentation?.isHydratingProject ?? false : false;
  const temporalProjectId = workflowMode === "temporal" ? temporalPresentation?.projectId ?? null : null;
  useEffect(() => {
    if (temporalRenderAuditModeLogged) {
      return;
    }
    temporalRenderAuditModeLogged = true;
    devLog("TEMPORAL_RENDER_AUDIT_MODE", {
      enabled: isTemporalRenderAuditEnabled(),
      envEnabled: import.meta.env.VITE_TEMPORAL_RENDER_AUDIT === "true",
      debugRenderAudit:
        typeof window !== "undefined"
          ? new URLSearchParams(window.location.search).get("debugRenderAudit") === "1"
          : false,
      validationMode: typeof window !== "undefined" ? new URLSearchParams(window.location.search).has("validation") : false,
    });
  }, []);
  useEffect(() => {
    if (workflowMode !== "temporal" || !temporalProjectId || temporalAvailableMilestoneIds.length === 0) {
      return;
    }
    devLog("TEMPORAL_MILESTONE_COLOR_MAP", {
      projectId: temporalProjectId,
      colorByReleaseIdentifier: temporalMilestoneColorByReleaseIdentifier,
    });
    if (usesGeneratedMilestoneColors(temporalAvailableMilestoneIds.length)) {
      console.warn("TEMPORAL_MILESTONE_COLOR_PALETTE_EXTENDED", {
        projectId: temporalProjectId,
        milestoneCount: temporalAvailableMilestoneIds.length,
        colorByReleaseIdentifier: temporalMilestoneColorByReleaseIdentifier,
      });
      devLog("TEMPORAL_MILESTONE_COLOR_PALETTE_EXTENDED", {
        projectId: temporalProjectId,
        milestoneCount: temporalAvailableMilestoneIds.length,
        colorByReleaseIdentifier: temporalMilestoneColorByReleaseIdentifier,
      });
    }
  }, [temporalAvailableMilestoneIds, temporalMilestoneColorByReleaseIdentifier, temporalProjectId, workflowMode]);
  const getTemporalOutputSyncLayerState = useCallback(
    (projectIdForSync: string | null): LayerToggleState => {
      if (!projectIdForSync) {
        return layerState;
      }
      const expectedScopeKey = `temporal:${projectIdForSync}`;
      if (activeLayerStateScopeRef.current === expectedScopeKey) {
        return layerState;
      }
      const defaultTemporalState = defaultLayerState("temporal", false);
      devLog("TEMPORAL_OUTPUT_LAYER_STATE_DEFAULTED_FOR_SYNC", {
        projectId: projectIdForSync,
        reason: "temporal_scope_not_initialized",
        expectedScopeKey,
        activeScopeKey: activeLayerStateScopeRef.current,
        enabledLayerKeys: dedupeStable(
          TEMPORAL_ADDED_LAYER_DEFINITIONS.filter((definition) => defaultTemporalState[definition.toggleKey]).map(
            (definition) => definition.toggleKey,
          ),
        ),
      });
      return defaultTemporalState;
    },
    [layerState],
  );
  const overlayBounds = useMemo(() => {
    if (workflowMode === "pairwise" && hasValidRasterBounds(result?.preview_images?.raster_bounds_wgs84)) {
      return imageCoordinatesFromBBox(result.preview_images.raster_bounds_wgs84);
    }
    return null;
  }, [result?.preview_images?.raster_bounds_wgs84, workflowMode]);
  const rasterOverlaysGeoreferenced = Boolean(overlayBounds);
  const overlaySources = useMemo(
    () => ({
      t1Preview:
        workflowMode === "pairwise" && result?.preview_images?.t1_preview_path
          ? buildBackendFileUrl(backendUrl, result.preview_images.t1_preview_path)
          : null,
      t2Preview:
        workflowMode === "pairwise" && result?.preview_images?.t2_preview_path
          ? buildBackendFileUrl(backendUrl, result.preview_images.t2_preview_path)
          : null,
      changeProbability:
        workflowMode === "pairwise" && result?.preview_images?.change_probability_preview_path
          ? buildBackendFileUrl(backendUrl, result.preview_images.change_probability_preview_path)
          : null,
      changeOverlay:
        workflowMode === "pairwise" && result?.preview_images?.change_overlay_preview_path
          ? buildBackendFileUrl(backendUrl, result.preview_images.change_overlay_preview_path)
          : null,
    }),
    [backendUrl, result?.preview_images, workflowMode],
  );

  const hasTemporalResult =
    temporalVectors.temporalAdditions.features.length > 0 ||
    temporalVectors.temporalCumulativeBuffer10m.features.length > 0 ||
    temporalVectors.temporalCumulativeBuffer15m.features.length > 0 ||
    temporalVectors.temporalCumulativeBuffer20m.features.length > 0 ||
    temporalVectors.temporalAutomated.features.length > 0 ||
    temporalVectors.temporalAutomatedBuildingBlocks.features.length > 0 ||
    temporalVectors.temporalEffectiveBuildingBlocks.features.length > 0 ||
    temporalVectors.temporalCumulative.features.length > 0 ||
    temporalVectors.temporalCumulativeGrowthBlocks.features.length > 0 ||
    temporalVectors.temporalCumulativeGrowthEnvelope.features.length > 0 ||
    temporalVectors.temporalManualOverride.features.length > 0;
  const temporalReferenceImageryAvailable = Boolean(
    temporalReferenceImagery &&
      (temporalReferenceImagery.storageStrategy === "raster_tiles" ||
        temporalReferenceImagery.storageStrategy === "cog" ||
        (temporalReferenceImagery.storageStrategy === "image_overlay" &&
          temporalReferenceImagery.imageUrl &&
          temporalReferenceImagery.bounds)),
  );
  const temporalReferenceImageryMissingGeoreference = Boolean(
    temporalReferenceImagery?.storageStrategy === "image_overlay" &&
      temporalReferenceImagery.imageUrl &&
      !temporalReferenceImagery.bounds,
  );

  const referenceLayers = useMemo(
    () => (workflowMode === "temporal" ? temporalPresentation?.referenceLayers ?? [] : []),
    [temporalPresentation?.referenceLayers, workflowMode],
  );

  const updateLayerState = useCallback(
    (updater: Partial<LayerToggleState> | ((current: LayerToggleState) => LayerToggleState)) => {
      setLayerState((current) => {
        const next =
          typeof updater === "function"
            ? (updater as (value: LayerToggleState) => LayerToggleState)(current)
            : { ...current, ...updater };
        if (mapRef.current) {
          applyLayerVisibilityState(mapRef.current, next, workflowMode);
        }
        if (workflowMode === "temporal" && temporalProjectId) {
          temporalLayerStateByProjectRef.current[temporalProjectId] = next;
        }
        return next;
      });
    },
    [temporalProjectId, workflowMode],
  );

  useEffect(() => {
    if (referenceLayers.some((layer) => layer.storage_strategy === "pmtiles" && layer.resolvedPmtilesUrl)) {
      ensurePmtilesProtocol();
    }
  }, [referenceLayers]);

  useEffect(() => {
    let cancelled = false;
    const layersToLoad = referenceLayers.filter(
      (layer) => layer.storage_strategy === "geojson" && layer.resolvedDisplayUrl && !referenceLayerData[layer.layer_id],
    );
    if (!layersToLoad.length) {
      return;
    }
    void Promise.all(
      layersToLoad.map(async (layer) => {
        const response = await fetch(layer.resolvedDisplayUrl as string);
        if (!response.ok) {
          throw new Error(`Reference layer ${layer.name} failed to load.`);
        }
        return [layer.layer_id, ensureFeatureCollection(await response.json())] as const;
      }),
    )
      .then((entries) => {
        if (!cancelled) {
          setReferenceLayerData((current) => ({ ...current, ...Object.fromEntries(entries) }));
        }
      })
      .catch(() => {
        // The layer remains listed even if its display artifact is unavailable.
      });
    return () => {
      cancelled = true;
    };
  }, [referenceLayerData, referenceLayers]);

  useEffect(() => {
    const map = mapRef.current;
    const projectId = temporalPresentation?.projectId ?? null;
    const clearTemporalReferenceLayers = () => {
      if (map) {
        for (const layerId of temporalReferenceLayerIdsRef.current) {
          if (map.getLayer(layerId)) {
            map.removeLayer(layerId);
          }
        }
        for (const sourceId of temporalReferenceSourceIdsRef.current) {
          if (map.getSource(sourceId)) {
            map.removeSource(sourceId);
          }
        }
      }
      temporalReferenceLayerIdsRef.current.clear();
      temporalReferenceSourceIdsRef.current.clear();
      temporalReferenceSourceSignaturesRef.current = {};
      temporalReferenceLoadedLayerIdsRef.current.clear();
      temporalReferenceTilejsonCacheRef.current = {};
      temporalReferenceTilejsonPendingRef.current = {};
      temporalReferencePrefetchedViewportKeysRef.current.clear();
      temporalReferencePrewarmAbortRef.current?.abort();
      temporalReferencePrewarmAbortRef.current = null;
      preloadedImageUrlsRef.current.clear();
      activeTemporalReferenceLayerIdRef.current = null;
      activeTemporalReferenceReleaseIdentifierRef.current = null;
      activeTemporalReferenceSwitchKeyRef.current = null;
      committedTemporalReferenceSwitchKeyRef.current = null;
      visualReadyTemporalReferenceSwitchKeysRef.current.clear();
      committedTemporalReferenceSwitchKeysRef.current.clear();
      loggedTemporalReferenceDuplicateSkipKeysRef.current.clear();
      pendingTemporalReferenceReadyCleanupRef.current?.();
      pendingTemporalReferenceReadyCleanupRef.current = null;
      setTemporalReferenceLoading(false);
    };

    if (activeTemporalProjectIdRef.current && activeTemporalProjectIdRef.current !== projectId) {
      clearTemporalReferenceLayers();
    }

    if (!map || !map.isStyleLoaded()) {
      activeTemporalProjectIdRef.current = projectId;
      return;
    }

    if (!projectId || !temporalPresentation) {
      clearTemporalReferenceLayers();
      activeTemporalProjectIdRef.current = null;
      return;
    }

    activeTemporalProjectIdRef.current = projectId;

    const timelineByRelease = new Map(
      temporalReferenceImageryTimeline.map((item) => [item.releaseIdentifier, item] as const),
    );
    const prevRelease =
      temporalSelectedMilestoneIndex > 0 ? temporalAvailableMilestoneIds[temporalSelectedMilestoneIndex - 1] ?? null : null;
    const nextRelease =
      temporalSelectedMilestoneIndex >= 0 && temporalSelectedMilestoneIndex + 1 < temporalAvailableMilestoneIds.length
        ? temporalAvailableMilestoneIds[temporalSelectedMilestoneIndex + 1] ?? null
        : null;
    const registrationCandidates =
      temporalReferenceImageryTimeline.length <= 5
        ? temporalReferenceImageryTimeline
        : [temporalReferenceImagery, timelineByRelease.get(prevRelease ?? "") ?? null, timelineByRelease.get(nextRelease ?? "") ?? null]
            .filter((value): value is TemporalReferenceImageryPresentation => value !== null);
    if (registrationCandidates.length) {
      const registerStartedAt = performance.now();
      let created = 0;
      let reused = 0;
      devLog("TEMPORAL_REFERENCE_LAYERS_REGISTER_START", {
        projectId,
        requested: registrationCandidates.length,
        milestoneCount: temporalReferenceImageryTimeline.length,
      });
      for (const imagery of registrationCandidates) {
        if (!imagery) {
          continue;
        }
        try {
          const lifecycle =
            imagery.storageStrategy === "image_overlay" && imagery.imageUrl && imagery.bounds
              ? ensureTemporalReferenceImageLayer(map, imagery, {
                  projectId,
                  sourceSignatures: temporalReferenceSourceSignaturesRef.current,
                })
              : ensureTemporalReferenceRasterLayer(map, imagery, {
                  projectId,
                  sourceSignatures: temporalReferenceSourceSignaturesRef.current,
                });
          temporalReferenceLayerIdsRef.current.add(lifecycle.layerId);
          temporalReferenceSourceIdsRef.current.add(lifecycle.sourceId);
          setTemporalReferenceLayerVisibility(
            map,
            lifecycle.layerId,
            layerState.temporalReferenceImagery && lifecycle.layerId === activeTemporalReferenceLayerIdRef.current,
          );
          if (lifecycle.mode === "create" || lifecycle.mode === "recreate") {
            created += 1;
          } else {
            reused += 1;
          }
        } catch (error) {
          devLog("TEMPORAL_REFERENCE_LAYER_REGISTER_FAILED", {
            projectId,
            releaseIdentifier: imagery.releaseIdentifier,
            reason: error instanceof Error ? error.message : String(error),
          });
        }
      }
      devLog("TEMPORAL_REFERENCE_LAYERS_REGISTER_DONE", {
        projectId,
        requested: registrationCandidates.length,
        created,
        reused,
        durationMs: Math.round(performance.now() - registerStartedAt),
      });
    }

    const hideTrackedLayers = (exceptLayerId: string | null = null) => {
      for (const layerId of temporalReferenceLayerIdsRef.current) {
        setTemporalReferenceLayerVisibility(map, layerId, layerId === exceptLayerId && layerState.temporalReferenceImagery);
      }
    };

    if (!layerState.temporalReferenceImagery || !temporalReferenceImageryAvailable || !temporalReferenceImagery) {
      hideTrackedLayers(null);
      activeTemporalReferenceLayerIdRef.current = null;
      activeTemporalReferenceReleaseIdentifierRef.current = null;
      setTemporalReferenceLoading(false);
      return;
    }

    const selectedLayerId = temporalReferenceLayerId(projectId, temporalReferenceImagery.releaseIdentifier);
    const selectedSourceId = temporalReferenceSourceId(projectId, temporalReferenceImagery.releaseIdentifier);
    const selectedSignature = temporalReferenceSourceSignature(projectId, temporalReferenceImagery);
    const selectedSwitchKey = `${projectId}:${temporalReferenceImagery.releaseIdentifier}:${selectedSignature}`;
    const selectedTileVersion =
      temporalReferenceImagery.tilesUrlTemplate ??
      temporalReferenceImagery.tilejsonUrl ??
      temporalReferenceImagery.cogUrl ??
      null;
    const selectedSignatureMatches =
      temporalReferenceSourceSignaturesRef.current[selectedSourceId] === selectedSignature;
    const alreadyCommittedSelectedSwitch =
      committedTemporalReferenceSwitchKeysRef.current.has(selectedSwitchKey) &&
      activeTemporalReferenceReleaseIdentifierRef.current === temporalReferenceImagery.releaseIdentifier &&
      activeTemporalReferenceLayerIdRef.current === selectedLayerId &&
      selectedSignatureMatches &&
      temporalReferenceLoadedLayerIdsRef.current.has(selectedLayerId) &&
      map.getLayer(selectedLayerId);
    if (alreadyCommittedSelectedSwitch) {
      hideTrackedLayers(selectedLayerId);
      moveReferenceOverlaysAboveTemporalImagery(map, selectedLayerId, referenceLayers);
      setTemporalReferenceLoading(false);
      const duplicateSkipKey = `${selectedSwitchKey}:committed`;
      if (!loggedTemporalReferenceDuplicateSkipKeysRef.current.has(duplicateSkipKey)) {
        loggedTemporalReferenceDuplicateSkipKeysRef.current.add(duplicateSkipKey);
        if (loggedTemporalReferenceDuplicateSkipKeysRef.current.size > 100) {
          const firstKey = Array.from(loggedTemporalReferenceDuplicateSkipKeysRef.current)[0];
          loggedTemporalReferenceDuplicateSkipKeysRef.current.delete(firstKey);
        }
        devLog("TEMPORAL_REFERENCE_SWITCH_DUPLICATE_SKIPPED", {
          projectId,
          releaseIdentifier: temporalReferenceImagery.releaseIdentifier,
          switchKey: selectedSwitchKey,
          reason: "committed",
          sourceId: selectedSourceId,
          layerId: selectedLayerId,
        });
      }
      return;
    }
    const alreadyActiveSelectedSwitch =
      activeTemporalReferenceSwitchKeyRef.current === selectedSwitchKey &&
      selectedSignatureMatches &&
      map.getLayer(selectedLayerId);
    if (alreadyActiveSelectedSwitch) {
      hideTrackedLayers(selectedLayerId);
      moveReferenceOverlaysAboveTemporalImagery(map, selectedLayerId, referenceLayers);
      setTemporalReferenceLoading(false);
      const duplicateSkipKey = `${selectedSwitchKey}:active`;
      if (!loggedTemporalReferenceDuplicateSkipKeysRef.current.has(duplicateSkipKey)) {
        loggedTemporalReferenceDuplicateSkipKeysRef.current.add(duplicateSkipKey);
        if (loggedTemporalReferenceDuplicateSkipKeysRef.current.size > 100) {
          const firstKey = Array.from(loggedTemporalReferenceDuplicateSkipKeysRef.current)[0];
          loggedTemporalReferenceDuplicateSkipKeysRef.current.delete(firstKey);
        }
        devLog("TEMPORAL_REFERENCE_SWITCH_DUPLICATE_SKIPPED", {
          projectId,
          releaseIdentifier: temporalReferenceImagery.releaseIdentifier,
          switchKey: selectedSwitchKey,
          reason: "active",
          sourceId: selectedSourceId,
          layerId: selectedLayerId,
        });
      }
      return;
    }

    activeTemporalReferenceSwitchKeyRef.current = selectedSwitchKey;
    pendingTemporalReferenceReadyCleanupRef.current?.();
    pendingTemporalReferenceReadyCleanupRef.current = null;
    const switchStartedAt = performance.now();
    devLog("TEMPORAL_REFERENCE_SWITCH_START", {
      projectId,
      releaseIdentifier: temporalReferenceImagery.releaseIdentifier,
      switchKey: selectedSwitchKey,
      sourceId: selectedSourceId,
      layerId: selectedLayerId,
      signatureHash: selectedSignature,
      tileVersion: selectedTileVersion,
      tilejsonUrl: temporalReferenceImagery.tilejsonUrl,
      tilesUrlTemplate: temporalReferenceImagery.tilesUrlTemplate,
      cogUrl: temporalReferenceImagery.cogUrl,
    });
    const tilejsonCacheKeyFor = (imagery: TemporalReferenceImageryPresentation): string =>
      `${projectId}:${imagery.releaseIdentifier}:${imagery.tilejsonUrl ?? ""}:${
        temporalReferenceSourceSignature(projectId, imagery) ?? ""
      }`;
    devLog("TEMPORAL_REFERENCE_SIGNATURE_CHECK", {
      releaseIdentifier: temporalReferenceImagery.releaseIdentifier,
      switchKey: selectedSwitchKey,
      previousSignatureHash: temporalReferenceSourceSignaturesRef.current[selectedSourceId] ?? null,
      nextSignatureHash: selectedSignature,
      sameSignature: selectedSignatureMatches,
      tileVersion: selectedTileVersion,
    });
    if (!selectedSignatureMatches) {
      temporalReferenceLoadedLayerIdsRef.current.delete(selectedLayerId);
    }
    if (temporalReferenceImagery.storageStrategy !== "image_overlay") {
      devLog("LEGACY_IMAGE_OVERLAY_BLOCKED", {
        projectId,
        releaseIdentifier: temporalReferenceImagery.releaseIdentifier,
        reason: "tile_or_cog_strategy",
      });
    }

    const waitForVisualReady = async (
      imagery: TemporalReferenceImageryPresentation,
      releaseIdentifier: string,
      mode: TemporalReferenceSwitchMode,
      firstTileMs: number,
      switchKey: string,
    ): Promise<TemporalReferenceVisualReadyResult | null> => {
      const visualStartedAt = performance.now();
      const sourceId = temporalReferenceSourceId(projectId, releaseIdentifier);
      const layerId = temporalReferenceLayerId(projectId, releaseIdentifier);

      const emitVisualReady = (readinessSource: TemporalReferenceReadinessSource): TemporalReferenceVisualReadyResult | null => {
        if (activeTemporalReferenceSwitchKeyRef.current !== switchKey) {
          devLog("TEMPORAL_REFERENCE_COMMIT_SKIPPED_STALE", {
            projectId,
            releaseIdentifier,
            switchKey,
            sourceId,
            layerId,
            mode,
            readinessSource,
            totalSwitchMs: Math.round(performance.now() - switchStartedAt),
          });
          return null;
        }
        pendingTemporalReferenceReadyCleanupRef.current = null;
        if (visualReadyTemporalReferenceSwitchKeysRef.current.has(switchKey)) {
          return null;
        }
        visualReadyTemporalReferenceSwitchKeysRef.current.add(switchKey);
        const duration = Math.round(performance.now() - visualStartedAt);
        const zoom = Math.max(
          imagery.minzoom ?? 0,
          Math.min(imagery.maxzoom ?? 22, Math.floor(map.getZoom())),
        );
        const visibleTileCount = visibleTileCoordinates(map, zoom).length;
        devLog("TEMPORAL_REFERENCE_VISUAL_READY", {
          projectId,
          releaseIdentifier,
          switchKey,
          sourceId,
          layerId,
          mode,
          readinessSource,
          tilejsonUrl: imagery.tilejsonUrl,
          tilesUrlTemplate: imagery.tilesUrlTemplate,
          tileVersion:
            imagery.tilesUrlTemplate ??
            imagery.tilejsonUrl ??
            imagery.cogUrl ??
            null,
          totalSwitchMs: Math.round(performance.now() - switchStartedAt),
          visualReadyMs: duration,
          firstTileMs,
          visibleTileCount,
        });
        return { visualReadyMs: duration, readinessSource };
      };

      // Check current readiness state
      const checkReadyState = () => {
        const source = map.getSource(sourceId);
        const layer = map.getLayer(layerId);
        const layerVisibility = layer ? (map.getLayoutProperty(layerId, "visibility") as string | null) : null;
        const isSourceLoaded =
          typeof map.isSourceLoaded === "function" && source ? map.isSourceLoaded(sourceId) : false;

        return {
          sourceExists: !!source,
          layerExists: !!layer,
          layerVisibility,
          isSourceLoaded,
        };
      };

      const initialState = checkReadyState();

      // Log initial state check
      devLog("TEMPORAL_REFERENCE_READY_STATE_CHECK", {
        projectId,
        releaseIdentifier,
        switchKey,
        sourceId,
        layerId,
        mode,
        sourceExists: initialState.sourceExists,
        layerExists: initialState.layerExists,
        layerVisibility: initialState.layerVisibility,
        isSourceLoaded: initialState.isSourceLoaded,
        elapsedMs: Math.round(performance.now() - visualStartedAt),
      });

      // Early exit if already visible and loaded
      if (initialState.layerVisibility === "visible" && initialState.isSourceLoaded) {
        return emitVisualReady("already_loaded");
      }

      // Timeouts: shorter for reuse, longer for create
      const timeoutMs = mode === "reuse" ? 500 : mode === "create" ? 1000 : 800;

      devLog("TEMPORAL_REFERENCE_READY_WAIT_STARTED", {
        projectId,
        releaseIdentifier,
        switchKey,
        sourceId,
        layerId,
        mode,
        timeoutMs,
        sourceExists: initialState.sourceExists,
        layerExists: initialState.layerExists,
        layerVisibility: initialState.layerVisibility,
        isSourceLoaded: initialState.isSourceLoaded,
        totalSwitchMs: Math.round(performance.now() - switchStartedAt),
      });

      const readinessSource = await new Promise<TemporalReferenceReadinessSource>((resolve) => {
        let resolved = false;
        let timeoutId: number | null = null;
        let renderFrameCallbackId: number | null = null;

        const finish = (source: TemporalReferenceReadinessSource) => {
          if (resolved) {
            return;
          }
          resolved = true;

          // Clean up all listeners and timers
          if (timeoutId !== null) {
            window.clearTimeout(timeoutId);
          }
          if (renderFrameCallbackId !== null) {
            cancelAnimationFrame(renderFrameCallbackId);
          }
          map.off("idle", handleIdle);
          map.off("sourcedata", handleSourceData);

          resolve(source);
        };

        const handleIdle = () => {
          devLog("TEMPORAL_REFERENCE_READY_EVENT", {
            projectId,
            releaseIdentifier,
            switchKey,
            eventType: "idle",
          });
          finish("idle");
        };

        const handleSourceData = (e: { sourceId?: string; isSourceLoaded?: boolean }) => {
          if (e.sourceId === sourceId && e.isSourceLoaded) {
            devLog("TEMPORAL_REFERENCE_READY_EVENT", {
              projectId,
              releaseIdentifier,
              switchKey,
              eventType: "sourcedata",
              isSourceLoaded: true,
            });
            finish("sourcedata");
          }
        };

        // Schedule 2 render frame checks as fallback before timeout
        let frameCount = 0;
        const scheduleRenderFrameCheck = () => {
          renderFrameCallbackId = requestAnimationFrame(() => {
            frameCount++;
            if (frameCount >= 2 && !resolved) {
              const state = checkReadyState();
              if (state.layerVisibility === "visible" && state.sourceExists && state.layerExists) {
                devLog("TEMPORAL_REFERENCE_RENDER_FRAME_READY", {
                  projectId,
                  releaseIdentifier,
                  switchKey,
                  frameCount,
                  sourceExists: state.sourceExists,
                  layerExists: state.layerExists,
                  layerVisibility: state.layerVisibility,
                  elapsedMs: Math.round(performance.now() - visualStartedAt),
                });
                finish("render_frame");
              }
            }
          });
        };

        // Schedule first render frame check immediately
        scheduleRenderFrameCheck();

        // Fallback timeout if events don't arrive
        timeoutId = window.setTimeout(() => {
          if (!resolved) {
            const finalState = checkReadyState();
            devLog("TEMPORAL_REFERENCE_READY_WAIT_TIMEOUT", {
              projectId,
              releaseIdentifier,
              switchKey,
              sourceId,
              layerId,
              mode,
              readinessSource: "timeout_fallback",
              sourceExists: finalState.sourceExists,
              layerExists: finalState.layerExists,
              layerVisibility: finalState.layerVisibility,
              isSourceLoaded: finalState.isSourceLoaded,
              elapsedMs: Math.round(performance.now() - visualStartedAt),
              totalSwitchMs: Math.round(performance.now() - switchStartedAt),
            });
            finish("timeout_fallback");
          }
        }, timeoutMs);

        // Store cleanup function
        pendingTemporalReferenceReadyCleanupRef.current = () => {
          finish("timeout_fallback");
        };

        // Listen for readiness events
        map.once("idle", handleIdle);
        map.on("sourcedata", handleSourceData);
      });

      return emitVisualReady(readinessSource);
    };

    const commitTemporalReferenceSwitch = (
      releaseIdentifier: string,
      sourceId: string,
      layerId: string,
      signature: string | null,
      mode: TemporalReferenceSwitchMode,
      previousReleaseIdentifier: string | null,
      result: TemporalReferenceVisualReadyResult | null,
    ): boolean => {
      if (!result) {
        return false;
      }
      if (activeTemporalReferenceSwitchKeyRef.current !== selectedSwitchKey) {
        devLog("TEMPORAL_REFERENCE_COMMIT_SKIPPED_STALE", {
          projectId,
          releaseIdentifier,
          switchKey: selectedSwitchKey,
          sourceId,
          layerId,
          mode,
          readinessSource: result.readinessSource,
          totalSwitchMs: Math.round(performance.now() - switchStartedAt),
        });
        return false;
      }
      if (committedTemporalReferenceSwitchKeysRef.current.has(selectedSwitchKey)) {
        return false;
      }
      committedTemporalReferenceSwitchKeysRef.current.add(selectedSwitchKey);
      committedTemporalReferenceSwitchKeyRef.current = selectedSwitchKey;
      activeTemporalReferenceSwitchKeyRef.current = null;
      pendingTemporalReferenceReadyCleanupRef.current = null;
      devLog("TEMPORAL_REFERENCE_SWITCH_COMMITTED", {
        projectId,
        previousReleaseIdentifier,
        nextReleaseIdentifier: releaseIdentifier,
        releaseIdentifier,
        switchKey: selectedSwitchKey,
        sourceId,
        layerId,
        signatureHash: signature,
        mode,
        tileVersion: selectedTileVersion,
        visibility: "visible",
        readinessSource: result.readinessSource,
        sourceExists: Boolean(map.getSource(sourceId)),
        layerExists: Boolean(map.getLayer(layerId)),
        totalSwitchMs: Math.round(performance.now() - switchStartedAt),
        visualReadyMs: result.visualReadyMs,
      });
      devLog("TEMPORAL_OUTPUT_SYNC_TRIGGER_AFTER_REFERENCE_COMMIT", {
        projectId,
        releaseIdentifier,
        hasSyncFunction: true,
        layerStateKeys: Object.keys(layerState),
        temporalLayerCount: temporalAddedLayerIdsRef.current.size,
      });
      syncTemporalOutputLayers({
        map,
        projectId,
        activeReleaseIdentifier: releaseIdentifier,
        availableReleaseIdentifiers: temporalAvailableMilestoneIds,
        includedAdditionReleaseIdentifiers: includedTemporalAdditionReleaseIdentifiers,
        colorByReleaseIdentifier: temporalMilestoneColorByReleaseIdentifier,
        layerState: getTemporalOutputSyncLayerState(projectId),
        registeredLayerIds: temporalAddedLayerIdsRef.current,
        expectedReleaseIdentifiersByKind: expectedTemporalOutputReleaseIdentifiersByKind,
        expectedActiveLayerIds: [],
      });
      return true;
    };

    const ensureImageryLayerReady = async (
      imagery: TemporalReferenceImageryPresentation,
    ): Promise<TemporalReferenceLayerLifecycle | null> => {
      const startedAt = performance.now();
      let tilejsonMs = 0;
      let firstTileMs = 0;
      devLog("MAP_SELECTED_REFERENCE_START", {
        projectId,
        releaseIdentifier: imagery.releaseIdentifier,
        storageStrategy: imagery.storageStrategy,
      });
      const fetchTilejsonIfNeeded = async (): Promise<TemporalReferenceTilejsonPayload | null> => {
        const cacheKey = tilejsonCacheKeyFor(imagery);
        if (temporalReferenceTilejsonCacheRef.current[cacheKey]) {
          return temporalReferenceTilejsonCacheRef.current[cacheKey];
        }
        const pendingRequest = temporalReferenceTilejsonPendingRef.current[cacheKey];
        if (pendingRequest) {
          return pendingRequest;
        }
        const tilejsonUrl = imagery.tilejsonUrl;
        if (!tilejsonUrl) {
          return null;
        }
        const pending = (async () => {
          const tilejsonStartedAt = performance.now();
          devLog("TEMPORAL_REFERENCE_TILEJSON_START", {
            projectId,
            releaseIdentifier: imagery.releaseIdentifier,
            tilejsonUrl,
          });
          const response = await fetch(tilejsonUrl);
          if (!response.ok) {
            throw new Error(`TileJSON request failed: ${response.status}`);
          }
          const payload = (await response.json()) as TemporalReferenceTilejsonPayload;
          temporalReferenceTilejsonCacheRef.current[cacheKey] = payload;
          tilejsonMs = Math.round(performance.now() - tilejsonStartedAt);
          devLog("TEMPORAL_REFERENCE_TILEJSON_DONE", {
            projectId,
            releaseIdentifier: imagery.releaseIdentifier,
            tilejsonUrl,
            tilejsonMs,
            tiles: Array.isArray(payload.tiles) ? payload.tiles.length : 0,
          });
          return payload;
        })();
        temporalReferenceTilejsonPendingRef.current[cacheKey] = pending;
        try {
          return await pending;
        } finally {
          delete temporalReferenceTilejsonPendingRef.current[cacheKey];
        }
      };

      const waitForFirstTile = async (sourceId: string): Promise<number> => {
        const firstTileStartedAt = performance.now();
        if (typeof map.areTilesLoaded === "function" && map.areTilesLoaded()) {
          return 0;
        }
        await new Promise<void>((resolve) => {
          let timeout = 0;
          const handleSourceData = (event: maplibregl.MapSourceDataEvent) => {
            if (activeTemporalReferenceSwitchKeyRef.current !== selectedSwitchKey) {
              window.clearTimeout(timeout);
              map.off("sourcedata", handleSourceData);
              resolve();
              return;
            }
            if (event.sourceId !== sourceId) {
              return;
            }
            window.clearTimeout(timeout);
            map.off("sourcedata", handleSourceData);
            resolve();
          };
          timeout = window.setTimeout(() => {
            map.off("sourcedata", handleSourceData);
            resolve();
          }, 1500);
          map.on("sourcedata", handleSourceData);
        });
        if (activeTemporalReferenceSwitchKeyRef.current !== selectedSwitchKey) {
          return -1;
        }
        const duration = Math.round(performance.now() - firstTileStartedAt);
        devLog("TEMPORAL_REFERENCE_FIRST_TILE_LOADED", {
          projectId,
          releaseIdentifier: imagery.releaseIdentifier,
          sourceId,
          firstTileMs: duration,
        });
        return duration;
      };

      try {
        if (
          (imagery.storageStrategy === "raster_tiles" || imagery.storageStrategy === "cog") &&
          (imagery.tilejsonUrl || imagery.tilesUrlTemplate)
        ) {
          const resolvedTilejson = await fetchTilejsonIfNeeded().catch(() => null);
          const lifecycle = ensureTemporalReferenceRasterLayer(map, imagery, {
            projectId,
            sourceSignatures: temporalReferenceSourceSignaturesRef.current,
            resolvedTilejson,
          });
          temporalReferenceLayerIdsRef.current.add(lifecycle.layerId);
          temporalReferenceSourceIdsRef.current.add(temporalReferenceSourceId(projectId, imagery.releaseIdentifier));
          if (!temporalReferenceLoadedLayerIdsRef.current.has(lifecycle.layerId) || !map.getLayer(lifecycle.layerId)) {
            firstTileMs = await waitForFirstTile(temporalReferenceSourceId(projectId, imagery.releaseIdentifier));
            await waitForTemporalRasterSource(map, temporalReferenceSourceId(projectId, imagery.releaseIdentifier));
            if (activeTemporalReferenceSwitchKeyRef.current !== selectedSwitchKey) {
              return null;
            }
            temporalReferenceLoadedLayerIdsRef.current.add(lifecycle.layerId);
          }
          lifecycle.firstTileMs = firstTileMs;
          moveReferenceOverlaysAboveTemporalImagery(map, lifecycle.layerId, referenceLayers);
          devLog("TEMPORAL_REFERENCE_READY", {
            projectId,
            releaseIdentifier: imagery.releaseIdentifier,
            sourceId: temporalReferenceSourceId(projectId, imagery.releaseIdentifier),
            layerId: lifecycle.layerId,
          });
          devLog("MAP_SELECTED_REFERENCE_VISIBLE", {
            projectId,
            releaseIdentifier: imagery.releaseIdentifier,
            storageStrategy: imagery.storageStrategy,
            sourceSetupMs: Math.round(performance.now() - startedAt),
            tilejsonMs,
            firstTileMs,
            durationMs: Math.round(performance.now() - startedAt),
          });
          return lifecycle;
        }

        if (imagery.storageStrategy === "image_overlay" && imagery.imageUrl && imagery.bounds) {
          devLog("LEGACY_IMAGE_OVERLAY_USED", {
            projectId,
            releaseIdentifier: imagery.releaseIdentifier,
            reason: "tile_metadata_missing",
          });
          if (!preloadedImageUrlsRef.current.has(imagery.imageUrl)) {
            await preloadImage(imagery.imageUrl);
            if (activeTemporalReferenceSwitchKeyRef.current !== selectedSwitchKey) {
              return null;
            }
            preloadedImageUrlsRef.current.add(imagery.imageUrl);
          }
          const lifecycle = ensureTemporalReferenceImageLayer(map, imagery, {
            projectId,
            sourceSignatures: temporalReferenceSourceSignaturesRef.current,
          });
          temporalReferenceLayerIdsRef.current.add(lifecycle.layerId);
          temporalReferenceSourceIdsRef.current.add(temporalReferenceSourceId(projectId, imagery.releaseIdentifier));
          temporalReferenceLoadedLayerIdsRef.current.add(lifecycle.layerId);
          moveReferenceOverlaysAboveTemporalImagery(map, lifecycle.layerId, referenceLayers);
          devLog("TEMPORAL_REFERENCE_READY", {
            projectId,
            releaseIdentifier: imagery.releaseIdentifier,
            sourceId: temporalReferenceSourceId(projectId, imagery.releaseIdentifier),
            layerId: lifecycle.layerId,
          });
          devLog("MAP_SELECTED_REFERENCE_VISIBLE", {
            projectId,
            releaseIdentifier: imagery.releaseIdentifier,
            storageStrategy: imagery.storageStrategy,
            durationMs: Math.round(performance.now() - startedAt),
          });
          return lifecycle;
        }
        if (imagery.storageStrategy !== "image_overlay") {
          devLog("LEGACY_IMAGE_OVERLAY_BLOCKED", {
            projectId,
            releaseIdentifier: imagery.releaseIdentifier,
            reason: "tile_or_cog_strategy",
          });
        }
      } catch (error) {
        console.warn("Failed to prepare temporal reference imagery layer", imagery.releaseIdentifier, error);
      }
      return null;
    };
    const preloadNeighbors = (imageryItems: TemporalReferenceImageryPresentation[]) => {
      if (!imageryItems.length || temporalHydratingProject) {
        return;
      }
      const neighborIds = imageryItems.map((item) => item.releaseIdentifier);
      devLog("TEMPORAL_REFERENCE_PREWARM_START", {
        projectId,
        selectedReleaseIdentifier: temporalReferenceImagery.releaseIdentifier,
        neighbors: neighborIds,
        reason: "scheduled_after_switch",
      });
      const onIdle = () => {
        const cancelIdle = scheduleIdle(() => {
          temporalReferencePrewarmAbortRef.current?.abort();
          const controller = new AbortController();
          temporalReferencePrewarmAbortRef.current = controller;
          const baseZoom = Math.floor(map.getZoom());
          for (const imagery of imageryItems) {
            const minZoom = imagery.minzoom ?? 0;
            const maxZoom = imagery.maxzoom ?? 22;
            const zooms = Array.from(new Set([baseZoom, baseZoom - 1, baseZoom + 1]))
              .filter((zoom) => zoom >= minZoom && zoom <= maxZoom)
              .sort((a, b) => a - b);
            const tiles = zooms.flatMap((zoom) => visibleTileCoordinates(map, zoom, 64));
            const missingTiles = tiles.filter((tile) => {
              const key = `${projectId}:${imagery.releaseIdentifier}:${tile.z}:${tile.x}:${tile.y}`;
              if (temporalReferencePrefetchedViewportKeysRef.current.has(key)) {
                return false;
              }
              temporalReferencePrefetchedViewportKeysRef.current.add(key);
              return true;
            });
            if (!missingTiles.length) {
              devLog("TEMPORAL_REFERENCE_PREWARM_SKIPPED", {
                projectId,
                releaseIdentifier: imagery.releaseIdentifier,
                reason: "already_warmed",
                zValues: zooms,
                tileCount: 0,
              });
              continue;
            }
            const prewarmStartedAt = performance.now();
            const prewarmUrl = `${backendUrl.replace(/\/$/, "")}/api/temporal-projects/${encodeURIComponent(
              projectId,
            )}/milestones/${encodeURIComponent(imagery.releaseIdentifier)}/reference/prewarm`;
            void fetch(prewarmUrl, {
              method: "POST",
              signal: controller.signal,
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ tiles: missingTiles }),
            })
              .then(async (response) => {
                if (!response.ok) {
                  throw new Error(`Prewarm failed with HTTP ${response.status}`);
                }
                const payload = (await response.json()) as {
                  hits?: number;
                  misses?: number;
                  generated?: number;
                  failed?: number;
                };
                devLog("TEMPORAL_REFERENCE_PREWARM_DONE", {
                  projectId,
                  releaseIdentifier: imagery.releaseIdentifier,
                  zValues: zooms,
                  tileCount: missingTiles.length,
                  durationMs: Math.round(performance.now() - prewarmStartedAt),
                  hits: payload.hits ?? 0,
                  misses: payload.misses ?? 0,
                  generated: payload.generated ?? 0,
                  failed: payload.failed ?? 0,
                });
              })
              .catch((error) => {
                devLog("TEMPORAL_REFERENCE_PREWARM_SKIPPED", {
                  projectId,
                  releaseIdentifier: imagery.releaseIdentifier,
                  reason: error instanceof Error ? error.message : String(error),
                  zValues: zooms,
                  tileCount: missingTiles.length,
                });
              });
          }
        }, 2000);
        void cancelIdle;
      };
      map.once("idle", onIdle);
    };

    const neighborImagery =
      temporalReferenceImageryTimeline.length <= 5
        ? [
            temporalReferenceImagery,
            ...temporalReferenceImageryTimeline.filter(
              (imagery) => imagery.releaseIdentifier !== temporalReferenceImagery.releaseIdentifier,
            ),
          ]
        : [
            temporalReferenceImagery,
            ...[prevRelease, nextRelease]
              .filter((releaseIdentifier): releaseIdentifier is string => Boolean(releaseIdentifier))
              .map((releaseIdentifier) => timelineByRelease.get(releaseIdentifier) ?? null)
              .filter((value): value is TemporalReferenceImageryPresentation => value !== null),
          ];

    if (
      selectedSignatureMatches &&
      temporalReferenceLoadedLayerIdsRef.current.has(selectedLayerId) &&
      map.getLayer(selectedLayerId)
    ) {
      const previousReleaseIdentifier = activeTemporalReferenceReleaseIdentifierRef.current;
      devLog("TEMPORAL_REFERENCE_SOURCE_REUSE", {
        projectId,
        releaseIdentifier: temporalReferenceImagery.releaseIdentifier,
        switchKey: selectedSwitchKey,
        sourceId: selectedSourceId,
        layerId: selectedLayerId,
        signatureHash: selectedSignature,
        changed: false,
        mode: "reuse",
        tileVersion: selectedTileVersion,
        tilejsonUrl: temporalReferenceImagery.tilejsonUrl,
        tilesUrlTemplate: temporalReferenceImagery.tilesUrlTemplate,
        cogUrl: temporalReferenceImagery.cogUrl,
      });
      hideTrackedLayers(selectedLayerId);
      activeTemporalReferenceLayerIdRef.current = selectedLayerId;
      activeTemporalReferenceReleaseIdentifierRef.current = temporalReferenceImagery.releaseIdentifier;
      moveReferenceOverlaysAboveTemporalImagery(map, selectedLayerId, referenceLayers);
      setTemporalReferenceLoading(false);
      committedTemporalReferenceSwitchKeysRef.current.add(selectedSwitchKey);
      committedTemporalReferenceSwitchKeyRef.current = selectedSwitchKey;
      activeTemporalReferenceSwitchKeyRef.current = null;
      pendingTemporalReferenceReadyCleanupRef.current = null;
      devLog("TEMPORAL_REFERENCE_SWITCH_COMMITTED", {
        projectId,
        previousReleaseIdentifier,
        nextReleaseIdentifier: temporalReferenceImagery.releaseIdentifier,
        releaseIdentifier: temporalReferenceImagery.releaseIdentifier,
        switchKey: selectedSwitchKey,
        sourceId: selectedSourceId,
        layerId: selectedLayerId,
        signatureHash: selectedSignature,
        mode: "reuse",
        tileVersion: selectedTileVersion,
        visibility: "visible",
        readinessSource: "already_registered",
        sourceExists: Boolean(map.getSource(selectedSourceId)),
        layerExists: Boolean(map.getLayer(selectedLayerId)),
        totalSwitchMs: Math.round(performance.now() - switchStartedAt),
      });
      devLog("TEMPORAL_OUTPUT_SYNC_TRIGGER_AFTER_REFERENCE_COMMIT", {
        projectId,
        releaseIdentifier: temporalReferenceImagery.releaseIdentifier,
        hasSyncFunction: true,
        layerStateKeys: Object.keys(layerState),
        temporalLayerCount: temporalAddedLayerIdsRef.current.size,
      });
      syncTemporalOutputLayers({
        map,
        projectId,
        activeReleaseIdentifier: temporalReferenceImagery.releaseIdentifier,
        availableReleaseIdentifiers: temporalAvailableMilestoneIds,
        includedAdditionReleaseIdentifiers: includedTemporalAdditionReleaseIdentifiers,
        colorByReleaseIdentifier: temporalMilestoneColorByReleaseIdentifier,
        layerState: getTemporalOutputSyncLayerState(projectId),
        registeredLayerIds: temporalAddedLayerIdsRef.current,
        expectedReleaseIdentifiersByKind: expectedTemporalOutputReleaseIdentifiersByKind,
        expectedActiveLayerIds: [],
      });
      preloadNeighbors(neighborImagery);
      return;
    }

    const previousLayerId = activeTemporalReferenceLayerIdRef.current;
    hideTrackedLayers(previousLayerId);
    setTemporalReferenceLoading(true);

    const previousReleaseIdentifier = activeTemporalReferenceReleaseIdentifierRef.current;
    void ensureImageryLayerReady(temporalReferenceImagery).then(async (lifecycle) => {
      if (!lifecycle) {
        if (activeTemporalReferenceSwitchKeyRef.current === selectedSwitchKey) {
          activeTemporalReferenceSwitchKeyRef.current = null;
          pendingTemporalReferenceReadyCleanupRef.current = null;
        }
        setTemporalReferenceLoading(false);
        return;
      }
      if (activeTemporalReferenceSwitchKeyRef.current !== selectedSwitchKey) {
        setTemporalReferenceLoading(false);
        return;
      }
      hideTrackedLayers(lifecycle.layerId);
      activeTemporalReferenceLayerIdRef.current = lifecycle.layerId;
      activeTemporalReferenceReleaseIdentifierRef.current = temporalReferenceImagery.releaseIdentifier;
      moveReferenceOverlaysAboveTemporalImagery(map, lifecycle.layerId, referenceLayers);
      setTemporalReferenceLoading(false);
      const committedMode = lifecycle.mode === "create" ? "create" : lifecycle.mode === "recreate" ? "recreate" : "ready_wait";
      const visualReadyResult = await waitForVisualReady(
        temporalReferenceImagery,
        temporalReferenceImagery.releaseIdentifier,
        committedMode,
        lifecycle.firstTileMs ?? 0,
        selectedSwitchKey,
      );
      commitTemporalReferenceSwitch(
        temporalReferenceImagery.releaseIdentifier,
        selectedSourceId,
        lifecycle.layerId,
        lifecycle.signature,
        committedMode,
        previousReleaseIdentifier,
        visualReadyResult,
      );
      preloadNeighbors(neighborImagery);
    });

    return;
  }, [
    layerState,
    layerState.temporalReferenceImagery,
    mapStyleRevision,
    getTemporalOutputSyncLayerState,
    referenceLayers,
    temporalPresentation,
    temporalAvailableMilestoneIds,
    includedTemporalAdditionReleaseIdentifiers,
    expectedTemporalOutputReleaseIdentifiersByKind,
    temporalMilestoneColorByReleaseIdentifier,
    temporalReferenceImagery,
    temporalReferenceImageryAvailable,
    temporalSelectedMilestoneIndex,
    temporalReferenceImageryTimeline,
    temporalHydratingProject,
  ]);

  temporalReferenceDebugContextRef.current = {
    workflowMode,
    projectId: temporalProjectId,
    selectedReleaseIdentifier: temporalPresentation?.selectedReleaseIdentifier ?? null,
    referenceImagery: temporalReferenceImagery,
    referenceImageryAvailable: temporalReferenceImageryAvailable,
    referenceLayerEnabled: layerState.temporalReferenceImagery,
    mapStyleRevision,
  };

  useEffect(() => {
    const map = mapRef.current;
    const projectId = temporalProjectId;
    const outputSyncLayerState = getTemporalOutputSyncLayerState(projectId);

    const clearTemporalAddedLayers = () => {
      if (map) {
        for (const layerId of temporalAddedLayerIdsRef.current) {
          if (map.getLayer(layerId)) {
            map.removeLayer(layerId);
          }
        }
        for (const sourceId of temporalAddedSourceIdsRef.current) {
          if (map.getSource(sourceId)) {
            map.removeSource(sourceId);
          }
        }
      }
      temporalAddedLayerIdsRef.current.clear();
      temporalAddedSourceIdsRef.current.clear();
      temporalAddedSourceSignaturesRef.current = {};
      temporalAddedRegistrationKeyRef.current = null;
      temporalAddedRegistrationStatsRef.current = {};
      temporalAddedSyncRetryKeyRef.current = null;
      activeTemporalAddedProjectIdRef.current = null;
      activeTemporalAddedReleaseIdentifierRef.current = null;
      activeTemporalAddedSwitchKeyRef.current = null;
    };

    if (!map) {
      syncTemporalOutputLayers({
        map,
        projectId,
        activeReleaseIdentifier: temporalPresentation?.selectedReleaseIdentifier ?? null,
        availableReleaseIdentifiers: temporalAvailableMilestoneIds,
        includedAdditionReleaseIdentifiers: includedTemporalAdditionReleaseIdentifiers,
        colorByReleaseIdentifier: temporalMilestoneColorByReleaseIdentifier,
        layerState: outputSyncLayerState,
        registeredLayerIds: temporalAddedLayerIdsRef.current,
        expectedReleaseIdentifiersByKind: expectedTemporalOutputReleaseIdentifiersByKind,
      });
      return;
    }
    if (!isMapStyleReadyForLayerMutation(map)) {
      syncTemporalOutputLayers({
        map,
        projectId,
        activeReleaseIdentifier: temporalPresentation?.selectedReleaseIdentifier ?? null,
        availableReleaseIdentifiers: temporalAvailableMilestoneIds,
        includedAdditionReleaseIdentifiers: includedTemporalAdditionReleaseIdentifiers,
        colorByReleaseIdentifier: temporalMilestoneColorByReleaseIdentifier,
        layerState: outputSyncLayerState,
        registeredLayerIds: temporalAddedLayerIdsRef.current,
        expectedReleaseIdentifiersByKind: expectedTemporalOutputReleaseIdentifiersByKind,
      });
      return;
    }
    if (workflowMode !== "temporal" || !projectId || !temporalPresentation) {
      clearTemporalAddedLayers();
      return;
    }
    if (activeTemporalAddedProjectIdRef.current && activeTemporalAddedProjectIdRef.current !== projectId) {
      clearTemporalAddedLayers();
    }
    activeTemporalAddedProjectIdRef.current = projectId;

    const selectedReleaseIdentifier = temporalPresentation.selectedReleaseIdentifier;
    if (!selectedReleaseIdentifier) {
      syncTemporalOutputLayers({
        map,
        projectId,
        activeReleaseIdentifier: null,
        availableReleaseIdentifiers: temporalAvailableMilestoneIds,
        includedAdditionReleaseIdentifiers: includedTemporalAdditionReleaseIdentifiers,
        colorByReleaseIdentifier: temporalMilestoneColorByReleaseIdentifier,
        layerState: outputSyncLayerState,
        registeredLayerIds: temporalAddedLayerIdsRef.current,
        expectedReleaseIdentifiersByKind: expectedTemporalOutputReleaseIdentifiersByKind,
      });
      activeTemporalAddedReleaseIdentifierRef.current = null;
      return;
    }

    const timelineByRelease = new Map(
      temporalAddedOverlayTimeline.map((overlay) => [overlay.releaseIdentifier, overlay] as const),
    );
    const baselineReleaseIdentifier = temporalAvailableMilestoneIds[0] ?? null;
    const registrationPlans: TemporalOutputLayerPlan[] = TEMPORAL_ADDED_LAYER_DEFINITIONS.flatMap((definition) => {
      const expectedReleases = expectedTemporalOutputReleaseIdentifiersByKind[definition.kind] ?? [];
      return expectedReleases.flatMap((releaseIdentifier) => {
        const overlay = timelineByRelease.get(releaseIdentifier) ?? null;
        if (!overlay) {
          devLog("TEMPORAL_OUTPUT_LAYER_PLAN_SKIPPED", {
            projectId,
            releaseIdentifier,
            layerKey: definition.toggleKey,
            kind: definition.kind,
            artifactKey: temporalAddedArtifactKey(definition.kind),
            reason: "missing_overlay",
          });
          return [];
        }
        const { plan, availability } = buildTemporalOutputLayerPlan({
          projectId,
          overlay,
          definition,
          baselineReleaseIdentifier,
          layerState: outputSyncLayerState,
        });
        if (!plan) {
          const isBaseline = baselineReleaseIdentifier === overlay.releaseIdentifier;
          devLog(isBaseline ? "TEMPORAL_EMPTY_BASELINE_LAYER_SKIPPED" : "TEMPORAL_EMPTY_OUTPUT_LAYER_SKIPPED", {
            projectId,
            releaseIdentifier: overlay.releaseIdentifier,
            layerKey: definition.toggleKey,
            kind: definition.kind,
            artifactKey: temporalAddedArtifactKey(definition.kind),
            reason: availability.reason,
            featureCount: availability.featureCount,
            payloadBytes: availability.payloadBytes,
            baselineReferenceImageryPreserved: isBaseline,
          });
          return [];
        }
        return [plan];
      });
    });
    const registrationCandidates = Array.from(new Set(registrationPlans.map((plan) => plan.releaseIdentifier)))
      .map((releaseIdentifier) => timelineByRelease.get(releaseIdentifier) ?? null)
      .filter((overlay): overlay is TemporalAddedOverlayPresentation => Boolean(overlay));
    devLog("TEMPORAL_OUTPUT_LAYER_PLAN_BUILT", {
      projectId,
      releaseIdentifier: selectedReleaseIdentifier,
      activeReleaseIdentifier: selectedReleaseIdentifier,
      sourceOfTruth: "milestone_artifacts",
      baselineReleaseIdentifier,
      includedAdditionReleaseIdentifiers: includedTemporalAdditionReleaseIdentifiers,
      expectedReleaseIdentifiersByKind: expectedTemporalOutputReleaseIdentifiersByKind,
      plannedLayerCount: registrationPlans.length,
      plannedLayers: registrationPlans.map((plan) => ({
        layerKey: plan.layerKey,
        artifactKey: plan.artifactKey,
        layerIds: plan.layerIds,
        sourceId: plan.sourceId,
        sourceType: plan.sourceType,
        renderStrategy: plan.renderStrategy,
        enabled: plan.enabled,
        featureCount: plan.featureCount,
        sizeBytes: plan.sizeBytes,
        reason: plan.availabilityReason,
        tilejsonUrl: plan.tilejsonUrl ?? null,
        sourceLayer: plan.sourceLayer ?? null,
      })),
    });
    if (selectedReleaseIdentifier === baselineReleaseIdentifier && registrationPlans.length === 0) {
      devLog("TEMPORAL_BASELINE_OUTPUT_LAYERS_NOT_EXPECTED", {
        projectId,
        releaseIdentifier: selectedReleaseIdentifier,
        referenceImageryPreserved: Boolean(temporalReferenceImageryTimeline.some((imagery) => imagery.releaseIdentifier === selectedReleaseIdentifier)),
        missingLayerIds: [],
      });
    }

    let createdSources = 0;
    let reusedSources = 0;
    let updatedSources = 0;
    let createdLayers = 0;
    let reusedLayers = 0;
    let totalFeatureCount = 0;
    let totalPayloadBytes = 0;
    const enabledRegistrationPlans = registrationPlans.filter((plan) => plan.enabled);
    if (registrationPlans.length !== enabledRegistrationPlans.length) {
      devLog("TEMPORAL_OUTPUT_OPTIONAL_LAYER_REGISTRATION_DEFERRED", {
        projectId,
        releaseIdentifier: selectedReleaseIdentifier,
        plannedLayerCount: registrationPlans.length,
        enabledLayerCount: enabledRegistrationPlans.length,
        deferredLayerKeys: registrationPlans.filter((plan) => !plan.enabled).map((plan) => plan.layerKey),
      });
    }
    const registrationKey = JSON.stringify({
      projectId,
      projectUpdatedAt: temporalPresentation.projectUpdatedAt,
      releases: registrationCandidates.map((overlay) => overlay.releaseIdentifier),
      definitions: enabledRegistrationPlans.map((plan) => `${plan.releaseIdentifier}:${plan.definition.kind}`),
      includedAdditionReleaseIdentifiers: includedTemporalAdditionReleaseIdentifiers,
      colors: enabledRegistrationPlans.map((plan) => [
        plan.releaseIdentifier,
        plan.definition.kind,
        temporalMilestoneColorByReleaseIdentifier[plan.releaseIdentifier] ?? "#B91C1C",
      ]),
    });
    const expectedLayerIds = enabledRegistrationPlans.flatMap((plan) => plan.layerIds);
    const registrationAlreadyReady =
      temporalAddedRegistrationKeyRef.current === registrationKey &&
      expectedLayerIds.length > 0 &&
      expectedLayerIds.every((layerId) => Boolean(map.getLayer(layerId)));

    if (registrationAlreadyReady) {
      reusedLayers = expectedLayerIds.length;
      reusedSources = expectedLayerIds.length;
      const stats = temporalAddedRegistrationStatsRef.current[registrationKey];
      totalFeatureCount = stats?.featureCount ?? 0;
      totalPayloadBytes = stats?.payloadBytes ?? 0;
      for (const { overlay, definition } of enabledRegistrationPlans) {
          applyTemporalAddedLayerStyle(
            map,
            projectId,
            overlay.releaseIdentifier,
            definition.kind,
            temporalMilestoneColorByReleaseIdentifier[overlay.releaseIdentifier] ?? "#B91C1C",
          );
      }
    } else {
      for (const { overlay, definition } of enabledRegistrationPlans) {
          const beforeLayerCount = temporalAddedLayerIds(projectId, overlay.releaseIdentifier, definition.kind).filter((layerId) =>
            Boolean(map.getLayer(layerId)),
          ).length;
          const lifecycle = ensureTemporalAddedLayer(
            map,
            projectId,
            overlay,
            definition,
            temporalAddedSourceSignaturesRef.current,
            temporalMilestoneColorByReleaseIdentifier[overlay.releaseIdentifier] ?? "#B91C1C",
          );
          temporalAddedSourceIdsRef.current.add(lifecycle.sourceId);
          temporalAddedLayerIdsRef.current.add(lifecycle.layerId);
          if (lifecycle.lineLayerId) {
            temporalAddedLayerIdsRef.current.add(lifecycle.lineLayerId);
          }
          totalFeatureCount += lifecycle.featureCount;
          totalPayloadBytes += lifecycle.payloadBytes;
          if (lifecycle.mode === "create") {
            createdSources += 1;
          } else if (lifecycle.mode === "update") {
            updatedSources += 1;
          } else {
            reusedSources += 1;
          }
          const afterLayerCount = [lifecycle.layerId, lifecycle.lineLayerId].filter(
            (layerId): layerId is string => Boolean(layerId),
          ).length;
          reusedLayers += beforeLayerCount;
          createdLayers += Math.max(0, afterLayerCount - beforeLayerCount);
      }
      temporalAddedRegistrationKeyRef.current = registrationKey;
      temporalAddedRegistrationStatsRef.current[registrationKey] = {
        featureCount: totalFeatureCount,
        payloadBytes: totalPayloadBytes,
      };
    }

    const previousReleaseIdentifier = activeTemporalAddedReleaseIdentifierRef.current;
    const switchStartedAt = performance.now();
    const switchKey = `${projectId}:${selectedReleaseIdentifier}:${temporalPresentation.projectUpdatedAt ?? "unknown"}`;
    activeTemporalAddedSwitchKeyRef.current = switchKey;
    devLog("TEMPORAL_ADDED_LAYER_SWITCH_START", {
      projectId,
      previousReleaseIdentifier,
      releaseIdentifier: selectedReleaseIdentifier,
      switchKey,
      registeredLayerCount: temporalAddedLayerIdsRef.current.size,
      createdSources,
      reusedSources,
      updatedSources,
      createdLayers,
      reusedLayers,
      featureCount: totalFeatureCount,
      payloadBytes: totalPayloadBytes,
    });

    if (previousReleaseIdentifier !== selectedReleaseIdentifier) {
      devLog("TEMPORAL_ACTIVE_MILESTONE_CHANGED", {
        projectId,
        previousReleaseIdentifier,
        nextReleaseIdentifier: selectedReleaseIdentifier,
      });
    }

    devLog("TEMPORAL_OUTPUT_REGISTRATION_DONE", {
      projectId,
      releaseIdentifier: selectedReleaseIdentifier,
      registeredLayerIds: Array.from(temporalAddedLayerIdsRef.current),
      registeredLayerCount: temporalAddedLayerIdsRef.current.size,
      releases: registrationCandidates.map((overlay) => overlay.releaseIdentifier),
      includedAdditionReleaseIdentifiers: includedTemporalAdditionReleaseIdentifiers,
      plannedLayerCount: registrationPlans.length,
      enabledLayerCount: enabledRegistrationPlans.length,
      createdSources,
      reusedSources,
      updatedSources,
      createdLayers,
      reusedLayers,
      featureCount: totalFeatureCount,
      payloadBytes: totalPayloadBytes,
    });
    devLog("TEMPORAL_OUTPUT_LAYER_REGISTRY_SNAPSHOT", {
      projectId,
      activeReleaseIdentifier: selectedReleaseIdentifier,
      layerIds: Array.from(temporalAddedLayerIdsRef.current),
      sourceIds: Array.from(temporalAddedSourceIdsRef.current),
      layerStateKeys: Object.keys(outputSyncLayerState),
      enabledLayerKeys: dedupeStable(
        enabledRegistrationPlans
          .map((plan) => plan.definition)
          .filter((definition) => outputSyncLayerState[definition.toggleKey])
          .map((definition) => definition.toggleKey),
      ),
      registeredLayerIds: Array.from(temporalAddedLayerIdsRef.current),
      temporalLayerCount: temporalAddedLayerIdsRef.current.size,
      expectedOutputLayerCount: expectedLayerIds.length,
      skippedOutputLayerCount: Object.values(expectedTemporalOutputReleaseIdentifiersByKind).flat().length - registrationPlans.length,
    });

    const syncResult = syncTemporalOutputLayers({
      map,
      projectId,
      activeReleaseIdentifier: selectedReleaseIdentifier,
      availableReleaseIdentifiers: temporalAvailableMilestoneIds,
      includedAdditionReleaseIdentifiers: includedTemporalAdditionReleaseIdentifiers,
      colorByReleaseIdentifier: temporalMilestoneColorByReleaseIdentifier,
      layerState: outputSyncLayerState,
      registeredLayerIds: temporalAddedLayerIdsRef.current,
      expectedReleaseIdentifiersByKind: expectedTemporalOutputReleaseIdentifiersByKind,
      expectedActiveLayerIds: expectedLayerIds,
    });
    const temporalDebugSnapshot = publishTemporalRuntimeDebugSnapshot({
      map,
      projectId,
      selectedReleaseIdentifier,
      layerState: outputSyncLayerState,
      includedAdditionReleaseIdentifiers: includedTemporalAdditionReleaseIdentifiers,
      availableAdditionReleaseIdentifiers,
      colorByReleaseIdentifier: temporalMilestoneColorByReleaseIdentifier,
      additionRegistrationPlans: enabledRegistrationPlans
        .filter((plan) => plan.definition.kind === "additions")
        .map((plan) => plan.releaseIdentifier),
      referenceReleaseIdentifier: temporalReferenceImagery?.releaseIdentifier ?? selectedReleaseIdentifier,
      expectedReleaseIdentifiersByKind: expectedTemporalOutputReleaseIdentifiersByKind,
      availableReleaseIdentifiersByKind: availableTemporalOutputReleaseIdentifiersByKind,
      labelByLayerKey: {
        allNewBuildings: temporalLayerLabels.allPreviousAdditions,
        selectedAdditions: temporalLayerLabels.selectedAdditions,
        temporalAdditions: temporalLayerLabels.allPreviousAdditions,
        selectedMilestoneAdditions: temporalLayerLabels.selectedAdditions,
        buffer10m: temporalLayerLabels.buffer10m,
        buffer15m: temporalLayerLabels.buffer15m,
        buffer20m: temporalLayerLabels.buffer20m,
        temporalCumulativeBuffer10m: temporalLayerLabels.cumulativeBuffer10m,
        temporalCumulativeBuffer15m: temporalLayerLabels.cumulativeBuffer15m,
        temporalCumulativeBuffer20m: temporalLayerLabels.cumulativeBuffer20m,
      },
    });
    if (temporalDebugSnapshot) {
      devLog("TEMPORAL_RUNTIME_DEBUG_SNAPSHOT", {
        projectId,
        selectedReleaseIdentifier,
        includedAdditionReleases: temporalDebugSnapshot.includedAdditionReleases,
        additionRegistrationPlans: temporalDebugSnapshot.additionRegistrationPlans,
        registeredAdditionSources: temporalDebugSnapshot.registeredAdditionSources,
        registeredAdditionLayers: temporalDebugSnapshot.registeredAdditionLayers,
        visibleAdditionLayers: temporalDebugSnapshot.visibleAdditionLayers,
        hiddenFutureAdditionLayers: temporalDebugSnapshot.hiddenFutureAdditionLayers,
        enabledLayerKeys: temporalDebugSnapshot.enabledLayerKeys,
        layerContracts: temporalDebugSnapshot.layerContracts,
        bufferLayers: temporalDebugSnapshot.bufferLayers,
        layerIdCollisions: temporalDebugSnapshot.layerIdCollisions,
        sourceIdCollisions: temporalDebugSnapshot.sourceIdCollisions,
        missing: temporalDebugSnapshot.missing,
      });
    }
    const renderAuditEnabled = isTemporalRenderAuditEnabled();
    if (renderAuditEnabled) {
      scheduleTemporalOutputRenderConfirmation({
        map,
        projectId,
        releaseIdentifier: selectedReleaseIdentifier,
        plans: registrationPlans,
        visibleLayerIds: syncResult.visibleLayerIds,
      });
    } else if (!temporalRenderAuditSkippedSwitchKeysRef.current.has(switchKey)) {
      temporalRenderAuditSkippedSwitchKeysRef.current.add(switchKey);
      devLog("TEMPORAL_RENDER_CHECK_SKIPPED_NORMAL_MODE", {
        projectId,
        releaseIdentifier: selectedReleaseIdentifier,
        switchKey,
        reason: "render_audit_disabled",
        check: "temporal_output_post_sync",
        visibleLayerCount: syncResult.visibleLayerIds.length,
        plannedLayerCount: registrationPlans.length,
      });
    }
    const retryKey = `${projectId}:${selectedReleaseIdentifier}:${temporalPresentation.projectUpdatedAt ?? "unknown"}:${temporalAddedLayerIdsRef.current.size}`;
    if ((syncResult.missingLayerCount > 0 || temporalAddedLayerIdsRef.current.size === 0) && temporalAddedSyncRetryKeyRef.current !== retryKey) {
      temporalAddedSyncRetryKeyRef.current = retryKey;
      devLog("TEMPORAL_OUTPUT_LAYER_SYNC_RETRY_SCHEDULED", {
        projectId,
        releaseIdentifier: selectedReleaseIdentifier,
        activeReleaseIdentifier: selectedReleaseIdentifier,
        attempt: 1,
        reason: syncResult.missingLayerCount > 0 ? "missing_layer" : "empty_registry",
      });
      requestAnimationFrame(() => {
        const retryResult = syncTemporalOutputLayers({
          map,
          projectId,
          activeReleaseIdentifier: selectedReleaseIdentifier,
          availableReleaseIdentifiers: temporalAvailableMilestoneIds,
          includedAdditionReleaseIdentifiers: includedTemporalAdditionReleaseIdentifiers,
          colorByReleaseIdentifier: temporalMilestoneColorByReleaseIdentifier,
          layerState: outputSyncLayerState,
          registeredLayerIds: temporalAddedLayerIdsRef.current,
          expectedReleaseIdentifiersByKind: expectedTemporalOutputReleaseIdentifiersByKind,
          expectedActiveLayerIds: expectedLayerIds,
        });
        if (retryResult.missingLayerCount > 0 || temporalAddedLayerIdsRef.current.size === 0) {
          window.setTimeout(() => {
            devLog("TEMPORAL_OUTPUT_LAYER_SYNC_RETRY_SCHEDULED", {
              projectId,
              releaseIdentifier: selectedReleaseIdentifier,
              activeReleaseIdentifier: selectedReleaseIdentifier,
              attempt: 2,
              reason: retryResult.missingLayerCount > 0 ? "missing_layer" : "empty_registry",
            });
            syncTemporalOutputLayers({
              map,
              projectId,
              activeReleaseIdentifier: selectedReleaseIdentifier,
              availableReleaseIdentifiers: temporalAvailableMilestoneIds,
              includedAdditionReleaseIdentifiers: includedTemporalAdditionReleaseIdentifiers,
              colorByReleaseIdentifier: temporalMilestoneColorByReleaseIdentifier,
              layerState: outputSyncLayerState,
              registeredLayerIds: temporalAddedLayerIdsRef.current,
              expectedReleaseIdentifiersByKind: expectedTemporalOutputReleaseIdentifiersByKind,
              expectedActiveLayerIds: expectedLayerIds,
            });
          }, 150);
        }
      });
    }

    activeTemporalAddedReleaseIdentifierRef.current = selectedReleaseIdentifier;
    const totalSwitchMs = Math.round(performance.now() - switchStartedAt);
    devLog("TEMPORAL_ADDED_VISIBILITY_COMMITTED", {
      projectId,
      previousReleaseIdentifier,
      releaseIdentifier: selectedReleaseIdentifier,
      switchKey,
      totalSwitchMs,
      sourceCreationCountDuringSwitch: 0,
      layerCreationCountDuringSwitch: 0,
      setDataCountDuringSwitch: 0,
      dataFetchCountDuringSwitch: 0,
      readinessSource: registrationPlans.length === 0 ? "no_output_layers_expected" : "registered_active_release",
    });

    if (!renderAuditEnabled) {
      return;
    }
    if (visualReadyTemporalAddedSwitchKeysRef.current.has(switchKey)) {
      return;
    }
    const visualStartedAt = performance.now();
    requestAnimationFrame(() => {
      if (activeTemporalAddedSwitchKeyRef.current !== switchKey) {
        return;
      }
      requestAnimationFrame(() => {
        if (activeTemporalAddedSwitchKeyRef.current !== switchKey) {
          return;
        }
        if (visualReadyTemporalAddedSwitchKeysRef.current.has(switchKey)) {
          return;
        }
        visualReadyTemporalAddedSwitchKeysRef.current.add(switchKey);
        const renderedFeatureCount = queryTemporalRenderedFeatureCount(map, syncResult.visibleLayerIds);
        const visualPayload = {
          projectId,
          releaseIdentifier: selectedReleaseIdentifier,
          switchKey,
          renderReadyMs: Math.round(performance.now() - visualStartedAt),
          totalSwitchMs,
          featureCount: totalFeatureCount,
          renderedFeatureCount,
          visibleLayerIds: syncResult.visibleLayerIds,
          payloadBytes: totalPayloadBytes,
          readinessSource: "render_frame",
        };
        devLog(
          syncResult.visibleLayerIds.length > 0 && renderedFeatureCount === 0
            ? "TEMPORAL_ADDED_VISUAL_PENDING"
            : "TEMPORAL_ADDED_VISUAL_READY",
          visualPayload,
        );
      });
    });
  }, [
    layerState,
    mapStyleRevision,
    temporalAddedOverlayTimeline,
    availableAdditionReleaseIdentifiers,
    availableTemporalOutputReleaseIdentifiersByKind,
    temporalAvailableMilestoneIds,
    includedTemporalAdditionReleaseIdentifiers,
    expectedTemporalOutputReleaseIdentifiersByKind,
    temporalMilestoneColorByReleaseIdentifier,
    temporalLayerLabels,
    temporalPresentation,
    temporalProjectId,
    temporalSelectedMilestoneIndex,
    workflowMode,
  ]);

  useEffect(() => {
    if (workflowMode === "temporal") {
      if (!temporalProjectId) {
        activeLayerStateScopeRef.current = null;
        return;
      }
      const scopeKey = `temporal:${temporalProjectId}`;
      if (activeLayerStateScopeRef.current === scopeKey) {
        return;
      }
      const saved = temporalLayerStateByProjectRef.current[temporalProjectId];
      const next = saved ?? defaultLayerState("temporal", false);
      if (!saved) {
        temporalLayerStateByProjectRef.current[temporalProjectId] = next;
      }
      activeLayerStateScopeRef.current = scopeKey;
      setLayerState(next);
      return;
    }

    const scopeKey = `pairwise:${result?.summary?.request_hash ?? "none"}:${Boolean(result?.success)}`;
    if (activeLayerStateScopeRef.current === scopeKey) {
      return;
    }
    activeLayerStateScopeRef.current = scopeKey;
    setLayerState((current) => ({
      ...defaultLayerState("pairwise", Boolean(result?.success)),
      labels: current.labels,
    }));
  }, [result?.success, result?.summary?.request_hash, temporalProjectId, workflowMode]);

  useEffect(() => {
    if (!containerRef.current || mapRef.current) {
      return;
    }
    if (!browserSupportsWebGL()) {
      setMapError(t("error.webgl_unavailable"));
      return;
    }

    let map: maplibregl.Map;
    try {
      map = new maplibregl.Map({
        container: containerRef.current,
        style: apiKey ? mapboxProvider.createStyle(apiKey) : createOpenStreetMapStyle(),
        center: [-7.62, 33.58],
        zoom: 12,
      });
      setMapError(null);
    } catch (error) {
      setMapError(error instanceof Error ? error.message : t("error.map_init_failed"));
      return;
    }

    map.addControl(new maplibregl.AttributionControl({ compact: true }));
    map.doubleClickZoom.disable();
    map.on("error", (event) => {
      const message = event.error instanceof Error ? event.error.message : "";
      if (/webgl/i.test(message)) {
        setMapError(t("error.webgl_init_failed"));
      }
    });

    map.on("load", () => {
      setMapStyleRevision((revision) => revision + 1);
      ensureOperationalLayers(map);
      if (latestPresentationRef.current) {
        syncMapPresentation(map, latestPresentationRef.current);
      } else {
        applyLabelVisibility(map, layerState.labels);
      }
    });

    map.on("styledata", () => {
      if (map.isStyleLoaded()) {
        setMapStyleRevision((revision) => revision + 1);
      }
      if (latestPresentationRef.current) {
        syncMapPresentation(map, latestPresentationRef.current);
      } else {
        applyLabelVisibility(map, layerState.labels);
      }
    });

    (
      window as Window & {
        __buildingChangeMap?: MapLibreMap;
        __buildingChangeMapDebug?: {
          getLayerState: () => LayerToggleState;
          setLayerState: (updater: Partial<LayerToggleState>) => void;
        };
      }
    ).__buildingChangeMap = map;
    (
      window as Window & {
        __buildingChangeMap?: MapLibreMap;
        __buildingChangeMapDebug?: {
          getLayerState: () => LayerToggleState;
          setLayerState: (updater: Partial<LayerToggleState>) => void;
        };
      }
    ).__buildingChangeMapDebug = {
      getLayerState: () =>
        latestPresentationRef.current?.layerState ?? defaultLayerState(workflowMode, Boolean(result?.success)),
      setLayerState: (updater) => updateLayerState(updater),
    };
    window.__BUILDING_CHANGE_REFERENCE_DEBUG__ = {
      getState: () => {
        const layers = map.getStyle()?.layers ?? [];
        return {
          sources: Object.keys(map.getStyle()?.sources ?? {}).filter((sourceId) => sourceId.startsWith("temporal-reference-source-")),
          layers: layers
            .filter((layer) => layer.id.startsWith("temporal-reference-layer-"))
            .map((layer) => ({
              id: layer.id,
              visibility: (map.getLayoutProperty(layer.id, "visibility") as string | null) ?? null,
              opacity: map.getPaintProperty(layer.id, "raster-opacity"),
              orderIndex: layers.findIndex((candidate) => candidate.id === layer.id),
            })),
          context: {
            ...temporalReferenceDebugContextRef.current,
            mapStyleLoaded: Boolean(map.isStyleLoaded()),
          },
        };
      },
    };
    const onTemporalReferenceSelection = (
      event: Event,
    ) => {
      const detail = (
        event as CustomEvent<{
          projectId?: string | null;
          referenceImagery?: TemporalReferenceImageryPresentation | null;
        }>
      ).detail;
      const projectId = detail?.projectId ?? null;
      const imagery = detail?.referenceImagery ?? null;
      if (!projectId || !imagery) {
        return;
      }

      const registerSelectedReference = () => {
        if (!map.isStyleLoaded()) {
          return;
        }
        const latestReferenceContext = temporalReferenceDebugContextRef.current;
        if (
          latestReferenceContext.projectId !== projectId ||
          latestReferenceContext.selectedReleaseIdentifier !== imagery.releaseIdentifier
        ) {
          devLog("TEMPORAL_REFERENCE_EVENT_REGISTRATION_SKIPPED", {
            projectId,
            releaseIdentifier: imagery.releaseIdentifier,
            currentProjectId: latestReferenceContext.projectId,
            currentReleaseIdentifier: latestReferenceContext.selectedReleaseIdentifier,
            reason: "stale_selection",
          });
          return;
        }
        try {
          const lifecycle =
            imagery.storageStrategy === "image_overlay" && imagery.imageUrl && imagery.bounds
              ? ensureTemporalReferenceImageLayer(map, imagery, {
                  projectId,
                  sourceSignatures: temporalReferenceSourceSignaturesRef.current,
                })
              : ensureTemporalReferenceRasterLayer(map, imagery, {
                  projectId,
                  sourceSignatures: temporalReferenceSourceSignaturesRef.current,
                });
          temporalReferenceLayerIdsRef.current.add(lifecycle.layerId);
          temporalReferenceSourceIdsRef.current.add(lifecycle.sourceId);
          setTemporalReferenceLayerVisibility(
            map,
            lifecycle.layerId,
            lifecycle.layerId === activeTemporalReferenceLayerIdRef.current,
          );
          devLog("TEMPORAL_REFERENCE_EVENT_REGISTERED_DEFERRED", {
            projectId,
            releaseIdentifier: imagery.releaseIdentifier,
            sourceId: lifecycle.sourceId,
            layerId: lifecycle.layerId,
            mode: lifecycle.mode,
            visibilityCommitted: false,
          });
        } catch (error) {
          console.warn("Failed to register temporal reference selection event", imagery.releaseIdentifier, error);
        }
      };

      if (map.isStyleLoaded()) {
        registerSelectedReference();
      } else {
        map.once("load", registerSelectedReference);
      }
    };
    window.addEventListener("building-change-temporal-reference-selection", onTemporalReferenceSelection);
    const onValidationMapJump = (event: Event) => {
      if (!import.meta.env.DEV) {
        return;
      }
      const detail = (event as CustomEvent<{ center?: [number, number]; zoom?: number }>).detail ?? {};
      const center = detail.center;
      if (!Array.isArray(center) || center.length !== 2) {
        return;
      }
      map.jumpTo({
        center,
        zoom: typeof detail.zoom === "number" ? detail.zoom : map.getZoom(),
      });
      map.triggerRepaint();
    };
    window.addEventListener("building-change-validation-map-jump", onValidationMapJump);

    mapRef.current = map;
    setMapStyleRevision((revision) => revision + 1);
    return () => {
      markersRef.current.forEach((marker) => marker.remove());
      markersRef.current = [];
      window.removeEventListener("building-change-validation-map-jump", onValidationMapJump);
      window.removeEventListener("building-change-temporal-reference-selection", onTemporalReferenceSelection);
      map.remove();
      const debugWindow = window as Window & {
        __buildingChangeMap?: MapLibreMap;
        __buildingChangeMapDebug?: {
          getLayerState: () => LayerToggleState;
          setLayerState: (updater: Partial<LayerToggleState>) => void;
        };
      };
      if (debugWindow.__buildingChangeMap === map) {
        delete debugWindow.__buildingChangeMap;
        delete debugWindow.__buildingChangeMapDebug;
        delete window.__BUILDING_CHANGE_REFERENCE_DEBUG__;
      }
      mapRef.current = null;
    };
  }, [apiKey]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || mapError) {
      return;
    }

    applyLabelVisibility(map, layerState.labels);
  }, [layerState.labels, mapError]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || mapError) {
      return;
    }

    const onClick = (event: maplibregl.MapMouseEvent) => {
      if (drawingMode !== "drawing") {
        return;
      }

      const state = useAppStore.getState();
      const currentDraftVertices = state.draftVertices;
      const currentDrawingSubMode = state.drawingSubMode;
      const vertex: [number, number] = [event.lngLat.lng, event.lngLat.lat];
      const firstPoint = currentDraftVertices[0] ? map.project(currentDraftVertices[0]) : null;
      const nearFirst =
        currentDrawingSubMode === "polygon" &&
        currentDraftVertices.length >= 3 &&
        firstPoint !== null &&
        isNearFirstVertex([event.point.x, event.point.y], [firstPoint.x, firstPoint.y]);
      const result = resolveDrawingClick(currentDrawingSubMode, currentDraftVertices, vertex, nearFirst);
      if (result.complete) {
        setDrawingInstruction("Zone enregistrée");
        completeDrawing(result.vertices);
      } else {
        setDraftVertices(result.vertices);
      }
    };

    const onContextMenu = (event: maplibregl.MapMouseEvent) => {
      if (drawingMode !== "drawing" || useAppStore.getState().draftVertices.length < 3) {
        return;
      }
      event.preventDefault();
      finishDrawing();
    };

    // Update cursor and show live preview for rectangle drawing
    const onMouseMove = (event: maplibregl.MapMouseEvent) => {
      if (drawingMode !== "drawing") {
        map.getCanvas().style.cursor = "";
        setDrawingPointer(null);
        setDrawingCursorCoordinate(null);
        setFirstVertexCloseTarget(false);
        return;
      }

      const state = useAppStore.getState();
      const subMode = state.drawingSubMode;
      setDrawingPointer([event.point.x, event.point.y]);
      setDrawingCursorCoordinate([event.lngLat.lng, event.lngLat.lat]);
      let closeTarget = false;

      if (subMode === "rectangle") {
        map.getCanvas().style.cursor = "crosshair";
      } else {
        map.getCanvas().style.cursor = "crosshair";
        const firstPoint = state.draftVertices[0] ? map.project(state.draftVertices[0]) : null;
        closeTarget =
          state.draftVertices.length >= 3 &&
          firstPoint !== null &&
          isNearFirstVertex([event.point.x, event.point.y], [firstPoint.x, firstPoint.y]);
        setFirstVertexCloseTarget(closeTarget);
      }
      setDrawingInstruction(drawingHelperMessage(subMode, state.draftVertices.length, closeTarget));
    };

    const onMouseLeave = () => {
      setDrawingPointer(null);
      setDrawingCursorCoordinate(null);
      setFirstVertexCloseTarget(false);
      map.getCanvas().style.cursor = drawingMode === "drawing" ? "crosshair" : "";
    };

    map.on("click", onClick);
    map.on("contextmenu", onContextMenu);
    map.on("mousemove", onMouseMove);
    map.on("mouseleave", onMouseLeave);

    return () => {
      map.off("click", onClick);
      map.off("contextmenu", onContextMenu);
      map.off("mousemove", onMouseMove);
      map.off("mouseleave", onMouseLeave);
      map.getCanvas().style.cursor = "";
    };
  }, [completeDrawing, drawingMode, finishDrawing, mapError, setDraftVertices]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !map.isStyleLoaded()) {
      return;
    }
    sourceData(
      map,
      "aoi-draft",
      drawingMode === "drawing"
        ? drawingPreviewFeatureCollection(drawingSubMode, draftVertices, drawingCursorCoordinate)
        : EMPTY_FEATURE_COLLECTION,
    );
    moveDrawingLayersToTop(map);
  }, [drawingCursorCoordinate, drawingMode, drawingSubMode, draftVertices, mapStyleRevision]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !map.isStyleLoaded()) {
      return;
    }
    const source = map.getSource("drawing-vertices") as GeoJSONSource | undefined;
    source?.setData({
      type: "FeatureCollection",
      features: drawingMode === "drawing"
        ? draftVertices.map((vertex, index) => ({
            type: "Feature" as const,
            geometry: { type: "Point" as const, coordinates: vertex },
            properties: { closeTarget: index === 0 && firstVertexCloseTarget },
          }))
        : [],
    });
  }, [draftVertices, drawingMode, firstVertexCloseTarget, mapStyleRevision]);

  // Show vertex markers during rectangle drawing
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !map.isStyleLoaded() || drawingMode !== "drawing") {
      if (map && map.isStyleLoaded()) {
        const source = map.getSource("rectangle-vertices") as GeoJSONSource | undefined;
        if (source) {
          source.setData(EMPTY_FEATURE_COLLECTION);
        }
      }
      return;
    }

    const drawingSubMode = useAppStore.getState().drawingSubMode;
    if (drawingSubMode !== "rectangle" || draftVertices.length === 0) {
      return;
    }

    // Show vertex marker(s) for rectangle
    const features = draftVertices.map((vertex) => ({
      type: "Feature" as const,
      geometry: {
        type: "Point" as const,
        coordinates: vertex,
      },
      properties: {},
    }));

    const source = map.getSource("rectangle-vertices") as GeoJSONSource | undefined;
    if (source) {
      source.setData({
        type: "FeatureCollection" as const,
        features,
      });
    }
  }, [drawingMode, draftVertices]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (drawingMode === "idle") {
        return;
      }

      const action = drawingKeyboardAction(event.key, draftVertices.length);
      if (action === "complete") {
        event.preventDefault();
        finishDrawing();
      }

      if (action === "cancel") {
        event.preventDefault();
        stopDrawing();
      }
    };

    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [draftVertices.length, drawingMode, finishDrawing, stopDrawing]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || mapError) {
      return;
    }

    latestPresentationRef.current = {
      aoi,
      exportGeometry,
      draftVertices,
      detectedPolygons,
      buildingBlocks,
      bufferLayers,
      pairwiseBuffers,
      temporalVectors,
      overlayBounds,
      overlaySources,
      referenceLayers,
      referenceLayerData,
      layerState,
      workflowMode,
    };

    syncMapPresentation(map, latestPresentationRef.current);
  }, [
    aoi,
    exportGeometry,
    draftVertices,
    detectedPolygons,
    buildingBlocks,
    bufferLayers,
    pairwiseBuffers,
    temporalVectors,
    mapError,
    overlayBounds,
    overlaySources,
    referenceLayers,
    referenceLayerData,
    layerState,
    workflowMode,
    drawingMode,
    selectedTemporalMilestoneReady,
  ]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || mapError || !map.isStyleLoaded()) {
      return;
    }
    syncAoiMapSource(map, aoi);
    moveDrawingLayersToTop(map);
  }, [aoi, mapError, mapStyleRevision]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || mapError) {
      return;
    }
    const syncCurrentAoi = (polygon: Polygon | null) => {
      if (!map.isStyleLoaded()) {
        return;
      }
      syncAoiMapSource(map, polygon);
      moveDrawingLayersToTop(map);
    };
    syncCurrentAoi(useAppStore.getState().aoi);
    return useAppStore.subscribe((state) => syncCurrentAoi(state.aoi));
  }, [mapError, mapStyleRevision]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || mapError) {
      return;
    }

    markersRef.current.forEach((marker) => marker.remove());
    markersRef.current = [];

    if (drawingMode !== "editing") {
      return;
    }

    draftVertices.forEach((vertex, index) => {
      const marker = new maplibregl.Marker({ element: createEditMarker(), draggable: true }).setLngLat(vertex).addTo(map);
      marker.on("dragend", () => {
        const lngLat = marker.getLngLat();
        updateDraftVertex(index, [lngLat.lng, lngLat.lat]);
      });
      markersRef.current.push(marker);
    });

    return () => {
      markersRef.current.forEach((marker) => marker.remove());
      markersRef.current = [];
    };
  }, [draftVertices, drawingMode, mapError, updateDraftVertex]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !aoi || mapError) {
      return;
    }
    fitPolygon(map, aoi);
  }, [aoi, mapError, mapFocusRequestId]);

  useEffect(() => {
    const map = mapRef.current;
    const bounds = referenceLayerFocus.bounds;
    if (!map || !bounds || mapError) {
      return;
    }
    map.fitBounds(new maplibregl.LngLatBounds([bounds[0], bounds[1]], [bounds[2], bounds[3]]), {
      padding: 80,
      duration: 600,
    });
  }, [referenceLayerFocus.requestId, mapError]);

  useEffect(() => {
    if (searchValue.trim().length < 3) {
      setSearchResults([]);
      setSearchLoading(false);
      setSearchError(null);
      setHighlightedResultIndex(-1);
      return;
    }

    const controller = new AbortController();
    const timeoutId = window.setTimeout(async () => {
      try {
        setSearchLoading(true);
        setSearchError(null);
        const results = await fetchSearchResults(searchValue, apiKey, controller.signal, t("map.unnamed_location"));
        setSearchResults(results);
        setHighlightedResultIndex(-1);
      } catch (error) {
        if (controller.signal.aborted) {
          return;
        }
        setSearchResults([]);
        setSearchError(error instanceof Error ? error.message : t("error.search_failed"));
      } finally {
        if (!controller.signal.aborted) {
          setSearchLoading(false);
        }
      }
    }, 260);

    return () => {
      controller.abort();
      window.clearTimeout(timeoutId);
  };
  }, [apiKey, searchValue]);

  const handleSearchSelection = (selection: SearchResult) => {
    const map = mapRef.current;
    if (!map) {
      return;
    }

    setSearchValue("");
    setSearchResults([]);
    setSearchError(null);
    setHighlightedResultIndex(-1);

    if (selection.bbox) {
      const bounds = new maplibregl.LngLatBounds(
        [selection.bbox[0], selection.bbox[1]],
        [selection.bbox[2], selection.bbox[3]],
      );
      map.fitBounds(bounds, { padding: 80, duration: 700 });
      return;
    }

    map.flyTo({ center: selection.center, zoom: 16, duration: 700 });
  };

  const draftModeActive = drawingMode === "drawing" || drawingMode === "editing";
  const temporalReferenceLayerToggleEnabled =
    selectedTemporalMilestoneReady &&
    temporalReferenceImageryAvailable &&
    !temporalReferenceImageryMissingGeoreference;
  const temporalReferenceWarningVisible =
    hasTemporalMosaicLayerContext &&
    selectedTemporalMilestoneReady &&
    temporalReferenceImageryAvailable &&
    temporalReferenceImageryMissingGeoreference;
  const referenceLayerSectionEntries = referenceLayers.filter((layer) => layer.storage_strategy === "geojson");
  const imagerySectionEntries =
    hasPairwiseLayerContext
      ? ([
          {
            key: "t1Preview",
            label: t("download.t1_archive"),
            enabled: Boolean(overlaySources.t1Preview) && rasterOverlaysGeoreferenced,
            description: overlaySources.t1Preview
              ? (!rasterOverlaysGeoreferenced ? t("map.reference_imagery_missing_georeference") : undefined)
              : t("map.reference_imagery_unavailable"),
          },
          {
            key: "t2Preview",
            label: t("download.t2_archive"),
            enabled: Boolean(overlaySources.t2Preview) && rasterOverlaysGeoreferenced,
            description: overlaySources.t2Preview
              ? (!rasterOverlaysGeoreferenced ? t("map.reference_imagery_missing_georeference") : undefined)
              : t("map.reference_imagery_unavailable"),
          },
          {
            key: "changeProbability",
            label: t("download.change_probability"),
            enabled: Boolean(overlaySources.changeProbability) && rasterOverlaysGeoreferenced,
            description: overlaySources.changeProbability
              ? (!rasterOverlaysGeoreferenced ? t("map.reference_imagery_missing_georeference") : undefined)
              : t("map.reference_imagery_unavailable"),
          },
          {
            key: "changeOverlay",
            label: t("download.change_overlay"),
            enabled: Boolean(overlaySources.changeOverlay) && rasterOverlaysGeoreferenced,
            description: overlaySources.changeOverlay
              ? (!rasterOverlaysGeoreferenced ? t("map.reference_imagery_missing_georeference") : undefined)
              : t("map.reference_imagery_unavailable"),
          },
        ] satisfies LayerEntry[])
      : hasTemporalMosaicLayerContext
        ? ([
            {
              key: "temporalReferenceImagery",
              label: t("map.reference_imagery"),
              enabled: temporalReferenceLayerToggleEnabled,
              description: temporalReferenceImageryAvailable
                ? (temporalReferenceImageryMissingGeoreference ? t("map.reference_imagery_missing_georeference") : undefined)
                : t("map.reference_imagery_unavailable"),
            },
          ] satisfies LayerEntry[])
      : ([] satisfies LayerEntry[]);
  const analysisSectionEntries =
    hasPairwiseLayerContext
      ? ([
          { key: "detectedPolygons", label: t("download.detected_polygons"), enabled: detectedPolygons.features.length > 0 },
          { key: "buildingBlocks", label: t("download.building_blocks"), enabled: buildingBlocks.features.length > 0 },
          { key: "buffer10m", label: `${t("map.building_change_buffer")} 10 m`, enabled: pairwiseBuffers.buffer10m.features.length > 0 },
          { key: "buffer15m", label: `${t("map.building_change_buffer")} 15 m`, enabled: pairwiseBuffers.buffer15m.features.length > 0 },
          { key: "buffer20m", label: `${t("map.building_change_buffer")} 20 m`, enabled: pairwiseBuffers.buffer20m.features.length > 0 },
        ] satisfies LayerEntry[])
      : hasTemporalMosaicLayerContext
        ? ([
            {
              key: "temporalAdditions",
              label: temporalLayerLabels.allPreviousAdditions,
              enabled: selectedTemporalMilestoneReady && allPreviousAdditionsAvailable,
              swatch: { color: activeTemporalMilestoneColor, opacity: 0.88 },
            },
            {
              key: "selectedMilestoneAdditions",
              label: selectedMilestoneAdditionsLabel,
              enabled: selectedTemporalMilestoneReady && selectedMilestoneAdditionsAvailable,
              swatch: { color: activeTemporalMilestoneColor, opacity: 0.88 },
            },
            {
              key: "buffer10m",
              label: temporalLayerLabels.buffer10m,
              enabled:
                selectedTemporalMilestoneReady &&
                (pairwiseBuffers.buffer10m.features.length > 0 || activeTemporalArtifactAvailable("building_change_buffer_10m")),
              swatch: { color: activeTemporalMilestoneColor, opacity: 1 },
            },
            {
              key: "buffer15m",
              label: temporalLayerLabels.buffer15m,
              enabled:
                selectedTemporalMilestoneReady &&
                (pairwiseBuffers.buffer15m.features.length > 0 || activeTemporalArtifactAvailable("building_change_buffer_15m")),
            },
            {
              key: "buffer20m",
              label: temporalLayerLabels.buffer20m,
              enabled:
                selectedTemporalMilestoneReady &&
                (pairwiseBuffers.buffer20m.features.length > 0 || activeTemporalArtifactAvailable("building_change_buffer_20m")),
            },
            {
              key: "temporalCumulativeBuffer10m",
              label: temporalLayerLabels.cumulativeBuffer10m,
              enabled:
                selectedTemporalMilestoneReady &&
                (temporalVectors.temporalCumulativeBuffer10m.features.length > 0 ||
                  activeTemporalArtifactAvailable("building_change_buffer_10m")),
            },
            {
              key: "temporalCumulativeBuffer15m",
              label: temporalLayerLabels.cumulativeBuffer15m,
              enabled:
                selectedTemporalMilestoneReady &&
                (temporalVectors.temporalCumulativeBuffer15m.features.length > 0 ||
                  activeTemporalArtifactAvailable("building_change_buffer_15m")),
            },
            {
              key: "temporalCumulativeBuffer20m",
              label: temporalLayerLabels.cumulativeBuffer20m,
              enabled:
                selectedTemporalMilestoneReady &&
                (temporalVectors.temporalCumulativeBuffer20m.features.length > 0 ||
                  activeTemporalArtifactAvailable("building_change_buffer_20m")),
            },
          ] satisfies LayerEntry[])
      : ([] satisfies LayerEntry[]);
  useEffect(() => {
    if (!hasTemporalMosaicLayerContext || !temporalPresentation?.selectedReleaseIdentifier) {
      return;
    }
    for (const entry of analysisSectionEntries) {
      const enabled = entry.enabled;
      const reason = enabled
        ? "available"
        : !selectedTemporalMilestoneReady
          ? "unsupported_for_baseline"
          : "empty_geojson";
      devLog("TEMPORAL_OUTPUT_LAYER_AVAILABILITY", {
        projectId: temporalPresentation.projectId,
        releaseIdentifier: temporalPresentation.selectedReleaseIdentifier,
        layerKey: entry.key,
        enabled,
        reason,
      });
      devLog(enabled ? "TEMPORAL_OUTPUT_LAYER_ENABLED" : "TEMPORAL_OUTPUT_LAYER_DISABLED", {
        projectId: temporalPresentation.projectId,
        releaseIdentifier: temporalPresentation.selectedReleaseIdentifier,
        layerKey: entry.key,
        reason,
      });
    }
  }, [
    hasTemporalMosaicLayerContext,
    selectedTemporalMilestoneReady,
    temporalPresentation?.projectId,
    temporalPresentation?.selectedReleaseIdentifier,
    visibleAdditionsLegendEntries.length,
    temporalVectors.temporalAdditions.features.length,
    temporalVectors.temporalCumulative.features.length,
    temporalVectors.temporalCumulativeBuffer10m.features.length,
    temporalVectors.temporalCumulativeBuffer15m.features.length,
    temporalVectors.temporalCumulativeBuffer20m.features.length,
    temporalVectors.temporalCumulativeGrowthEnvelope.features.length,
    pairwiseBuffers.buffer10m.features.length,
    pairwiseBuffers.buffer15m.features.length,
    pairwiseBuffers.buffer20m.features.length,
  ]);
  const showLayerPanel = workflowMode !== "temporal" && (hasPairwiseLayerContext || referenceLayers.length > 0);
  const temporalStatisticsAvailable = Boolean(
    workflowMode === "temporal" &&
      temporalPresentation?.selectedMilestone &&
      temporalPresentation.selectedMilestone.metrics,
  );
  const renderLayerEntry = (entry: LayerEntry) => (
    <label
      key={entry.key}
      className={cn(
        "flex items-start justify-between gap-3 rounded px-2 py-2 text-sm",
        entry.enabled ? "text-foreground" : "text-muted-foreground",
      )}
    >
      <span className="min-w-0">
        <span className="flex min-w-0 items-center gap-2">
          {entry.swatch ? (
            <span
              aria-hidden="true"
              className="h-3 w-3 shrink-0 rounded-[2px] border border-border"
              style={{ backgroundColor: entry.swatch.color, opacity: entry.swatch.opacity ?? 1 }}
            />
          ) : null}
          <span className="block truncate">{entry.label}</span>
        </span>
        {entry.description ? <span className="mt-0.5 block text-caption text-muted-foreground">{entry.description}</span> : null}
      </span>
      <input
        type="checkbox"
        checked={layerState[entry.key]}
        onChange={(event) => updateLayerState({ [entry.key]: event.target.checked })}
        disabled={!entry.enabled}
        className="mt-0.5 h-5 w-5 rounded border-white/50 bg-transparent accent-primary disabled:opacity-40"
      />
    </label>
  );

  useEffect(() => {
    if (!onTemporalLayerControlsChange) {
      return;
    }
    if (!hasTemporalMosaicLayerContext) {
      if (lastTemporalControlsSignatureRef.current !== "none") {
        lastTemporalControlsSignatureRef.current = "none";
        onTemporalLayerControlsChange(null);
      }
      return;
    }
    const controls: TemporalLayerControlsPresentation = {
      satellite: imagerySectionEntries.map((entry) => ({
        ...entry,
        checked: layerState[entry.key],
        onCheckedChange: (checked: boolean) => updateLayerState({ [entry.key]: checked }),
      })),
      buildingEvolution: analysisSectionEntries.map((entry) => ({
        ...entry,
        checked: layerState[entry.key],
        onCheckedChange: (checked: boolean) => updateLayerState({ [entry.key]: checked }),
      })),
      manualReferenceLayers: referenceLayerSectionEntries.map((layer) => ({
        id: layer.layer_id,
        name: layer.name,
        geometryType: layer.geometry_type ?? null,
        storageStrategy: layer.storage_strategy ?? null,
        opacity: layer.opacity,
      })),
      referenceWarning: temporalReferenceWarningVisible ? t("map.georeference_warning") : null,
    };
    const signature = JSON.stringify({
      satellite: controls.satellite.map((entry) => ({
        key: entry.key,
        label: entry.label,
        enabled: entry.enabled,
        checked: entry.checked,
        description: entry.description ?? null,
        swatch: entry.swatch ?? null,
      })),
      buildingEvolution: controls.buildingEvolution.map((entry) => ({
        key: entry.key,
        label: entry.label,
        enabled: entry.enabled,
        checked: entry.checked,
        description: entry.description ?? null,
        swatch: entry.swatch ?? null,
      })),
      manualReferenceLayers: controls.manualReferenceLayers,
      referenceWarning: controls.referenceWarning,
    });
    if (lastTemporalControlsSignatureRef.current === signature) {
      return;
    }
    lastTemporalControlsSignatureRef.current = signature;
    onTemporalLayerControlsChange(controls);
  }, [
    analysisSectionEntries,
    hasTemporalMosaicLayerContext,
    imagerySectionEntries,
    layerState,
    onTemporalLayerControlsChange,
    referenceLayerSectionEntries,
    t,
    temporalReferenceWarningVisible,
    updateLayerState,
  ]);

  if (mapError) {
    return (
      <div className="flex min-w-0 flex-1 items-center justify-center bg-background p-6">
        <div className="max-w-md text-center">
          <p className="text-sm font-medium text-foreground">{t("error.map_unavailable")}</p>
          <p className="mt-2 text-sm text-muted-foreground">{mapError}</p>
        </div>
      </div>
    );
  }

  return (
    <section className="relative min-h-[60vh] min-w-0 flex-1 overflow-hidden bg-background lg:min-h-0">
      <div ref={containerRef} className="absolute inset-0" />

      {temporalReferenceLoading ? (
        <div className="pointer-events-none absolute left-1/2 top-4 z-20 -translate-x-1/2">
          <div className="flex items-center gap-2 rounded-sm border border-border bg-card/95 px-3 py-2 text-sm text-foreground shadow-panel backdrop-blur-sm">
            <Loader2 className="h-4 w-4 animate-spin text-primary" />
            <span>{t("map.loading_reference_imagery")}</span>
          </div>
        </div>
      ) : null}

      <div className="absolute left-4 top-4 z-20 w-[24rem] max-w-[calc(100%-7rem)]">
        <div className="relative">
          <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            aria-label={t("map.search_placeholder")}
            placeholder={t("map.search_placeholder")}
            value={searchValue}
            onChange={(event) => setSearchValue(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "ArrowDown") {
                event.preventDefault();
                setHighlightedResultIndex((current) => Math.min(current + 1, searchResults.length - 1));
              }
              if (event.key === "ArrowUp") {
                event.preventDefault();
                setHighlightedResultIndex((current) => Math.max(current - 1, 0));
              }
              if (event.key === "Enter" && highlightedResultIndex >= 0 && searchResults[highlightedResultIndex]) {
                event.preventDefault();
                handleSearchSelection(searchResults[highlightedResultIndex]);
              }
              if (event.key === "Escape") {
                setSearchResults([]);
                setHighlightedResultIndex(-1);
              }
            }}
            className="h-11 rounded-sm border-0 bg-card px-10 text-sm text-foreground shadow-panel placeholder:text-muted-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
          />
          {searchValue ? (
            <button
              type="button"
              onClick={() => {
                setSearchValue("");
                setSearchResults([]);
                setSearchError(null);
                setHighlightedResultIndex(-1);
              }}
              className="absolute right-1.5 top-1/2 flex h-8 w-8 -translate-y-1/2 items-center justify-center rounded text-muted-foreground transition hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              aria-label={t("map.clear_search")}
            >
              <X className="h-4 w-4" />
            </button>
          ) : null}
        </div>

        {(searchLoading || searchError || searchResults.length > 0) && (
          <div className="mt-2 overflow-hidden rounded-sm bg-card shadow-panel backdrop-blur-sm border border-border">
            {searchLoading ? (
              <div className="flex items-center gap-2 px-4 py-3 text-sm text-foreground">
                <Loader2 className="h-4 w-4 animate-spin" />
                {t("map.search_loading")}
              </div>
            ) : searchError ? (
              <div className="px-4 py-3 text-sm text-red-600 dark:text-red-400">{t("error.search_failed")} {searchError}</div>
            ) : (
              <div className="divide-y divide-border">
                {searchResults.map((resultItem, index) => (
                  <button
                    key={resultItem.id}
                    type="button"
                    onMouseDown={(event) => {
                      event.preventDefault();
                      handleSearchSelection(resultItem);
                    }}
                    className={cn(
                      "flex w-full items-start justify-between gap-3 px-4 py-3 text-left transition-colors",
                      highlightedResultIndex === index ? "bg-surface" : "hover:bg-surface/50",
                    )}
                  >
                    <div className="min-w-0">
                      <p className="truncate text-sm font-medium text-foreground">{resultItem.label || t("map.unnamed_location")}</p>
                      {resultItem.subtitle ? <p className="truncate text-caption text-muted-foreground">{resultItem.subtitle}</p> : null}
                    </div>
                    {highlightedResultIndex === index ? <Check className="mt-0.5 h-4 w-4 shrink-0 text-primary" /> : null}
                  </button>
                ))}
              </div>
            )}
          </div>
        )}
      </div>

      <div className="absolute left-4 top-[4.75rem] z-10 flex flex-col gap-2">
        <div className="overflow-hidden rounded-sm bg-card shadow-panel border border-border">
          <button
            type="button"
            onClick={() => mapRef.current?.zoomIn()}
            className="flex h-12 w-12 items-center justify-center border-b border-border text-foreground transition hover:bg-surface"
            aria-label={t("map.zoom_in")}
          >
            <Plus className="h-5 w-5" />
          </button>
          <button
            type="button"
            onClick={() => mapRef.current?.zoomOut()}
            className="flex h-12 w-12 items-center justify-center text-foreground transition hover:bg-surface"
            aria-label={t("map.zoom_out")}
          >
            <Minus className="h-5 w-5" />
          </button>
        </div>

        {aoi ? (
          <div className="overflow-hidden rounded-sm bg-card shadow-panel border border-border">
            <button
              type="button"
              onClick={() => {
                if (mapRef.current) {
                  fitPolygon(mapRef.current, aoi);
                }
              }}
              className="flex h-12 w-12 items-center justify-center text-foreground transition hover:bg-surface"
              aria-label={t("map.fit_to_aoi")}
            >
              <Maximize2 className="h-5 w-5" />
            </button>
          </div>
        ) : null}
      </div>

      {temporalStatisticsAvailable ? (
        <div className="absolute right-4 top-4 z-10">
          <button
            type="button"
            onClick={() => setStatisticsOpen((current) => !current)}
            className="flex h-11 items-center gap-2 rounded-sm border border-border bg-card px-4 text-sm font-medium text-foreground shadow-panel transition hover:bg-surface"
            aria-expanded={statisticsOpen}
          >
            <BarChart3 className="h-4 w-4 text-primary" aria-hidden="true" />
            <span>{statisticsOpen ? t("map.hide_statistics") : t("map.show_statistics")}</span>
          </button>
        </div>
      ) : null}

      {showLayerPanel ? (
      <div className="absolute right-4 top-4 z-10 w-72 max-w-[calc(100%-2rem)]">
        <div className="rounded-sm bg-card shadow-panel backdrop-blur-sm border border-border">
          <button
            type="button"
            onClick={() => setLayersOpen((current) => !current)}
            className="flex w-full items-center justify-between px-4 py-3 text-left text-sm text-foreground"
          >
            <span className="flex items-center gap-2">
              <Layers3 className="h-4 w-4 text-primary" />
              {t("map.temporal_layers")}
            </span>
            <span className="label-xs-upper">{layersOpen ? t("map.hide_layers") : t("map.show_layers")}</span>
          </button>

          {layersOpen ? (
            <div className="space-y-1 border-t border-border px-3 py-3">
              <p className="px-2 pb-2 text-caption text-muted-foreground">
                {t("map.temporal_layers_description")}
              </p>
              {temporalReferenceWarningVisible ? (
                <p className="px-2 pb-2 text-caption text-amber-600 dark:text-amber-200/90">
                  {t("map.georeference_warning")}
                </p>
              ) : null}
              <div className="space-y-3">
                {imagerySectionEntries.length ? (
                  <div>
                    <p className="px-2 pb-1 label-xs-upper">
                      {t("map.reference_imagery_section")}
                    </p>
                    {imagerySectionEntries.map(renderLayerEntry)}
                  </div>
                ) : null}
                {analysisSectionEntries.length ? (
                  <div>
                    <p className="px-2 pb-1 label-xs-upper">
                      {t("map.temporal_outputs_section")}
                    </p>
                    {analysisSectionEntries.map(renderLayerEntry)}
                  </div>
                ) : null}
                {referenceLayerSectionEntries.length ? (
                  <div>
                    <p className="px-2 pb-1 label-xs-upper">
                      {t("reference_layer.map_section")}
                    </p>
                    {referenceLayerSectionEntries.map((layer) => (
                      <div key={layer.layer_id} className="rounded px-2 py-2 text-sm text-foreground">
                        <div className="flex items-center justify-between gap-3">
                          <span className="min-w-0 truncate">{layer.name}</span>
                          <span className="text-caption text-muted-foreground">{Math.round(layer.opacity * 100)}%</span>
                        </div>
                        <p className="mt-0.5 text-caption text-muted-foreground">
                          {formatReferenceLayerKindLabel(layer.geometry_type, layer.storage_strategy)}
                        </p>
                      </div>
                    ))}
                  </div>
                ) : null}
              </div>
            </div>
          ) : null}
        </div>
      </div>
      ) : null}

      {statisticsOpen || visibleAdditionsLegendEntries.length > 0 ? (
        <div className="absolute bottom-4 right-4 z-10 flex max-h-[calc(100%-6rem)] w-[24rem] max-w-[calc(100%-2rem)] flex-col items-end gap-3 overflow-y-auto">
          {statisticsOpen && temporalPresentation?.selectedMilestone ? (
            <div className="pointer-events-auto w-full rounded-sm border border-border bg-card/95 p-3 shadow-panel backdrop-blur-sm">
              <MilestoneMetricCards
                milestone={temporalPresentation.selectedMilestone}
                milestones={temporalPresentation.milestones}
                selectedMilestoneId={temporalPresentation.selectedReleaseIdentifier}
                onSelectMilestone={() => undefined}
                t={t}
                variant="stats"
              />
            </div>
          ) : null}
          {visibleAdditionsLegendEntries.length > 0 ? (
            <div className="pointer-events-none w-48 rounded-sm border border-border bg-card/95 px-4 py-3 text-sm text-foreground shadow-panel backdrop-blur-sm">
              <div className="mb-2 text-sm font-semibold text-foreground">{t("map.additions_by_date")}</div>
              <div className="space-y-1.5">
                {visibleAdditionsLegendEntries.map((entry) => (
                  <div key={entry.releaseIdentifier} className="flex items-center gap-2 text-sm text-foreground">
                    <span
                      aria-hidden="true"
                      className="h-3.5 w-3.5 shrink-0 rounded-[2px] border border-border"
                      style={{ backgroundColor: entry.color }}
                    />
                    <span>{entry.label}</span>
                  </div>
                ))}
              </div>
            </div>
          ) : null}
        </div>
      ) : null}

      {draftModeActive ? (
        <>
          {drawingMode === "editing" ? (
            <div className="pointer-events-none absolute left-1/2 top-4 z-10 -translate-x-1/2">
              <div className="rounded-sm bg-card px-4 py-2 text-caption text-foreground shadow-panel backdrop-blur-sm border border-border">
                <>
                  {t("draw.drag_vertices_to_edit")}
                  <span className="mx-2 text-muted-foreground">|</span>
                  {t("draw.press_enter_to_save")}
                  <span className="mx-2 text-muted-foreground">|</span>
                  {t("draw.press_esc_to_cancel_edit")}
                </>
              </div>
            </div>
          ) : null}

          {drawingMode === "drawing" && drawingPointer ? (
            <div
              className="pointer-events-none absolute z-20 max-w-80 rounded-md border border-warning/40 bg-warning px-3 py-2 text-[13px] font-semibold text-warning-foreground shadow-panel"
              style={{ left: drawingPointer[0] + 16, top: drawingPointer[1] + 16 }}
            >
              {drawingInstruction || drawingHelperMessage(drawingSubMode, draftVertices.length, firstVertexCloseTarget)}
            </div>
          ) : null}
        </>
      ) : null}
    </section>
  );
}
