#!/usr/bin/env python3
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TextIO
import sys
import time


PROCESS_START = time.perf_counter()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Persistent BANDON inference worker.")
    parser.add_argument("--config", required=True)
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--device", choices=["auto", "cpu", "cuda", "mps"], default="auto")
    parser.add_argument("--effective-backend", default="bandon_mps", choices=["bandon_mps", "mtgcdnet_s2looking_mps"])
    parser.add_argument("--normalization", default="app_0_1", choices=["app_0_1", "mmseg_imagenet"])
    parser.add_argument("--allow-mps-fallback", action="store_true")
    return parser.parse_args()


def _import_infer_core(args: argparse.Namespace):
    if args.allow_mps_fallback:
        os.environ["PYTORCH_ENABLE_MPS_FALLBACK"] = "1"
    script_path = Path(__file__).with_name("infer_mps.py")
    old_argv = sys.argv[:]
    sys.argv = [
        str(script_path),
        "--config",
        str(args.config),
        "--checkpoint",
        str(args.checkpoint),
        "--image-a",
        "__persistent_worker_placeholder_a__.png",
        "--image-b",
        "__persistent_worker_placeholder_b__.png",
        "--device",
        args.device,
        "--outdir",
        "__persistent_worker_placeholder_out__",
        "--effective-backend",
        args.effective_backend,
        "--normalization",
        args.normalization,
    ]
    if args.allow_mps_fallback:
        sys.argv.append("--allow-mps-fallback")
    try:
        import infer_mps as core  # type: ignore[import-not-found]
    finally:
        sys.argv = old_argv
    return core


def _write_protocol(out: TextIO, payload: dict[str, Any]) -> None:
    out.write(json.dumps(payload, separators=(",", ":")) + "\n")
    out.flush()


def _append_stage(timings: Any, name: str, *, duration_ms: float, metadata: dict[str, Any]) -> None:
    timings._stages.append(
        {
            "name": name,
            "duration_ms": round(float(duration_ms), 3),
            "status": "success",
            "metadata": metadata,
        }
    )


class PersistentWorker:
    def __init__(self, args: argparse.Namespace, core: Any) -> None:
        self.args = args
        self.core = core
        self.prediction_count = 0
        self.model_load_count = 1
        self.repo_root = core.REPO_ROOT
        self.config_path = (
            (self.repo_root / args.config).resolve()
            if not Path(args.config).is_absolute()
            else Path(args.config).resolve()
        )
        self.checkpoint_path = Path(args.checkpoint).resolve()
        self.device = core.resolve_device(args.device)
        self.timings = core.ChildStageRecorder()
        self.load_timing_ms: dict[str, float] = {}
        self.mps_test_cfg: dict[str, Any] = {}
        self.model_load_metadata: dict[str, Any] = {}
        self.checkpoint_compatibility: dict[str, Any] = {}
        self.checkpoint_loader = ""
        self.missing_after_load: list[str] = []
        self.unexpected_after_load: list[str] = []
        self._load_model_once()

    def _runner_metadata(self, **extra: Any) -> dict[str, Any]:
        return {
            "runner_family": "bandon_mps",
            "effective_backend": self.args.effective_backend,
            **extra,
        }

    def _load_model_once(self) -> None:
        core = self.core
        core.ARGS = SimpleNamespace(effective_backend=self.args.effective_backend)
        load_total_started = time.perf_counter()
        with self.timings.stage("runner_startup", **self._runner_metadata(requested_device=self.args.device)):
            pass
        with self.timings.stage(
            "model_load_or_reuse",
            **self._runner_metadata(
                device=str(self.device),
                model_cached_or_reused=False,
                used_inference_mode=False,
                used_no_grad=False,
                model_reload_count_this_job=1,
                model_reused=False,
                process_id=os.getpid(),
                checkpoint_path=str(self.checkpoint_path),
                **core._device_memory_metadata(self.device),
            ),
        ):
            config_started = time.perf_counter()
            self.cfg = core.prepare_config(self.config_path)
            self.mps_test_cfg = core.apply_mps_safe_test_cfg(self.cfg, self.device)
            self.mps_test_cfg["ablation_stride_override"] = core.apply_bandon_ablation_stride_override(self.cfg)
            self.load_timing_ms["config_load_ms"] = core._elapsed_ms(config_started)

            model_build_started = time.perf_counter()
            self.model = core.build_segmentor(self.cfg.model, test_cfg=self.cfg.get("test_cfg"))
            self.load_timing_ms["model_build_ms"] = core._elapsed_ms(model_build_started)

            checkpoint_started = time.perf_counter()
            checkpoint_obj = core.torch.load(str(self.checkpoint_path), map_location="cpu")
            self.checkpoint_compatibility = core.build_checkpoint_compatibility_diagnostics(
                checkpoint_path=self.checkpoint_path,
                checkpoint_obj=checkpoint_obj,
                model=self.model,
            )
            if self.args.effective_backend == "mtgcdnet_s2looking_mps" and self.checkpoint_compatibility["loadable_key_ratio"] < 0.90:
                raise RuntimeError(
                    "S2Looking checkpoint is incompatible with the configured MTGCDNet architecture: "
                    f"loadable_key_ratio={self.checkpoint_compatibility['loadable_key_ratio']:.3f}, "
                    f"missing_keys_count={self.checkpoint_compatibility['missing_keys_count']}, "
                    f"unexpected_keys_count={self.checkpoint_compatibility['unexpected_keys_count']}."
                )
            if self.args.effective_backend == "mtgcdnet_s2looking_mps":
                self.missing_after_load, self.unexpected_after_load = core.load_filtered_checkpoint_state(
                    model=self.model,
                    checkpoint_obj=checkpoint_obj,
                )
                checkpoint = checkpoint_obj if isinstance(checkpoint_obj, dict) else {}
                self.checkpoint_loader = "filtered_model_state_dict"
            else:
                checkpoint = core.load_checkpoint(self.model, str(self.checkpoint_path), map_location="cpu")
                self.checkpoint_loader = "mmcv_load_checkpoint"
            self.load_timing_ms["checkpoint_load_ms"] = core._elapsed_ms(checkpoint_started)

            checkpoint_meta = checkpoint.get("meta", {}) or {}
            self.classes = checkpoint_meta.get("CLASSES") or ["unchange", "change"]
            self.palette = checkpoint_meta.get("PALETTE") or [[0, 0, 0], [255, 255, 255]]
            self.model.CLASSES = self.classes
            self.model.PALETTE = self.palette
            self.model.cfg = self.cfg

            model_to_device_started = time.perf_counter()
            self.model.to(self.device)
            self.load_timing_ms["model_to_device_ms"] = core._elapsed_ms(model_to_device_started)

            model_eval_started = time.perf_counter()
            self.model.eval()
            self.load_timing_ms["model_eval_ms"] = core._elapsed_ms(model_eval_started)

            self.model_load_metadata = core.build_model_load_diagnostics(
                model=self.model,
                device=self.device,
                device_configured=self.args.device,
                model_reload_count_this_job=1,
                model_reused=False,
                no_grad_active=False,
                inference_mode_active=False,
                checkpoint_path=str(self.checkpoint_path),
                process_id=os.getpid(),
                mps_memory=core._device_memory_metadata(self.device),
            )
            self.model_load_metadata.update(
                {
                    "device_requested": self.args.device,
                    "device_resolved": str(self.device),
                    "cuda_available": bool(core.torch.cuda.is_available()),
                    "cuda_device_count": int(core.torch.cuda.device_count()) if core.torch.cuda.is_available() else 0,
                    "torch_cuda_version": core.torch.version.cuda,
                    "checkpoint_loader": self.checkpoint_loader,
                    "missing_keys_after_load_count": len(self.missing_after_load),
                    "unexpected_keys_after_load_count": len(self.unexpected_after_load),
                    "missing_keys_after_load_sample": self.missing_after_load[:25],
                    "unexpected_keys_after_load_sample": self.unexpected_after_load[:25],
                    **core._cuda_memory_metadata(self.device),
                }
            )
            self.model_load_metadata = core._drop_legacy_backend_field_for_configured_backend(self.model_load_metadata)
            self.model_load_metadata.update(self.checkpoint_compatibility)
        self.load_timing_ms["model_load_total_ms"] = core._elapsed_ms(load_total_started)

    def _request_args(self, request: dict[str, Any]) -> SimpleNamespace:
        return SimpleNamespace(
            config=str(self.config_path),
            checkpoint=str(self.checkpoint_path),
            image_a=str(request["image_a"]),
            image_b=str(request["image_b"]),
            device=self.args.device,
            outdir=str(request["outdir"]),
            allow_mps_fallback=bool(self.args.allow_mps_fallback),
            skip_invalid_crops=bool(request.get("skip_invalid_crops")),
            skip_outside_aoi_crops=bool(request.get("skip_outside_aoi_crops", True)),
            skip_nodata_crops=bool(request.get("skip_nodata_crops", True)),
            t1_valid_mask=request.get("t1_valid_mask"),
            t2_valid_mask=request.get("t2_valid_mask"),
            aoi_mask=request.get("aoi_mask"),
            effective_backend=self.args.effective_backend,
            normalization=self.args.normalization,
            min_valid_ratio_within_aoi=float(request.get("min_valid_ratio_within_aoi", 0.01)),
        )

    def predict(self, request: dict[str, Any]) -> dict[str, Any]:
        core = self.core
        prediction_started = time.perf_counter()
        self.prediction_count += 1
        model_reused = self.prediction_count > 1
        model_load_this_prediction = 0 if model_reused else 1
        core.ARGS = self._request_args(request)
        outdir = Path(request["outdir"]).resolve()
        outdir.mkdir(parents=True, exist_ok=True)
        image_a_path = Path(request["image_a"]).resolve()
        image_b_path = Path(request["image_b"]).resolve()
        child_timing_ms: dict[str, float | bool | int | str | None] = {}
        timings = core.ChildStageRecorder()
        _append_stage(
            timings,
            "model_load_or_reuse",
            duration_ms=self.load_timing_ms["model_load_total_ms"] if not model_reused else 0.0,
            metadata={
                **self._runner_metadata(
                    device=str(self.device),
                    model_cached_or_reused=bool(model_reused),
                    used_inference_mode=False,
                    used_no_grad=False,
                    model_reload_count_this_job=self.model_load_count,
                    model_reused=bool(model_reused),
                    process_id=os.getpid(),
                    checkpoint_path=str(self.checkpoint_path),
                    **core._device_memory_metadata(self.device),
                ),
                **self.model_load_metadata,
            },
        )

        with timings.stage(
            "preprocess",
            **self._runner_metadata(
                device=str(self.device),
                normalization_used=core.normalization_config(self.args.normalization),
                input_order="t1_then_t2",
            ),
        ):
            input_read_started = time.perf_counter()
            raw_image_a = core.mmcv.imread(str(image_a_path), flag="color", backend="cv2")
            raw_image_b = core.mmcv.imread(str(image_b_path), flag="color", backend="cv2")
            if raw_image_a is None:
                raise RuntimeError(f"Failed to read image: {image_a_path}")
            if raw_image_b is None:
                raise RuntimeError(f"Failed to read image: {image_b_path}")
            child_timing_ms["input_read_ms"] = core._elapsed_ms(input_read_started)

            input_preprocess_started = time.perf_counter()
            norm_cfg = core.normalization_config(self.args.normalization)
            image_a = core.mmcv.imnormalize(
                raw_image_a,
                mean=core.np.array(norm_cfg["mean"], dtype=core.np.float32),
                std=core.np.array(norm_cfg["std"], dtype=core.np.float32),
                to_rgb=bool(norm_cfg["to_rgb"]),
            ).astype(core.np.float32)
            image_b = core.mmcv.imnormalize(
                raw_image_b,
                mean=core.np.array(norm_cfg["mean"], dtype=core.np.float32),
                std=core.np.array(norm_cfg["std"], dtype=core.np.float32),
                to_rgb=bool(norm_cfg["to_rgb"]),
            ).astype(core.np.float32)
            input_tensor = core.build_input_tensor(image_a, image_b, self.device)
            img_metas = core.build_img_meta(image_a.shape, image_a_path, image_b_path, normalization=self.args.normalization)
            child_timing_ms["input_preprocess_ms"] = core._elapsed_ms(input_preprocess_started)

            mask_read_started = time.perf_counter()
            crop_skip_masks = core.load_crop_skip_masks((int(image_a.shape[0]), int(image_a.shape[1])))
            child_timing_ms["mask_read_ms"] = core._elapsed_ms(mask_read_started)
            if crop_skip_masks is not None:
                t1_valid_mask_for_skip, t2_valid_mask_for_skip, aoi_mask_for_skip = crop_skip_masks
                valid_pair_mask_for_skip = t1_valid_mask_for_skip & t2_valid_mask_for_skip
            else:
                aoi_mask_for_skip = None
                valid_pair_mask_for_skip = None

        mps_sync_used = False
        cuda_sync_used = False
        if self.device.type == "mps":
            sync = getattr(getattr(core.torch, "mps", None), "synchronize", None)
            mps_sync_used = callable(sync)
        elif self.device.type == "cuda":
            cuda_sync_used = True

        with timings.stage(
            "forward",
            **self._runner_metadata(
                device=str(self.device),
                used_inference_mode=False,
                used_no_grad=True,
                **core._device_memory_metadata(self.device),
            ),
        ):
            test_cfg = self.cfg.get("test_cfg") if hasattr(self.cfg, "get") else getattr(self.cfg, "test_cfg", None)
            if isinstance(test_cfg, dict):
                crop_height, crop_width = core._ensure_tuple2(test_cfg.get("crop_size"), "test_cfg.crop_size")
                stride_height, stride_width = core._ensure_tuple2(test_cfg.get("stride"), "test_cfg.stride")
            else:
                crop_height = crop_width = stride_height = stride_width = None
            crop_bounds = core.build_slide_crop_bounds(
                input_height=int(image_a.shape[0]),
                input_width=int(image_a.shape[1]),
                crop_height=crop_height,
                crop_width=crop_width,
                stride_height=stride_height,
                stride_width=stride_width,
            )
            crop_summaries: list[dict[str, Any]] = []
            coverage_counts = core.np.zeros((int(image_a.shape[0]), int(image_a.shape[1])), dtype=core.np.uint16)
            orig_encode_decode = self.model.encode_decode
            crop_bounds_iter = iter(crop_bounds)
            previous_bounds: tuple[int, int, int, int] | None = None
            crop_count_total = len(crop_bounds)
            crop_count_forwarded = 0
            crop_count_skipped_before_forward = 0
            crop_skip_reason_counts: dict[str, int] = {}
            mps_synchronize_ms = 0.0

            def sync_and_measure() -> None:
                nonlocal mps_synchronize_ms
                if self.device.type not in {"cuda", "mps"}:
                    return
                sync_started = time.perf_counter()
                core._maybe_sync_device(self.device)
                if self.device.type == "mps":
                    mps_synchronize_ms += core._elapsed_ms(sync_started)

            def traced_encode_decode(img, img_metas):
                nonlocal crop_count_forwarded, crop_count_skipped_before_forward, previous_bounds
                crop_meta = next(crop_bounds_iter, None)
                if crop_meta is None:
                    crop_count_forwarded += 1
                    return orig_encode_decode(img, img_metas)
                x0 = int(crop_meta["x0"])
                y0 = int(crop_meta["y0"])
                x1 = int(crop_meta["x1"])
                y1 = int(crop_meta["y1"])
                decision = None
                if core.ARGS.skip_invalid_crops and aoi_mask_for_skip is not None and valid_pair_mask_for_skip is not None:
                    decision = core.should_skip_crop(
                        aoi_mask_for_skip[y0:y1, x0:x1],
                        valid_pair_mask_for_skip[y0:y1, x0:x1],
                        core.ARGS.min_valid_ratio_within_aoi,
                        skip_outside_aoi=core.ARGS.skip_outside_aoi_crops,
                        skip_nodata=core.ARGS.skip_nodata_crops,
                    )
                sync_and_measure()
                start_ns = time.perf_counter_ns()
                if decision is not None and decision.skip:
                    output = core.zero_change_output_like(img)
                    crop_count_skipped_before_forward += 1
                    reason = decision.reason or "unknown"
                    crop_skip_reason_counts[reason] = crop_skip_reason_counts.get(reason, 0) + 1
                else:
                    output = orig_encode_decode(img, img_metas)
                    crop_count_forwarded += 1
                sync_and_measure()
                duration_ms = core._duration_ms(start_ns, time.perf_counter_ns())
                coverage_before_pixels = int(core.np.count_nonzero(coverage_counts[y0:y1, x0:x1]))
                crop_summary = core.build_crop_summary(
                    index=len(crop_summaries),
                    x0=x0,
                    y0=y0,
                    x1=x1,
                    y1=y1,
                    duration_ms=duration_ms,
                    input_tensor=img,
                    output_tensor=output,
                    previous_bounds=previous_bounds,
                    coverage_before_pixels=coverage_before_pixels,
                )
                if decision is not None:
                    crop_summary.update(
                        {
                            "skipped_before_forward": bool(decision.skip),
                            "skip_reason": decision.reason,
                            "aoi_pixels": decision.aoi_pixels,
                            "aoi_ratio": decision.aoi_ratio,
                            "valid_inside_aoi_pixels": decision.valid_inside_aoi_pixels,
                            "valid_inside_aoi_ratio": decision.valid_inside_aoi_ratio,
                            "valid_ratio_within_aoi": decision.valid_ratio_within_aoi,
                        }
                    )
                else:
                    crop_summary["skipped_before_forward"] = False
                crop_summaries.append(crop_summary)
                coverage_counts[y0:y1, x0:x1] += 1
                previous_bounds = (x0, y0, x1, y1)
                return output

            self.model.encode_decode = traced_encode_decode
            try:
                forward_total_started = time.perf_counter()
                sync_and_measure()
                forward_model_started = time.perf_counter()
                with core.torch.no_grad():
                    forward_mode_flags = core.current_torch_mode_flags()
                    result = self.model(return_loss=False, img=[input_tensor], img_metas=img_metas, rescale=True)
                child_timing_ms["forward_model_ms"] = core._elapsed_ms(forward_model_started)
                sync_and_measure()
                child_timing_ms["forward_total_ms"] = core._elapsed_ms(forward_total_started)
                child_timing_ms["mps_synchronize_ms"] = round(mps_synchronize_ms, 3)
                forward_metadata = core.build_forward_diagnostics(
                    input_tensor=input_tensor,
                    model=self.model,
                    result=result,
                    device=self.device,
                    device_configured=self.args.device,
                    aoi_height=int(image_a.shape[0]),
                    aoi_width=int(image_a.shape[1]),
                    no_grad_active=forward_mode_flags["no_grad_active"],
                    inference_mode_active=forward_mode_flags["inference_mode_active"],
                    mps_synchronize_used=mps_sync_used,
                    cpu_to_mps_transfer_count=1 if self.device.type == "mps" else 0,
                    mps_to_cpu_transfer_count=0,
                    transfer_sites=(["build_input_tensor.to(device)"] if self.device.type == "mps" else []),
                    model_reload_count_this_job=self.model_load_count,
                    model_reused=bool(model_reused),
                    mps_available=core._mps_is_available(),
                    mps_built=bool(hasattr(core.torch.backends, "mps") and core.torch.backends.mps.is_built()),
                    crop_summaries=crop_summaries,
                    coverage_counts=coverage_counts,
                )
                forward_metadata = core._drop_legacy_backend_field_for_configured_backend(forward_metadata)
                forward_metadata.update(
                    {
                        "bandon_crop_skip_enabled": bool(core.ARGS.skip_invalid_crops and crop_skip_masks is not None),
                        "crop_count_total": int(crop_count_total),
                        "crop_count_forwarded": int(crop_count_forwarded),
                        "crop_count_skipped_before_forward": int(crop_count_skipped_before_forward),
                        "crop_skip_reason_counts": dict(crop_skip_reason_counts),
                        "min_valid_ratio_within_aoi": float(core.ARGS.min_valid_ratio_within_aoi),
                        "forward_call_count": int(crop_count_forwarded),
                        "device_requested": self.args.device,
                        "device_resolved": str(self.device),
                        "cuda_available": bool(core.torch.cuda.is_available()),
                        "cuda_device_count": int(core.torch.cuda.device_count()) if core.torch.cuda.is_available() else 0,
                        "cuda_synchronize_used": bool(cuda_sync_used),
                        "torch_cuda_version": core.torch.version.cuda,
                        **core._cuda_memory_metadata(self.device),
                    }
                )
            finally:
                self.model.encode_decode = orig_encode_decode
        core._merge_stage_metadata(timings, "forward", forward_metadata)

        output_decode_started = time.perf_counter()
        with timings.stage("output_decode", **self._runner_metadata(device=str(self.device), decode_method="simple_test_softmax_channel_1")):
            if not isinstance(result, list) or not result or not isinstance(result[0], list) or not result[0]:
                raise RuntimeError(f"Unexpected inference output structure: {type(result)}")
            prediction = result[0][0]
            if not isinstance(prediction, core.np.ndarray) or prediction.ndim != 3 or prediction.shape[0] != 2:
                raise RuntimeError(
                    f"Expected a (2, H, W) probability tensor, got {type(prediction)} with shape {getattr(prediction, 'shape', None)}"
                )
            change_probability = prediction[1].astype(core.np.float32)
            change_mask = core.np.argmax(prediction, axis=0).astype(core.np.uint8)
            probability_stats = core.array_stats(change_probability)
            probability_stats_inside_aoi = core.array_stats(change_probability, mask=aoi_mask_for_skip) if aoi_mask_for_skip is not None else None
            output_channel_stats = core.channel_stats(prediction.astype(core.np.float32))
        child_timing_ms["output_decode_ms"] = core._elapsed_ms(output_decode_started)
        core._merge_stage_metadata(
            timings,
            "output_decode",
            {
                "probability_stats": probability_stats,
                "probability_stats_inside_aoi": probability_stats_inside_aoi,
                **output_channel_stats,
            },
        )

        probability_npy = outdir / "change_probability.npy"
        probability_png = outdir / "change_probability.png"
        mask_png = outdir / "change_mask.png"
        overlay_png = outdir / "change_overlay.png"
        metadata_json = outdir / "run_metadata.json"
        output_write_started = time.perf_counter()
        with timings.stage("mask_or_raster_write", **self._runner_metadata(device=str(self.device))):
            core.np.save(probability_npy, change_probability)
            core.save_probability_png(probability_png, change_probability)
            core.save_mask_png(mask_png, change_mask)
            core.save_overlay(overlay_png, image_b_path, change_mask)
        child_timing_ms["output_write_ms"] = core._elapsed_ms(output_write_started)

        for key in ("config_load_ms", "model_build_ms", "checkpoint_load_ms", "model_to_device_ms", "model_eval_ms"):
            child_timing_ms[key] = self.load_timing_ms[key] if not model_reused else 0.0
        child_timing_ms.update(
            {
                "child_total_wall_ms": core._elapsed_ms(prediction_started),
                "model_cached_or_reused": bool(model_reused),
                "model_reload_count_this_invocation": int(model_load_this_prediction),
                "model_load_count_this_prediction": int(model_load_this_prediction),
                "model_load_count_total": int(self.model_load_count),
                "model_reused": bool(model_reused),
                "model_reused_numeric": 1 if model_reused else 0,
                "device_resolved": str(self.device),
                "mps_available": core._mps_is_available(),
                "mps_built": bool(hasattr(core.torch.backends, "mps") and core.torch.backends.mps.is_built()),
                "pytorch_enable_mps_fallback": os.environ.get("PYTORCH_ENABLE_MPS_FALLBACK"),
                "allow_mps_fallback": bool(self.args.allow_mps_fallback),
                "metadata_write_ms": 0.0,
            }
        )

        metadata = {
            "repo_root": str(self.repo_root),
            "config": str(self.config_path),
            "checkpoint": str(self.checkpoint_path),
            "checkpoint_sha256": self.checkpoint_compatibility["checkpoint_sha256"],
            "runner_family": "bandon_mps",
            "effective_backend": self.args.effective_backend,
            "bandon_inference_mode": "persistent_runner",
            "persistent_worker_pid": os.getpid(),
            "image_a": str(image_a_path),
            "image_b": str(image_b_path),
            "device_requested": self.args.device,
            "device_resolved": str(self.device),
            "allow_mps_fallback": self.args.allow_mps_fallback,
            "pytorch_enable_mps_fallback": os.environ.get("PYTORCH_ENABLE_MPS_FALLBACK"),
            "torch_version": core.torch.__version__,
            "mmcv_version": core.mmcv.__version__,
            "cuda_available": bool(core.torch.cuda.is_available()),
            "cuda_device_count": int(core.torch.cuda.device_count()) if core.torch.cuda.is_available() else 0,
            "cuda_device_name": core.torch.cuda.get_device_name(0) if core.torch.cuda.is_available() else None,
            "torch_cuda_version": core.torch.version.cuda,
            "mps_built": bool(hasattr(core.torch.backends, "mps") and core.torch.backends.mps.is_built()),
            "mps_available": core._mps_is_available(),
            "mps_test_cfg": self.mps_test_cfg,
            "normalization_used": core.normalization_config(self.args.normalization),
            "input_order": "t1_then_t2",
            "decode_method": "simple_test_softmax_channel_1",
            "checkpoint_diagnostics": self.checkpoint_compatibility,
            "checkpoint_loader": self.checkpoint_loader,
            "missing_keys_after_load_count": len(self.missing_after_load),
            "unexpected_keys_after_load_count": len(self.unexpected_after_load),
            "missing_keys_after_load_sample": self.missing_after_load[:25],
            "unexpected_keys_after_load_sample": self.unexpected_after_load[:25],
            "probability_stats": probability_stats,
            "probability_stats_inside_aoi": probability_stats_inside_aoi,
            **output_channel_stats,
            "classes": self.classes,
            "palette": self.palette,
            "input_shape": list(image_a.shape),
            "bandon_crop_skip_enabled": bool(forward_metadata.get("bandon_crop_skip_enabled")),
            "crop_count_total": int(forward_metadata.get("crop_count_total") or forward_metadata.get("crop_count") or 0),
            "crop_count_forwarded": int(forward_metadata.get("crop_count_forwarded") or forward_metadata.get("forward_call_count") or 0),
            "crop_count_skipped_before_forward": int(forward_metadata.get("crop_count_skipped_before_forward") or 0),
            "crop_skip_reason_counts": forward_metadata.get("crop_skip_reason_counts") or {},
            "min_valid_ratio_within_aoi": float(core.ARGS.min_valid_ratio_within_aoi),
            "model_load_count_total": int(self.model_load_count),
            "model_load_count_this_prediction": int(model_load_this_prediction),
            "model_reused": bool(model_reused),
            "model_reused_numeric": 1 if model_reused else 0,
            **core._cuda_memory_metadata(self.device),
            "outputs": {
                "change_probability_npy": str(probability_npy),
                "change_probability_png": str(probability_png),
                "change_mask_png": str(mask_png),
                "change_overlay_png": str(overlay_png),
            },
            "stage_timings": timings.to_dict(),
            "child_timing_ms": child_timing_ms,
        }
        metadata_write_started = time.perf_counter()
        metadata_json.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        metadata["child_timing_ms"]["metadata_write_ms"] = core._elapsed_ms(metadata_write_started)
        metadata_json.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        return {
            "status": "ok",
            "prediction_index": self.prediction_count,
            "model_reused": bool(model_reused),
            "model_load_count_total": self.model_load_count,
            "metadata_path": str(metadata_json),
            "probability_path": str(probability_npy),
            "mask_path": str(mask_png),
        }


def main() -> int:
    protocol_out = sys.stdout
    args = parse_args()
    try:
        with redirect_stdout(sys.stderr):
            core = _import_infer_core(args)
            worker = PersistentWorker(args, core)
        _write_protocol(
            protocol_out,
            {
                "status": "ready",
                "pid": os.getpid(),
                "device_resolved": str(worker.device),
                "model_load_count": worker.model_load_count,
                "model_load_ms": worker.load_timing_ms.get("model_load_total_ms"),
                "checkpoint_load_ms": worker.load_timing_ms.get("checkpoint_load_ms"),
            },
        )
        for raw_line in sys.stdin:
            if not raw_line.strip():
                continue
            try:
                request = json.loads(raw_line)
                if request.get("command") == "shutdown":
                    _write_protocol(protocol_out, {"status": "bye"})
                    return 0
                if request.get("command") != "predict":
                    raise RuntimeError(f"Unsupported command: {request.get('command')!r}")
                with redirect_stdout(sys.stderr):
                    response = worker.predict(request)
                _write_protocol(protocol_out, response)
            except Exception as exc:  # noqa: BLE001
                _write_protocol(
                    protocol_out,
                    {
                        "status": "error",
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    },
                )
        return 0
    except Exception as exc:  # noqa: BLE001
        _write_protocol(
            protocol_out,
            {
                "status": "error",
                "error_type": type(exc).__name__,
                "error": str(exc),
            },
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
