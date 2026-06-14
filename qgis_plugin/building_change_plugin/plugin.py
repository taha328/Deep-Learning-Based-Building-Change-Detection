from __future__ import annotations

from pathlib import Path
from typing import Optional

from qgis.PyQt.QtCore import QLocale, QSettings, Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction

from .dock import BuildingChangeDockWidget
from .i18n import TranslationManager


class BuildingChangePlugin:
    def __init__(self, iface) -> None:
        self.iface = iface
        self.plugin_dir = Path(__file__).resolve().parent
        self.translation_manager = TranslationManager(self.plugin_dir)
        self.action: Optional[QAction] = None
        self.dock_widget: Optional[BuildingChangeDockWidget] = None

    def tr(self, text: str) -> str:
        from qgis.PyQt.QtCore import QCoreApplication

        return QCoreApplication.translate("BuildingChangePlugin", text)

    def initGui(self) -> None:
        locale_name = QSettings().value("locale/userLocale", "", type=str) or QLocale.system().name()
        self.translation_manager.install(locale_name)

        icon = QIcon(str(self.plugin_dir / "icon.svg"))
        self.action = QAction(icon, self.tr("Building Change Detection"), self.iface.mainWindow())
        self.action.triggered.connect(self.show_dock)
        self.iface.addPluginToMenu(self.tr("&Building Change Detection"), self.action)
        self.iface.addToolBarIcon(self.action)

    def unload(self) -> None:
        if self.action is not None:
            self.iface.removePluginMenu(self.tr("&Building Change Detection"), self.action)
            self.iface.removeToolBarIcon(self.action)
            self.action = None
        if self.dock_widget is not None:
            self.dock_widget.close()
            self.iface.mainWindow().removeDockWidget(self.dock_widget)
            self.dock_widget = None
        self.translation_manager.uninstall()

    def show_dock(self) -> None:
        if self.dock_widget is None:
            self.dock_widget = BuildingChangeDockWidget(self.iface, self.plugin_dir)
            self.iface.addDockWidget(Qt.RightDockWidgetArea, self.dock_widget)
        self.dock_widget.show()
        self.dock_widget.raise_()
