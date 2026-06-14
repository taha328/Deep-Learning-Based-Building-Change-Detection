#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile
import zipfile

from qgis.PyQt.QtCore import QSize
from qgis.PyQt.QtGui import QColor
from qgis.core import QgsApplication, QgsMapRendererParallelJob, QgsMapSettings, QgsProject


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate and render a Download Results ESRI Shapefile QGIS export.")
    parser.add_argument("zip_path", type=Path)
    parser.add_argument("--output", type=Path, default=Path("/tmp/qgis_results_shapefile_render.png"))
    args = parser.parse_args()

    app = QgsApplication([], False)
    app.initQgis()
    with tempfile.TemporaryDirectory(prefix="results-shapefile-qgis-validation-") as tmp_name:
        extract_root = Path(tmp_name)
        with zipfile.ZipFile(args.zip_path) as archive:
            archive.extractall(extract_root)
        qgz_path = next(extract_root.glob("*.qgz"))
        project = QgsProject.instance()
        if not project.read(str(qgz_path)):
            raise SystemExit("QGIS could not read the exported project.")

        layers = list(project.mapLayers().values())
        invalid = [layer.name() for layer in layers if not layer.isValid()]
        escaped = [
            layer.source()
            for layer in layers
            if layer.providerType() != "wms"
            and not Path(layer.source().split("|", 1)[0]).resolve().is_relative_to(extract_root.resolve())
        ]
        if invalid or escaped or not project.crs().isValid():
            raise SystemExit(json.dumps({"invalid_layers": invalid, "escaped_sources": escaped, "crs": project.crs().authid()}))

        date_root = project.layerTreeRoot().findGroup("Bâtiments ajoutés par date")
        if date_root is None or not date_root.isMutuallyExclusive():
            raise SystemExit("Bâtiments ajoutés par date is missing or not mutually exclusive.")
        if project.layerTreeRoot().findGroup("Vue par date") or project.layerTreeRoot().findGroup("Fond de carte en ligne"):
            raise SystemExit("Forbidden legacy/online basemap group is present.")
        if any(layer.name() in {"OpenStreetMap", "Google Satellite"} for layer in layers):
            raise SystemExit("Forbidden online basemap layer is present.")
        date_groups = date_root.children()
        bad_date_order = []
        invalid_group_extents = []
        for group in date_groups:
            group_layers = [node.layer() for node in group.findLayers() if node.layer() is not None]
            providers = [layer.providerType() for layer in group_layers]
            if "gdal" in providers and providers[-1] != "gdal":
                bad_date_order.append(group.name())
            extent = None
            for layer in group_layers:
                if extent is None:
                    extent = layer.extent()
                else:
                    extent.combineExtentWith(layer.extent())
            if extent is None or extent.isEmpty():
                invalid_group_extents.append(group.name())
        if bad_date_order or invalid_group_extents:
            raise SystemExit(json.dumps({"bad_date_order": bad_date_order, "invalid_group_extents": invalid_group_extents}))
        initially_checked_groups = [group.name() for group in date_groups if group.itemVisibilityChecked()]
        if len(initially_checked_groups) != 1:
            raise SystemExit(json.dumps({"initially_checked_date_groups": initially_checked_groups}))
        if len(date_groups) > 1:
            date_groups[1].setItemVisibilityChecked(True)
            switched_checked_groups = [group.name() for group in date_groups if group.itemVisibilityChecked()]
            if switched_checked_groups != [date_groups[1].name()]:
                raise SystemExit(json.dumps({"mutual_exclusion_simulation": switched_checked_groups}))
            switched_layers = [layer.name() for layer in project.layerTreeRoot().checkedLayers()]
            expected_switched_fragments = (
                f"Bâtiments ajoutés {date_groups[1].name()}",
                f"Buffer 10m {date_groups[1].name()}",
                f"Imagerie de référence – {date_groups[1].name()}",
            )
            if not all(fragment in switched_layers for fragment in expected_switched_fragments):
                raise SystemExit(json.dumps({"switched_layers": switched_layers}))
            date_groups[0].setItemVisibilityChecked(True)

        settings = QgsMapSettings()
        settings.setLayers(project.layerTreeRoot().checkedLayers())
        settings.setDestinationCrs(project.crs())
        checked_layers = project.layerTreeRoot().checkedLayers()
        reference_raster = next((layer for layer in checked_layers if layer.providerType() == "gdal"), None)
        settings.setExtent(reference_raster.extent() if reference_raster is not None else project.viewSettings().defaultViewExtent())
        settings.setOutputSize(QSize(1400, 1000))
        settings.setBackgroundColor(QColor("white"))
        job = QgsMapRendererParallelJob(settings)
        job.start()
        job.waitForFinished()
        image = job.renderedImage()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        image.save(str(args.output), "PNG")
        white = QColor("white").rgba()
        total = image.width() * image.height()
        non_white = sum(
            image.pixel(x, y) != white
            for y in range(image.height())
            for x in range(image.width())
        )
        result = {
            "project": str(qgz_path),
            "project_crs": project.crs().authid(),
            "layer_count": len(layers),
            "invalid_layers": invalid,
            "checked_layers": [layer.name() for layer in project.layerTreeRoot().checkedLayers()],
            "tree_order": [layer.name() for layer in project.layerTreeRoot().layerOrder()],
            "date_groups_validated": len(date_groups),
            "mutually_exclusive": date_root.isMutuallyExclusive(),
            "initially_checked_date_groups": initially_checked_groups,
            "bad_date_order": bad_date_order,
            "invalid_group_extents": invalid_group_extents,
            "output": str(args.output),
            "non_white_pixels": non_white,
            "non_white_ratio": non_white / total,
        }
        print(json.dumps(result, indent=2))
        if non_white == 0:
            raise SystemExit("Rendered image is blank.")
    app.exitQgis()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
