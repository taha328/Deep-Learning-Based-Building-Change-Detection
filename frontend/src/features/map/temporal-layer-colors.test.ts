import assert from "node:assert/strict";
import { test } from "node:test";

import {
  buildTemporalLayerLabels,
  buildTimelineLabelsFromReleases,
  HIGH_CONTRAST_TEMPORAL_COLORS,
  getTemporalLayerExpectedReleases,
  getIncludedAdditionReleasesForCumulativeLayer,
  getIncludedTemporalMilestones,
  getMilestoneColorMap,
  getSelectedReleaseSet,
  getTemporalLayerPaint,
  temporalAdditionVisibilityReason,
  TEMPORAL_LAYER_CONTRACTS,
  TEMPORAL_MILESTONE_COLOR_PALETTE,
} from "./temporal-layer-colors.ts";

const TANGER_MILESTONES = [
  { releaseIdentifier: "WB_2020_R04", releaseDate: "2020-03-23" },
  { releaseIdentifier: "WB_2023_R02", releaseDate: "2023-03-15" },
  { releaseIdentifier: "WB_2024_R02", releaseDate: "2024-03-07" },
  { releaseIdentifier: "WB_2025_R03", releaseDate: "2025-03-27" },
  { releaseIdentifier: "WB_2026_R04", releaseDate: "2026-04-30" },
];

test("high contrast temporal palette is the canonical sequence", () => {
  assert.deepEqual(HIGH_CONTRAST_TEMPORAL_COLORS, [
    "#00B050",
    "#FFD700",
    "#0066FF",
    "#E31A1C",
    "#00C8C8",
    "#FF1493",
    "#7FFF00",
    "#8B4513",
  ]);
  assert.deepEqual(TEMPORAL_MILESTONE_COLOR_PALETTE, HIGH_CONTRAST_TEMPORAL_COLORS);
});

test("high contrast temporal palette avoids black and white", () => {
  const forbidden = new Set(["#000000", "#000", "#FFFFFF", "#FFF"]);
  for (const color of HIGH_CONTRAST_TEMPORAL_COLORS) {
    assert.equal(forbidden.has(color.toUpperCase()), false, color);
  }
});

test("primary project non-baseline years map to high contrast chronological colors", () => {
  const colors = getMilestoneColorMap(TANGER_MILESTONES);
  assert.equal(colors.WB_2023_R02, "#00B050");
  assert.equal(colors.WB_2024_R02, "#FFD700");
  assert.equal(colors.WB_2025_R03, "#0066FF");
  assert.equal(colors.WB_2026_R04, "#E31A1C");
});

test("secondary project non-baseline years map to the same chronological palette", () => {
  const colors = getMilestoneColorMap([
    { releaseIdentifier: "WB_2019_R03", releaseDate: "2019-03-13" },
    { releaseIdentifier: "WB_2023_R04", releaseDate: "2023-05-03" },
    { releaseIdentifier: "WB_2026_R05", releaseDate: "2026-04-30" },
  ]);
  assert.equal(colors.WB_2023_R04, "#00B050");
  assert.equal(colors.WB_2026_R05, "#FFD700");
});

test("colors are stable regardless of input order", () => {
  const first = getMilestoneColorMap(["2016", "2020", "2026"]);
  const second = getMilestoneColorMap(["2026", "2016", "2020"]);
  assert.deepEqual(second, first);
});

test("primary project non-baseline colors are distinct", () => {
  const colors = getMilestoneColorMap(TANGER_MILESTONES);
  const nonBaselineColors = [colors.WB_2023_R02, colors.WB_2024_R02, colors.WB_2025_R03, colors.WB_2026_R04];
  assert.equal(new Set(nonBaselineColors).size, nonBaselineColors.length);
  assert.deepEqual(nonBaselineColors, ["#00B050", "#FFD700", "#0066FF", "#E31A1C"]);
});

test("temporal layer contracts explicitly separate selected and cumulative buffers", () => {
  assert.deepEqual(TEMPORAL_LAYER_CONTRACTS.buffer10m, {
    artifactKey: "building_change_buffer_10m",
    mode: "selected",
  });
  assert.deepEqual(TEMPORAL_LAYER_CONTRACTS.buffer15m, {
    artifactKey: "building_change_buffer_15m",
    mode: "selected",
  });
  assert.deepEqual(TEMPORAL_LAYER_CONTRACTS.buffer20m, {
    artifactKey: "building_change_buffer_20m",
    mode: "selected",
  });
  assert.deepEqual(TEMPORAL_LAYER_CONTRACTS.temporalCumulativeBuffer15m, {
    artifactKey: "building_change_buffer_15m",
    mode: "cumulative",
  });
});

test("colors are not cycled", () => {
  const milestones = Array.from({ length: TEMPORAL_MILESTONE_COLOR_PALETTE.length + 4 }, (_, index) =>
    String(2000 + index),
  );
  const colors = Object.values(getMilestoneColorMap(milestones));
  assert.equal(new Set(colors).size, colors.length);
});

test("generated colors are deterministic and not duplicates", () => {
  const milestones = Array.from({ length: TEMPORAL_MILESTONE_COLOR_PALETTE.length + 6 }, (_, index) =>
    String(2000 + index),
  );
  const first = getMilestoneColorMap(milestones);
  const second = getMilestoneColorMap(milestones);
  assert.deepEqual(second, first);
  assert.equal(new Set(Object.values(first)).size, milestones.length);
});

test("same milestone list returns the same mapping across calls", () => {
  const milestones = [
    { releaseIdentifier: "WB_2016_R01", releaseDate: "2016-01-01" },
    { releaseIdentifier: "WB_2020_R01", releaseDate: "2020-01-01" },
    { releaseIdentifier: "WB_2026_R01", releaseDate: "2026-01-01" },
  ];
  assert.deepEqual(getMilestoneColorMap(milestones), getMilestoneColorMap(milestones));
});

test("included temporal milestones stop at selected release", () => {
  const milestones = [
    { releaseIdentifier: "WB_2023_R02", releaseDate: "2023-03-15" },
    { releaseIdentifier: "WB_2024_R02", releaseDate: "2024-03-07" },
    { releaseIdentifier: "WB_2025_R03", releaseDate: "2025-03-27" },
    { releaseIdentifier: "WB_2026_R04", releaseDate: "2026-04-30" },
  ];

  assert.deepEqual(getIncludedTemporalMilestones(milestones, "WB_2025_R03"), [
    { releaseIdentifier: "WB_2023_R02", label: "2023 Q1" },
    { releaseIdentifier: "WB_2024_R02", label: "2024 Q1" },
    { releaseIdentifier: "WB_2025_R03", label: "2025 Q1" },
  ]);
});

test("included temporal milestones preserve chronological order independent of input order", () => {
  const milestones = [
    { releaseIdentifier: "WB_2026_R04", releaseDate: "2026-04-30" },
    { releaseIdentifier: "WB_2023_R02", releaseDate: "2023-03-15" },
    { releaseIdentifier: "WB_2025_R03", releaseDate: "2025-03-27" },
    { releaseIdentifier: "WB_2024_R02", releaseDate: "2024-03-07" },
  ];

  assert.deepEqual(getIncludedTemporalMilestones(milestones, "WB_2024_R02"), [
    { releaseIdentifier: "WB_2023_R02", label: "2023 Q1" },
    { releaseIdentifier: "WB_2024_R02", label: "2024 Q1" },
  ]);
});

test("initial cumulative additions plan includes only available non-baseline releases up to selected", () => {
  const milestones = [
    { releaseIdentifier: "WB_2019_R03", releaseDate: "2019-03-13" },
    { releaseIdentifier: "WB_2023_R04", releaseDate: "2023-05-03" },
    { releaseIdentifier: "WB_2026_R05", releaseDate: "2026-04-30" },
  ];

  assert.deepEqual(
    getIncludedAdditionReleasesForCumulativeLayer(milestones, "WB_2026_R05", [
      "WB_2023_R04",
      "WB_2026_R05",
    ]),
    ["WB_2023_R04", "WB_2026_R05"],
  );
});

test("initial cumulative additions plan excludes future releases", () => {
  const milestones = [
    { releaseIdentifier: "WB_2019_R03", releaseDate: "2019-03-13" },
    { releaseIdentifier: "WB_2023_R04", releaseDate: "2023-05-03" },
    { releaseIdentifier: "WB_2026_R05", releaseDate: "2026-04-30" },
  ];

  assert.deepEqual(
    getIncludedAdditionReleasesForCumulativeLayer(milestones, "WB_2023_R04", [
      "WB_2023_R04",
      "WB_2026_R05",
    ]),
    ["WB_2023_R04"],
  );
});

test("initial cumulative additions plan ignores baseline even if metadata says additions exist", () => {
  const milestones = [
    { releaseIdentifier: "WB_2019_R03", releaseDate: "2019-03-13" },
    { releaseIdentifier: "WB_2023_R04", releaseDate: "2023-05-03" },
  ];

  assert.deepEqual(
    getIncludedAdditionReleasesForCumulativeLayer(milestones, "WB_2023_R04", [
      "WB_2019_R03",
      "WB_2023_R04",
    ]),
    ["WB_2023_R04"],
  );
});

test("selected release set returns only the active available release", () => {
  assert.deepEqual(getSelectedReleaseSet("WB_2026_R04", ["WB_2023_R02", "WB_2026_R04"]), ["WB_2026_R04"]);
  assert.deepEqual(getSelectedReleaseSet("WB_2026_R04", ["WB_2023_R02"]), []);
});

test("buffer10m selected mode plans only selected release", () => {
  const milestones = [
    { releaseIdentifier: "WB_2020_R04", releaseDate: "2020-03-23" },
    { releaseIdentifier: "WB_2023_R02", releaseDate: "2023-03-15" },
    { releaseIdentifier: "WB_2024_R02", releaseDate: "2024-03-07" },
    { releaseIdentifier: "WB_2025_R03", releaseDate: "2025-03-27" },
    { releaseIdentifier: "WB_2026_R04", releaseDate: "2026-04-30" },
  ];

  assert.deepEqual(
    getTemporalLayerExpectedReleases({
      layerKey: "buffer10m",
      milestones,
      selectedReleaseIdentifier: "WB_2026_R04",
      availableReleaseIdentifiers: ["WB_2023_R02", "WB_2024_R02", "WB_2025_R03", "WB_2026_R04"],
    }),
    ["WB_2026_R04"],
  );
});

test("buffer15m selected mode plans only selected release", () => {
  const milestones = [
    { releaseIdentifier: "WB_2020_R04", releaseDate: "2020-03-23" },
    { releaseIdentifier: "WB_2023_R02", releaseDate: "2023-03-15" },
    { releaseIdentifier: "WB_2024_R02", releaseDate: "2024-03-07" },
    { releaseIdentifier: "WB_2025_R03", releaseDate: "2025-03-27" },
    { releaseIdentifier: "WB_2026_R04", releaseDate: "2026-04-30" },
  ];

  assert.deepEqual(
    getTemporalLayerExpectedReleases({
      layerKey: "buffer15m",
      milestones,
      selectedReleaseIdentifier: "WB_2026_R04",
      availableReleaseIdentifiers: ["WB_2023_R02", "WB_2024_R02", "WB_2025_R03", "WB_2026_R04"],
    }),
    ["WB_2026_R04"],
  );
});

test("buffer20m selected mode plans only selected release", () => {
  const milestones = [
    { releaseIdentifier: "WB_2020_R04", releaseDate: "2020-03-23" },
    { releaseIdentifier: "WB_2023_R02", releaseDate: "2023-03-15" },
    { releaseIdentifier: "WB_2024_R02", releaseDate: "2024-03-07" },
    { releaseIdentifier: "WB_2025_R03", releaseDate: "2025-03-27" },
    { releaseIdentifier: "WB_2026_R04", releaseDate: "2026-04-30" },
  ];

  assert.deepEqual(
    getTemporalLayerExpectedReleases({
      layerKey: "buffer20m",
      milestones,
      selectedReleaseIdentifier: "WB_2026_R04",
      availableReleaseIdentifiers: ["WB_2023_R02", "WB_2024_R02", "WB_2025_R03", "WB_2026_R04"],
    }),
    ["WB_2026_R04"],
  );
});

test("cumulative buffer modes plan all available non-baseline releases up to selected", () => {
  const milestones = [
    { releaseIdentifier: "WB_2020_R04", releaseDate: "2020-03-23" },
    { releaseIdentifier: "WB_2023_R02", releaseDate: "2023-03-15" },
    { releaseIdentifier: "WB_2024_R02", releaseDate: "2024-03-07" },
    { releaseIdentifier: "WB_2025_R03", releaseDate: "2025-03-27" },
    { releaseIdentifier: "WB_2026_R04", releaseDate: "2026-04-30" },
  ];

  for (const layerKey of [
    "temporalCumulativeBuffer10m",
    "temporalCumulativeBuffer15m",
    "temporalCumulativeBuffer20m",
  ] as const) {
    assert.deepEqual(
      getTemporalLayerExpectedReleases({
        layerKey,
        milestones,
        selectedReleaseIdentifier: "WB_2025_R03",
        availableReleaseIdentifiers: ["WB_2023_R02", "WB_2024_R02", "WB_2025_R03", "WB_2026_R04"],
      }),
      ["WB_2023_R02", "WB_2024_R02", "WB_2025_R03"],
    );
  }
});

test("cumulative buffer modes exclude future and unavailable releases", () => {
  const milestones = [
    { releaseIdentifier: "WB_2020_R04", releaseDate: "2020-03-23" },
    { releaseIdentifier: "WB_2023_R02", releaseDate: "2023-03-15" },
    { releaseIdentifier: "WB_2024_R02", releaseDate: "2024-03-07" },
    { releaseIdentifier: "WB_2025_R03", releaseDate: "2025-03-27" },
    { releaseIdentifier: "WB_2026_R04", releaseDate: "2026-04-30" },
  ];

  assert.deepEqual(
    getTemporalLayerExpectedReleases({
      layerKey: "temporalCumulativeBuffer15m",
      milestones,
      selectedReleaseIdentifier: "WB_2024_R02",
      availableReleaseIdentifiers: ["WB_2023_R02", "WB_2025_R03", "WB_2026_R04"],
    }),
    ["WB_2023_R02"],
  );
});

test("all new buildings remains cumulative while selected additions stays selected", () => {
  const milestones = [
    { releaseIdentifier: "WB_2019_R03", releaseDate: "2019-03-13" },
    { releaseIdentifier: "WB_2023_R04", releaseDate: "2023-05-03" },
    { releaseIdentifier: "WB_2026_R05", releaseDate: "2026-04-30" },
  ];

  assert.deepEqual(
    getTemporalLayerExpectedReleases({
      layerKey: "allNewBuildings",
      milestones,
      selectedReleaseIdentifier: "WB_2026_R05",
      availableReleaseIdentifiers: ["WB_2023_R04", "WB_2026_R05"],
    }),
    ["WB_2023_R04", "WB_2026_R05"],
  );
  assert.deepEqual(
    getTemporalLayerExpectedReleases({
      layerKey: "selectedAdditions",
      milestones,
      selectedReleaseIdentifier: "WB_2026_R05",
      availableReleaseIdentifiers: ["WB_2023_R04", "WB_2026_R05"],
    }),
    ["WB_2026_R05"],
  );
});

test("all new buildings visibility controls every included addition release", () => {
  const included = ["WB_2023_R04", "WB_2026_R05"];

  assert.equal(
    temporalAdditionVisibilityReason({
      releaseIdentifier: "WB_2023_R04",
      selectedReleaseIdentifier: "WB_2026_R05",
      includedAdditionReleaseIdentifiers: included,
      allNewBuildingsEnabled: true,
      selectedAdditionsEnabled: false,
    }),
    "allNewBuildings",
  );
  assert.equal(
    temporalAdditionVisibilityReason({
      releaseIdentifier: "WB_2026_R05",
      selectedReleaseIdentifier: "WB_2026_R05",
      includedAdditionReleaseIdentifiers: included,
      allNewBuildingsEnabled: true,
      selectedAdditionsEnabled: false,
    }),
    "allNewBuildings",
  );
});

test("selected additions visibility controls only the selected release", () => {
  assert.equal(
    temporalAdditionVisibilityReason({
      releaseIdentifier: "WB_2026_R05",
      selectedReleaseIdentifier: "WB_2026_R05",
      includedAdditionReleaseIdentifiers: [],
      allNewBuildingsEnabled: false,
      selectedAdditionsEnabled: true,
    }),
    "selectedMilestoneAdditions",
  );
  assert.equal(
    temporalAdditionVisibilityReason({
      releaseIdentifier: "WB_2023_R04",
      selectedReleaseIdentifier: "WB_2026_R05",
      includedAdditionReleaseIdentifiers: [],
      allNewBuildingsEnabled: false,
      selectedAdditionsEnabled: true,
    }),
    null,
  );
});

test("future additions are not visible through all new buildings", () => {
  assert.equal(
    temporalAdditionVisibilityReason({
      releaseIdentifier: "WB_2026_R05",
      selectedReleaseIdentifier: "WB_2023_R04",
      includedAdditionReleaseIdentifiers: ["WB_2023_R04"],
      allNewBuildingsEnabled: true,
      selectedAdditionsEnabled: false,
    }),
    null,
  );
});

test("all previous additions paint keeps the release color", () => {
  const paint = getTemporalLayerPaint("additions", "#00B050");

  assert.equal(paint.fillPaint["fill-color"], "#00B050");
  assert.equal(paint.fillPaint["fill-opacity"], 0.88);
  assert.equal(paint.fillOpacity, 0.88);
  assert.equal(paint.linePaint["line-color"], "#00B050");
});

test("cumulative buffer paints use release colors at half opacity", () => {
  for (const layerKind of ["cumulativeBuffer10m", "cumulativeBuffer15m", "cumulativeBuffer20m"] as const) {
    const paint = getTemporalLayerPaint(layerKind, "#FFD700");

    assert.equal(paint.fillPaint["fill-color"], "#FFD700");
    assert.equal(paint.fillPaint["fill-opacity"], 0.5);
    assert.equal(paint.fillOpacity, 0.5);
    assert.equal(paint.fillPaint["fill-outline-color"], "rgba(0, 0, 0, 0)");
    assert.equal(paint.linePaint["line-color"], "#FFD700");
    assert.equal(paint.linePaint["line-opacity"], 0);
  }
});

test("cumulative buffer plans keep per-release colors instead of the selected release color", () => {
  const colors = getMilestoneColorMap(TANGER_MILESTONES);
  const selectedReleaseIdentifier = "WB_2026_R04";
  const availableReleaseIdentifiers = ["WB_2023_R02", "WB_2024_R02", "WB_2025_R03", "WB_2026_R04"];

  for (const layerKey of [
    "temporalCumulativeBuffer10m",
    "temporalCumulativeBuffer15m",
    "temporalCumulativeBuffer20m",
  ] as const) {
    const releases = getTemporalLayerExpectedReleases({
      layerKey,
      milestones: TANGER_MILESTONES,
      selectedReleaseIdentifier,
      availableReleaseIdentifiers,
    });
    const paintByRelease = Object.fromEntries(
      releases.map((releaseIdentifier) => [
        releaseIdentifier,
        getTemporalLayerPaint(
          layerKey.replace("temporalC", "c") as "cumulativeBuffer10m" | "cumulativeBuffer15m" | "cumulativeBuffer20m",
          colors[releaseIdentifier],
        ).fillPaint["fill-color"],
      ]),
    );

    assert.deepEqual(paintByRelease, {
      WB_2023_R02: "#00B050",
      WB_2024_R02: "#FFD700",
      WB_2025_R03: "#0066FF",
      WB_2026_R04: "#E31A1C",
    });
  }
});

test("normal buffer paints use release colors", () => {
  for (const layerKind of ["buffer10m", "buffer15m", "buffer20m"] as const) {
    const paint = getTemporalLayerPaint(layerKind, "#0066FF");

    assert.equal(paint.fillPaint["fill-color"], "#0066FF");
    assert.equal(paint.fillPaint["fill-opacity"], 0.5);
    assert.equal(paint.fillOpacity, 0.5);
    assert.equal(paint.fillPaint["fill-outline-color"], "rgba(0, 0, 0, 0)");
    assert.equal(paint.linePaint["line-color"], "#0066FF");
    assert.equal(paint.linePaint["line-opacity"], 0);
  }
});

test("temporal layer labels use baseline to selected release ranges", () => {
  const labels = buildTemporalLayerLabels(
    [
      { releaseIdentifier: "WB_2019_R03", releaseDate: "2019-03-13" },
      { releaseIdentifier: "WB_2023_R04", releaseDate: "2023-05-03" },
      { releaseIdentifier: "WB_2026_R05", releaseDate: "2026-04-30" },
    ],
    "WB_2026_R05",
  );

  assert.equal(labels.allPreviousAdditions, "All new buildings 2019 Q1 -> 2026 Q2");
  assert.equal(labels.selectedAdditions, "Added building in 2026 Q2");
  assert.equal(labels.buffer10m, "Buffer 10m 2026 Q2");
  assert.equal(labels.buffer15m, "Buffer 15m 2026 Q2");
  assert.equal(labels.buffer20m, "Buffer 20m 2026 Q2");
  assert.equal(labels.cumulativeBuffer10m, "Buffer 10m 2019 Q1 -> 2026 Q2");
  assert.equal(labels.cumulativeBuffer15m, "Buffer 15m 2019 Q1 -> 2026 Q2");
  assert.equal(labels.cumulativeBuffer20m, "Buffer 20m 2019 Q1 -> 2026 Q2");
});

test("temporal layer labels parse years from release identifiers", () => {
  const labels = buildTemporalLayerLabels(["WB_2019_R03", "WB_2023_R04"], "WB_2023_R04");

  assert.equal(labels.allPreviousAdditions, "All new buildings 2019 -> 2023");
  assert.equal(labels.selectedAdditions, "Added building in 2023");
  assert.equal(labels.buffer10m, "Buffer 10m 2023");
});

test("temporal layer labels accept translated prefixes and arrow separator", () => {
  const labels = buildTemporalLayerLabels(
    [
      { releaseIdentifier: "WB_2020_R04", releaseDate: "2020-03-23" },
      { releaseIdentifier: "WB_2025_R03", releaseDate: "2025-03-27" },
    ],
    "WB_2025_R03",
    {
      allNewBuildings: "Tous les nouveaux bâtiments",
      addedBuildingIn: "Bâtiments ajoutés en",
      buffer10m: "Buffer 10m",
      buffer15m: "Buffer 15m",
      buffer20m: "Buffer 20m",
      rangeSeparator: "→",
    },
  );

  assert.equal(labels.allPreviousAdditions, "Tous les nouveaux bâtiments 2020 Q1 → 2025 Q1");
  assert.equal(labels.selectedAdditions, "Bâtiments ajoutés en 2025 Q1");
  assert.equal(labels.cumulativeBuffer10m, "Buffer 10m 2020 Q1 → 2025 Q1");
});

test("timeline labels use first comparison label for the baseline row", () => {
  const labels = buildTimelineLabelsFromReleases(["WB_2020_R04", "WB_2023_R02", "WB_2024_R02"], { before: "Avant" });

  assert.deepEqual(
    labels.map((item) => item.label),
    ["Avant 2023", "2023", "2024"],
  );
});

test("timeline labels use release quarters from full dates", () => {
  const labels = buildTimelineLabelsFromReleases(
    [
      { releaseIdentifier: "WB_2025_R02", releaseDate: "26/06/2025" },
      { releaseIdentifier: "WB_2025_R03", releaseDate: "25/09/2025" },
      { releaseIdentifier: "WB_2025_R04", releaseDate: "18/12/2025" },
      { releaseIdentifier: "WB_2026_R01", releaseDate: "25/03/2026" },
    ],
    { before: "Avant" },
  );

  assert.deepEqual(
    labels.map((item) => item.label),
    ["Avant 2025 Q3", "2025 Q3", "2025 Q4", "2026 Q1"],
  );
});

test("timeline labels map quarter boundaries correctly", () => {
  const labels = buildTimelineLabelsFromReleases(
    [
      { releaseIdentifier: "baseline", releaseDate: "2024" },
      { releaseIdentifier: "march", releaseDate: "2025-03-31" },
      { releaseIdentifier: "june", releaseDate: "2025-06-30" },
      { releaseIdentifier: "september", releaseDate: "2025-09-30" },
      { releaseIdentifier: "december", releaseDate: "2025-12-31" },
    ],
    { before: "Before" },
  );

  assert.deepEqual(
    labels.map((item) => item.label),
    ["Before 2025 Q1", "2025 Q1", "2025 Q2", "2025 Q3", "2025 Q4"],
  );
});

test("timeline baseline prefix is localized", () => {
  const milestones = ["WB_2020_R04", "WB_2023_R02"];

  assert.equal(buildTimelineLabelsFromReleases(milestones, { before: "Avant" })[0]?.label, "Avant 2023");
  assert.equal(buildTimelineLabelsFromReleases(milestones, { before: "Before" })[0]?.label, "Before 2023");
});

test("timeline labels treat month-based release dates as quarter labels", () => {
  const labels = buildTimelineLabelsFromReleases(
    [
      { releaseIdentifier: "WB_2025_R02", releaseDate: "2025-06" },
      { releaseIdentifier: "WB_2025_R03", releaseDate: "2025-09" },
    ],
    { before: "Before" },
  );

  assert.deepEqual(
    labels.map((item) => item.label),
    ["Before 2025 Q3", "2025 Q3"],
  );
});
