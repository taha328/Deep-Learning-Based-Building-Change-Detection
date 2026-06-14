from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional


PROJECT_KEY = "building_change/project_id"
RELEASE_KEY = "building_change/releaseIdentifier"
DATE_KEY = "building_change/date"
LAYER_KEY = "building_change/layer_key"
MILESTONE_GROUP_KEY = "building_change/milestone_group"
DEFAULT_VISIBILITY_KEY = "building_change/default_visibility"
REMEMBERED_VISIBILITY_KEY = "building_change/remembered_visibility"

REFERENCE_LAYER_KEY = "reference_imagery"
ADDITIONS_LAYER_KEY = "additions"
CUMULATIVE_GROWTH_LAYER_KEY = "cumulative_growth"
BUFFER_10_LAYER_KEY = "cumulative_buffer_10m"
BUFFER_15_LAYER_KEY = "cumulative_buffer_15m"
BUFFER_20_LAYER_KEY = "cumulative_buffer_20m"
DIAGNOSTICS_LAYER_KEY = "addition_candidate_diagnostics"

DEFAULT_VISIBLE_LAYER_KEYS = {
    ADDITIONS_LAYER_KEY,
    BUFFER_10_LAYER_KEY,
}


def milestone_release_identifier(milestone: Dict[str, Any]) -> str:
    return str(milestone.get("release_identifier") or milestone.get("releaseIdentifier") or "")


def milestone_date(milestone: Dict[str, Any]) -> str:
    value = milestone.get("release_date") or milestone.get("date") or milestone.get("label")
    if value:
        return str(value)[:10]
    return ""


def milestone_display_label(milestone: Dict[str, Any], index: int) -> str:
    date_value = milestone_date(milestone)
    if date_value:
        return date_value
    return milestone_release_identifier(milestone) or f"milestone-{index}"


def sorted_milestones_newest_first(milestones: Iterable[Any]) -> List[Dict[str, Any]]:
    typed = [milestone for milestone in milestones if isinstance(milestone, dict)]
    return sorted(
        typed,
        key=lambda milestone: (
            milestone_date(milestone),
            milestone_release_identifier(milestone),
        ),
        reverse=True,
    )


def sorted_milestones_oldest_first(milestones: Iterable[Any]) -> List[Dict[str, Any]]:
    return list(reversed(sorted_milestones_newest_first(milestones)))


def default_layer_visibility(layer_key: str, *, is_baseline: bool = False) -> bool:
    if is_baseline and layer_key != REFERENCE_LAYER_KEY:
        return False
    if layer_key == REFERENCE_LAYER_KEY:
        return True
    return layer_key in DEFAULT_VISIBLE_LAYER_KEYS


def is_baseline_milestone(milestone: Dict[str, Any], ordered_oldest_first: List[Dict[str, Any]]) -> bool:
    release_id = milestone_release_identifier(milestone)
    if not ordered_oldest_first:
        return False
    first = ordered_oldest_first[0]
    if release_id:
        return milestone_release_identifier(first) == release_id
    return first is milestone


def select_active_milestone(
    project: Dict[str, Any],
    *,
    requested_release_identifier: Optional[str] = None,
    requested_display_date: Optional[str] = None,
) -> tuple[Optional[Dict[str, Any]], str]:
    milestones = sorted_milestones_newest_first(project.get("milestones") or [])
    if not milestones:
        return None, "no_milestones"
    requested_release_identifier = (requested_release_identifier or "").strip()
    requested_display_date = (requested_display_date or "").strip()[:10]
    if requested_release_identifier:
        for milestone in milestones:
            if milestone_release_identifier(milestone) == requested_release_identifier:
                return milestone, "requested"
    if requested_display_date:
        for milestone in milestones:
            if milestone_date(milestone) == requested_display_date:
                return milestone, "requested"
    milestones_oldest_first = sorted_milestones_oldest_first(milestones)
    for milestone in milestones:
        if not is_baseline_milestone(milestone, milestones_oldest_first):
            return milestone, "latest_non_baseline"
    return milestones[0], "latest_available"


class TemporalLayerController:
    def __init__(self, project_id: str) -> None:
        self.project_id = project_id
        self._syncing = False
        self._connected = False
        self._visibility_state: Dict[str, bool] = {}

    def connect(self) -> None:
        if self._connected:
            return
        try:
            from qgis.core import QgsProject

            QgsProject.instance().layerTreeRoot().visibilityChanged.connect(self._on_visibility_changed)
            self._connected = True
        except Exception:
            self._connected = False

    def disconnect(self) -> None:
        if not self._connected:
            return
        try:
            from qgis.core import QgsProject

            QgsProject.instance().layerTreeRoot().visibilityChanged.disconnect(self._on_visibility_changed)
        except Exception:
            pass
        self._connected = False

    def tag_group(self, group: Any, *, release_identifier: str, release_date: str) -> None:
        self._set_node_property(group, PROJECT_KEY, self.project_id)
        self._set_node_property(group, RELEASE_KEY, release_identifier)
        self._set_node_property(group, DATE_KEY, release_date)
        self._set_node_property(group, MILESTONE_GROUP_KEY, f"{self.project_id}:{release_identifier}")

    def tag_layer(self, layer: Any, *, release_identifier: str, release_date: str, layer_key: str, default_visible: bool) -> None:
        layer.setCustomProperty(PROJECT_KEY, self.project_id)
        layer.setCustomProperty(RELEASE_KEY, release_identifier)
        layer.setCustomProperty(DATE_KEY, release_date)
        layer.setCustomProperty(LAYER_KEY, layer_key)
        layer.setCustomProperty(MILESTONE_GROUP_KEY, f"{self.project_id}:{release_identifier}")
        layer.setCustomProperty(DEFAULT_VISIBILITY_KEY, "1" if default_visible else "0")

    def initialize_node(self, node: Any, *, visible: bool) -> None:
        self._set_node_property(node, DEFAULT_VISIBILITY_KEY, "1" if visible else "0")
        self._set_node_property(node, REMEMBERED_VISIBILITY_KEY, "1" if visible else "0")
        node.setItemVisibilityChecked(visible)

    def initialize_milestone_group(self, group: Any, *, visible: bool, expanded: bool) -> None:
        self._syncing = True
        try:
            group.setItemVisibilityChecked(visible)
            try:
                group.setExpanded(expanded)
            except Exception:
                pass
        finally:
            self._syncing = False

    def _on_visibility_changed(self, node: Any) -> None:
        if self._syncing:
            return
        try:
            project_id = self._node_project_id(node)
            if project_id != self.project_id:
                return
            release_id = self._node_release_identifier(node)
            if not release_id:
                return
            layer_key = self._node_layer_key(node)
            if layer_key:
                self._remember_layer_visibility(release_id, layer_key, node)
                return
            self._sync_milestone_visibility(release_id, self._node_visible(node))
        except Exception:
            return

    def _sync_milestone_visibility(self, release_id: str, visible: bool) -> None:
        self._syncing = True
        try:
            for node in self._matching_layer_nodes(release_id):
                layer_key = self._node_layer_key(node)
                if visible:
                    node.setItemVisibilityChecked(self._remembered_visibility(release_id, layer_key, node))
                else:
                    self._remember_layer_visibility(release_id, layer_key, node)
                    node.setItemVisibilityChecked(False)
        finally:
            self._syncing = False

    def _remember_layer_visibility(self, release_id: str, layer_key: str, node: Any) -> None:
        visible = self._node_visible(node)
        self._visibility_state[self._visibility_key(release_id, layer_key)] = visible
        self._set_node_property(node, REMEMBERED_VISIBILITY_KEY, "1" if visible else "0")

    def _matching_layer_nodes(self, release_id: str) -> List[Any]:
        try:
            from qgis.core import QgsLayerTreeLayer, QgsProject

            root = QgsProject.instance().layerTreeRoot()
            return [
                node
                for node in root.findLayers()
                if isinstance(node, QgsLayerTreeLayer)
                and self._node_project_id(node) == self.project_id
                and self._node_release_identifier(node) == release_id
            ]
        except Exception:
            return []

    def _remembered_visibility(self, release_id: str, layer_key: str, node: Any) -> bool:
        key = self._visibility_key(release_id, layer_key)
        if key in self._visibility_state:
            return self._visibility_state[key]
        value = self._node_property(node, REMEMBERED_VISIBILITY_KEY)
        if value in ("0", "1"):
            return value == "1"
        value = self._node_default_visibility(node)
        if value in ("0", "1"):
            return value == "1"
        return default_layer_visibility(layer_key)

    def _visibility_key(self, release_id: str, layer_key: str) -> str:
        return f"{self.project_id}:{release_id}:{layer_key}"

    def _node_layer_key(self, node: Any) -> str:
        layer = getattr(node, "layer", lambda: None)()
        if layer is not None:
            return str(layer.customProperty(LAYER_KEY, "") or "")
        return str(self._node_property(node, LAYER_KEY) or "")

    def _node_release_identifier(self, node: Any) -> str:
        layer = getattr(node, "layer", lambda: None)()
        if layer is not None:
            return str(layer.customProperty(RELEASE_KEY, "") or "")
        return str(self._node_property(node, RELEASE_KEY) or "")

    def _node_project_id(self, node: Any) -> str:
        layer = getattr(node, "layer", lambda: None)()
        if layer is not None:
            return str(layer.customProperty(PROJECT_KEY, "") or "")
        return str(self._node_property(node, PROJECT_KEY) or "")

    def _node_visible(self, node: Any) -> bool:
        try:
            return bool(node.itemVisibilityChecked())
        except Exception:
            return False

    def _node_default_visibility(self, node: Any) -> Optional[str]:
        layer = getattr(node, "layer", lambda: None)()
        if layer is not None:
            value = layer.customProperty(DEFAULT_VISIBILITY_KEY, "")
            return str(value) if value is not None else None
        return self._node_property(node, DEFAULT_VISIBILITY_KEY)

    def _set_node_property(self, node: Any, key: str, value: str) -> None:
        try:
            node.setCustomProperty(key, value)
        except Exception:
            pass

    def _node_property(self, node: Any, key: str) -> Optional[str]:
        try:
            value = node.customProperty(key, "")
        except Exception:
            return None
        return str(value) if value is not None else None


_CONTROLLERS: Dict[str, TemporalLayerController] = {}


def install_temporal_layer_controller(project_id: str) -> TemporalLayerController:
    existing = _CONTROLLERS.get(project_id)
    if existing is not None:
        existing.disconnect()
    controller = TemporalLayerController(project_id)
    controller.connect()
    _CONTROLLERS[project_id] = controller
    return controller
