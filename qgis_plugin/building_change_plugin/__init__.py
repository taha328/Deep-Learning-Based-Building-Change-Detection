from __future__ import annotations


def classFactory(iface):  # noqa: N802
    from .plugin import BuildingChangePlugin

    return BuildingChangePlugin(iface)
