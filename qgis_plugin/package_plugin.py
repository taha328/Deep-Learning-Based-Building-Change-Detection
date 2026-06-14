from __future__ import annotations

from pathlib import Path
import zipfile


def build_plugin_zip() -> Path:
    root = Path(__file__).resolve().parent
    plugin_dir = root / "building_change_plugin"
    dist_dir = root / "dist"
    dist_dir.mkdir(parents=True, exist_ok=True)
    zip_path = dist_dir / "building_change_plugin.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for file_path in plugin_dir.rglob("*"):
            relative_path = file_path.relative_to(plugin_dir)
            if "__pycache__" in relative_path.parts or file_path.name == ".DS_Store":
                continue
            if file_path.is_file():
                archive.write(file_path, arcname=str(Path("building_change_plugin") / relative_path))
    return zip_path


if __name__ == "__main__":
    print(build_plugin_zip())
