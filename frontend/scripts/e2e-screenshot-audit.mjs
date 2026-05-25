import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(__dirname, "..", "..");
const python = path.join(repoRoot, "backend", ".venv", "bin", "python");

if (!existsSync(python)) {
  console.error(`Python runtime not found: ${python}`);
  process.exit(1);
}

const auditScript = String.raw`
import json
import re
from pathlib import Path
from PIL import Image

repo = Path(__file__).resolve().parents[2] if "__file__" in globals() else Path.cwd().parents[0]
root = Path("${repoRoot}")
project_id = "temporal-tanger-mpe3tcih-qq1c73"
project_json = root / "backend" / "runtime_cache" / "temporal_projects" / project_id / "project.json"
screens = root / "backend" / "runtime_cache" / "debug_screenshots" / project_id
expected = {
    "screenshot_01_WB_2020_R04_reference_only.png": None,
    "screenshot_02_WB_2023_R02_reference_additions_buffer10m_visible.png": 100,
    "screenshot_03_WB_2024_R02_reference_additions_buffer10m_visible.png": 100,
    "screenshot_04_WB_2025_R03_reference_additions_buffer10m_visible.png": 100,
    "screenshot_05_WB_2026_R04_reference_additions_buffer10m_visible.png": 100,
    "screenshot_06_layer_panel_active_release.png": None,
}
payload = json.loads(project_json.read_text())
missing = []
for milestone in payload.get("milestones", []):
    release = milestone.get("release_identifier")
    if release not in {"WB_2020_R04", "WB_2023_R02", "WB_2024_R02", "WB_2025_R03", "WB_2026_R04"}:
        continue
    imagery = milestone.get("reference_imagery") or {}
    template = imagery.get("tiles_url_template") or ""
    if not imagery.get("raster_bounds_wgs84") or imagery.get("minzoom") is None or imagery.get("maxzoom") is None:
        missing.append(f"{release}: incomplete reference metadata")
    if not re.search(r"\{z\}/\{x\}/\{y\}", template):
        missing.append(f"{release}: raw tile placeholders missing")
    if re.search(r"%7Bz%7D|%7Bx%7D|%7By%7D", template, flags=re.I):
        missing.append(f"{release}: encoded tile placeholders found")

for name, red_threshold in expected.items():
    path = screens / name
    if not path.is_file():
        missing.append(f"{name}: screenshot missing")
        continue
    image = Image.open(path).convert("RGBA")
    if image.width < 640 or image.height < 480:
        missing.append(f"{name}: screenshot dimensions too small {image.size}")
        continue
    # Audit map area, excluding the left timeline and right layer panel.
    left = int(image.width * 0.36)
    right = int(image.width * 0.76)
    overlay_pixels = 0
    nontransparent = 0
    for r, g, b, a in image.crop((left, 0, right, image.height)).getdata():
        if a > 150:
            nontransparent += 1
        is_red_overlay = r > 130 and g < 110 and b < 110
        is_blue_overlay = b > 130 and r < 120 and g < 150
        is_purple_overlay = r > 90 and b > 120 and g < 130 and abs(r - b) < 110
        if a > 150 and (is_red_overlay or is_blue_overlay or is_purple_overlay):
            overlay_pixels += 1
    if nontransparent < 10_000:
        missing.append(f"{name}: map crop appears blank")
    if red_threshold is not None and overlay_pixels <= red_threshold:
        missing.append(f"{name}: overlay pixels {overlay_pixels} <= threshold {red_threshold}")
    print(f"{name}: overlay_pixels={overlay_pixels} nontransparent={nontransparent}")

if missing:
    print("\n".join(missing))
    raise SystemExit(1)
print("temporal-reference-template-buffer-rendering e2e audit passed")
`;

const result = spawnSync(python, ["-"], {
  cwd: repoRoot,
  input: auditScript,
  encoding: "utf8",
  stdio: ["pipe", "inherit", "inherit"],
});

if (result.status === 0) {
  await fetch("http://127.0.0.1:8000/api/dev/client-log", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      event: "TEMPORAL_SCREENSHOT_PIXEL_AUDIT_PASSED",
      payload: {
        projectId: "temporal-tanger-mpe3tcih-qq1c73",
        screenshotCount: 6,
        audit: "red_overlay_pixels_and_reference_metadata",
      },
      timestamp: new Date().toISOString(),
      source: "e2e-screenshot-audit",
    }),
  }).catch(() => {
    // Backend may be stopped when this script is used as an offline artifact check.
  });
}

process.exit(result.status ?? 1);
