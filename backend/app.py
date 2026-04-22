from __future__ import annotations

from src.config import get_settings
from src.routes import create_app


SETTINGS = get_settings()
demo = create_app(SETTINGS)


if __name__ == "__main__":
    demo.launch(allowed_paths=[str(path.resolve()) for path in SETTINGS.gradio_allowed_paths])
