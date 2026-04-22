import { create } from "zustand";
import type { Position } from "geojson";

import type {
  ModeName,
  ModelBackendName,
  RunResponse,
  Sam3BackendMode,
  TemporalProject,
  ValidationResponse,
} from "@/api/contracts";
import { getFrontendRuntimeConfig } from "@/lib/env";
import { createIdleRunProgress, type RunProgressState } from "@/lib/run-progress";

export type LngLatTuple = [number, number];
export type DrawingMode = "idle" | "drawing" | "editing";
export type DrawingSubMode = "polygon" | "rectangle";
type TemporalProjectUpdate =
  | TemporalProject
  | null
  | ((current: TemporalProject | null) => TemporalProject | null);
export interface DetectionSettings {
  t1Release: string;
  t2Release: string;
  mode: ModeName;
  modelBackend: ModelBackendName;
  sam3BackendMode: Sam3BackendMode;
  changeThreshold: number;
  semanticThreshold: number;
  mergeCloseGapM: number;
  buildingBlockGapM: number;
  bufferDistancesText: string;
}

interface AppState {
  aoi: GeoJSON.Polygon | null;
  draftVertices: LngLatTuple[];
  drawingMode: DrawingMode;
  drawingSubMode: DrawingSubMode;
  mapFocusRequestId: number;
  temporalProject: TemporalProject | null;
  temporalProjectBootstrap: TemporalProject | null;
  selectedReleaseIds: string[];
  validation: ValidationResponse | null;
  validationRequestKey: string | null;
  result: RunResponse | null;
  runStatus: string;
  runProgress: RunProgressState;
  isRunning: boolean;
  settings: DetectionSettings;
  setSetting: <K extends keyof DetectionSettings>(key: K, value: DetectionSettings[K]) => void;
  startDrawing: () => void;
  startRectangleDrawing: () => void;
  startEditing: () => void;
  stopDrawing: () => void;
  setDrawingSubMode: (mode: DrawingSubMode) => void;
  setDraftVertices: (vertices: LngLatTuple[]) => void;
  appendDraftVertex: (vertex: LngLatTuple) => void;
  updateDraftVertex: (index: number, vertex: LngLatTuple) => void;
  finishDrawing: () => void;
  setAoiFromImport: (polygon: GeoJSON.Polygon) => void;
  requestMapFocusToAoi: () => void;
  setTemporalProject: (project: TemporalProjectUpdate) => void;
  setTemporalProjectBootstrap: (project: TemporalProject | null) => void;
  setSelectedReleaseIds: (releaseIds: string[]) => void;
  clearAoi: () => void;
  setValidation: (response: ValidationResponse | null, requestKey: string | null) => void;
  setResult: (response: RunResponse | null) => void;
  setRunStatus: (message: string) => void;
  setRunProgress: (progress: RunProgressState) => void;
  setIsRunning: (value: boolean) => void;
}

function buildPolygon(vertices: LngLatTuple[]): GeoJSON.Polygon | null {
  if (vertices.length < 3) {
    return null;
  }
  return {
    type: "Polygon",
    coordinates: [[...vertices, vertices[0]]],
  };
}

function toVertices(positions: Position[]): LngLatTuple[] {
  return positions.map((position) => [Number(position[0]), Number(position[1])] as LngLatTuple);
}

function buildDefaultSettings(): DetectionSettings {
  const runtimeConfig = getFrontendRuntimeConfig();
  return {
    t1Release: "",
    t2Release: "",
    mode: "full_run",
    modelBackend: runtimeConfig.defaultModelBackend,
    sam3BackendMode: runtimeConfig.defaultSam3BackendMode,
    changeThreshold: 0.65,
    semanticThreshold: 0.5,
    mergeCloseGapM: 10,
    buildingBlockGapM: 25,
    bufferDistancesText: "10,15,20",
  };
}

const defaultSettings: DetectionSettings = buildDefaultSettings();

export const useAppStore = create<AppState>((set, get) => ({
  aoi: null,
  draftVertices: [],
  drawingMode: "idle",
  drawingSubMode: "polygon",
  mapFocusRequestId: 0,
  temporalProject: null,
  temporalProjectBootstrap: null,
  selectedReleaseIds: [],
  validation: null,
  validationRequestKey: null,
  result: null,
  runStatus: "Draw an AOI and validate the request.",
  runProgress: createIdleRunProgress(),
  isRunning: false,
  settings: defaultSettings,
  setSetting: (key, value) =>
    set((state) => ({
      settings: { ...state.settings, [key]: value },
      validation: null,
      validationRequestKey: null,
    })),
  startDrawing: () =>
    set({
      drawingMode: "drawing",
      draftVertices: [],
      validation: null,
      validationRequestKey: null,
      result: null,
      runStatus: "Drawing AOI. Click to add vertices, then press Enter or right-click to finish.",
      runProgress: createIdleRunProgress(),
    }),
  startRectangleDrawing: () =>
    set({
      drawingMode: "drawing",
      drawingSubMode: "rectangle",
      draftVertices: [],
      validation: null,
      validationRequestKey: null,
      result: null,
      runStatus: "Drawing rectangle. Click and drag to create, or click twice for opposite corners.",
      runProgress: createIdleRunProgress(),
    }),
  startEditing: () =>
    set((state) => ({
      drawingMode: state.aoi ? "editing" : "idle",
      draftVertices: state.aoi ? toVertices(state.aoi.coordinates[0].slice(0, -1)) : [],
      validation: null,
      validationRequestKey: null,
      runStatus: state.aoi ? "Editing AOI. Drag vertices, press Enter to save, or Escape to cancel." : state.runStatus,
    })),
  stopDrawing: () =>
    set((state) => ({
      drawingMode: "idle",
      draftVertices: state.aoi ? toVertices(state.aoi.coordinates[0].slice(0, -1)) : [],
    })),
  setDrawingSubMode: (mode) => set({ drawingSubMode: mode }),
  setDraftVertices: (vertices) => set({ draftVertices: vertices }),
  appendDraftVertex: (vertex) => set((state) => ({ draftVertices: [...state.draftVertices, vertex] })),
  updateDraftVertex: (index, vertex) =>
    set((state) => ({
      draftVertices: state.draftVertices.map((item, itemIndex) => (itemIndex === index ? vertex : item)),
      validation: null,
      validationRequestKey: null,
    })),
  finishDrawing: () =>
    set((state) => {
      const polygon = buildPolygon(state.draftVertices);
      if (!polygon) {
        return state;
      }
      return {
        aoi: polygon,
        drawingMode: "idle",
        drawingSubMode: "polygon",
        validation: null,
        validationRequestKey: null,
        runStatus: "AOI ready. Review releases and validate the request before running detection.",
        runProgress: createIdleRunProgress(),
      };
    }),
  setAoiFromImport: (polygon) =>
    set({
      aoi: polygon,
      drawingMode: "idle",
      draftVertices: [],
      validation: null,
      validationRequestKey: null,
      runStatus: "AOI imported successfully. Review releases and validate the request before running detection.",
      runProgress: createIdleRunProgress(),
    }),
  requestMapFocusToAoi: () => set((state) => ({ mapFocusRequestId: state.mapFocusRequestId + 1 })),
  setTemporalProject: (project) =>
    set((state) => ({
      temporalProject: typeof project === "function" ? project(state.temporalProject) : project,
    })),
  setTemporalProjectBootstrap: (project) => set({ temporalProjectBootstrap: project }),
  setSelectedReleaseIds: (releaseIds) => set({ selectedReleaseIds: releaseIds }),
  clearAoi: () =>
    set({
      aoi: null,
      draftVertices: [],
      drawingMode: "idle",
      validation: null,
      validationRequestKey: null,
      result: null,
      runStatus: "AOI cleared.",
      runProgress: createIdleRunProgress(),
    }),
  setValidation: (response, requestKey) => set({ validation: response, validationRequestKey: requestKey }),
  setResult: (response) => set({ result: response }),
  setRunStatus: (message) => set({ runStatus: message }),
  setRunProgress: (progress) => set({ runProgress: progress }),
  setIsRunning: (value) => set({ isRunning: value }),
}));

if (typeof window !== "undefined" && import.meta.env.DEV) {
  (window as Window & { __buildingChangeStore?: typeof useAppStore }).__buildingChangeStore = useAppStore;
}

export function getCurrentVertices(state: AppState): LngLatTuple[] {
  if (state.draftVertices.length > 0) {
    return state.draftVertices;
  }
  return state.aoi?.coordinates[0].slice(0, -1) as LngLatTuple[] | undefined ?? [];
}
