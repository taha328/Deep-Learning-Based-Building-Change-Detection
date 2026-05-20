from __future__ import annotations

from pathlib import Path


FORBIDDEN_TOKENS = [
    "SA" + "M3",
    "sa" + "m3",
    "gra" + "dio_client",
    "gra" + "dio",
    "hugging" + "face",
    "Hugging" + " Face",
    "zero" + "gpu",
    "Zero" + "GPU",
    "remote_" + "segmentation",
    "public_" + "zero" + "gpu",
    "hugging" + "face_" + "gpu",
    "sa" + "m3_" + "backend_mode",
]


def test_no_legacy_remote_inference_references_in_backend_src() -> None:
    root = Path(__file__).resolve().parents[1] / "src"
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        rel = str(path.relative_to(root))
        text = path.read_text(encoding="utf-8")
        for token in FORBIDDEN_TOKENS:
            if token in text:
                offenders.append(f"{rel}: {token}")
    assert offenders == []
