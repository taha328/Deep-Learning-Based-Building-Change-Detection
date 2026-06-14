from __future__ import annotations

from qgis.PyQt.QtGui import QColor
from qgis.core import (
    QgsColorRampShader,
    QgsFillSymbol,
    QgsMultiBandColorRenderer,
    QgsPalettedRasterRenderer,
    QgsRasterLayer,
    QgsRasterShader,
    QgsSingleBandPseudoColorRenderer,
    QgsSingleSymbolRenderer,
    QgsStyle,
    QgsVectorLayer,
)

from .temporal_colors import TemporalQgisStyle


def style_reference_raster(layer: QgsRasterLayer) -> None:
    provider = layer.dataProvider()
    if provider.bandCount() >= 3:
        renderer = QgsMultiBandColorRenderer(provider, 1, 2, 3)
        if provider.bandCount() >= 4 and hasattr(renderer, "setAlphaBand"):
            renderer.setAlphaBand(4)
        layer.setRenderer(renderer)
        layer.triggerRepaint()


def style_probability_raster(layer: QgsRasterLayer) -> None:
    provider = layer.dataProvider()
    ramp = QgsStyle.defaultStyle().colorRamp("Viridis")
    shader = QgsRasterShader()
    color_shader = QgsColorRampShader()
    color_shader.setMinimumValue(0.0)
    color_shader.setMaximumValue(1.0)
    if ramp is not None:
        color_shader.setSourceColorRamp(ramp)
    shader.setRasterShaderFunction(color_shader)
    layer.setRenderer(QgsSingleBandPseudoColorRenderer(provider, 1, shader))
    layer.triggerRepaint()


def style_mask_raster(layer: QgsRasterLayer) -> None:
    provider = layer.dataProvider()
    classes = [
        QgsPalettedRasterRenderer.Class(0, QColor(0, 0, 0, 0), "0"),
        QgsPalettedRasterRenderer.Class(1, QColor(255, 0, 0, 220), "1"),
    ]
    layer.setRenderer(QgsPalettedRasterRenderer(provider, 1, classes))
    layer.triggerRepaint()


def style_polygon(
    layer: QgsVectorLayer,
    *,
    fill: QColor,
    outline: QColor,
    width: str = "0.8",
) -> None:
    symbol = QgsFillSymbol.createSimple(
        {
            "color": f"{fill.red()},{fill.green()},{fill.blue()},{fill.alpha()}",
            "outline_color": f"{outline.red()},{outline.green()},{outline.blue()},{outline.alpha()}",
            "outline_width": width,
        }
    )
    layer.setRenderer(QgsSingleSymbolRenderer(symbol))
    layer.triggerRepaint()


def _qcolor_from_hex(color: str, opacity: float) -> QColor:
    value = color.lstrip("#")
    alpha = max(0, min(255, round(opacity * 255)))
    return QColor(int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16), alpha)


def style_temporal_artifact(layer: QgsVectorLayer, style: TemporalQgisStyle) -> None:
    style_polygon(
        layer,
        fill=_qcolor_from_hex(style.fill_color, style.fill_opacity),
        outline=_qcolor_from_hex(style.outline_color, style.outline_opacity),
        width=style.outline_width,
    )


def style_additions(layer: QgsVectorLayer) -> None:
    style_polygon(layer, fill=QColor(231, 76, 60, 255), outline=QColor(120, 20, 20, 255))


def style_automated(layer: QgsVectorLayer) -> None:
    style_polygon(layer, fill=QColor(255, 111, 0, 255), outline=QColor(120, 50, 0, 255))


def style_blocks(layer: QgsVectorLayer) -> None:
    style_polygon(layer, fill=QColor(255, 170, 0, 255), outline=QColor(145, 80, 0, 255))


def style_cumulative(layer: QgsVectorLayer) -> None:
    style_polygon(layer, fill=QColor(0, 170, 200, 255), outline=QColor(0, 90, 130, 255), width="1.1")


def style_buffer(layer: QgsVectorLayer) -> None:
    style_polygon(layer, fill=QColor(255, 128, 0, 255), outline=QColor(140, 60, 0, 255))


def style_buffer_15(layer: QgsVectorLayer) -> None:
    style_polygon(layer, fill=QColor(180, 90, 220, 255), outline=QColor(95, 35, 145, 255))


def style_buffer_20(layer: QgsVectorLayer) -> None:
    style_polygon(layer, fill=QColor(120, 80, 255, 255), outline=QColor(55, 35, 150, 255))


def style_rejected(layer: QgsVectorLayer) -> None:
    style_polygon(layer, fill=QColor(120, 120, 120, 35), outline=QColor(90, 90, 90, 180))


def style_flagged(layer: QgsVectorLayer) -> None:
    style_polygon(layer, fill=QColor(255, 213, 79, 255), outline=QColor(120, 70, 0, 255))


def style_aoi(layer: QgsVectorLayer) -> None:
    style_polygon(layer, fill=QColor(46, 204, 113, 55), outline=QColor(39, 174, 96, 255), width="1.2")
