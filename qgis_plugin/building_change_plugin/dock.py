from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from qgis.PyQt.QtCore import QCoreApplication, Qt, QTimer
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QDockWidget,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qgis.core import (
    Qgis,
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsGeometry,
    QgsMapLayerType,
    QgsMessageLog,
    QgsProject,
    QgsVectorLayer,
    QgsWkbTypes,
)

from .aoi import PolygonCaptureTool
from .api_client import BackendClient
from .layer_loader import load_temporal_project_layers
from .models import (
    build_temporal_project_payload,
    clean_temporal_project_summaries,
    normalize_aoi_geojson_geometry,
    project_display_label,
    release_display_label,
    release_date_text,
    release_identifier,
    sorted_unique_releases,
)
from .settings import PluginSettings, load_plugin_settings, save_plugin_settings
from .styles import style_aoi
from .tasks import ApiTask


LOG_CATEGORY = "Building Change"
AOI_LAYER_NAME = "Building Change AOI"

# ── Dark palette — tuned to sit inside QGIS's dark theme ─────────────────────
_BG          = "#1a1a1a"   # dock root background
_SURFACE     = "#222222"   # card / group box fill
_SURFACE_ALT = "#1a1a1a"   # alternating row tint
_BORDER      = "#3a3a3a"   # default border
_BORDER_LT   = "#4a4a4a"   # slightly lighter border
_INPUT_BG    = "#2a2a2a"   # text fields / combos
_BTN_BG      = "#2e2e2e"   # neutral button fill
_BTN_HOVER   = "#383838"
_BTN_PRESS   = "#181818"
_TEXT        = "#ffffff"   # primary text — pure white
_TEXT_SUB    = "#c0c0c0"   # secondary text — bright
_TEXT_DIS    = "#505050"   # disabled text

# Accent — matches QGIS highlight blue
_ACCENT      = "#4ec9b0"   # teal accent (QGIS-ish)
_ACCENT_DIM  = "#1a3a35"   # dim accent background
_BLUE        = "#569cd6"   # secondary accent
_BLUE_DIM    = "#1c2d3d"

# Status
_OK_BG   = "#1a3328";  _OK_FG   = "#4ec9b0"
_ERR_BG  = "#3a1919";  _ERR_FG  = "#f48771"
_WARN_BG = "#332b14";  _WARN_FG = "#dcdcaa"
_IDLE_BG = "#252526";  _IDLE_FG = "#858585"

# ── Reusable style builders ───────────────────────────────────────────────────

def _chip(bg: str, fg: str) -> str:
    return (
        f"background:{bg}; color:{fg}; border-radius:10px;"
        f"padding:3px 11px; font-size:11px; font-weight:600;"
        f"border:1px solid {fg}44;"
    )

_CHIP_NEUTRAL  = _chip(_IDLE_BG, _IDLE_FG)
_CHIP_OK       = _chip(_OK_BG,   _OK_FG)
_CHIP_ERROR    = _chip(_ERR_BG,  _ERR_FG)
_CHIP_CHECKING = _chip(_WARN_BG, _WARN_FG)

def _aoi_pill(bg: str, fg: str, border: str) -> str:
    return (
        f"color:{fg}; font-size:11px; font-weight:500;"
        f"padding:7px 10px; background:{bg};"
        f"border:1px solid {border}; border-radius:5px;"
    )

_AOI_EMPTY = _aoi_pill(_SURFACE,  _TEXT_SUB, _BORDER)
_AOI_SET   = _aoi_pill(_OK_BG,    _OK_FG,    "#2a5a4a")
_AOI_ERROR = _aoi_pill(_ERR_BG,   _ERR_FG,   "#5a2a2a")

_BTN_DRAW_ACTIVE = (
    f"QPushButton{{background:{_WARN_BG}; border:1px solid {_WARN_FG}55;"
    f"color:{_WARN_FG}; font-weight:600; border-radius:5px;"
    f"padding:6px 14px; min-height:28px;}}"
    f"QPushButton:hover{{background:#3d3318;}}"
)

# ── Group box style variants ──────────────────────────────────────────────────

def _group_style(bg: str = _SURFACE, border: str = _BORDER,
                 title_fg: str = "#ffffff") -> str:
    return (
        f"QGroupBox{{background:{bg}; border:1px solid {border};"
        f"border-radius:5px; margin-top:12px;"
        f"padding:10px 8px 8px 8px;"
        f"font-size:10px; font-weight:700; color:{title_fg};"
        f"letter-spacing:0.8px; text-transform:uppercase;}}"
        f"QGroupBox::title{{subcontrol-origin:margin;"
        f"subcontrol-position:top left; left:8px;"
        f"padding:0 5px; background:{bg}; color:{title_fg};}}"
    )

_GRP_DEFAULT  = _group_style()
_GRP_ACTIVE   = _group_style(_SURFACE, _ACCENT + "66", _ACCENT)


def _h_divider() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.HLine)
    line.setFrameShadow(QFrame.Plain)
    line.setStyleSheet(f"background:{_BORDER}; max-height:1px; border:none;")
    return line


# ─────────────────────────────────────────────────────────────────────────────

class BuildingChangeDockWidget(QDockWidget):

    def __init__(self, iface, plugin_dir: Path) -> None:
        super().__init__(iface.mainWindow())
        self.iface = iface
        self.plugin_dir = plugin_dir
        self.settings_state = load_plugin_settings()
        self.releases: List[Dict[str, Any]] = []
        self.release_lookup: Dict[str, Dict[str, Any]] = {}
        self.selected_release_ids: List[str] = []
        self.projects: List[Dict[str, Any]] = []
        self.current_project: Optional[Dict[str, Any]] = None
        self.current_aoi: Optional[Dict[str, Any]] = None
        self.pending_project_for_run: Optional[Dict[str, Any]] = None
        self.capture_tool: Optional[PolygonCaptureTool] = None
        self.active_task: Optional[ApiTask] = None
        self._drawing_active: bool = False
        self._build_ui()
        self._apply_settings()
        self.refresh_polygon_layers()
        self._sync_actions()

    def tr(self, text: str) -> str:
        return QCoreApplication.translate("BuildingChangeDockWidget", text)

    # ─────────────────────────── UI CONSTRUCTION ──────────────────────────────

    def _build_ui(self) -> None:
        self.setObjectName("BuildingChangeDockWidget")
        self.setWindowTitle(self.tr("Building Change Detection"))
        self.setMinimumWidth(460)

        # Scroll container — ensures progress bar is always reachable
        self._scroll_area = QScrollArea(self)
        self._scroll_area.setWidgetResizable(True)
        self._scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._scroll_area.setFrameShape(QFrame.NoFrame)
        self._scroll_area.setStyleSheet(
            f"QScrollArea {{ background:{_BG}; border:none; }}"
            f"QScrollBar:vertical {{ background:{_SURFACE}; width:7px; "
            f"border-radius:3px; margin:0; }}"
            f"QScrollBar::handle:vertical {{ background:{_BORDER_LT}; "
            f"border-radius:3px; min-height:24px; }}"
            f"QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical "
            f"{{ height:0; }}"
        )

        root = QWidget()
        root.setObjectName("RootWidget")
        outer = QVBoxLayout(root)
        outer.setContentsMargins(8, 8, 8, 10)
        outer.setSpacing(5)
        outer.addWidget(self._build_header())
        outer.addWidget(self._build_project_group())
        outer.addWidget(self._build_aoi_group())
        outer.addWidget(self._build_release_group())
        outer.addWidget(self._build_progress_group())
        outer.addWidget(self._build_results_group())
        outer.addStretch(1)

        root.setStyleSheet(self._stylesheet())
        self._scroll_area.setWidget(root)
        self.setWidget(self._scroll_area)

    # ── Master stylesheet ─────────────────────────────────────────────────────

    def _stylesheet(self) -> str:
        return f"""
        QWidget#RootWidget {{
            background-color: {_BG};
        }}

        /* ── Group boxes ───────────────────────────────────────────────── */
        QGroupBox {{
            background: {_SURFACE};
            border: 1px solid {_BORDER};
            border-radius: 5px;
            margin-top: 12px;
            padding: 10px 8px 8px 8px;
            font-size: 10px;
            font-weight: 700;
            color: #ffffff;
            letter-spacing: 0.8px;
            text-transform: uppercase;
        }}
        QGroupBox::title {{
            subcontrol-origin: margin;
            subcontrol-position: top left;
            left: 8px;
            padding: 0 5px;
            background: {_SURFACE};
            color: #ffffff;
        }}

        /* ── Labels ────────────────────────────────────────────────────── */
        QLabel {{
            color: #ffffff;
            font-size: 12px;
            background: transparent;
        }}
        QLabel#PluginTitle {{
            font-size: 13px;
            font-weight: 700;
            color: #ffffff;
            letter-spacing: -0.1px;
        }}
        QLabel#PluginSub {{
            font-size: 10px;
            color: {_TEXT_SUB};
        }}
        QLabel#FormLabel {{
            font-size: 11px;
            color: #ffffff;
            font-weight: 600;
        }}
        QLabel#HintLabel {{
            font-size: 10px;
            color: {_TEXT_SUB};
            padding: 0 1px;
        }}
        QLabel#UrlLabel {{
            font-size: 10px;
            color: {_TEXT_SUB};
            font-weight: 600;
            letter-spacing: 0.4px;
            text-transform: uppercase;
        }}

        /* ── Text inputs ───────────────────────────────────────────────── */
        QLineEdit {{
            border: 1px solid {_BORDER};
            border-radius: 4px;
            padding: 4px 8px;
            background: {_INPUT_BG};
            color: #ffffff;
            font-size: 11px;
            selection-background-color: {_BLUE_DIM};
        }}
        QLineEdit:focus {{
            border-color: {_ACCENT};
            background: #2e2e2e;
        }}
        QLineEdit:disabled {{
            background: {_BTN_BG};
            color: {_TEXT_DIS};
            border-color: {_BORDER};
        }}

        /* ── Combo boxes ───────────────────────────────────────────────── */
        QComboBox {{
            border: 1px solid {_BORDER};
            border-radius: 4px;
            padding: 4px 8px;
            background: {_INPUT_BG};
            color: #ffffff;
            font-size: 11px;
            min-height: 24px;
        }}
        QComboBox:focus {{
            border-color: {_ACCENT};
        }}
        QComboBox:disabled {{
            background: {_BTN_BG};
            color: {_TEXT_DIS};
            border-color: {_BORDER};
        }}
        QComboBox::drop-down {{
            border: none;
            width: 18px;
        }}
        QComboBox QAbstractItemView {{
            border: 1px solid {_BORDER_LT};
            background: {_INPUT_BG};
            color: #ffffff;
            outline: none;
            selection-background-color: {_BLUE_DIM};
            selection-color: {_BLUE};
        }}
        QComboBox QAbstractItemView::item {{
            padding: 4px 8px;
            min-height: 20px;
        }}

        /* ── Buttons — base ────────────────────────────────────────────── */
        QPushButton {{
            border: 1px solid {_BORDER_LT};
            border-radius: 4px;
            padding: 5px 12px;
            background: {_BTN_BG};
            color: #ffffff;
            font-size: 11px;
            font-weight: 600;
            min-height: 26px;
        }}
        QPushButton:hover   {{ background: {_BTN_HOVER}; border-color: #585858; }}
        QPushButton:pressed {{ background: {_BTN_PRESS}; }}
        QPushButton:disabled {{
            color: {_TEXT_DIS};
            border-color: {_BORDER};
            background: {_BTN_BG};
        }}

        /* ── Buttons — primary (teal) ──────────────────────────────────── */
        QPushButton#PrimaryBtn {{
            background: #1a3830;
            border-color: {_ACCENT}66;
            color: {_ACCENT};
            font-weight: 700;
        }}
        QPushButton#PrimaryBtn:hover {{
            background: #1f4038;
            border-color: {_ACCENT}aa;
        }}
        QPushButton#PrimaryBtn:pressed {{
            background: #122620;
        }}
        QPushButton#PrimaryBtn:disabled {{
            background: {_SURFACE};
            border-color: {_BORDER};
            color: {_TEXT_DIS};
        }}

        /* ── Buttons — danger (red-tinted) ─────────────────────────────── */
        QPushButton#DangerBtn {{
            background: {_ERR_BG};
            border-color: {_ERR_FG}44;
            color: {_ERR_FG};
        }}
        QPushButton#DangerBtn:hover {{
            background: #451f1f;
            border-color: {_ERR_FG}88;
        }}
        QPushButton#DangerBtn:disabled {{
            background: {_BTN_BG};
            border-color: {_BORDER};
            color: {_TEXT_DIS};
        }}

        /* ── List widget ───────────────────────────────────────────────── */
        QListWidget {{
            border: 1px solid {_BORDER};
            border-radius: 4px;
            background: {_INPUT_BG};
            font-size: 11px;
            color: #ffffff;
            outline: none;
        }}
        QListWidget::item {{
            padding: 5px 8px;
            border-bottom: 1px solid {_BORDER}55;
        }}
        QListWidget::item:last-child {{
            border-bottom: none;
        }}
        QListWidget::item:alternate {{
            background: {_SURFACE_ALT};
        }}
        QListWidget::item:selected {{
            background: {_ACCENT_DIM};
            color: {_ACCENT};
            border-bottom: 1px solid {_ACCENT}33;
        }}
        QListWidget::item:hover:!selected {{
            background: {_BTN_HOVER};
        }}

        /* ── Progress bar ──────────────────────────────────────────────── */
        QProgressBar {{
            border: none;
            border-radius: 4px;
            background: {_BTN_BG};
            text-align: center;
            font-size: 10px;
            font-weight: 700;
            color: #ffffff;
            min-height: 18px;
            max-height: 18px;
        }}
        QProgressBar::chunk {{
            background: qlineargradient(
                x1:0, y1:0, x2:1, y2:0,
                stop:0 #1d6b5a,
                stop:1 {_ACCENT}
            );
            border-radius: 4px;
        }}
        """

    # ── Header ────────────────────────────────────────────────────────────────

    def _build_header(self) -> QWidget:
        container = QWidget()
        container.setObjectName("HeaderWidget")
        container.setStyleSheet(
            f"QWidget#HeaderWidget {{"
            f"background:{_SURFACE}; border:1px solid {_BORDER};"
            f"border-radius:5px;}}"
        )
        vbox = QVBoxLayout(container)
        vbox.setContentsMargins(8, 7, 8, 7)
        vbox.setSpacing(5)

        # Title row: name + status chip
        title_row = QHBoxLayout()
        title_row.setSpacing(6)
        title = QLabel(self.tr("Building Change Detection"))
        title.setObjectName("PluginTitle")
        self.status_chip = QLabel(self.tr("Not verified"))
        self.status_chip.setStyleSheet(_CHIP_NEUTRAL)
        self.status_chip.setAlignment(Qt.Alignment(Qt.AlignRight | Qt.AlignVCenter))
        title_row.addWidget(title)
        title_row.addStretch(1)
        title_row.addWidget(self.status_chip)
        vbox.addLayout(title_row)

        # URL row: compact inline
        url_row = QHBoxLayout()
        url_row.setSpacing(5)
        url_lbl = QLabel(self.tr("URL"))
        url_lbl.setObjectName("UrlLabel")
        url_lbl.setFixedWidth(24)
        self.backend_url_edit = QLineEdit()
        self.backend_url_edit.setPlaceholderText("http://localhost:8000")
        self.backend_url_edit.textChanged.connect(lambda _: self._backend_url_changed())
        self.health_button = QPushButton(self.tr("Check"))
        self.health_button.setObjectName("PrimaryBtn")
        self.health_button.setFixedWidth(58)
        self.health_button.setFixedHeight(24)
        self.health_button.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.health_button.clicked.connect(self.check_backend_health)
        url_row.addWidget(url_lbl)
        url_row.addWidget(self.backend_url_edit, 1)
        url_row.addWidget(self.health_button)
        vbox.addLayout(url_row)

        return container

    # ── Connection (folded into header — kept for API compatibility) ──────────

    def _build_connection_group(self) -> QWidget:
        """Stub: connection UI is now embedded in the header widget."""
        w = QWidget()
        w.setVisible(False)
        return w

    # ── Project ───────────────────────────────────────────────────────────────

    def _build_project_group(self) -> QWidget:
        group = QGroupBox(self.tr("Project"))
        layout = QVBoxLayout(group)
        layout.setSpacing(5)
        layout.setContentsMargins(8, 8, 8, 8)

        # Existing project row
        ex_lbl = QLabel(self.tr("Existing"))
        ex_lbl.setObjectName("FormLabel")
        ex_lbl.setFixedWidth(52)
        self.project_combo = QComboBox()
        self.project_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.project_combo.currentIndexChanged.connect(lambda _: self._sync_actions())
        self.refresh_projects_button = QPushButton(self.tr("Refresh"))
        self.refresh_projects_button.setFixedWidth(64)
        self.refresh_projects_button.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.refresh_projects_button.clicked.connect(self.refresh_projects)
        ex_row = QHBoxLayout()
        ex_row.setSpacing(5)
        ex_row.addWidget(ex_lbl)
        ex_row.addWidget(self.project_combo, 1)
        ex_row.addWidget(self.refresh_projects_button)
        layout.addLayout(ex_row)

        # Name row
        nm_lbl = QLabel(self.tr("Name"))
        nm_lbl.setObjectName("FormLabel")
        nm_lbl.setFixedWidth(52)
        self.project_name_edit = QLineEdit()
        self.project_name_edit.setPlaceholderText(self.tr("New project name…"))
        nm_row = QHBoxLayout()
        nm_row.setSpacing(5)
        nm_row.addWidget(nm_lbl)
        nm_row.addWidget(self.project_name_edit, 1)
        layout.addLayout(nm_row)

        # Action row
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.load_project_button = QPushButton(self.tr("Load Project"))
        self.load_project_button.setObjectName("PrimaryBtn")
        self.load_project_button.setFixedWidth(100)
        self.load_project_button.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.load_project_button.clicked.connect(self.load_selected_project)
        btn_row.addWidget(self.load_project_button)
        layout.addLayout(btn_row)

        return group

    # ── AOI ───────────────────────────────────────────────────────────────────

    def _build_aoi_group(self) -> QWidget:
        group = QGroupBox(self.tr("Area of Interest"))
        layout = QVBoxLayout(group)
        layout.setSpacing(5)
        layout.setContentsMargins(8, 8, 8, 8)

        # Layer picker row
        ly_lbl = QLabel(self.tr("Layer"))
        ly_lbl.setObjectName("FormLabel")
        ly_lbl.setFixedWidth(38)
        self.layer_combo = QComboBox()
        self.layer_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.layer_combo.currentIndexChanged.connect(lambda _: self._sync_actions())
        self.refresh_layers_button = QPushButton(self.tr("Refresh"))
        self.refresh_layers_button.setFixedWidth(64)
        self.refresh_layers_button.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.refresh_layers_button.clicked.connect(self.refresh_polygon_layers)
        ly_row = QHBoxLayout()
        ly_row.setSpacing(5)
        ly_row.addWidget(ly_lbl)
        ly_row.addWidget(self.layer_combo, 1)
        ly_row.addWidget(self.refresh_layers_button)
        layout.addLayout(ly_row)

        # Action buttons — equal width
        self.use_selection_button = QPushButton(self.tr("Use Selected Feature"))
        self.use_selection_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.use_selection_button.clicked.connect(self.use_selected_feature)
        self.draw_button = QPushButton(self.tr("Draw on Map"))
        self.draw_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.draw_button.clicked.connect(self._toggle_draw)
        act_row = QHBoxLayout()
        act_row.setSpacing(5)
        act_row.addWidget(self.use_selection_button, 1)
        act_row.addWidget(self.draw_button, 1)
        layout.addLayout(act_row)

        # Status pill
        self.aoi_status_label = QLabel(self.tr("No area of interest defined"))
        self.aoi_status_label.setWordWrap(True)
        self.aoi_status_label.setStyleSheet(_AOI_EMPTY)
        layout.addWidget(self.aoi_status_label)

        # Drawing hint — hidden until active
        self.draw_hint_label = QLabel(
            self.tr(
                "Left-click to place vertices  ·  Right-click or Enter to finish  ·  Esc to cancel"
            )
        )
        self.draw_hint_label.setWordWrap(True)
        self.draw_hint_label.setStyleSheet(
            f"font-size:10px; color:{_WARN_FG}; background:{_WARN_BG};"
            f"border:1px solid {_WARN_FG}44; border-radius:4px; padding:5px 8px;"
        )
        self.draw_hint_label.setVisible(False)
        layout.addWidget(self.draw_hint_label)

        return group

    # ── Wayback Archives ──────────────────────────────────────────────────────

    def _build_release_group(self) -> QWidget:
        group = QGroupBox(self.tr("Wayback Archives"))
        layout = QVBoxLayout(group)
        layout.setSpacing(5)
        layout.setContentsMargins(8, 8, 8, 8)

        # Picker row
        self.release_combo = QComboBox()
        self.release_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.release_combo.currentIndexChanged.connect(lambda _: self._sync_actions())
        self.add_release_button = QPushButton(self.tr("Add"))
        self.add_release_button.setObjectName("PrimaryBtn")
        self.add_release_button.setFixedWidth(52)
        self.add_release_button.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.add_release_button.clicked.connect(self.add_selected_release)
        pick_row = QHBoxLayout()
        pick_row.setSpacing(5)
        pick_row.addWidget(self.release_combo, 1)
        pick_row.addWidget(self.add_release_button)
        layout.addLayout(pick_row)

        # Count hint
        self.release_count_label = QLabel(
            self.tr("No archives selected  ·  minimum 2 required")
        )
        self.release_count_label.setObjectName("HintLabel")
        layout.addWidget(self.release_count_label)

        # Selected list
        self.selected_releases_list = QListWidget()
        self.selected_releases_list.setAlternatingRowColors(True)
        self.selected_releases_list.setMinimumHeight(60)
        self.selected_releases_list.setMaximumHeight(100)
        self.selected_releases_list.currentItemChanged.connect(
            lambda *_: self._sync_actions()
        )
        layout.addWidget(self.selected_releases_list)

        # Thin divider before action row
        layout.addWidget(_h_divider())

        # Action row — equal proportional widths, no clipping
        act_row = QHBoxLayout()
        act_row.setSpacing(5)
        self.remove_release_button = QPushButton(self.tr("Remove"))
        self.remove_release_button.setObjectName("DangerBtn")
        self.remove_release_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.remove_release_button.clicked.connect(self.remove_selected_release)
        self.validate_button = QPushButton(self.tr("Validate"))
        self.validate_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.validate_button.clicked.connect(self.validate_current_project)
        self.run_button = QPushButton(self.tr("Run Analysis"))
        self.run_button.setObjectName("PrimaryBtn")
        self.run_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.run_button.clicked.connect(self.run_temporal_job)
        act_row.addWidget(self.remove_release_button, 2)
        act_row.addWidget(self.validate_button, 2)
        act_row.addWidget(self.run_button, 3)
        layout.addLayout(act_row)

        return group

    # ── Progress ──────────────────────────────────────────────────────────────

    def _build_progress_group(self) -> QWidget:
        # Store reference so we can scroll to it and restyle it
        self._progress_group = QGroupBox(self.tr("Progress"))
        self._progress_group.setStyleSheet(_GRP_DEFAULT)
        layout = QVBoxLayout(self._progress_group)
        layout.setSpacing(5)
        layout.setContentsMargins(8, 8, 8, 8)

        self.progress_bar = QProgressBar()
        self.progress_bar.setFormat("%p%")
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        layout.addWidget(self.progress_bar)

        self.progress_label = QLabel(self.tr("Idle"))
        self.progress_label.setWordWrap(True)
        self.progress_label.setObjectName("HintLabel")
        layout.addWidget(self.progress_label)

        return self._progress_group

    # ── Results ───────────────────────────────────────────────────────────────

    def _build_results_group(self) -> QWidget:
        group = QGroupBox(self.tr("Results"))
        layout = QVBoxLayout(group)
        layout.setSpacing(5)
        layout.setContentsMargins(8, 8, 8, 8)

        self.load_layers_button = QPushButton(self.tr("Load Layers"))
        self.load_layers_button.setObjectName("PrimaryBtn")
        self.load_layers_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.load_layers_button.clicked.connect(self.load_current_project_layers)
        self.excel_button = QPushButton(self.tr("Export Excel"))
        self.excel_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.excel_button.clicked.connect(lambda: self.download_export("results.xlsx"))
        self.kml_button = QPushButton(self.tr("Export KML"))
        self.kml_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.kml_button.clicked.connect(lambda: self.download_export("results.kml"))

        btn_row = QHBoxLayout()
        btn_row.setSpacing(5)
        btn_row.addWidget(self.load_layers_button, 2)
        btn_row.addWidget(self.excel_button, 1)
        btn_row.addWidget(self.kml_button, 1)
        layout.addLayout(btn_row)

        self.results_label = QLabel(self.tr("No layers loaded"))
        self.results_label.setObjectName("HintLabel")
        layout.addWidget(self.results_label)
        return group

    # ─────────────────────── DRAW-MODE TOGGLE ────────────────────────────────

    def _toggle_draw(self) -> None:
        if self._drawing_active:
            self._cancel_draw()
        else:
            self.start_capture()

    def _cancel_draw(self) -> None:
        if self.capture_tool is not None:
            self.capture_tool.reset()
        self._set_draw_inactive()
        self._warn(self.tr("AOI drawing cancelled."))

    def _set_draw_active(self) -> None:
        self._drawing_active = True
        self.draw_button.setText(self.tr("Cancel Drawing"))
        self.draw_button.setStyleSheet(_BTN_DRAW_ACTIVE)
        self.draw_hint_label.setVisible(True)

    def _set_draw_inactive(self) -> None:
        self._drawing_active = False
        self.draw_button.setText(self.tr("Draw on Map"))
        self.draw_button.setStyleSheet("")
        self.draw_hint_label.setVisible(False)

    # ─────────────────── PROGRESS VISIBILITY HELPERS ─────────────────────────

    def _activate_progress(self) -> None:
        """Highlight the progress group and scroll it into view."""
        self._progress_group.setStyleSheet(_GRP_ACTIVE)
        self.progress_bar.setValue(0)
        self.progress_label.setText(self.tr("Starting…"))
        # Small delay lets the layout settle before scrolling
        QTimer.singleShot(80, self._scroll_to_progress)

    def _deactivate_progress(self) -> None:
        self._progress_group.setStyleSheet(_GRP_DEFAULT)

    def _scroll_to_progress(self) -> None:
        self._scroll_area.ensureWidgetVisible(self._progress_group, 0, 30)

    # ─────────────────────────── SETTINGS / INIT ─────────────────────────────

    def _apply_settings(self) -> None:
        self.backend_url_edit.setText(self.settings_state.backend_base_url)

    def _backend_url_changed(self) -> None:
        self._sync_actions()
        QTimer.singleShot(350, self.check_backend_health)

    def _client(self) -> BackendClient:
        self.settings_state.backend_base_url = (
            self.backend_url_edit.text().strip().rstrip("/")
        )
        save_plugin_settings(self.settings_state)
        return BackendClient(self.settings_state.backend_base_url)

    # ─────────────────────────── TASK INFRASTRUCTURE ─────────────────────────

    def _start_task(
        self,
        description: str,
        operation: str,
        on_result: Callable[[object], None],
        on_error: Optional[Callable[[str], None]] = None,
        **kwargs: Any,
    ) -> None:
        if self.active_task is not None:
            self._warn(self.tr("A task is already running."))
            return
        task = ApiTask(description, self._client(), operation, **kwargs)
        task.signals.resultReady.connect(
            lambda result: self._task_finished(result, on_result)
        )
        task.signals.errorRaised.connect(
            lambda message: self._task_failed(message, on_error)
        )
        task.signals.progressChanged.connect(self._progress_changed)
        self.active_task = task
        self._activate_progress()
        self._sync_actions()
        QgsApplication.taskManager().addTask(task)

    def _task_finished(
        self, result: object, on_result: Callable[[object], None]
    ) -> None:
        self.active_task = None
        on_result(result)
        self._deactivate_progress()
        self._sync_actions()

    def _task_failed(
        self,
        message: str,
        on_error: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.active_task = None
        if on_error is not None:
            on_error(message)
        self.progress_label.setText(message)
        self.progress_label.setStyleSheet(
            f"font-size:11px; color:{_ERR_FG}; padding:0 1px;"
        )
        if "Backend" in message or "backend" in message:
            self.status_chip.setText(self.tr("Unavailable"))
            self.status_chip.setStyleSheet(_CHIP_ERROR)
        self._error(message)
        self._deactivate_progress()
        self._sync_actions()

    def _progress_changed(self, progress: float, message: str) -> None:
        self.progress_bar.setValue(int(progress * 100))
        label = self._pipeline_stage_label(message)
        self.progress_label.setText(label)
        self.progress_label.setStyleSheet(
            f"font-size:11px; color:{_TEXT_SUB}; padding:0 1px;"
        )

    def _pipeline_stage_label(self, message: str) -> str:
        lowered = message.lower()
        if "download" in lowered or "tuile" in lowered or "imagery" in lowered:
            return self.tr("Downloading imagery…")
        if "mosaic" in lowered:
            return self.tr("Generating mosaic…")
        if "inference" in lowered or "bandon" in lowered:
            return self.tr("Running inference…")
        if "post" in lowered or "vector" in lowered:
            return self.tr("Post-processing & vectorization…")
        if "export" in lowered:
            return self.tr("Exporting results…")
        return message

    # ─────────────────────────── BACKEND HEALTH ──────────────────────────────

    def check_backend_health(self) -> None:
        self.status_chip.setText(self.tr("Checking…"))
        self.status_chip.setStyleSheet(_CHIP_CHECKING)
        self._start_task(self.tr("Checking backend"), "health", self._health_loaded)

    def _health_loaded(self, payload: object) -> None:
        self.status_chip.setText(self.tr("Connected"))
        self.status_chip.setStyleSheet(_CHIP_OK)
        self._log(f"Backend health: {payload}")
        if not self.projects:
            self.refresh_projects()

    # ─────────────────────────── RELEASES ────────────────────────────────────

    def refresh_releases(self) -> None:
        self._start_task(
            self.tr("Loading releases"), "releases", self._releases_loaded
        )

    def add_selected_release(self) -> None:
        release_id = self.release_combo.currentData()
        if not isinstance(release_id, str) or not release_id:
            self._warn(self.tr("Select a Wayback archive first."))
            return
        if release_id in self.selected_release_ids:
            self._warn(self.tr("This archive is already in the list."))
            return
        self.selected_release_ids.append(release_id)
        self._refresh_selected_releases()

    def remove_selected_release(self) -> None:
        item = self.selected_releases_list.currentItem()
        if item is None:
            self._warn(self.tr("Select an archive to remove."))
            return
        release_id = item.data(Qt.UserRole)
        self.selected_release_ids = [
            i for i in self.selected_release_ids if i != release_id
        ]
        self._refresh_selected_releases()

    def _refresh_selected_releases(self) -> None:
        releases = [
            self.release_lookup[rid]
            for rid in self.selected_release_ids
            if rid in self.release_lookup
        ]
        releases = sorted_unique_releases(releases)
        self.selected_release_ids = [release_identifier(r) for r in releases]
        self.selected_releases_list.clear()
        for release in releases:
            item = QListWidgetItem(release_display_label(release))
            item.setData(Qt.UserRole, release_identifier(release))
            self.selected_releases_list.addItem(item)
        # Update count hint text
        n = len(self.selected_release_ids)
        if n == 0:
            self.release_count_label.setText(
                self.tr("No archives selected  ·  minimum 2 required")
            )
        elif n == 1:
            self.release_count_label.setText(
                self.tr("1 archive selected  ·  add 1 more to enable Run")
            )
        else:
            self.release_count_label.setText(
                self.tr(f"{n} archives selected  ·  ready to run")
            )
        self._sync_actions()

    def _releases_loaded(self, payload: object) -> None:
        self.releases = sorted_unique_releases(
            payload if isinstance(payload, list) else []
        )
        self.release_lookup = {}
        self.release_combo.clear()
        for release in self.releases:
            if not isinstance(release, dict):
                continue
            identifier = release_identifier(release)
            if not identifier:
                continue
            self.release_lookup[identifier] = release
            self.release_combo.addItem(release_display_label(release), identifier)
        self._log(f"Loaded {len(self.release_lookup)} Wayback releases.")
        self._sync_actions()

    # ─────────────────────────── PROJECTS ────────────────────────────────────

    def refresh_projects(self) -> None:
        self._start_task(
            self.tr("Loading projects"), "projects", self._projects_loaded
        )

    def _projects_loaded(self, payload: object) -> None:
        self.projects = clean_temporal_project_summaries(payload)
        self.project_combo.clear()
        for project in self.projects:
            project_id = str(
                project.get("project_id") or project.get("id") or ""
            )
            if project_id:
                self.project_combo.addItem(project_display_label(project), project_id)
        self._log(f"Loaded {len(self.projects)} temporal projects.")
        if not self.release_lookup:
            self.refresh_releases()
        self._sync_actions()

    def load_selected_project(self) -> None:
        project_id = self.project_combo.currentData()
        if not project_id:
            self._warn(self.tr("Select a project first."))
            return
        self._log(
            f"BUILDING_CHANGE_PLUGIN project_load_start project_id={project_id}"
        )
        self._start_task(
            self.tr("Loading project"),
            "get_project",
            self._project_loaded,
            on_error=lambda error: self._log(
                f"BUILDING_CHANGE_PLUGIN project_load_failed "
                f"project_id={project_id} error={error}"
            ),
            project_id=project_id,
        )

    def _project_loaded(self, payload: object) -> None:
        if not isinstance(payload, dict):
            self._error(self.tr("Invalid project response."))
            return
        self.current_project = payload
        self.settings_state.last_project_id = str(payload.get("project_id") or "")
        save_plugin_settings(self.settings_state)
        self.project_name_edit.setText(str(payload.get("name") or ""))
        milestones = payload.get("milestones") or []
        if isinstance(milestones, list):
            self.selected_release_ids = [
                str(m.get("release_identifier"))
                for m in milestones
                if isinstance(m, dict) and m.get("release_identifier")
            ]
            self._refresh_selected_releases()
        self._log(
            f"BUILDING_CHANGE_PLUGIN project_load_success "
            f"project_id={payload.get('project_id')}"
        )
        self._sync_actions()

    # ─────────────────────────── AOI ─────────────────────────────────────────

    def refresh_polygon_layers(self) -> None:
        self.layer_combo.clear()
        for layer in QgsProject.instance().mapLayers().values():
            if layer.type() != QgsMapLayerType.VectorLayer:
                continue
            if not isinstance(layer, QgsVectorLayer):
                continue
            if (
                QgsWkbTypes.geometryType(layer.wkbType())
                != QgsWkbTypes.PolygonGeometry
            ):
                continue
            self.layer_combo.addItem(layer.name(), layer.id())
        self._sync_actions()

    def use_selected_feature(self) -> None:
        layer = self._selected_polygon_layer()
        if layer is None:
            self._warn(self.tr("Select a polygon layer first."))
            return
        feature = next(layer.getSelectedFeatures(), None)
        if feature is None:
            self._warn(self.tr("Select one polygon feature in the layer."))
            return
        self._set_aoi_from_geometry(feature.geometry(), layer.crs())

    def start_capture(self) -> None:
        canvas = self.iface.mapCanvas()
        self.capture_tool = PolygonCaptureTool(canvas)
        self.capture_tool.geometryCaptured.connect(
            lambda geometry: self._on_capture_finished(
                geometry, canvas.mapSettings().destinationCrs()
            )
        )
        self.capture_tool.captureCancelled.connect(self._on_capture_cancelled)
        canvas.setMapTool(self.capture_tool)
        self._set_draw_active()
        self._log(
            "Draw AOI: left-click to add vertices, "
            "right-click or Enter to finish, Esc to cancel."
        )

    def _on_capture_finished(self, geometry: object, crs: object) -> None:
        self._set_draw_inactive()
        self._set_aoi_from_geometry(geometry, crs)

    def _on_capture_cancelled(self) -> None:
        self._set_draw_inactive()
        self._warn(self.tr("AOI drawing cancelled."))

    def _set_aoi_from_geometry(
        self,
        geometry: QgsGeometry,
        source_crs: QgsCoordinateReferenceSystem,
    ) -> None:
        if geometry.isEmpty():
            self._warn(self.tr("AOI geometry is empty."))
            return
        target_crs = QgsCoordinateReferenceSystem("EPSG:4326")
        transformed = QgsGeometry(geometry)
        if source_crs != target_crs:
            transform = QgsCoordinateTransform(
                source_crs, target_crs, QgsProject.instance()
            )
            transformed.transform(transform)
        try:
            self.current_aoi = normalize_aoi_geojson_geometry(
                json.loads(transformed.asJson())
            )
        except ValueError as exc:
            self.current_aoi = None
            self.aoi_status_label.setText(self.tr("Invalid AOI geometry."))
            self.aoi_status_label.setStyleSheet(_AOI_ERROR)
            self._error(str(exc))
            self._sync_actions()
            return
        self._upsert_aoi_layer(transformed, str(self.current_aoi["type"]))
        self.aoi_status_label.setText(
            self.tr(
                f"AOI defined  ·  {self.current_aoi['type']}  ·  EPSG:4326"
            )
        )
        self.aoi_status_label.setStyleSheet(_AOI_SET)
        self._log(
            f"BUILDING_CHANGE_PLUGIN_AOI_PAYLOAD type={self.current_aoi['type']}"
        )
        self._sync_actions()

    def _upsert_aoi_layer(
        self, geometry: QgsGeometry, geometry_type: str
    ) -> None:
        existing_layers = [
            layer
            for layer in QgsProject.instance().mapLayersByName(AOI_LAYER_NAME)
            if isinstance(layer, QgsVectorLayer)
        ]
        layer = existing_layers[0] if existing_layers else None
        layer_matches_type = layer is not None and (
            (
                geometry_type == "MultiPolygon"
                and QgsWkbTypes.isMultiType(layer.wkbType())
            )
            or geometry_type == "Polygon"
        )
        if (
            layer is not None
            and layer.isValid()
            and layer.crs().authid() == "EPSG:4326"
            and layer_matches_type
        ):
            provider = layer.dataProvider()
            feature_ids = [f.id() for f in layer.getFeatures()]
            if feature_ids:
                provider.deleteFeatures(feature_ids)
        else:
            for stale in existing_layers:
                QgsProject.instance().removeMapLayer(stale.id())
            uri_type = (
                "MultiPolygon" if geometry_type == "MultiPolygon" else "Polygon"
            )
            layer = QgsVectorLayer(
                f"{uri_type}?crs=EPSG:4326", AOI_LAYER_NAME, "memory"
            )
            QgsProject.instance().addMapLayer(layer, False)
            root = QgsProject.instance().layerTreeRoot()
            group = root.findGroup("Building Change") or root.insertGroup(
                0, "Building Change"
            )
            group.insertLayer(0, layer)
            style_aoi(layer)
        feature = QgsFeature()
        feature.setGeometry(geometry)
        layer.dataProvider().addFeatures([feature])
        layer.updateExtents()
        layer.triggerRepaint()
        self.iface.mapCanvas().refresh()

    def _selected_polygon_layer(self) -> Optional[QgsVectorLayer]:
        layer_id = self.layer_combo.currentData()
        layer = QgsProject.instance().mapLayer(layer_id) if layer_id else None
        return layer if isinstance(layer, QgsVectorLayer) else None

    # ─────────────────────────── VALIDATION & RUN ────────────────────────────

    def _build_project_from_controls(self) -> Optional[Dict[str, Any]]:
        if self.current_aoi is None:
            self._warn(self.tr("Define an area of interest first."))
            return None
        try:
            aoi_geojson = normalize_aoi_geojson_geometry(self.current_aoi)
        except ValueError as exc:
            self._warn(str(exc))
            return None
        if len(self.selected_release_ids) < 2:
            self._warn(self.tr("Add at least two Wayback archives to compare."))
            return None
        releases = [
            self.release_lookup[rid]
            for rid in self.selected_release_ids
            if rid in self.release_lookup
        ]
        releases = sorted_unique_releases(releases)
        if len(releases) < 2:
            self._warn(
                self.tr("Refresh Wayback archives and select at least two.")
            )
            return None
        self._log(f"BUILDING_CHANGE_PLUGIN_AOI_PAYLOAD type={aoi_geojson['type']}")
        return build_temporal_project_payload(
            name=self.project_name_edit.text(),
            aoi_geojson=aoi_geojson,
            releases=releases,
        )

    def validate_current_project(self) -> None:
        project = self._build_project_from_controls()
        if project is None:
            return
        self._start_task(
            self.tr("Validating project"),
            "validate_project",
            self._validation_loaded,
            project=project,
        )

    def _validation_loaded(self, payload: object) -> None:
        if not isinstance(payload, dict):
            self._error(self.tr("Invalid validation response."))
            return
        blocking = payload.get("blocking_errors") or []
        warnings = payload.get("warnings") or []
        if blocking:
            self._error("\n".join(str(i) for i in blocking))
            return
        self._log(f"Validation OK. Warnings: {len(warnings)}")

    def run_temporal_job(self) -> None:
        project = self._build_project_from_controls()
        if project is None:
            return
        self.pending_project_for_run = project
        self._start_task(
            self.tr("Saving project"),
            "save_project",
            self._project_saved_for_run,
            on_error=lambda _: setattr(self, "pending_project_for_run", None),
            project=project,
        )

    def _project_saved_for_run(self, payload: object) -> None:
        if not isinstance(payload, dict):
            self._error(self.tr("Invalid saved project response."))
            return
        project = self.pending_project_for_run or {}
        self.pending_project_for_run = None
        project_id = str(
            payload.get("project_id") or project.get("project_id") or ""
        )
        if not project_id:
            self._error(
                self.tr("Saved project response did not include a project id.")
            )
            return
        project["project_id"] = project_id
        if payload.get("updated_at"):
            project["updated_at"] = payload.get("updated_at")
        if payload.get("download_bundle_path"):
            project["download_bundle_path"] = payload.get("download_bundle_path")
        self._start_run(project)

    def _start_run(self, project: Dict[str, Any]) -> None:
        self.current_project = project
        self.settings_state.last_project_id = str(project.get("project_id") or "")
        save_plugin_settings(self.settings_state)
        self._start_task(
            self.tr("Running temporal project"),
            "run_project",
            self._run_loaded,
            project_id=str(project["project_id"]),
        )

    def _run_loaded(self, payload: object) -> None:
        project = (
            payload.get("project")
            if isinstance(payload, dict)
            and isinstance(payload.get("project"), dict)
            else payload
        )
        if not isinstance(project, dict):
            self._error(self.tr("Invalid run response."))
            return
        self.current_project = project
        self.progress_bar.setValue(100)
        self.progress_label.setText(self.tr("Analysis complete."))
        self.progress_label.setStyleSheet(
            f"font-size:11px; color:{_OK_FG}; padding:0 1px;"
        )
        self._log(f"Run complete for project {project.get('project_id')}.")
        self.load_current_project_layers()

    # ─────────────────────────── RESULTS ─────────────────────────────────────

    def load_current_project_layers(self) -> None:
        project_id = self._current_project_id()
        if project_id and not self._has_hydrated_project_layers(
            self.current_project
        ):
            self._start_task(
                self.tr("Loading project"),
                "get_project",
                self._load_layers_from_project,
                project_id=project_id,
            )
            return
        if self.current_project is None:
            self._warn(self.tr("No project loaded."))
            return
        self._load_layers_from_project(self.current_project)

    def _load_layers_from_project(self, payload: object) -> None:
        if not isinstance(payload, dict):
            self._error(self.tr("Invalid project response."))
            return
        self.current_project = payload
        project_id = str(payload.get("project_id") or "")
        self._activate_progress()
        self.progress_label.setText(self.tr("Preparing QGIS layers…"))
        self._log(f"QGIS_LOAD_LAYERS_STAGE projectId={project_id} stage=load_project_payload")
        try:
            added = load_temporal_project_layers(
                payload,
                client=self._client(),
                output_dir=Path(self.settings_state.output_dir),
                active_release_identifier=self._active_project_milestone_release_id(payload),
                active_display_date=self._active_project_milestone_display_date(payload),
            )
        except Exception as exc:  # noqa: BLE001
            self._log(f"QGIS_LOAD_LAYERS_FAILED projectId={project_id} error={exc}")
            self._error(str(exc))
            self._deactivate_progress()
            self._sync_actions()
            return
        finally:
            self._sync_actions()
        count = len(added)
        self.progress_bar.setValue(100)
        self.progress_label.setText(self.tr("Layers loaded."))
        self.results_label.setText(
            self.tr(f"{count} layer{'s' if count != 1 else ''} loaded · defaults: additions + 10 m buffer")
        )
        milestone_count = len(payload.get("milestones") or [])
        self._log(f"QGIS_LOAD_LAYERS_DONE projectId={project_id} milestoneCount={milestone_count} layerCount={count}")
        self._deactivate_progress()
        self._log(f"Loaded {milestone_count} milestones, {count} QGIS layers. Default visibility: additions + 10 m buffer.")

    def _active_project_milestone_release_id(self, payload: Dict[str, Any]) -> str:
        item = self.selected_releases_list.currentItem()
        if item is not None:
            release_id = item.data(Qt.UserRole)
            if isinstance(release_id, str) and release_id:
                return release_id
        milestones = payload.get("milestones")
        if isinstance(milestones, list):
            release_ids = {
                str(milestone.get("release_identifier") or milestone.get("releaseIdentifier"))
                for milestone in milestones
                if isinstance(milestone, dict) and (milestone.get("release_identifier") or milestone.get("releaseIdentifier"))
            }
            if len(self.selected_release_ids) == 1 and self.selected_release_ids[0] in release_ids:
                return self.selected_release_ids[0]
        return ""

    def _active_project_milestone_display_date(self, payload: Dict[str, Any]) -> str:
        item = self.selected_releases_list.currentItem()
        if item is not None:
            text = item.text()
            if len(text) >= 10 and text[4:5] == "-" and text[7:8] == "-":
                return text[:10]
        return ""

    def download_export(self, export_name: str) -> None:
        project_id = self._current_project_id()
        if not project_id:
            self._warn(self.tr("No project loaded."))
            return
        suffix = "xlsx" if export_name.endswith(".xlsx") else "kml"
        target, _ = QFileDialog.getSaveFileName(
            self,
            self.tr("Save export"),
            str(Path(self.settings_state.output_dir) / export_name),
            f"*.{suffix}",
        )
        if not target:
            return
        self.settings_state.output_dir = str(Path(target).parent)
        save_plugin_settings(self.settings_state)
        self._start_task(
            self.tr("Downloading export"),
            "download_export",
            lambda path: self._log(f"Export saved: {path}"),
            project_id=project_id,
            export_name=export_name,
            target_path=target,
        )

    # ─────────────────────────── STATE HELPERS ───────────────────────────────

    def _current_project_id(self) -> str:
        if isinstance(self.current_project, dict) and self.current_project.get(
            "project_id"
        ):
            return str(self.current_project["project_id"])
        return self.settings_state.last_project_id

    def _project_has_outputs(self) -> bool:
        project = self.current_project
        if not isinstance(project, dict):
            return False
        for milestone in project.get("milestones") or []:
            if not isinstance(milestone, dict):
                continue
            if isinstance(milestone.get("reference_imagery"), dict):
                return True
            for key in (
                "additions_geojson",
                "automated_additions_geojson",
                "automated_building_blocks_geojson",
                "effective_building_blocks_geojson",
                "effective_footprint_geojson",
                "cumulative_union_geojson",
                "cumulative_growth_blocks_geojson",
                "automated_candidate_footprint_geojson",
            ):
                if isinstance(milestone.get(key), dict):
                    return True
            if (
                isinstance(milestone.get("buffer_layers_geojson"), dict)
                and milestone.get("buffer_layers_geojson")
            ):
                return True
            artifacts = milestone.get("artifacts")
            if isinstance(artifacts, list) and artifacts:
                return True
        return False

    def _has_hydrated_project_layers(
        self, project: Optional[Dict[str, Any]]
    ) -> bool:
        if not isinstance(project, dict):
            return False
        milestones = project.get("milestones")
        if not isinstance(milestones, list) or not milestones:
            return False
        for milestone in milestones:
            if isinstance(milestone, dict) and (
                isinstance(milestone.get("reference_imagery"), dict)
                or isinstance(milestone.get("artifacts"), list)
                or isinstance(milestone.get("additions_geojson"), dict)
            ):
                return True
        return False

    def _sync_actions(self) -> None:
        busy = self.active_task is not None
        backend_ready = bool(self.backend_url_edit.text().strip())
        has_project_choice = bool(self.project_combo.currentData())
        has_layer_choice = bool(self.layer_combo.currentData())
        has_aoi = (
            isinstance(self.current_aoi, dict)
            and self.current_aoi.get("type") in {"Polygon", "MultiPolygon"}
        )
        has_release_choice = isinstance(
            self.release_combo.currentData(), str
        ) and bool(self.release_combo.currentData())
        has_two_releases = len(set(self.selected_release_ids)) >= 2
        has_project_id = bool(self._current_project_id())
        has_outputs = self._project_has_outputs()

        self.health_button.setEnabled(not busy and backend_ready)
        self.refresh_projects_button.setEnabled(not busy and backend_ready)
        self.load_project_button.setEnabled(not busy and has_project_choice)
        self.add_release_button.setEnabled(not busy and has_release_choice)
        self.remove_release_button.setEnabled(
            not busy
            and self.selected_releases_list.currentItem() is not None
        )
        self.refresh_layers_button.setEnabled(not busy)
        self.use_selection_button.setEnabled(not busy and has_layer_choice)
        self.draw_button.setEnabled(not busy or self._drawing_active)
        self.validate_button.setEnabled(not busy and has_aoi and has_two_releases)
        self.run_button.setEnabled(not busy and has_aoi and has_two_releases)
        self.load_layers_button.setEnabled(
            not busy and has_project_id and has_outputs
        )
        self.excel_button.setEnabled(not busy and has_project_id and has_outputs)
        self.kml_button.setEnabled(not busy and has_project_id and has_outputs)

    # ─────────────────────────── LOGGING ─────────────────────────────────────

    def _log(self, message: str) -> None:
        QgsMessageLog.logMessage(message, LOG_CATEGORY, Qgis.Info)

    def _warn(self, message: str) -> None:
        self.iface.messageBar().pushWarning(LOG_CATEGORY, message)
        QgsMessageLog.logMessage(message, LOG_CATEGORY, Qgis.Warning)

    def _error(self, message: str) -> None:
        self.iface.messageBar().pushCritical(LOG_CATEGORY, message)
        QgsMessageLog.logMessage(message, LOG_CATEGORY, Qgis.Critical)
        QMessageBox.critical(self, LOG_CATEGORY, message)
