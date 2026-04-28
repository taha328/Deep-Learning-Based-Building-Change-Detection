import type { FeatureCollection, Polygon } from "geojson";
import maplibregl, { type GeoJSONSource, type Map as MapLibreMap } from "maplibre-gl";
import { Check, Layers3, Loader2, Maximize2, Minus, Plus, Search, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { useAppStore } from "@/app/store";
import { useI18n } from "@/lib/i18n";
import { Input } from "@/components/ui/input";
import { createOpenStreetMapStyle, mapboxProvider } from "@/lib/basemap";
import { buildBackendFileUrl } from "@/lib/backend-files";
import { cn } from "@/lib/utils";
import type { TemporalMapPresentation } from "@/features/temporal/types";

const EMPTY_FEATURE_COLLECTION: FeatureCollection = {
  type: "FeatureCollection",
  features: [],
};

const BUILDING_CHANGE_BUFFER_FILL_COLORS = {
  "10m": "#dc2626",
  "15m": "#facc15",
  "20m": "#2563eb",
} as const;

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
  | "temporalCumulativeBuffer10m"
  | "temporalCumulativeBuffer15m"
  | "temporalCumulativeBuffer20m"
  | "temporalAutomated"
  | "temporalAutomatedBuildingBlocks"
  | "temporalEffectiveBuildingBlocks"
  | "temporalConvexHull"
  | "temporalCumulative"
  | "temporalCumulativeGrowthBlocks"
  | "temporalCumulativeGrowthEnvelope"
  | "temporalManualOverride"
  | "labels";

type LayerToggleState = Record<LayerToggleKey, boolean>;

type OverlaySources = {
  t1Preview: string | null;
  t2Preview: string | null;
  temporalReferenceImagery: string | null;
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
  temporalConvexHull: FeatureCollection;
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
};

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

function draftFeatureCollection(vertices: [number, number][]): FeatureCollection {
  if (vertices.length < 2) {
    return EMPTY_FEATURE_COLLECTION;
  }
  return {
    type: "FeatureCollection",
    features: [
      {
        type: "Feature",
        geometry: {
          type: vertices.length >= 3 ? "Polygon" : "LineString",
          coordinates: vertices.length >= 3 ? [[...vertices, vertices[0]]] : vertices,
        },
        properties: {},
      },
    ],
  } as FeatureCollection;
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

function sourceData(map: maplibregl.Map, sourceId: string, data: FeatureCollection) {
  const source = map.getSource(sourceId) as GeoJSONSource | undefined;
  if (source) {
    source.setData(data);
  }
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
      map.setLayoutProperty(layer.id, "visibility", showLabels ? "visible" : "none");
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

  map.setPaintProperty(layerId, "raster-opacity", opacity);
  map.setLayoutProperty(layerId, "visibility", visible ? "visible" : "none");
}

function setLayerVisibility(map: MapLibreMap, layerId: string, visible: boolean) {
  if (!map.getLayer(layerId)) {
    return;
  }
  map.setLayoutProperty(layerId, "visibility", visible ? "visible" : "none");
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

function ensureOperationalLayers(map: MapLibreMap) {
  ensureGeoJsonSource(map, "aoi");
  ensureGeoJsonSource(map, "aoi-draft");
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
  ensureGeoJsonSource(map, "temporal-convex-hull");
  ensureGeoJsonSource(map, "temporal-cumulative");
  ensureGeoJsonSource(map, "temporal-cumulative-growth-blocks");
  ensureGeoJsonSource(map, "temporal-cumulative-growth-envelope");
  ensureGeoJsonSource(map, "temporal-manual-override");

  ensureFillLayer(map, "aoi-fill", "aoi", { "fill-color": "#fbbf24", "fill-opacity": 0.15 });
  ensureLineLayer(map, "aoi-line", "aoi", { "line-color": "#fbbf24", "line-width": 2.5 });
  ensureLineLayer(map, "aoi-draft-line", "aoi-draft", {
    "line-color": "#fbbf24",
    "line-width": 2.25,
    "line-dasharray": [3, 2],
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
  ensureFillLayer(map, "temporal-convex-hull-fill", "temporal-convex-hull", {
    "fill-color": "#f59e0b",
    "fill-opacity": 0.45,
    "fill-outline-color": "#b45309",
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
    "fill-outline-color": "#7c3aed",
  });
  ensureLineLayer(map, "buffer-layers-line", "buffer-layers", {
    "line-color": "#7c3aed",
    "line-width": 2.5,
    "line-dasharray": [4, 3],
  });
  ensureFillLayer(map, "buffer-10m-fill", "buffer-10m", {
    "fill-color": BUILDING_CHANGE_BUFFER_FILL_COLORS["10m"],
    "fill-opacity": 1,
    "fill-outline-color": "#16a34a",
  });
  ensureLineLayer(map, "buffer-10m-line", "buffer-10m", {
    "line-color": "#16a34a",
    "line-width": 3,
    "line-dasharray": [4, 3],
  });
  ensureFillLayer(map, "buffer-15m-fill", "buffer-15m", {
    "fill-color": BUILDING_CHANGE_BUFFER_FILL_COLORS["15m"],
    "fill-opacity": 1,
    "fill-outline-color": "#d97706",
  });
  ensureLineLayer(map, "buffer-15m-line", "buffer-15m", {
    "line-color": "#d97706",
    "line-width": 3.25,
    "line-dasharray": [5, 3],
  });
  ensureFillLayer(map, "buffer-20m-fill", "buffer-20m", {
    "fill-color": BUILDING_CHANGE_BUFFER_FILL_COLORS["20m"],
    "fill-opacity": 1,
    "fill-outline-color": "#a855f7",
  });
  ensureLineLayer(map, "buffer-20m-line", "buffer-20m", {
    "line-color": "#a855f7",
    "line-width": 3.5,
    "line-dasharray": [6, 3],
  });
}

function syncMapPresentation(
  map: MapLibreMap,
  params: {
    aoi: Polygon | null;
    draftVertices: [number, number][];
    detectedPolygons: FeatureCollection;
    buildingBlocks: FeatureCollection;
    bufferLayers: FeatureCollection;
    pairwiseBuffers: PairwiseBufferSources;
    temporalVectors: TemporalVectorSources;
    overlayBounds: [[number, number], [number, number], [number, number], [number, number]] | null;
    overlaySources: OverlaySources;
    layerState: LayerToggleState;
  },
) {
  if (!map.isStyleLoaded()) {
    return;
  }

  ensureOperationalLayers(map);

  sourceData(map, "aoi", polygonFeatureCollection(params.aoi));
  sourceData(map, "aoi-draft", draftFeatureCollection(params.draftVertices));
  sourceData(map, "rectangle-preview", EMPTY_FEATURE_COLLECTION);
  sourceData(map, "rectangle-vertices", EMPTY_FEATURE_COLLECTION);
  sourceData(map, "detected-polygons", params.detectedPolygons);
  sourceData(map, "building-blocks", params.buildingBlocks);
  sourceData(map, "buffer-layers", params.bufferLayers);
  sourceData(map, "buffer-10m", params.pairwiseBuffers.buffer10m);
  sourceData(map, "buffer-15m", params.pairwiseBuffers.buffer15m);
  sourceData(map, "buffer-20m", params.pairwiseBuffers.buffer20m);
  sourceData(map, "temporal-additions", params.temporalVectors.temporalAdditions);
  sourceData(map, "temporal-cumulative-buffer-20m", params.temporalVectors.temporalCumulativeBuffer20m);
  sourceData(map, "temporal-cumulative-buffer-15m", params.temporalVectors.temporalCumulativeBuffer15m);
  sourceData(map, "temporal-cumulative-buffer-10m", params.temporalVectors.temporalCumulativeBuffer10m);
  sourceData(map, "temporal-automated", params.temporalVectors.temporalAutomated);
  sourceData(map, "temporal-automated-building-blocks", params.temporalVectors.temporalAutomatedBuildingBlocks);
  sourceData(map, "temporal-effective-building-blocks", params.temporalVectors.temporalEffectiveBuildingBlocks);
  sourceData(map, "temporal-convex-hull", params.temporalVectors.temporalConvexHull);
  sourceData(map, "temporal-cumulative", params.temporalVectors.temporalCumulative);
  sourceData(map, "temporal-cumulative-growth-blocks", params.temporalVectors.temporalCumulativeGrowthBlocks);
  sourceData(map, "temporal-cumulative-growth-envelope", params.temporalVectors.temporalCumulativeGrowthEnvelope);
  sourceData(map, "temporal-manual-override", params.temporalVectors.temporalManualOverride);

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
    "overlay-temporal-reference-imagery",
    "overlay-temporal-reference-imagery-layer",
    params.overlaySources.temporalReferenceImagery,
    params.overlayBounds,
    0.9,
    params.layerState.temporalReferenceImagery,
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

  applyLabelVisibility(map, params.layerState.labels);
  setLayerVisibility(map, "overlay-t1-preview-layer", params.layerState.t1Preview);
  setLayerVisibility(map, "overlay-t2-preview-layer", params.layerState.t2Preview);
  setLayerVisibility(map, "overlay-temporal-reference-imagery-layer", params.layerState.temporalReferenceImagery);
  setLayerVisibility(map, "overlay-change-probability-layer", params.layerState.changeProbability);
  setLayerVisibility(map, "overlay-change-overlay-layer", params.layerState.changeOverlay);
  setLayerVisibility(map, "detected-polygons-fill", params.layerState.detectedPolygons);
  setLayerVisibility(map, "detected-polygons-line", params.layerState.detectedPolygons);
  setLayerVisibility(map, "building-blocks-line", params.layerState.buildingBlocks);
  setLayerVisibility(map, "buffer-layers-fill", params.layerState.buffers);
  setLayerVisibility(map, "buffer-layers-line", params.layerState.buffers);
  setLayerVisibility(map, "buffer-10m-fill", params.layerState.buffer10m);
  setLayerVisibility(map, "buffer-10m-line", params.layerState.buffer10m);
  setLayerVisibility(map, "buffer-15m-fill", params.layerState.buffer15m);
  setLayerVisibility(map, "buffer-15m-line", params.layerState.buffer15m);
  setLayerVisibility(map, "buffer-20m-fill", params.layerState.buffer20m);
  setLayerVisibility(map, "buffer-20m-line", params.layerState.buffer20m);
  setLayerVisibility(map, "temporal-additions-fill", params.layerState.temporalAdditions);
  setLayerVisibility(map, "temporal-cumulative-buffer-20m-fill", params.layerState.temporalCumulativeBuffer20m);
  setLayerVisibility(map, "temporal-cumulative-buffer-15m-fill", params.layerState.temporalCumulativeBuffer15m);
  setLayerVisibility(map, "temporal-cumulative-buffer-10m-fill", params.layerState.temporalCumulativeBuffer10m);
  setLayerVisibility(map, "temporal-automated-fill", params.layerState.temporalAutomated);
  setLayerVisibility(map, "temporal-automated-building-blocks-fill", params.layerState.temporalAutomatedBuildingBlocks);
  setLayerVisibility(map, "temporal-effective-building-blocks-fill", params.layerState.temporalEffectiveBuildingBlocks);
  setLayerVisibility(map, "temporal-convex-hull-fill", params.layerState.temporalConvexHull);
  setLayerVisibility(map, "temporal-cumulative-fill", params.layerState.temporalCumulative);
  setLayerVisibility(map, "temporal-cumulative-growth-blocks-fill", params.layerState.temporalCumulativeGrowthBlocks);
  setLayerVisibility(map, "temporal-cumulative-growth-envelope-fill", params.layerState.temporalCumulativeGrowthEnvelope);
  setLayerVisibility(map, "temporal-manual-override-fill", params.layerState.temporalManualOverride);
}

function applyLayerVisibilityState(map: MapLibreMap, layerState: LayerToggleState) {
  applyLabelVisibility(map, layerState.labels);
  setLayerVisibility(map, "overlay-t1-preview-layer", layerState.t1Preview);
  setLayerVisibility(map, "overlay-t2-preview-layer", layerState.t2Preview);
  setLayerVisibility(map, "overlay-temporal-reference-imagery-layer", layerState.temporalReferenceImagery);
  setLayerVisibility(map, "overlay-change-probability-layer", layerState.changeProbability);
  setLayerVisibility(map, "overlay-change-overlay-layer", layerState.changeOverlay);
  setLayerVisibility(map, "detected-polygons-fill", layerState.detectedPolygons);
  setLayerVisibility(map, "detected-polygons-line", layerState.detectedPolygons);
  setLayerVisibility(map, "building-blocks-line", layerState.buildingBlocks);
  setLayerVisibility(map, "buffer-layers-fill", layerState.buffers);
  setLayerVisibility(map, "buffer-layers-line", layerState.buffers);
  setLayerVisibility(map, "buffer-10m-fill", layerState.buffer10m);
  setLayerVisibility(map, "buffer-10m-line", layerState.buffer10m);
  setLayerVisibility(map, "buffer-15m-fill", layerState.buffer15m);
  setLayerVisibility(map, "buffer-15m-line", layerState.buffer15m);
  setLayerVisibility(map, "buffer-20m-fill", layerState.buffer20m);
  setLayerVisibility(map, "buffer-20m-line", layerState.buffer20m);
  setLayerVisibility(map, "temporal-additions-fill", layerState.temporalAdditions);
  setLayerVisibility(map, "temporal-cumulative-buffer-20m-fill", layerState.temporalCumulativeBuffer20m);
  setLayerVisibility(map, "temporal-cumulative-buffer-15m-fill", layerState.temporalCumulativeBuffer15m);
  setLayerVisibility(map, "temporal-cumulative-buffer-10m-fill", layerState.temporalCumulativeBuffer10m);
  setLayerVisibility(map, "temporal-automated-fill", layerState.temporalAutomated);
  setLayerVisibility(map, "temporal-automated-building-blocks-fill", layerState.temporalAutomatedBuildingBlocks);
  setLayerVisibility(map, "temporal-effective-building-blocks-fill", layerState.temporalEffectiveBuildingBlocks);
  setLayerVisibility(map, "temporal-convex-hull-fill", layerState.temporalConvexHull);
  setLayerVisibility(map, "temporal-cumulative-fill", layerState.temporalCumulative);
  setLayerVisibility(map, "temporal-cumulative-growth-blocks-fill", layerState.temporalCumulativeGrowthBlocks);
  setLayerVisibility(map, "temporal-cumulative-growth-envelope-fill", layerState.temporalCumulativeGrowthEnvelope);
  setLayerVisibility(map, "temporal-manual-override-fill", layerState.temporalManualOverride);
}

function defaultLayerState(workflowMode: WorkflowMode, hasPairResult: boolean): LayerToggleState {
  return {
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
    temporalCumulativeBuffer10m: false,
    temporalCumulativeBuffer15m: false,
    temporalCumulativeBuffer20m: false,
    temporalAutomated: false,
    temporalAutomatedBuildingBlocks: false,
    temporalEffectiveBuildingBlocks: false,
    temporalConvexHull: false,
    temporalCumulative: false,
    temporalCumulativeGrowthBlocks: false,
    temporalCumulativeGrowthEnvelope: false,
    temporalManualOverride: workflowMode === "temporal",
    labels: true,
  };
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
}: {
  apiKey: string;
  backendUrl: string;
  workflowMode: WorkflowMode;
  temporalPresentation: TemporalMapPresentation | null;
}) {
  const { t } = useI18n();
  const containerRef = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const markersRef = useRef<maplibregl.Marker[]>([]);
  const [mapError, setMapError] = useState<string | null>(null);
  const [layersOpen, setLayersOpen] = useState(false);
  const [layerState, setLayerState] = useState<LayerToggleState>(() => defaultLayerState("pairwise", false));
  const [searchValue, setSearchValue] = useState("");
  const [searchResults, setSearchResults] = useState<SearchResult[]>([]);
  const [searchLoading, setSearchLoading] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [highlightedResultIndex, setHighlightedResultIndex] = useState(-1);
  const [drawingInstruction, setDrawingInstruction] = useState<string | null>(null);
  const [liveRectanglePreview, setLiveRectanglePreview] = useState<[number, number] | null>(null);
  const latestPresentationRef = useRef<{
    aoi: Polygon | null;
    draftVertices: [number, number][];
    detectedPolygons: FeatureCollection;
    buildingBlocks: FeatureCollection;
    bufferLayers: FeatureCollection;
    pairwiseBuffers: PairwiseBufferSources;
    temporalVectors: TemporalVectorSources;
    overlayBounds: [[number, number], [number, number], [number, number], [number, number]] | null;
    overlaySources: OverlaySources;
    layerState: LayerToggleState;
  } | null>(null);

  const aoi = useAppStore((state) => state.aoi);
  const draftVertices = useAppStore((state) => state.draftVertices);
  const mapFocusRequestId = useAppStore((state) => state.mapFocusRequestId);
  const drawingMode = useAppStore((state) => state.drawingMode);
  const appendDraftVertex = useAppStore((state) => state.appendDraftVertex);
  const setDraftVertices = useAppStore((state) => state.setDraftVertices);
  const finishDrawing = useAppStore((state) => state.finishDrawing);
  const stopDrawing = useAppStore((state) => state.stopDrawing);
  const updateDraftVertex = useAppStore((state) => state.updateDraftVertex);
  const result = useAppStore((state) => state.result);
  const temporalVectors = useMemo<TemporalVectorSources>(
    () => ({
      temporalAdditions: ensureFeatureCollection(temporalPresentation?.additions),
      temporalCumulativeBuffer10m: ensureFeatureCollection(temporalPresentation?.cumulativeBuffer10m),
      temporalCumulativeBuffer15m: ensureFeatureCollection(temporalPresentation?.cumulativeBuffer15m),
      temporalCumulativeBuffer20m: ensureFeatureCollection(temporalPresentation?.cumulativeBuffer20m),
      temporalAutomated: ensureFeatureCollection(temporalPresentation?.automatedCandidate),
      temporalAutomatedBuildingBlocks: ensureFeatureCollection(temporalPresentation?.automatedBuildingBlocks),
      temporalEffectiveBuildingBlocks: ensureFeatureCollection(temporalPresentation?.effectiveBuildingBlocks),
      temporalConvexHull: ensureFeatureCollection(temporalPresentation?.cumulativeConvexHull),
      temporalCumulative: ensureFeatureCollection(temporalPresentation?.cumulativeUnion),
      temporalCumulativeGrowthBlocks: ensureFeatureCollection(temporalPresentation?.cumulativeGrowthBlocks),
      temporalCumulativeGrowthEnvelope: ensureFeatureCollection(temporalPresentation?.cumulativeGrowthEnvelope),
      temporalManualOverride: ensureFeatureCollection(temporalPresentation?.manualOverride),
    }),
    [temporalPresentation],
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
          : ensureFeatureCollection(temporalPresentation?.bufferLayers?.["10m"]),
      buffer15m:
        workflowMode === "pairwise"
          ? bufferFeatureCollection(result?.buffer_layers_geojson, "15m")
          : ensureFeatureCollection(temporalPresentation?.bufferLayers?.["15m"]),
      buffer20m:
        workflowMode === "pairwise"
          ? bufferFeatureCollection(result?.buffer_layers_geojson, "20m")
          : ensureFeatureCollection(temporalPresentation?.bufferLayers?.["20m"]),
    }),
    [result?.buffer_layers_geojson, temporalPresentation?.bufferLayers, workflowMode],
  );
  const overlayBounds = useMemo(() => {
    if (workflowMode === "pairwise" && hasValidRasterBounds(result?.preview_images?.raster_bounds_wgs84)) {
      return imageCoordinatesFromBBox(result.preview_images.raster_bounds_wgs84);
    }
    if (workflowMode === "temporal" && hasValidRasterBounds(temporalPresentation?.referenceImageryBounds)) {
      return imageCoordinatesFromBBox(temporalPresentation.referenceImageryBounds);
    }
    return null;
  }, [result?.preview_images?.raster_bounds_wgs84, temporalPresentation?.referenceImageryBounds, workflowMode]);
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
      temporalReferenceImagery:
        workflowMode === "temporal" && temporalPresentation?.referenceImageryUrl
          ? temporalPresentation.referenceImageryUrl
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
    [backendUrl, result?.preview_images, temporalPresentation?.referenceImageryUrl, workflowMode],
  );

  const hasTemporalResult =
    temporalVectors.temporalAdditions.features.length > 0 ||
    temporalVectors.temporalCumulativeBuffer10m.features.length > 0 ||
    temporalVectors.temporalCumulativeBuffer15m.features.length > 0 ||
    temporalVectors.temporalCumulativeBuffer20m.features.length > 0 ||
    temporalVectors.temporalAutomated.features.length > 0 ||
    temporalVectors.temporalAutomatedBuildingBlocks.features.length > 0 ||
    temporalVectors.temporalEffectiveBuildingBlocks.features.length > 0 ||
    temporalVectors.temporalConvexHull.features.length > 0 ||
    temporalVectors.temporalCumulative.features.length > 0 ||
    temporalVectors.temporalCumulativeGrowthBlocks.features.length > 0 ||
    temporalVectors.temporalCumulativeGrowthEnvelope.features.length > 0 ||
    temporalVectors.temporalManualOverride.features.length > 0;

  useEffect(() => {
    setLayerState((current) => ({
      ...defaultLayerState(workflowMode, Boolean(result?.success)),
      labels: current.labels,
      temporalCumulativeBuffer10m: current.temporalCumulativeBuffer10m,
      temporalCumulativeBuffer15m: current.temporalCumulativeBuffer15m,
      temporalCumulativeBuffer20m: current.temporalCumulativeBuffer20m,
      temporalConvexHull: current.temporalConvexHull,
      temporalCumulativeGrowthEnvelope: current.temporalCumulativeGrowthEnvelope,
    }));
  }, [result?.success, result?.summary?.request_hash, workflowMode, temporalPresentation?.selectedReleaseIdentifier, hasTemporalResult]);

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
      ensureOperationalLayers(map);
      if (latestPresentationRef.current) {
        syncMapPresentation(map, latestPresentationRef.current);
      } else {
        applyLabelVisibility(map, layerState.labels);
      }
    });

    map.on("styledata", () => {
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
      setLayerState: (updater) =>
        setLayerState((current) => {
          const next = { ...current, ...updater };
          if (mapRef.current) {
            applyLayerVisibilityState(mapRef.current, next);
          }
          return next;
        }),
    };

    mapRef.current = map;
    return () => {
      markersRef.current.forEach((marker) => marker.remove());
      markersRef.current = [];
      map.remove();
      delete (window as Window & { __buildingChangeMap?: MapLibreMap }).__buildingChangeMap;
      delete (
        window as Window & {
          __buildingChangeMapDebug?: {
            getLayerState: () => LayerToggleState;
            setLayerState: (updater: Partial<LayerToggleState>) => void;
          };
        }
      ).__buildingChangeMapDebug;
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

    const drawingSubMode = useAppStore.getState().drawingSubMode;

    const onClick = (event: maplibregl.MapMouseEvent) => {
      if (drawingMode !== "drawing") {
        return;
      }

      const vertex: [number, number] = [event.lngLat.lng, event.lngLat.lat];

      if (drawingSubMode === "rectangle") {
        if (draftVertices.length === 0) {
          // First corner placed
          appendDraftVertex(vertex);
          setDrawingInstruction(t("draw.click_to_finish"));
        } else if (draftVertices.length === 1) {
          // Second corner placed - complete rectangle
          const [lng1, lat1] = draftVertices[0];
          const [lng2, lat2] = vertex;

          const minLng = Math.min(lng1, lng2);
          const maxLng = Math.max(lng1, lng2);
          const minLat = Math.min(lat1, lat2);
          const maxLat = Math.max(lat1, lat2);

          const rectangleVertices: [number, number][] = [
            [minLng, minLat],
            [maxLng, minLat],
            [maxLng, maxLat],
            [minLng, maxLat],
          ];

          setDraftVertices(rectangleVertices);
          setDrawingInstruction(null);
          // Automatically finish rectangle drawing
          setTimeout(() => {
            finishDrawing();
          }, 0);
        }
      } else {
        // Polygon mode: add vertex on click
        appendDraftVertex(vertex);
      }
    };

    const onContextMenu = (event: maplibregl.MapMouseEvent) => {
      if (drawingMode !== "drawing" || draftVertices.length < 3) {
        return;
      }
      event.preventDefault();
      finishDrawing();
    };

    // Update cursor and show live preview for rectangle drawing
    const onMouseMove = (event: maplibregl.MapMouseEvent) => {
      if (drawingMode !== "drawing") {
        map.getCanvas().style.cursor = "";
        return;
      }

      const state = useAppStore.getState();
      const subMode = state.drawingSubMode;

      if (subMode === "rectangle") {
        // Show crosshair cursor for drawing
        map.getCanvas().style.cursor = "crosshair";

        if (state.draftVertices.length === 0) {
          // Before first click
          setDrawingInstruction(t("draw.click_first_vertex"));
          setLiveRectanglePreview(null);
        } else if (state.draftVertices.length === 1) {
          // After first click - show live preview
          const [lng1, lat1] = state.draftVertices[0];
          const [lng2, lat2] = [event.lngLat.lng, event.lngLat.lat];

          setLiveRectanglePreview([lng2, lat2]);
        }
      } else {
        // Polygon mode cursor
        map.getCanvas().style.cursor = "crosshair";
      }
    };

    map.on("click", onClick);
    map.on("contextmenu", onContextMenu);
    map.on("mousemove", onMouseMove);
    map.on("mouseleave", () => {
      if (drawingMode === "drawing") {
        map.getCanvas().style.cursor = "crosshair";
      }
    });

    return () => {
      map.off("click", onClick);
      map.off("contextmenu", onContextMenu);
      map.off("mousemove", onMouseMove);
      map.off("mouseleave", () => {});
    };
  }, [appendDraftVertex, draftVertices, drawingMode, finishDrawing, mapError, setDraftVertices]);

  // Update rectangle preview layers in real-time
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !map.isStyleLoaded() || drawingMode !== "drawing") {
      if (map && map.isStyleLoaded()) {
        const source = map.getSource("rectangle-preview") as GeoJSONSource | undefined;
        if (source) {
          source.setData(EMPTY_FEATURE_COLLECTION);
        }
      }
      return;
    }

    const drawingSubMode = useAppStore.getState().drawingSubMode;
    if (drawingSubMode !== "rectangle" || draftVertices.length !== 1 || !liveRectanglePreview) {
      return;
    }

    // Show live rectangle preview
    const [lng1, lat1] = draftVertices[0];
    const [lng2, lat2] = liveRectanglePreview;

    const minLng = Math.min(lng1, lng2);
    const maxLng = Math.max(lng1, lng2);
    const minLat = Math.min(lat1, lat2);
    const maxLat = Math.max(lat1, lat2);

    const rectanglePolygon: Polygon = {
      type: "Polygon",
      coordinates: [
        [
          [minLng, minLat],
          [maxLng, minLat],
          [maxLng, maxLat],
          [minLng, maxLat],
          [minLng, minLat],
        ],
      ],
    };

    const source = map.getSource("rectangle-preview") as GeoJSONSource | undefined;
    if (source) {
      source.setData({
        type: "FeatureCollection",
        features: [
          {
            type: "Feature",
            geometry: rectanglePolygon,
            properties: {},
          },
        ],
      });
    }
  }, [drawingMode, draftVertices, liveRectanglePreview]);

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

      if (event.key === "Enter" && draftVertices.length >= 3) {
        event.preventDefault();
        finishDrawing();
      }

      if (event.key === "Escape") {
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
      draftVertices,
      detectedPolygons,
      buildingBlocks,
      bufferLayers,
      pairwiseBuffers,
      temporalVectors,
      overlayBounds,
      overlaySources,
      layerState,
    };

    syncMapPresentation(map, latestPresentationRef.current);
  }, [aoi, draftVertices, detectedPolygons, buildingBlocks, bufferLayers, pairwiseBuffers, temporalVectors, mapError, overlayBounds, overlaySources, layerState]);

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
  const temporalReferenceImageryHasSource = Boolean(overlaySources.temporalReferenceImagery);
  const temporalRasterOverlayAvailable = hasTemporalMosaicLayerContext && temporalReferenceImageryHasSource;
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
              enabled: selectedTemporalMilestoneReady && temporalReferenceImageryHasSource && rasterOverlaysGeoreferenced,
              description: temporalReferenceImageryHasSource
                ? (!rasterOverlaysGeoreferenced ? t("map.reference_imagery_missing_georeference") : undefined)
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
              label: t("map.additions"),
              enabled: selectedTemporalMilestoneReady && temporalVectors.temporalAdditions.features.length > 0,
            },
            {
              key: "buffer10m",
              label: `${t("map.building_change_buffer")} 10 m`,
              enabled: selectedTemporalMilestoneReady && pairwiseBuffers.buffer10m.features.length > 0,
            },
            {
              key: "buffer15m",
              label: `${t("map.building_change_buffer")} 15 m`,
              enabled: selectedTemporalMilestoneReady && pairwiseBuffers.buffer15m.features.length > 0,
            },
            {
              key: "buffer20m",
              label: `${t("map.building_change_buffer")} 20 m`,
              enabled: selectedTemporalMilestoneReady && pairwiseBuffers.buffer20m.features.length > 0,
            },
            {
              key: "temporalCumulativeBuffer10m",
              label: t("map.cumulative_building_change_buffer_10m"),
              enabled: selectedTemporalMilestoneReady && temporalVectors.temporalCumulativeBuffer10m.features.length > 0,
            },
            {
              key: "temporalCumulativeBuffer15m",
              label: t("map.cumulative_building_change_buffer_15m"),
              enabled: selectedTemporalMilestoneReady && temporalVectors.temporalCumulativeBuffer15m.features.length > 0,
            },
            {
              key: "temporalCumulativeBuffer20m",
              label: t("map.cumulative_building_change_buffer_20m"),
              enabled: selectedTemporalMilestoneReady && temporalVectors.temporalCumulativeBuffer20m.features.length > 0,
            },
            {
              key: "temporalConvexHull",
              label: t("map.convex_hull"),
              enabled: selectedTemporalMilestoneReady && temporalVectors.temporalConvexHull.features.length > 0,
            },
            {
              key: "temporalCumulative",
              label: t("map.cumulative_union"),
              enabled: selectedTemporalMilestoneReady && temporalVectors.temporalCumulative.features.length > 0,
            },
            {
              key: "temporalCumulativeGrowthEnvelope",
              label: t("map.growth_envelope"),
              enabled: selectedTemporalMilestoneReady && temporalVectors.temporalCumulativeGrowthEnvelope.features.length > 0,
            },
            {
              key: "temporalManualOverride",
              label: t("map.manual_override"),
              enabled: selectedTemporalMilestoneReady && temporalVectors.temporalManualOverride.features.length > 0,
            },
          ] satisfies LayerEntry[])
      : ([] satisfies LayerEntry[]);
  const advancedSectionEntries =
    hasTemporalMosaicLayerContext
      ? ([
          {
            key: "temporalAutomated",
            label: t("map.automated_candidate"),
            enabled: selectedTemporalMilestoneReady && temporalVectors.temporalAutomated.features.length > 0,
          },
          {
            key: "temporalAutomatedBuildingBlocks",
            label: t("map.automated_addition_blocks"),
            enabled: selectedTemporalMilestoneReady && temporalVectors.temporalAutomatedBuildingBlocks.features.length > 0,
          },
          {
            key: "temporalEffectiveBuildingBlocks",
            label: t("map.effective_building_blocks"),
            enabled: selectedTemporalMilestoneReady && temporalVectors.temporalEffectiveBuildingBlocks.features.length > 0,
          },
          {
            key: "temporalCumulativeGrowthBlocks",
            label: t("map.cumulative_growth_blocks"),
            enabled: selectedTemporalMilestoneReady && temporalVectors.temporalCumulativeGrowthBlocks.features.length > 0,
          },
        ] satisfies LayerEntry[])
      : ([] satisfies LayerEntry[]);
  const baseSectionEntries = ([{ key: "labels", label: t("download.reference_labels"), enabled: true }] satisfies LayerEntry[]);
  const showLayerPanel = hasPairwiseLayerContext || hasTemporalMosaicLayerContext;
  const renderLayerEntry = (entry: LayerEntry) => (
    <label
      key={entry.key}
      className={cn(
        "flex items-start justify-between gap-3 rounded px-2 py-2 text-sm",
        entry.enabled ? "text-foreground" : "text-muted-foreground",
      )}
    >
      <span className="min-w-0">
        <span className="block">{entry.label}</span>
        {entry.description ? <span className="mt-0.5 block text-caption text-muted-foreground">{entry.description}</span> : null}
      </span>
      <input
        type="checkbox"
        checked={layerState[entry.key]}
        onChange={(event) =>
          setLayerState((current) => {
            const next = { ...current, [entry.key]: event.target.checked };
            if (mapRef.current) {
              applyLayerVisibilityState(mapRef.current, next);
            }
            return next;
          })
        }
        disabled={!entry.enabled}
        className="mt-0.5 h-4 w-4 rounded border-white/50 bg-transparent accent-sky-400 disabled:opacity-40"
      />
    </label>
  );

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
            className="h-11 rounded-sm border-0 bg-card px-10 text-sm text-foreground shadow-panel placeholder:text-muted-foreground focus-visible:ring-0 focus-visible:ring-ring"
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
              className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground transition hover:text-foreground"
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
              {!rasterOverlaysGeoreferenced && temporalRasterOverlayAvailable ? (
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
                {analysisSectionEntries.length || advancedSectionEntries.length ? (
                  <div>
                    <p className="px-2 pb-1 label-xs-upper">
                      {t("map.temporal_outputs_section")}
                    </p>
                    {analysisSectionEntries.map(renderLayerEntry)}
                    {advancedSectionEntries.length ? (
                      <details className="mt-1">
                        <summary className="cursor-pointer px-2 py-2 text-label font-medium text-muted-foreground">
                          {t("map.derived_layers_section")}
                        </summary>
                        <div className="space-y-1">
                          {advancedSectionEntries.map(renderLayerEntry)}
                        </div>
                      </details>
                    ) : null}
                  </div>
                ) : null}
                <div>
                  <p className="px-2 pb-1 text-[10px] font-semibold uppercase tracking-[0.12em] text-muted-foreground">
                    {t("map.reference_labels_section")}
                  </p>
                  {baseSectionEntries.map(renderLayerEntry)}
                </div>
              </div>
            </div>
          ) : null}
        </div>
      </div>
      ) : null}

      {draftModeActive ? (
        <>
          <div className="pointer-events-none absolute left-1/2 top-4 z-10 -translate-x-1/2">
              <div className="rounded-sm bg-card px-4 py-2 text-caption text-foreground shadow-panel backdrop-blur-sm border border-border">
              {drawingMode === "drawing" ? (
                <>
                  {drawingInstruction || t("draw.click_to_add_points")}
                  <span className="mx-2 text-muted-foreground">|</span>
                  {t("draw.press_enter_to_finish")}
                  <span className="mx-2 text-muted-foreground">|</span>
                  {t("draw.press_esc_to_cancel")}
                </>
              ) : (
                <>
                  {t("draw.drag_vertices_to_edit")}
                  <span className="mx-2 text-muted-foreground">|</span>
                  {t("draw.press_enter_to_save")}
                  <span className="mx-2 text-muted-foreground">|</span>
                  {t("draw.press_esc_to_cancel_edit")}
                </>
              )}
            </div>
          </div>

          {drawingMode === "drawing" ? (
            <div className="pointer-events-none absolute left-1/2 top-1/2 z-10 -translate-x-1/2 -translate-y-1/2">
              <div className="relative">
                <div className="absolute left-1/2 top-1/2 h-12 w-px -translate-x-1/2 -translate-y-1/2 bg-primary/70" />
                <div className="absolute left-1/2 top-1/2 h-px w-12 -translate-x-1/2 -translate-y-1/2 bg-primary/70" />
                <div className="h-2.5 w-2.5 rounded-full border-2 border-primary bg-surface" />
              </div>
            </div>
          ) : null}
        </>
      ) : null}
    </section>
  );
}
