from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
from typing import List, Optional


def _find_lrelease(plugin_dir: Path) -> str:
    candidates = [
        shutil.which("pyside6-lrelease"),
        str(plugin_dir.parents[1] / "backend" / ".venv" / "bin" / "pyside6-lrelease"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    raise RuntimeError("Unable to find a Qt translation compiler (pyside6-lrelease).")


def compile_qm_translations(plugin_dir: Optional[Path] = None) -> List[Path]:
    root = Path(__file__).resolve().parent
    plugin_root = plugin_dir or (root / "building_change_plugin")
    i18n_dir = plugin_root / "i18n"
    lrelease = _find_lrelease(plugin_root)
    compiled: List[Path] = []
    for ts_path in sorted(i18n_dir.glob("*.ts")):
        qm_path = ts_path.with_suffix(".qm")
        subprocess.run([lrelease, str(ts_path), "-qm", str(qm_path)], check=True)
        compiled.append(qm_path)
    return compiled


if __name__ == "__main__":
    for path in compile_qm_translations():
        print(path)
