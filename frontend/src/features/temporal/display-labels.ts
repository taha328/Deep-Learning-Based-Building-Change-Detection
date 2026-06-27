import type { ReferenceLayerPreflight, TemporalMilestone } from "@/api/contracts";

const DATE_FALLBACK = "Date non disponible";

export function formatDateDmy(value: string | null | undefined, fallback = DATE_FALLBACK): string {
  if (!value) {
    return fallback;
  }
  const match = value.match(/^(\d{4})-(\d{2})-(\d{2})/);
  if (match) {
    return `${match[3]}/${match[2]}/${match[1]}`;
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return fallback;
  }
  return new Intl.DateTimeFormat("fr-FR", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
  }).format(date);
}

export function archiveCodeFromIdentifier(value: string | null | undefined): string | null {
  const match = value?.match(/R\d+$/i);
  return match ? match[0].toUpperCase() : null;
}

export function formatMilestonePrimaryLabel(milestone: TemporalMilestone): string {
  return formatDateDmy(milestone.release_date);
}

export function formatMilestoneSecondaryLabel(milestone: TemporalMilestone): string {
  const archiveCode = archiveCodeFromIdentifier(milestone.release_identifier);
  return archiveCode ? `Archive ${archiveCode}` : "Archive";
}

export function formatMilestoneActionLabel(milestone: TemporalMilestone): string {
  const dateLabel = formatMilestonePrimaryLabel(milestone);
  const archiveLabel = formatMilestoneSecondaryLabel(milestone);
  return archiveLabel === "Archive" ? dateLabel : `${dateLabel} (${archiveLabel})`;
}

export function formatGeometryTypeLabel(value: string | null | undefined): string {
  switch ((value ?? "").toLowerCase()) {
    case "polygon":
      return "Polygone";
    case "multipolygon":
      return "Multipolygone";
    case "line":
    case "linestring":
    case "multilinestring":
      return "Ligne";
    case "point":
    case "multipoint":
      return "Point";
    case "mixed":
      return "Géométries mixtes";
    case "raster":
      return "Image raster";
    default:
      return "Type non disponible";
  }
}

export function formatStorageStrategyLabel(value: string | null | undefined): string {
  switch ((value ?? "").toLowerCase()) {
    case "geojson":
      return "Fichier GeoJSON";
    case "gpkg":
      return "GeoPackage";
    case "postgis":
      return "Couche spatiale";
    case "pmtiles":
    case "mbtiles":
      return "Tuiles vectorielles optimisées";
    case "cog":
    case "raster_tiles":
      return "Image cartographique optimisée";
    case "image_overlay":
      return "Image géoréférencée";
    case "auto":
      return "Choix automatique";
    default:
      return "Affichage standard";
  }
}

export function formatReferenceLayerKindLabel(
  geometryType: string | null | undefined,
  storageStrategy: string | null | undefined,
): string {
  return `${formatGeometryTypeLabel(geometryType)} / ${formatStorageStrategyLabel(storageStrategy)}`;
}

export function formatPreflightDisplayStatus(preflight: ReferenceLayerPreflight): string {
  if (preflight.errors.length) {
    return "La couche doit être corrigée avant l’import.";
  }
  if (preflight.storage_strategy === "pmtiles" || preflight.storage_strategy === "mbtiles") {
    return "Couche prête pour l’affichage cartographique optimisé.";
  }
  return "Couche prête pour l’affichage cartographique.";
}

export function formatDiagnosticStageLabel(stage: string): string {
  const normalized = stage.toLowerCase().replace(/[\s-]+/g, "_");
  if (/preflight|validat|metadata|availability/.test(normalized)) {
    return "Vérification initiale";
  }
  if (/mosaic|download|imagery|tile|wayback|reference/.test(normalized)) {
    return "Préparation des images";
  }
  if (/infer|bandon|segmentation|change/.test(normalized)) {
    return "Analyse des changements";
  }
  if (/vector|postprocess|post_process|result/.test(normalized)) {
    return "Génération des résultats";
  }
  if (/export|artifact|bundle/.test(normalized)) {
    return "Préparation de l’export";
  }
  if (/complete|completed|done|success/.test(normalized)) {
    return "Terminé";
  }
  return "Étape avancée";
}

export function formatDurationLabel(seconds: number): string {
  const rounded = Math.max(0, Math.round(seconds));
  if (rounded < 60) {
    return `${rounded} s`;
  }
  const minutes = Math.floor(rounded / 60);
  const remainingSeconds = rounded % 60;
  return remainingSeconds === 0 ? `${minutes} min` : `${minutes} min ${remainingSeconds} s`;
}
