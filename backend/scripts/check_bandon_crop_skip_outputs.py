from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from PIL import Image


def _load_mask(path: Path) -> np.ndarray:
    return np.asarray(Image.open(path).convert("L"), dtype=np.uint8)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compare skip-off and skip-on BANDON change-mask outputs.")
    parser.add_argument("--skip-off-mask", type=Path, required=True)
    parser.add_argument("--skip-on-mask", type=Path, required=True)
    parser.add_argument("--diff-out", type=Path, required=True)
    parser.add_argument("--skip-off-probability", type=Path)
    parser.add_argument("--skip-on-probability", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    off_mask = _load_mask(args.skip_off_mask)
    on_mask = _load_mask(args.skip_on_mask)
    if off_mask.shape != on_mask.shape:
        raise RuntimeError(f"Mask shapes differ: {off_mask.shape} vs {on_mask.shape}")

    diff = np.abs(off_mask.astype(np.int16) - on_mask.astype(np.int16)).astype(np.uint8)
    args.diff_out.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(diff).save(args.diff_out)

    payload: dict[str, object] = {
        "mask_shape": list(off_mask.shape),
        "skip_off_changed_pixels": int(np.count_nonzero(off_mask)),
        "skip_on_changed_pixels": int(np.count_nonzero(on_mask)),
        "absolute_diff_pixels": int(np.count_nonzero(diff)),
        "difference_ratio": float(np.count_nonzero(diff) / diff.size) if diff.size else 0.0,
        "diff_image": str(args.diff_out),
    }

    if args.skip_off_probability and args.skip_on_probability:
        off_prob = np.load(args.skip_off_probability)
        on_prob = np.load(args.skip_on_probability)
        payload["probability_shape_match"] = bool(off_prob.shape == on_prob.shape)
        payload["skip_off_probability_shape"] = list(off_prob.shape)
        payload["skip_on_probability_shape"] = list(on_prob.shape)
        if off_prob.shape != on_prob.shape:
            raise RuntimeError(f"Probability shapes differ: {off_prob.shape} vs {on_prob.shape}")

    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
