from __future__ import annotations

from typing import List

from qgis.PyQt.QtCore import QObject, Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor
from qgis.core import QgsGeometry, QgsPointXY, QgsWkbTypes
from qgis.gui import QgsMapToolEmitPoint, QgsRubberBand


class PolygonCaptureTool(QgsMapToolEmitPoint):
    geometryCaptured = pyqtSignal(object)
    captureCancelled = pyqtSignal()

    def __init__(self, canvas) -> None:
        super().__init__(canvas)
        self.canvas = canvas
        self._points: List[QgsPointXY] = []
        self._rubber_band = QgsRubberBand(self.canvas, QgsWkbTypes.PolygonGeometry)
        self._rubber_band.setStrokeColor(QColor(46, 204, 113))
        self._rubber_band.setFillColor(QColor(46, 204, 113, 60))
        self._rubber_band.setWidth(2)

    def canvasPressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            point = self.toMapCoordinates(event.pos())
            self._points.append(point)
            self._refresh_band()
        elif event.button() == Qt.RightButton:
            self._finalize()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key_Escape:
            self.reset()
            self.captureCancelled.emit()
        elif event.key() in (Qt.Key_Return, Qt.Key_Enter):
            self._finalize()

    def deactivate(self) -> None:
        super().deactivate()
        self._rubber_band.hide()

    def reset(self) -> None:
        self._points = []
        self._rubber_band.reset(QgsWkbTypes.PolygonGeometry)

    def _refresh_band(self) -> None:
        self._rubber_band.reset(QgsWkbTypes.PolygonGeometry)
        if not self._points:
            return
        for point in self._points:
            self._rubber_band.addPoint(point, False)
        self._rubber_band.addPoint(self._points[0], True)
        self._rubber_band.show()

    def _finalize(self) -> None:
        if len(self._points) < 3:
            self.captureCancelled.emit()
            self.reset()
            return
        ring = [QgsPointXY(point) for point in self._points]
        if ring[0] != ring[-1]:
            ring.append(QgsPointXY(ring[0]))
        geometry = QgsGeometry.fromPolygonXY([ring])
        self.geometryCaptured.emit(geometry)
        self.reset()
