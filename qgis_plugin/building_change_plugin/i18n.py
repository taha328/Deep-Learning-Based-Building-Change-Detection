from __future__ import annotations

from pathlib import Path
from typing import Optional

from qgis.PyQt.QtCore import QCoreApplication, QTranslator


class TranslationManager:
    def __init__(self, plugin_dir: Path) -> None:
        self.plugin_dir = plugin_dir
        self._translator: Optional[QTranslator] = None

    def install(self, locale_name: str) -> None:
        self.uninstall()
        if not locale_name.lower().startswith("fr"):
            return
        qm_path = self.plugin_dir / "i18n" / "building_change_plugin_fr.qm"
        if not qm_path.exists():
            return
        translator = QTranslator()
        if not translator.load(str(qm_path)):
            return
        self._translator = translator
        QCoreApplication.installTranslator(self._translator)

    def uninstall(self) -> None:
        if self._translator is not None:
            QCoreApplication.removeTranslator(self._translator)
            self._translator = None
