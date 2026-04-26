from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
import tempfile
import threading
import time
from typing import Any, Callable

from gradio_client import handle_file
import numpy as np
from PIL import Image
import requests

from src.config import Settings
from src.domain.postprocess import (
    dilate_mask,
    remove_small_components,
    suppress_edge_hugging_components,
)
from src.domain.tiling import iter_patch_windows, pad_patch_rgb
from src.utils.logging import get_logger


LOGGER = get_logger(__name__)

_PATCH_CACHE_LOCKS_GUARD = threading.Lock()
_PATCH_CACHE_LOCKS: dict[str, threading.Lock] = {}


@dataclass(frozen=True)
class InferenceDiagnostics:
    patch_count: int
    patch_prepare_seconds: float
    remote_seconds: float
    mask_decode_seconds: float


def _patch_cache_lock(cache_path: Path) -> threading.Lock:
    key = str(cache_path.resolve())
    with _PATCH_CACHE_LOCKS_GUARD:
        return _PATCH_CACHE_LOCKS.setdefault(key, threading.Lock())


def _scene_patch_cache_dir(base_cache_dir: Path | None, scene_label: str) -> Path | None:
    if base_cache_dir is None:
        return None
    path = base_cache_dir / "remote_sam3_cache" / scene_label
    path.mkdir(parents=True, exist_ok=True)
    return path


def _scene_patch_cache_path(scene_cache_dir: Path, patch_index: int) -> Path:
    return scene_cache_dir / f"patch_{patch_index:05d}.npz"


def _load_cached_patch_mask(cache_path: Path) -> np.ndarray | None:
    if not cache_path.exists():
        return None
    try:
        with np.load(cache_path, allow_pickle=False) as data:
            return data["mask"].astype(bool)
    except Exception:
        cache_path.unlink(missing_ok=True)
        return None


def _save_cached_patch_mask(cache_path: Path, mask: np.ndarray) -> None:
    with tempfile.NamedTemporaryFile(
        suffix=".npz",
        prefix=f"{cache_path.stem}_",
        dir=cache_path.parent,
        delete=False,
    ) as tmp_file:
        tmp_path = Path(tmp_file.name)
    try:
        np.savez_compressed(tmp_path, mask=mask.astype(np.uint8))
        tmp_path.replace(cache_path)
    finally:
        tmp_path.unlink(missing_ok=True)


def _open_annotation_image(
    image_ref: dict[str, Any] | str,
    *,
    session: requests.Session,
    timeout_sec: int,
) -> Image.Image:
    if isinstance(image_ref, dict):
        url = image_ref.get("url")
        path = image_ref.get("path")
        if isinstance(url, str) and url:
            response = session.get(url, timeout=timeout_sec)
            response.raise_for_status()
            return Image.open(BytesIO(response.content)).convert("RGBA")
        if isinstance(path, str) and path:
            return Image.open(path).convert("RGBA")
    elif isinstance(image_ref, str):
        return Image.open(image_ref).convert("RGBA")
    raise ValueError("Remote annotation image does not contain a usable url or path.")


def decode_remote_segmentation_result(
    result: dict[str, Any] | list[Any] | tuple[Any, ...],
    *,
    expected_shape: tuple[int, int],
    session: requests.Session,
    timeout_sec: int,
) -> np.ndarray:
    if isinstance(result, (list, tuple)):
        annotated_result = next(
            (
                item
                for item in result
                if isinstance(item, dict)
                and "annotations" in item
                and isinstance(item.get("annotations"), list)
            ),
            None,
        )
        if annotated_result is None:
            raise ValueError("Remote SAM3 response tuple does not contain an annotated-image payload.")
        result = annotated_result

    if not isinstance(result, dict):
        raise ValueError("Remote SAM3 response payload is not a dict or annotated tuple.")

    height, width = expected_shape
    combined = np.zeros((height, width), dtype=bool)
    for annotation in result.get("annotations", []):
        annotation_ref = annotation.get("image")
        if annotation_ref is None:
            continue
        annotation_image = _open_annotation_image(
            annotation_ref,
            session=session,
            timeout_sec=timeout_sec,
        )
        if annotation_image.size != (width, height):
            annotation_image = annotation_image.resize((width, height), resample=Image.NEAREST)
        combined |= np.asarray(annotation_image.getchannel("A"), dtype=np.uint8) > 0
    return combined


def derive_change_probability(t1_building_prob: np.ndarray, t2_building_prob: np.ndarray) -> np.ndarray:
    return np.clip(t2_building_prob - t1_building_prob, 0.0, 1.0).astype(np.float32)


def _predict_remote_patch_mask(
    patch_rgb: np.ndarray,
    *,
    settings: Settings,
    semantic_threshold: float,
    session: requests.Session,
    x_ip_token: str | None = None,
) -> tuple[np.ndarray, float, float, float]:
    from src.domain.model import (
        REMOTE_SEGMENTATION_CLIENTS,
        REMOTE_SEGMENTATION_PROVIDER_POOL,
        is_invalid_provider_error,
        is_refreshable_provider_error,
    )

    prepare_start = time.perf_counter()
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp_file:
        patch_path = Path(tmp_file.name)
    Image.fromarray(patch_rgb).save(patch_path)
    patch_prepare_seconds = time.perf_counter() - prepare_start

    try:
        last_error: Exception | None = None
        provider_errors: list[str] = []
        provider_count = max(1, len(settings.remote_segmentation_spaces))
        max_failure_events = max(1, settings.remote_segmentation_retries) * provider_count
        failure_events = 0
        provider_refresh_attempts: dict[str, int] = {}
        provider_banned_until: dict[str, float] = {}
        patience_window_sec = max(
            settings.remote_segmentation_provider_patience_sec,
            settings.remote_segmentation_timeout_sec,
        )
        start_time = time.monotonic()
        deadline = start_time + patience_window_sec
        while failure_events < max_failure_events and time.monotonic() < deadline:
            provider_space: str | None = None
            while provider_space is None and time.monotonic() < deadline:
                now = time.monotonic()
                ready_spaces = [
                    space
                    for space in REMOTE_SEGMENTATION_PROVIDER_POOL.get_ready_spaces(settings)
                    if provider_banned_until.get(space, 0.0) <= now
                ]
                if not ready_spaces:
                    local_cooldowns = [
                        until - now for until in provider_banned_until.values() if until > now
                    ]
                    sleep_candidates = [REMOTE_SEGMENTATION_PROVIDER_POOL.next_retry_delay(settings), *local_cooldowns]
                    sleep_seconds = min(
                        max(0.1, min(delay for delay in sleep_candidates if delay > 0)),
                        12.0,
                        max(0.1, deadline - now),
                    )
                    LOGGER.info(
                        "No remote SAM3 providers ready, waiting %.1fs after %d/%d failure events",
                        sleep_seconds,
                        failure_events,
                        max_failure_events,
                    )
                    time.sleep(sleep_seconds)
                    continue

                for candidate_space in ready_spaces:
                    if REMOTE_SEGMENTATION_PROVIDER_POOL.try_acquire(candidate_space, settings=settings):
                        provider_space = candidate_space
                        break
                if provider_space is None:
                    time.sleep(0.05)

            if provider_space is None:
                break

            try:
                client = REMOTE_SEGMENTATION_CLIENTS.get_client(
                    space=provider_space,
                    timeout_sec=settings.remote_segmentation_timeout_sec,
                    hf_token=settings.remote_segmentation_hf_token,
                    x_ip_token=x_ip_token,
                    force_refresh=provider_refresh_attempts.get(provider_space, 0) > 0,
                )
                remote_start = time.perf_counter()
                result = client.predict(
                    source_img=handle_file(str(patch_path)),
                    text_query=settings.remote_segmentation_prompt,
                    conf_thresh=float(semantic_threshold),
                    api_name=settings.remote_segmentation_api_name,
                )
                remote_seconds = time.perf_counter() - remote_start

                decode_start = time.perf_counter()
                mask = decode_remote_segmentation_result(
                    result,
                    expected_shape=patch_rgb.shape[:2],
                    session=session,
                    timeout_sec=settings.remote_segmentation_timeout_sec,
                )
                mask_decode_seconds = time.perf_counter() - decode_start
                provider_refresh_attempts.pop(provider_space, None)
                REMOTE_SEGMENTATION_PROVIDER_POOL.report_success(
                    provider_space,
                    settings=settings,
                )
                return mask, patch_prepare_seconds, remote_seconds, mask_decode_seconds
            except Exception as exc:  # pragma: no cover - covered indirectly via retries
                last_error = exc
                provider_errors.append(f"{provider_space}: {type(exc).__name__}: {exc}")
                REMOTE_SEGMENTATION_CLIENTS.invalidate(
                    space=provider_space,
                    timeout_sec=settings.remote_segmentation_timeout_sec,
                    hf_token=settings.remote_segmentation_hf_token,
                    x_ip_token=x_ip_token,
                )
                if is_refreshable_provider_error(exc):
                    provider_refresh_attempts[provider_space] = provider_refresh_attempts.get(provider_space, 0) + 1
                    refresh_attempt = provider_refresh_attempts[provider_space]
                    single_provider_mode = provider_count == 1
                    if single_provider_mode and refresh_attempt < settings.remote_segmentation_client_refresh_retries:
                        LOGGER.warning(
                            "Remote SAM3 client for %s returned a refreshable token error (%d/%d); refreshing client immediately because it is the only configured provider: %s",
                            provider_space,
                            refresh_attempt,
                            settings.remote_segmentation_client_refresh_retries,
                            exc,
                        )
                        continue

                    failure_events += 1
                    REMOTE_SEGMENTATION_PROVIDER_POOL.report_failure(
                        provider_space,
                        exc,
                        settings=settings,
                    )
                    if refresh_attempt >= settings.remote_segmentation_client_refresh_retries:
                        provider_banned_until[provider_space] = deadline
                        LOGGER.warning(
                            "Remote SAM3 provider %s exhausted its refresh budget and is quarantined for the remainder of this patch after %d/%d failure events: %s",
                            provider_space,
                            failure_events,
                            max_failure_events,
                            exc,
                        )
                    else:
                        LOGGER.warning(
                            "Remote SAM3 provider %s returned a refreshable token error (%d/%d); cooling it down and rotating to another provider (%d/%d failure events): %s",
                            provider_space,
                            refresh_attempt,
                            settings.remote_segmentation_client_refresh_retries,
                            failure_events,
                            max_failure_events,
                            exc,
                        )
                    continue

                provider_refresh_attempts.pop(provider_space, None)
                failure_events += 1
                if is_invalid_provider_error(exc):
                    provider_banned_until[provider_space] = deadline
                REMOTE_SEGMENTATION_PROVIDER_POOL.report_failure(
                    provider_space,
                    exc,
                    settings=settings,
                )
                LOGGER.warning(
                    "Remote SAM3 patch inference failed via %s after %d/%d failure events: %s",
                    provider_space,
                    failure_events,
                    max_failure_events,
                    exc,
                )
            finally:
                REMOTE_SEGMENTATION_PROVIDER_POOL.release(provider_space, settings=settings)

        raise RuntimeError(
            "Remote SAM3 segmentation failed after trying public providers "
            f"for {round(max(0.0, time.monotonic() - start_time), 1)}s "
            f"and {failure_events}/{max_failure_events} provider failure events: "
            + " | ".join(provider_errors[-6:])
            if provider_errors
            else (
                "Remote SAM3 segmentation failed after "
                f"{failure_events}/{max_failure_events} provider failure events: {type(last_error).__name__}: {last_error}"
            )
        )
    finally:
        patch_path.unlink(missing_ok=True)


def _segment_scene_patch(
    scene_rgb: np.ndarray,
    *,
    window: Any,
    patch_index: int,
    settings: Settings,
    semantic_threshold: float,
    cache_path: Path | None,
    x_ip_token: str | None = None,
) -> tuple[int, Any, np.ndarray, float, float, float]:
    if cache_path is not None:
        cache_lock = _patch_cache_lock(cache_path)
        with cache_lock:
            cached_mask = _load_cached_patch_mask(cache_path)
            if cached_mask is not None:
                return patch_index, window, cached_mask, 0.0, 0.0, 0.0

            patch = pad_patch_rgb(
                scene_rgb[window.y0 : window.y1, window.x0 : window.x1].copy(),
                settings.patch_size,
            )
            with requests.Session() as session:
                session.headers.update({"User-Agent": "Building-Change-Remote-SAM3/1.0"})
                mask, patch_prepare, remote_elapsed, decode_elapsed = _predict_remote_patch_mask(
                    patch,
                    settings=settings,
                    semantic_threshold=semantic_threshold,
                    session=session,
                    x_ip_token=x_ip_token,
                )
            _save_cached_patch_mask(cache_path, mask)
            return patch_index, window, mask, patch_prepare, remote_elapsed, decode_elapsed

    patch = pad_patch_rgb(
        scene_rgb[window.y0 : window.y1, window.x0 : window.x1].copy(),
        settings.patch_size,
    )
    with requests.Session() as session:
        session.headers.update({"User-Agent": "Building-Change-Remote-SAM3/1.0"})
        mask, patch_prepare, remote_elapsed, decode_elapsed = _predict_remote_patch_mask(
            patch,
            settings=settings,
            semantic_threshold=semantic_threshold,
            session=session,
            x_ip_token=x_ip_token,
        )
    return patch_index, window, mask, patch_prepare, remote_elapsed, decode_elapsed


def _run_scene_segmentation(
    scene_rgb: np.ndarray,
    *,
    scene_label: str,
    settings: Settings,
    semantic_threshold: float,
    cache_dir: Path | None = None,
    x_ip_token: str | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> tuple[np.ndarray, InferenceDiagnostics]:
    height, width = scene_rgb.shape[:2]
    windows = list(iter_patch_windows(height, width, settings.patch_size, settings.stride))
    score_sum = np.zeros((height, width), dtype=np.float32)
    counts = np.zeros((height, width), dtype=np.float32)

    patch_prepare_seconds = 0.0
    remote_seconds = 0.0
    mask_decode_seconds = 0.0
    scene_cache_dir = _scene_patch_cache_dir(cache_dir, scene_label)
    pending_jobs: list[tuple[int, Any, Path | None]] = []
    completed_patches = 0

    for patch_index, window in enumerate(windows, start=1):
        cache_path = (
            _scene_patch_cache_path(scene_cache_dir, patch_index)
            if scene_cache_dir is not None
            else None
        )
        cached_mask = _load_cached_patch_mask(cache_path) if cache_path is not None else None
        if cached_mask is not None:
            orig_h = window.y1 - window.y0
            orig_w = window.x1 - window.x0
            score_sum[window.y0 : window.y1, window.x0 : window.x1] += cached_mask[:orig_h, :orig_w].astype(
                np.float32
            )
            counts[window.y0 : window.y1, window.x0 : window.x1] += 1.0
            completed_patches += 1
            if progress_callback:
                progress_callback(
                    f"Reused cached {scene_label} patch progress {completed_patches}/{len(windows)} "
                    f"(patch {patch_index}/{len(windows)})"
                )
            continue
        pending_jobs.append((patch_index, window, cache_path))

    if pending_jobs:
        max_workers = max(
            1,
            min(
                len(pending_jobs),
                max(1, settings.remote_segmentation_max_parallel_patches),
            ),
        )
        futures: dict[Future[tuple[int, Any, np.ndarray, float, float, float]], tuple[int, Any, Path | None]] = {}
        try:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                for patch_index, window, cache_path in pending_jobs:
                    future = executor.submit(
                        _segment_scene_patch,
                        scene_rgb,
                        window=window,
                        patch_index=patch_index,
                        settings=settings,
                        semantic_threshold=semantic_threshold,
                        cache_path=cache_path,
                        x_ip_token=x_ip_token,
                    )
                    futures[future] = (patch_index, window, cache_path)

                for future in as_completed(futures):
                    patch_index, window, _ = futures[future]
                    try:
                        _, _, mask, patch_prepare, remote_elapsed, decode_elapsed = future.result()
                    except RuntimeError as exc:
                        for pending_future in futures:
                            if pending_future is not future:
                                pending_future.cancel()
                        cached_patch_count = 0
                        if scene_cache_dir is not None:
                            cached_patch_count = len(list(scene_cache_dir.glob("patch_*.npz")))
                        completed_patch_count = max(completed_patches, cached_patch_count)
                        if completed_patch_count > 0:
                            raise RuntimeError(
                                f"{exc}. Partial {scene_label} progress is cached ({completed_patch_count}/{len(windows)} patches). "
                                "Rerun the same request to resume automatically."
                            ) from exc
                        raise

                    patch_prepare_seconds += patch_prepare
                    remote_seconds += remote_elapsed
                    mask_decode_seconds += decode_elapsed

                    orig_h = window.y1 - window.y0
                    orig_w = window.x1 - window.x0
                    score_sum[window.y0 : window.y1, window.x0 : window.x1] += mask[:orig_h, :orig_w].astype(
                        np.float32
                    )
                    counts[window.y0 : window.y1, window.x0 : window.x1] += 1.0
                    completed_patches += 1

                    if progress_callback:
                        progress_callback(
                            f"Segmented {scene_label} patch progress {completed_patches}/{len(windows)} "
                            f"via remote SAM3 (finished patch {patch_index}/{len(windows)})"
                        )
        finally:
            for future in futures:
                future.cancel()

    return (
        score_sum / np.maximum(counts, 1.0),
        InferenceDiagnostics(
            patch_count=len(windows),
            patch_prepare_seconds=round(patch_prepare_seconds, 4),
            remote_seconds=round(remote_seconds, 4),
            mask_decode_seconds=round(mask_decode_seconds, 4),
        ),
    )


def run_tiled_inference(
    arr_t1: np.ndarray,
    arr_t2: np.ndarray,
    *,
    settings: Settings,
    semantic_threshold: float,
    cache_dir: Path | None = None,
    x_ip_token: str | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> tuple[dict[str, np.ndarray], InferenceDiagnostics]:
    if arr_t1.shape != arr_t2.shape:
        raise ValueError("T1 and T2 arrays must have identical shapes before inference.")

    scene_results: dict[str, tuple[np.ndarray, InferenceDiagnostics]] = {}
    max_scene_workers = max(1, min(2, settings.scene_segmentation_concurrency))
    with ThreadPoolExecutor(max_workers=max_scene_workers) as executor:
        future_map = {
            executor.submit(
                _run_scene_segmentation,
                arr_t1,
                scene_label="t1",
                settings=settings,
                semantic_threshold=semantic_threshold,
                cache_dir=cache_dir,
                x_ip_token=x_ip_token,
                progress_callback=progress_callback,
            ): "t1",
            executor.submit(
                _run_scene_segmentation,
                arr_t2,
                scene_label="t2",
                settings=settings,
                semantic_threshold=semantic_threshold,
                cache_dir=cache_dir,
                x_ip_token=x_ip_token,
                progress_callback=progress_callback,
            ): "t2",
        }
        for future in as_completed(future_map):
            scene_label = future_map[future]
            try:
                scene_results[scene_label] = future.result()
            except Exception:
                for other_future in future_map:
                    if other_future is not future:
                        other_future.cancel()
                raise

    t1_building_prob, t1_diag = scene_results["t1"]
    t2_building_prob, t2_diag = scene_results["t2"]

    return (
        {
            "change_prediction": derive_change_probability(t1_building_prob, t2_building_prob),
            "t1_semantic_prediction": t1_building_prob,
            "t2_semantic_prediction": t2_building_prob,
        },
        InferenceDiagnostics(
            patch_count=t1_diag.patch_count + t2_diag.patch_count,
            patch_prepare_seconds=round(t1_diag.patch_prepare_seconds + t2_diag.patch_prepare_seconds, 4),
            remote_seconds=round(t1_diag.remote_seconds + t2_diag.remote_seconds, 4),
            mask_decode_seconds=round(t1_diag.mask_decode_seconds + t2_diag.mask_decode_seconds, 4),
        ),
    )


def run_single_scene_inference(
    scene_rgb: np.ndarray,
    *,
    settings: Settings,
    semantic_threshold: float,
    cache_dir: Path | None = None,
    x_ip_token: str | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> tuple[dict[str, np.ndarray], InferenceDiagnostics]:
    prediction, diagnostics = _run_scene_segmentation(
        scene_rgb,
        scene_label="source",
        settings=settings,
        semantic_threshold=semantic_threshold,
        cache_dir=cache_dir,
        x_ip_token=x_ip_token,
        progress_callback=progress_callback,
    )
    return {"segmentation_prediction": prediction}, diagnostics


def derive_new_building_products(
    change_prob: np.ndarray,
    t1_building_prob: np.ndarray,
    t2_building_prob: np.ndarray,
    *,
    change_threshold: float,
    semantic_threshold: float,
    min_new_building_pixels: int,
    old_building_mask_dilation_pixels: int,
    new_building_core_distance_pixels: int = 0,
    valid_comparison_mask: np.ndarray | None = None,
) -> dict[str, np.ndarray]:
    valid_mask = (
        np.ones(change_prob.shape, dtype=bool)
        if valid_comparison_mask is None
        else valid_comparison_mask.astype(bool)
    )
    change_mask = (change_prob >= change_threshold) & valid_mask
    t1_building_mask = (t1_building_prob >= semantic_threshold) & valid_mask
    t2_building_mask = (t2_building_prob >= semantic_threshold) & valid_mask
    t1_building_mask_dilated = dilate_mask(t1_building_mask, old_building_mask_dilation_pixels) & valid_mask
    new_building_mask_raw = change_mask & (~t1_building_mask_dilated) & t2_building_mask
    new_building_mask_filtered = suppress_edge_hugging_components(
        new_building_mask_raw,
        reference_mask=t1_building_mask_dilated,
        min_core_distance_pixels=new_building_core_distance_pixels,
    )
    new_building_mask, new_building_labels = remove_small_components(
        new_building_mask_filtered,
        min_new_building_pixels,
    )
    return {
        "change_mask": change_mask,
        "t1_building_mask": t1_building_mask,
        "t1_building_mask_dilated": t1_building_mask_dilated,
        "t2_building_mask": t2_building_mask,
        "new_building_mask_raw": new_building_mask_raw,
        "new_building_mask_filtered": new_building_mask_filtered,
        "new_building_mask": new_building_mask,
        "new_building_labels": new_building_labels,
    }
