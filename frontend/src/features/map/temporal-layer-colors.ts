export type TemporalMilestoneColorInput =
  | string
  | {
      releaseIdentifier?: string | null;
      release_identifier?: string | null;
      id?: string | null;
      date?: string | null;
      releaseDate?: string | null;
      release_date?: string | null;
    };

export type TemporalStyledLayerKind = "additions" | "buffer10m";

export type TemporalLayerPaint = {
  fillPaint: {
    "fill-color": string;
    "fill-opacity": number;
    "fill-outline-color": string;
  };
  linePaint: {
    "line-color": string;
    "line-opacity": number;
    "line-width": number;
  };
  fillOpacity: number;
  lineOpacity: number;
};

export const TEMPORAL_MILESTONE_COLOR_PALETTE = [
  "#B91C1C",
  "#1D4ED8",
  "#C2410C",
  "#6D28D9",
  "#047857",
  "#0E7490",
  "#BE185D",
  "#854D0E",
  "#374151",
  "#312E81",
  "#166534",
  "#7F1D1D",
  "#1E3A8A",
  "#581C87",
  "#064E3B",
] as const;

const LATEST_MILESTONE_COLOR = TEMPORAL_MILESTONE_COLOR_PALETTE[0];
const MIN_GENERATED_RGB_DISTANCE = 90;
const GOLDEN_ANGLE = 137.508;

function normalizeIdentifier(milestone: TemporalMilestoneColorInput): string {
  if (typeof milestone === "string") {
    return milestone;
  }
  return milestone.releaseIdentifier ?? milestone.release_identifier ?? milestone.id ?? "";
}

function milestoneDateValue(milestone: TemporalMilestoneColorInput): number {
  const dateValue =
    typeof milestone === "string" ? null : milestone.releaseDate ?? milestone.release_date ?? milestone.date ?? null;
  if (dateValue) {
    const parsed = Date.parse(dateValue);
    if (Number.isFinite(parsed)) {
      return parsed;
    }
  }
  const identifier = normalizeIdentifier(milestone);
  const yearMatch = identifier.match(/(?:19|20)\d{2}/);
  if (yearMatch) {
    return Date.UTC(Number(yearMatch[0]), 0, 1);
  }
  return Number.NEGATIVE_INFINITY;
}

function hexToRgb(color: string): [number, number, number] {
  const value = color.replace("#", "");
  return [
    Number.parseInt(value.slice(0, 2), 16),
    Number.parseInt(value.slice(2, 4), 16),
    Number.parseInt(value.slice(4, 6), 16),
  ];
}

function rgbDistance(left: string, right: string): number {
  const [lr, lg, lb] = hexToRgb(left);
  const [rr, rg, rb] = hexToRgb(right);
  return Math.sqrt((lr - rr) ** 2 + (lg - rg) ** 2 + (lb - rb) ** 2);
}

function hslToHex(hue: number, saturation: number, lightness: number): string {
  const h = (((hue % 360) + 360) % 360) / 360;
  const s = saturation / 100;
  const l = lightness / 100;
  const hueToRgb = (p: number, q: number, t: number) => {
    let adjusted = t;
    if (adjusted < 0) adjusted += 1;
    if (adjusted > 1) adjusted -= 1;
    if (adjusted < 1 / 6) return p + (q - p) * 6 * adjusted;
    if (adjusted < 1 / 2) return q;
    if (adjusted < 2 / 3) return p + (q - p) * (2 / 3 - adjusted) * 6;
    return p;
  };
  const q = l < 0.5 ? l * (1 + s) : l + s - l * s;
  const p = 2 * l - q;
  const rgb = [hueToRgb(p, q, h + 1 / 3), hueToRgb(p, q, h), hueToRgb(p, q, h - 1 / 3)];
  return `#${rgb.map((channel) => Math.round(channel * 255).toString(16).padStart(2, "0")).join("")}`.toUpperCase();
}

function isRedLikeHue(hue: number): boolean {
  const normalized = ((hue % 360) + 360) % 360;
  return normalized <= 25 || normalized >= 335;
}

function generateDarkMilestoneColor(index: number, previousColor: string, usedColors: Set<string>): string {
  for (let attempt = 0; attempt < 48; attempt += 1) {
    const hue = (211 + (index + attempt * 3) * GOLDEN_ANGLE) % 360;
    if (isRedLikeHue(hue)) {
      continue;
    }
    const saturation = 68 + ((index + attempt) % 4) * 6;
    const lightness = 26 + ((index + attempt * 2) % 7) * 2;
    const color = hslToHex(hue, saturation, Math.min(lightness, 38));
    if (!usedColors.has(color) && rgbDistance(color, previousColor) >= MIN_GENERATED_RGB_DISTANCE) {
      return color;
    }
  }

  for (let attempt = 0; attempt < 360; attempt += 17) {
    const hue = (89 + index * 53 + attempt) % 360;
    if (isRedLikeHue(hue)) {
      continue;
    }
    const color = hslToHex(hue, 72, 31);
    if (!usedColors.has(color)) {
      return color;
    }
  }

  throw new Error("Unable to generate a distinct temporal milestone color.");
}

export function getMilestoneColorMap(milestones: TemporalMilestoneColorInput[]): Record<string, string> {
  const uniqueMilestones = new Map<string, TemporalMilestoneColorInput>();
  for (const milestone of milestones) {
    const identifier = normalizeIdentifier(milestone);
    if (identifier && !uniqueMilestones.has(identifier)) {
      uniqueMilestones.set(identifier, milestone);
    }
  }

  const sorted = Array.from(uniqueMilestones.entries()).sort(([leftId, left], [rightId, right]) => {
    const dateDelta = milestoneDateValue(left) - milestoneDateValue(right);
    return dateDelta === 0 ? leftId.localeCompare(rightId) : dateDelta;
  });
  const newestFirst = [...sorted].reverse();
  const usedColors = new Set<string>();
  const colorByReleaseIdentifier: Record<string, string> = {};

  newestFirst.forEach(([identifier], index) => {
    const previousColor = index > 0 ? colorByReleaseIdentifier[newestFirst[index - 1][0]] : LATEST_MILESTONE_COLOR;
    const color =
      index < TEMPORAL_MILESTONE_COLOR_PALETTE.length
        ? TEMPORAL_MILESTONE_COLOR_PALETTE[index]
        : generateDarkMilestoneColor(index, previousColor, usedColors);
    usedColors.add(color);
    colorByReleaseIdentifier[identifier] = color;
  });

  return colorByReleaseIdentifier;
}

export function usesGeneratedMilestoneColors(milestoneCount: number): boolean {
  return milestoneCount > TEMPORAL_MILESTONE_COLOR_PALETTE.length;
}

export function getTemporalLayerPaint(layerKind: TemporalStyledLayerKind, milestoneColor: string): TemporalLayerPaint {
  if (layerKind === "buffer10m") {
    return {
      fillPaint: {
        "fill-color": milestoneColor,
        "fill-opacity": 0.3,
        "fill-outline-color": milestoneColor,
      },
      linePaint: {
        "line-color": milestoneColor,
        "line-opacity": 0.95,
        "line-width": 1.5,
      },
      fillOpacity: 0.3,
      lineOpacity: 0.95,
    };
  }

  return {
    fillPaint: {
      "fill-color": milestoneColor,
      "fill-opacity": 0.88,
      "fill-outline-color": milestoneColor,
    },
    linePaint: {
      "line-color": milestoneColor,
      "line-opacity": 1,
      "line-width": 1,
    },
    fillOpacity: 0.88,
    lineOpacity: 1,
  };
}
