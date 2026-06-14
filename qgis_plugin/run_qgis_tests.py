from __future__ import annotations

import os
from pathlib import Path
import sys

import pytest


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("PROJ_LIB", "/Applications/QGIS-LTR.app/Contents/Resources/proj")
    sys.path.insert(0, str(repo_root / "qgis_plugin"))
    return pytest.main([str(repo_root / "qgis_plugin" / "tests")])


if __name__ == "__main__":
    raise SystemExit(main())
