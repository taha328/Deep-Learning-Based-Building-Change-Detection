import { default as WKTParser } from "wkt";
import togeojson from "togeojson";
import * as shpjs from "shpjs";
import JSZip from "jszip";
import { bbox } from "@turf/bbox";
import type { Polygon, GeoJSON as GeoJSONType } from "geojson";

export interface ParseResult {
  valid: true;
  geometry: Polygon;
  bounds: [number, number, number, number];
  format: string;
}

export interface ParseError {
  valid: false;
  error: string;
  format?: string;
}

export type ParseOutput = ParseResult | ParseError;

/**
 * Detect CRS from GeoJSON object
 * Returns EPSG code if detected, null otherwise
 */
function detectCRS(json: any): string | null {
  if (json?.crs?.properties?.name) {
    const name = json.crs.properties.name as string;
    // Extract EPSG code from "urn:ogc:def:crs:EPSG::XXXX" format
    const match = name.match(/EPSG::(\d+)/);
    if (match) {
      return `EPSG:${match[1]}`;
    }
  }
  return null;
}

/**
 * Transform coordinates from Web Mercator (EPSG:3857) to WGS84 (EPSG:4326)
 * Formula: https://wiki.openstreetmap.org/wiki/Slippy_map_tilenames#Inverse
 */
function webMercatorToWGS84(x: number, y: number): [number, number] {
  const lng = (x / 20037508.34) * 180;
  const lat = (y / 20037508.34) * 180;
  const latConverted =
    (2 * Math.atan(Math.exp((lat * Math.PI) / 180)) - Math.PI / 2) * (180 / Math.PI);
  return [lng, latConverted];
}

/**
 * Transform polygon coordinates based on detected CRS
 */
function transformPolygonCoordinatesIfNeeded(polygon: Polygon, detectedCRS: string | null): Polygon {
  // Only transform if CRS is Web Mercator
  if (detectedCRS !== "EPSG:3857") {
    return polygon;
  }

  // Transform all coordinates in the polygon
  const transformedCoordinates = polygon.coordinates.map((ring) =>
    ring.map(([x, y]) => webMercatorToWGS84(x, y))
  );

  return {
    type: "Polygon",
    coordinates: transformedCoordinates,
  };
}

/**
 * Validate bounds are within valid WGS84 range and contain valid numbers
 */
function isValidBounds(bounds: [number, number, number, number]): boolean {
  const [minLng, minLat, maxLng, maxLat] = bounds;

  // Check for NaN, Infinity
  if (!Number.isFinite(minLng) || !Number.isFinite(minLat) || 
      !Number.isFinite(maxLng) || !Number.isFinite(maxLat)) {
    return false;
  }

  // Check WGS84 ranges
  // Longitude: [-180, 180]
  // Latitude: [-90, 90]
  if (minLng < -180 || maxLng > 180 || minLat < -90 || maxLat > 90) {
    return false;
  }

  // Check bounds are not inverted
  if (minLng > maxLng || minLat > maxLat) {
    return false;
  }

  // Check bounds have non-zero area (minimum sensible extent)
  if (Math.abs(maxLng - minLng) < 0.00001 || Math.abs(maxLat - minLat) < 0.00001) {
    return false;
  }

  return true;
}

/**
 * Validate that a geometry is a Polygon and convert MultiPolygon to Polygon if possible
 */
function validateAndNormalizePolygon(geometry: any): Polygon | null {
  try {
    // If it's a Polygon, use it directly
    if (geometry.type === "Polygon") {
      return geometry as Polygon;
    }

    // If it's a MultiPolygon, use the largest polygon
    if (geometry.type === "MultiPolygon" && Array.isArray(geometry.coordinates)) {
      if (geometry.coordinates.length === 0) return null;
      
      // Find largest polygon by area (ring length as proxy)
      let maxIndex = 0;
      let maxSize = 0;
      geometry.coordinates.forEach((rings: any[], i: number) => {
        if (rings[0]?.length > maxSize) {
          maxSize = rings[0].length;
          maxIndex = i;
        }
      });

      const firstPoly = geometry.coordinates[maxIndex];
      if (Array.isArray(firstPoly) && firstPoly.length > 0) {
        return {
          type: "Polygon",
          coordinates: firstPoly,
        };
      }
    }

    // If it's a Feature, extract geometry
    if (geometry.type === "Feature" && geometry.geometry) {
      return validateAndNormalizePolygon(geometry.geometry);
    }

    // If it's a FeatureCollection, take first feature
    if (geometry.type === "FeatureCollection" && Array.isArray(geometry.features)) {
      if (geometry.features.length > 0) {
        return validateAndNormalizePolygon(geometry.features[0].geometry);
      }
    }

    return null;
  } catch {
    return null;
  }
}

/**
 * Parse GeoJSON string
 */
export async function parseGeoJSON(text: string): Promise<ParseOutput> {
  try {
    const json = JSON.parse(text.trim());
    
    // Detect CRS from GeoJSON
    const detectedCRS = detectCRS(json);
    
    let polygon = validateAndNormalizePolygon(json);

    if (!polygon) {
      return {
        valid: false,
        error: "Could not extract a valid Polygon from the GeoJSON. Expected Polygon or MultiPolygon geometry.",
        format: "GeoJSON",
      };
    }

    // Transform coordinates if necessary (e.g., Web Mercator to WGS84)
    polygon = transformPolygonCoordinatesIfNeeded(polygon, detectedCRS);

    // Calculate bounds from the transformed polygon
    let bounds = bbox(polygon) as [number, number, number, number];
    
    // Validate bounds are within valid WGS84 range
    if (!isValidBounds(bounds)) {
      return {
        valid: false,
        error: `Invalid bounds detected: [${bounds.join(", ")}]. Coordinates may be in an unsupported projection or malformed.${detectedCRS ? ` Detected CRS: ${detectedCRS}` : ""}`,
        format: "GeoJSON",
      };
    }

    return {
      valid: true,
      geometry: polygon,
      bounds,
      format: "GeoJSON",
    };
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    return {
      valid: false,
      error: `GeoJSON parsing failed: ${msg}`,
      format: "GeoJSON",
    };
  }
}

/**
 * Parse WKT (Well-Known Text) string
 */
export async function parseWKT(text: string): Promise<ParseOutput> {
  try {
    const trimmed = text.trim();
    const geom = WKTParser.parse(trimmed);

    // WKTParser returns a geometry object
    const polygon = validateAndNormalizePolygon(geom);

    if (!polygon) {
      return {
        valid: false,
        error: "WKT does not contain a valid Polygon. Expected POLYGON or MULTIPOLYGON.",
        format: "WKT",
      };
    }

    const bounds = bbox(polygon) as [number, number, number, number];
    
    // Validate bounds are within valid WGS84 range
    if (!isValidBounds(bounds)) {
      return {
        valid: false,
        error: `Invalid bounds detected: [${bounds.join(", ")}]. Coordinates may be malformed or in an unsupported projection.`,
        format: "WKT",
      };
    }
    
    return {
      valid: true,
      geometry: polygon,
      bounds,
      format: "WKT",
    };
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    return {
      valid: false,
      error: `WKT parsing failed: ${msg}. Check format and try again.`,
      format: "WKT",
    };
  }
}

/**
 * Parse KML file
 */
export async function parseKML(file: File): Promise<ParseOutput> {
  try {
    const text = await file.text();
    const parser = new DOMParser();
    const xmlDoc = parser.parseFromString(text, "application/xml");

    if (xmlDoc.getElementsByTagName("parsererror").length > 0) {
      return {
        valid: false,
        error: "KML parsing failed: Invalid XML format.",
        format: "KML",
      };
    }

    const geojson = togeojson.kml(xmlDoc);
    const polygon = validateAndNormalizePolygon(geojson);

    if (!polygon) {
      return {
        valid: false,
        error: "KML does not contain valid Polygon geometry.",
        format: "KML",
      };
    }

    const bounds = bbox(polygon) as [number, number, number, number];
    
    // Validate bounds are within valid WGS84 range
    if (!isValidBounds(bounds)) {
      return {
        valid: false,
        error: `Invalid bounds detected: [${bounds.join(", ")}]. Coordinates may be malformed or in an unsupported projection.`,
        format: "KML",
      };
    }
    
    return {
      valid: true,
      geometry: polygon,
      bounds,
      format: "KML",
    };
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    return {
      valid: false,
      error: `KML parsing failed: ${msg}`,
      format: "KML",
    };
  }
}

/**
 * Parse KMZ (zipped KML) file
 */
export async function parseKMZ(file: File): Promise<ParseOutput> {
  try {
    const zip = new JSZip();
    const loaded = await zip.loadAsync(file);

    // Find .kml file in archive
    let kmlFile: string | null = null;
    let kmlContent = "";

    for (const [filename, file] of Object.entries(loaded.files)) {
      if (filename.toLowerCase().endsWith(".kml")) {
        kmlFile = filename;
        kmlContent = await file.async("string");
        break;
      }
    }

    if (!kmlFile || !kmlContent) {
      return {
        valid: false,
        error: "KMZ archive does not contain a .kml file.",
        format: "KMZ",
      };
    }

    const parser = new DOMParser();
    const xmlDoc = parser.parseFromString(kmlContent, "application/xml");

    if (xmlDoc.getElementsByTagName("parsererror").length > 0) {
      return {
        valid: false,
        error: "KML parsing failed: Invalid XML format.",
        format: "KMZ",
      };
    }

    const geojson = togeojson.kml(xmlDoc);
    const polygon = validateAndNormalizePolygon(geojson);

    if (!polygon) {
      return {
        valid: false,
        error: "KML does not contain valid Polygon geometry.",
        format: "KMZ",
      };
    }

    const bounds = bbox(polygon) as [number, number, number, number];
    
    // Validate bounds are within valid WGS84 range
    if (!isValidBounds(bounds)) {
      return {
        valid: false,
        error: `Invalid bounds detected: [${bounds.join(", ")}]. Coordinates may be malformed or in an unsupported projection.`,
        format: "KMZ",
      };
    }
    
    return {
      valid: true,
      geometry: polygon,
      bounds,
      format: "KMZ",
    };
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    return {
      valid: false,
      error: `KMZ parsing failed: ${msg}`,
      format: "KMZ",
    };
  }
}

/**
 * Parse GPX file
 */
export async function parseGPX(file: File): Promise<ParseOutput> {
  try {
    const text = await file.text();
    const parser = new DOMParser();
    const xmlDoc = parser.parseFromString(text, "application/xml");

    if (xmlDoc.getElementsByTagName("parsererror").length > 0) {
      return {
        valid: false,
        error: "GPX parsing failed: Invalid XML format.",
        format: "GPX",
      };
    }

    const geojson = togeojson.gpx(xmlDoc);
    const polygon = validateAndNormalizePolygon(geojson);

    if (!polygon) {
      return {
        valid: false,
        error: "GPX does not contain valid Polygon geometry (GPX usually contains LineStrings/Points).",
        format: "GPX",
      };
    }

    const bounds = bbox(polygon) as [number, number, number, number];
    
    // Validate bounds are within valid WGS84 range
    if (!isValidBounds(bounds)) {
      return {
        valid: false,
        error: `Invalid bounds detected: [${bounds.join(", ")}]. Coordinates may be malformed or in an unsupported projection.`,
        format: "GPX",
      };
    }
    
    return {
      valid: true,
      geometry: polygon,
      bounds,
      format: "GPX",
    };
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    return {
      valid: false,
      error: `GPX parsing failed: ${msg}`,
      format: "GPX",
    };
  }
}

/**
 * Parse Shapefile (must be zipped .shp, .dbf, .shx files)
 */
export async function parseShapefile(file: File): Promise<ParseOutput> {
  try {
    const arrayBuffer = await file.arrayBuffer();

    // shpjs.parseZip expects an ArrayBuffer
    const geojson = await shpjs.parseZip(arrayBuffer);
    const polygon = validateAndNormalizePolygon(geojson);

    if (!polygon) {
      return {
        valid: false,
        error: "Shapefile does not contain valid Polygon geometry.",
        format: "Shapefile",
      };
    }

    const bounds = bbox(polygon) as [number, number, number, number];
    
    // Validate bounds are within valid WGS84 range
    if (!isValidBounds(bounds)) {
      return {
        valid: false,
        error: `Invalid bounds detected: [${bounds.join(", ")}]. Coordinates may be malformed or in an unsupported projection.`,
        format: "Shapefile",
      };
    }
    
    return {
      valid: true,
      geometry: polygon,
      bounds,
      format: "Shapefile",
    };
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    return {
      valid: false,
      error: `Shapefile parsing failed: ${msg}. Ensure the file is a zipped archive containing .shp, .dbf, and .shx files.`,
      format: "Shapefile",
    };
  }
}

/**
 * Detect file format and parse accordingly
 */
export async function parseGeometryFile(file: File): Promise<ParseOutput> {
  const name = file.name.toLowerCase();

  if (name.endsWith(".json") || name.endsWith(".geojson")) {
    const text = await file.text();
    return parseGeoJSON(text);
  }

  if (name.endsWith(".wkt") || name.endsWith(".txt")) {
    const text = await file.text();
    return parseWKT(text);
  }

  if (name.endsWith(".kml")) {
    return parseKML(file);
  }

  if (name.endsWith(".kmz")) {
    return parseKMZ(file);
  }

  if (name.endsWith(".gpx")) {
    return parseGPX(file);
  }

  if (name.endsWith(".zip")) {
    // Assume it's a shapefile zip
    return parseShapefile(file);
  }

  // Try GeoJSON first
  try {
    const text = await file.text();
    return parseGeoJSON(text);
  } catch {
    // Try WKT
    try {
      const text = await file.text();
      return parseWKT(text);
    } catch {
      return {
        valid: false,
        error: "Unsupported file format. Supported formats: GeoJSON, WKT, KML, KMZ, GPX, Shapefile (zipped).",
      };
    }
  }
}

/**
 * Create a polygon from bounding box [minLon, minLat, maxLon, maxLat]
 */
export function polygonFromBounds(bounds: [number, number, number, number]): Polygon {
  const [minLon, minLat, maxLon, maxLat] = bounds;
  return {
    type: "Polygon",
    coordinates: [
      [
        [minLon, minLat],
        [maxLon, minLat],
        [maxLon, maxLat],
        [minLon, maxLat],
        [minLon, minLat],
      ],
    ],
  };
}
