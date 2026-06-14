from __future__ import annotations

from pathlib import Path


def main() -> None:
    plugin_dir = Path(__file__).resolve().parent / "building_change_plugin"
    required = ["metadata.txt", "__init__.py", "plugin.py", "dock.py", "api_client.py", "tasks.py", "layer_loader.py"]
    missing = [name for name in required if not (plugin_dir / name).exists()]
    if missing:
        raise SystemExit(f"Missing plugin files: {', '.join(missing)}")
    print(f"Plugin layout OK: {plugin_dir}")


if __name__ == "__main__":
    main()
