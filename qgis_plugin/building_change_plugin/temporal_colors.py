from __future__ import annotations

from dataclasses import dataclass
from math import sqrt
from typing import Any, Dict, Iterable, List, Optional, Tuple


TEMPORAL_MILESTONE_COLOR_PALETTE = (
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
)

LATEST_MILESTONE_COLOR = TEMPORAL_MILESTONE_COLOR_PALETTE[0]
MIN_GENERATED_RGB_DISTANCE = 90
GOLDEN_ANGLE = 137.508

ADDITIONS_ARTIFACT_KEYS = {"additions"}
BUFFER_10_ARTIFACT_KEYS = {"building_change_buffer_10m", "buffer_10m", "buffer10m", "cumulative_buffer_10m"}
BUFFER_15_ARTIFACT_KEYS = {"building_change_buffer_15m", "buffer_15m", "buffer15m", "cumulative_buffer_15m"}
BUFFER_20_ARTIFACT_KEYS = {"building_change_buffer_20m", "buffer_20m", "buffer20m", "cumulative_buffer_20m"}
BUFFER_ARTIFACT_KEYS = BUFFER_10_ARTIFACT_KEYS | BUFFER_15_ARTIFACT_KEYS | BUFFER_20_ARTIFACT_KEYS


@dataclass(frozen=True)
class TemporalQgisStyle:
    fill_color: str
    outline_color: str
    fill_opacity: float
    outline_opacity: float
    outline_width: str


def release_identifier(milestone: Any) -> str:
    if isinstance(milestone, str):
        return milestone
    if not isinstance(milestone, dict):
        return ""
    return str(
        milestone.get("releaseIdentifier")
        or milestone.get("release_identifier")
        or milestone.get("identifier")
        or milestone.get("id")
        or ""
    )


def milestone_date_value(milestone: Any) -> Tuple[int, str]:
    date_value = None
    if isinstance(milestone, dict):
        date_value = milestone.get("releaseDate") or milestone.get("release_date") or milestone.get("date")
    if isinstance(date_value, str) and date_value:
        digits = "".join(ch for ch in date_value if ch.isdigit())
        if len(digits) >= 8:
            return int(digits[:8]), release_identifier(milestone)
    identifier = release_identifier(milestone)
    for index in range(len(identifier) - 3):
        candidate = identifier[index : index + 4]
        if candidate.isdigit() and 1900 <= int(candidate) <= 2099:
            return int(candidate) * 10000 + 101, identifier
    return -1, identifier


def milestone_year_label(milestone: Any) -> str:
    if isinstance(milestone, dict):
        date_value = milestone.get("releaseDate") or milestone.get("release_date") or milestone.get("date")
        if isinstance(date_value, str) and len(date_value) >= 4 and date_value[:4].isdigit():
            return date_value[:4]
    identifier = release_identifier(milestone)
    for index in range(len(identifier) - 3):
        candidate = identifier[index : index + 4]
        if candidate.isdigit() and 1900 <= int(candidate) <= 2099:
            return candidate
    return identifier


def temporal_range_label(milestones: Iterable[Any], active_release_identifier: str) -> str:
    sorted_milestones: List[Any] = sorted(list(milestones), key=milestone_date_value)
    if not sorted_milestones:
        return milestone_year_label(active_release_identifier)
    first_label = milestone_year_label(sorted_milestones[0])
    active_milestone = None
    for milestone in sorted_milestones:
        if release_identifier(milestone) == active_release_identifier:
            active_milestone = milestone
            break
    if active_milestone is None:
        active_milestone = sorted_milestones[-1]
    active_label = milestone_year_label(active_milestone)
    return first_label if first_label == active_label else "%s -> %s" % (first_label, active_label)


def additions_label(release_or_milestone: Any) -> str:
    label = milestone_year_label(release_or_milestone)
    return "Added building in %s" % label if label else "Added building"


def buffer_label(distance_m: int, release_or_milestone: Any) -> str:
    label = milestone_year_label(release_or_milestone)
    return "Buffer %sm %s" % (distance_m, label) if label else "Buffer %sm" % distance_m


def cumulative_buffer_label(distance_m: int, milestones: Iterable[Any], active_release_identifier: str) -> str:
    label = temporal_range_label(milestones, active_release_identifier)
    return "Buffer %sm %s" % (distance_m, label) if label else "Buffer %sm" % distance_m


def all_previous_additions_label(milestones: Iterable[Any], active_release_identifier: str) -> str:
    label = temporal_range_label(milestones, active_release_identifier)
    return "All new buildings %s" % label if label else "All new buildings"


def hex_to_rgb(color: str) -> Tuple[int, int, int]:
    value = color.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def rgb_distance(left: str, right: str) -> float:
    lr, lg, lb = hex_to_rgb(left)
    rr, rg, rb = hex_to_rgb(right)
    return sqrt((lr - rr) ** 2 + (lg - rg) ** 2 + (lb - rb) ** 2)


def hsl_to_hex(hue: float, saturation: float, lightness: float) -> str:
    h = ((hue % 360) + 360) % 360 / 360
    s = saturation / 100
    l = lightness / 100

    def hue_to_rgb(p: float, q: float, t: float) -> float:
        adjusted = t
        if adjusted < 0:
            adjusted += 1
        if adjusted > 1:
            adjusted -= 1
        if adjusted < 1 / 6:
            return p + (q - p) * 6 * adjusted
        if adjusted < 1 / 2:
            return q
        if adjusted < 2 / 3:
            return p + (q - p) * (2 / 3 - adjusted) * 6
        return p

    q = l * (1 + s) if l < 0.5 else l + s - l * s
    p = 2 * l - q
    rgb = (hue_to_rgb(p, q, h + 1 / 3), hue_to_rgb(p, q, h), hue_to_rgb(p, q, h - 1 / 3))
    return "#" + "".join(f"{round(channel * 255):02X}" for channel in rgb)


def is_red_like_hue(hue: float) -> bool:
    normalized = ((hue % 360) + 360) % 360
    return normalized <= 25 or normalized >= 335


def generate_dark_milestone_color(index: int, previous_color: str, used_colors: set[str]) -> str:
    for attempt in range(48):
        hue = (211 + (index + attempt * 3) * GOLDEN_ANGLE) % 360
        if is_red_like_hue(hue):
            continue
        saturation = 68 + ((index + attempt) % 4) * 6
        lightness = 26 + ((index + attempt * 2) % 7) * 2
        color = hsl_to_hex(hue, saturation, min(lightness, 38))
        if color not in used_colors and rgb_distance(color, previous_color) >= MIN_GENERATED_RGB_DISTANCE:
            return color
    for attempt in range(0, 360, 17):
        hue = (89 + index * 53 + attempt) % 360
        if is_red_like_hue(hue):
            continue
        color = hsl_to_hex(hue, 72, 31)
        if color not in used_colors:
            return color
    return "#7F1D1D"


def get_milestone_color_map(milestones: Iterable[Any]) -> Dict[str, str]:
    unique: Dict[str, Any] = {}
    for milestone in milestones:
        identifier = release_identifier(milestone)
        if identifier and identifier not in unique:
            unique[identifier] = milestone
    sorted_items = sorted(unique.items(), key=lambda item: milestone_date_value(item[1]))
    newest_first = list(reversed(sorted_items))
    used_colors: set[str] = set()
    color_by_release: Dict[str, str] = {}
    for index, (identifier, _milestone) in enumerate(newest_first):
        previous_color = color_by_release[newest_first[index - 1][0]] if index > 0 else LATEST_MILESTONE_COLOR
        color = (
            TEMPORAL_MILESTONE_COLOR_PALETTE[index]
            if index < len(TEMPORAL_MILESTONE_COLOR_PALETTE)
            else generate_dark_milestone_color(index, previous_color, used_colors)
        )
        used_colors.add(color)
        color_by_release[identifier] = color
    return color_by_release


def darker_outline(color: str) -> str:
    red, green, blue = hex_to_rgb(color)
    return f"#{max(0, int(red * 0.55)):02X}{max(0, int(green * 0.55)):02X}{max(0, int(blue * 0.55)):02X}"


def temporal_style_for_artifact(
    release_identifier_value: str,
    artifact_key: str,
    milestone_colors: Optional[Dict[str, str]] = None,
) -> TemporalQgisStyle:
    color = (milestone_colors or {}).get(release_identifier_value) or LATEST_MILESTONE_COLOR
    normalized_key = artifact_key.strip().lower()
    if normalized_key in BUFFER_ARTIFACT_KEYS:
        return TemporalQgisStyle(
            fill_color=color,
            outline_color=color,
            fill_opacity=1.0,
            outline_opacity=0.0,
            outline_width="0",
        )
    if normalized_key in ADDITIONS_ARTIFACT_KEYS:
        return TemporalQgisStyle(
            fill_color=color,
            outline_color=darker_outline(color),
            fill_opacity=1.0,
            outline_opacity=1.0,
            outline_width="0.9",
        )
    return TemporalQgisStyle(
        fill_color=color,
        outline_color=darker_outline(color),
        fill_opacity=1.0,
        outline_opacity=1.0,
        outline_width="0.8",
    )
