import type { StyleSpecification } from "maplibre-gl";

export interface BasemapProvider {
  id: string;
  label: string;
  createStyle: (apiKey: string) => StyleSpecification;
}

export const mapboxProvider: BasemapProvider = {
  id: "mapbox",
  label: "Mapbox",
  createStyle: (apiKey: string): StyleSpecification => ({
    version: 8,
    sources: {
      "mapbox-satellite": {
        type: "raster",
        tiles: [
          `https://api.mapbox.com/v4/mapbox.satellite/{z}/{x}/{y}@2x.png?access_token=${apiKey}`,
        ],
        tileSize: 256,
        attribution: "© Mapbox © OpenStreetMap contributors",
      },
    },
    layers: [
      {
        id: "mapbox-satellite",
        type: "raster",
        source: "mapbox-satellite",
      },
    ],
  }),
};

export function createOpenStreetMapStyle(): StyleSpecification {
  return {
    version: 8,
    sources: {
      osm: {
        type: "raster",
        tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
        tileSize: 256,
        attribution: "© OpenStreetMap contributors",
      },
    },
    layers: [
      {
        id: "osm",
        type: "raster",
        source: "osm",
      },
    ],
  };
}
