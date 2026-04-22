from __future__ import annotations

import threading
import time

import numpy as np
from PIL import Image
import requests

from src.config import Settings
from src.domain.inference import (
    _predict_remote_patch_mask,
    _run_scene_segmentation,
    decode_remote_segmentation_result,
    derive_change_probability,
    run_tiled_inference,
)
from src.domain.model import RemoteSegmentationClientRegistry, RemoteSegmentationProviderPool


def test_remote_segmentation_client_registry_passes_hf_token_and_x_ip_token(monkeypatch) -> None:
    client_calls: list[dict[str, object]] = []

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            client_calls.append({"args": args, "kwargs": kwargs})

    monkeypatch.setattr("src.domain.model.Client", FakeClient)
    monkeypatch.setattr("src.domain.model._CLIENT_ACCEPTS_TOKEN", True)

    registry = RemoteSegmentationClientRegistry()
    registry.get_client(
        space="provider-a",
        timeout_sec=123,
        hf_token="hf_test_token",
        x_ip_token="x-ip-test",
    )

    assert len(client_calls) == 1
    assert client_calls[0]["args"] == ("provider-a",)
    assert client_calls[0]["kwargs"]["token"] == "hf_test_token"
    assert client_calls[0]["kwargs"]["headers"] == {
        "x-ip-token": "x-ip-test",
        "Authorization": "Bearer hf_test_token",
    }
    assert client_calls[0]["kwargs"]["download_files"] is False


def test_remote_segmentation_client_registry_falls_back_to_auth_header_when_token_kwarg_unsupported(monkeypatch) -> None:
    client_calls: list[dict[str, object]] = []

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            client_calls.append({"args": args, "kwargs": kwargs})

    monkeypatch.setattr("src.domain.model.Client", FakeClient)
    monkeypatch.setattr("src.domain.model._CLIENT_ACCEPTS_TOKEN", False)

    registry = RemoteSegmentationClientRegistry()
    registry.get_client(
        space="provider-a",
        timeout_sec=123,
        hf_token="hf_test_token",
        x_ip_token="x-ip-test",
    )

    assert len(client_calls) == 1
    assert "token" not in client_calls[0]["kwargs"]
    assert client_calls[0]["kwargs"]["headers"] == {
        "x-ip-token": "x-ip-test",
        "Authorization": "Bearer hf_test_token",
    }


def test_decode_remote_segmentation_result_unions_masks(tmp_path) -> None:
    mask_a_path = tmp_path / "mask_a.png"
    mask_b_path = tmp_path / "mask_b.png"

    mask_a = np.zeros((4, 4, 4), dtype=np.uint8)
    mask_a[0:2, 0:2, 0] = 255
    mask_a[0:2, 0:2, 3] = 255
    Image.fromarray(mask_a).save(mask_a_path)

    mask_b = np.zeros((4, 4, 4), dtype=np.uint8)
    mask_b[2:4, 2:4, 0] = 255
    mask_b[2:4, 2:4, 3] = 255
    Image.fromarray(mask_b).save(mask_b_path)

    result = {
        "annotations": [
            {"image": {"path": str(mask_a_path)}, "label": "building (0.90)"},
            {"image": {"path": str(mask_b_path)}, "label": "building (0.88)"},
        ]
    }

    decoded = decode_remote_segmentation_result(
        result,
        expected_shape=(4, 4),
        session=requests.Session(),
        timeout_sec=1,
    )

    expected = np.array(
        [
            [True, True, False, False],
            [True, True, False, False],
            [False, False, True, True],
            [False, False, True, True],
        ]
    )
    assert np.array_equal(decoded, expected)


def test_decode_remote_segmentation_result_accepts_tuple_payload(tmp_path) -> None:
    mask_path = tmp_path / "mask.png"

    mask = np.zeros((4, 4, 4), dtype=np.uint8)
    mask[1:3, 1:3, 0] = 255
    mask[1:3, 1:3, 3] = 255
    Image.fromarray(mask).save(mask_path)

    tuple_result = (
        {
            "image": {"path": str(mask_path)},
            "annotations": [{"image": {"path": str(mask_path)}, "label": "building (0.90)"}],
        },
        [{"image": {"path": str(mask_path)}, "caption": "object"}],
        "markdown output",
    )

    decoded = decode_remote_segmentation_result(
        tuple_result,
        expected_shape=(4, 4),
        session=requests.Session(),
        timeout_sec=1,
    )

    expected = np.zeros((4, 4), dtype=bool)
    expected[1:3, 1:3] = True
    assert np.array_equal(decoded, expected)


def test_derive_change_probability_is_positive_difference() -> None:
    t1 = np.array([[0.2, 0.6], [0.7, 0.1]], dtype=np.float32)
    t2 = np.array([[0.8, 0.4], [0.9, 0.1]], dtype=np.float32)

    change = derive_change_probability(t1, t2)

    expected = np.array([[0.6, 0.0], [0.2, 0.0]], dtype=np.float32)
    assert np.allclose(change, expected)


def test_scene_segmentation_reuses_cached_patch_results(tmp_path, monkeypatch) -> None:
    settings = Settings(
        patch_size=2,
        stride=2,
        remote_segmentation_max_parallel_patches=4,
        remote_segmentation_spaces=("provider-a",),
    )
    scene = np.ones((4, 4, 3), dtype=np.uint8) * 255
    expected_mask = np.array(
        [
            [True, False, True, False],
            [False, True, False, True],
            [True, False, True, False],
            [False, True, False, True],
        ]
    )
    call_count = {"value": 0}

    def fake_predict(patch_rgb, *args, **kwargs):
        call_count["value"] += 1
        patch_mask = np.array([[True, False], [False, True]])
        return patch_mask, 0.1, 0.2, 0.3

    monkeypatch.setattr("src.domain.inference._predict_remote_patch_mask", fake_predict)
    first_prob, _ = _run_scene_segmentation(
        scene,
        scene_label="t1",
        settings=settings,
        semantic_threshold=0.5,
        cache_dir=tmp_path,
    )
    assert call_count["value"] == 4
    assert np.array_equal(first_prob, expected_mask.astype(np.float32))

    def fail_predict(*args, **kwargs):
        raise AssertionError("Remote segmentation should not be called when patch cache exists.")

    monkeypatch.setattr("src.domain.inference._predict_remote_patch_mask", fail_predict)
    second_prob, second_diag = _run_scene_segmentation(
        scene,
        scene_label="t1",
        settings=settings,
        semantic_threshold=0.5,
        cache_dir=tmp_path,
    )
    assert np.array_equal(second_prob, expected_mask.astype(np.float32))
    assert second_diag.remote_seconds == 0.0


def test_provider_pool_skips_quota_limited_provider() -> None:
    settings = Settings(
        remote_segmentation_space="provider-a",
        remote_segmentation_spaces=("provider-a", "provider-b"),
    )
    pool = RemoteSegmentationProviderPool()

    assert pool.get_ready_spaces(settings) == ["provider-a", "provider-b"]

    pool.report_failure(
        "provider-a",
        RuntimeError("You have exceeded your GPU quota (60s requested vs. 0s left)."),
        settings=settings,
    )

    assert pool.get_ready_spaces(settings) == ["provider-b"]


def test_provider_pool_cools_refreshable_provider() -> None:
    settings = Settings(
        remote_segmentation_space="provider-a",
        remote_segmentation_spaces=("provider-a", "provider-b"),
        remote_segmentation_refreshable_provider_cooldown_sec=60,
    )
    pool = RemoteSegmentationProviderPool()

    pool.report_failure(
        "provider-a",
        RuntimeError("Expired ZeroGPU proxy token"),
        settings=settings,
    )

    assert pool.get_ready_spaces(settings) == ["provider-b"]


def test_provider_pool_enforces_provider_and_global_concurrency() -> None:
    settings = Settings(
        remote_segmentation_space="provider-a",
        remote_segmentation_spaces=("provider-a", "provider-b"),
        remote_segmentation_provider_max_concurrent_requests=1,
        remote_segmentation_max_parallel_patches=2,
    )
    pool = RemoteSegmentationProviderPool()

    assert pool.try_acquire("provider-a", settings=settings) is True
    assert pool.try_acquire("provider-a", settings=settings) is False
    assert pool.try_acquire("provider-b", settings=settings) is True
    assert pool.active_requests_total == 2
    assert pool.try_acquire("provider-b", settings=settings) is False
    assert pool.active_request_counts(settings) == {"provider-a": 1, "provider-b": 1}

    pool.release("provider-a", settings=settings)
    assert pool.try_acquire("provider-b", settings=settings) is False
    pool.release("provider-b", settings=settings)
    assert pool.try_acquire("provider-b", settings=settings) is True


def test_predict_remote_patch_mask_refreshes_expired_proxy_tokens_for_single_provider(monkeypatch) -> None:
    from src.domain import model as model_module

    settings = Settings(
        remote_segmentation_space="provider-a",
        remote_segmentation_spaces=("provider-a",),
        remote_segmentation_retries=2,
        remote_segmentation_client_refresh_retries=3,
    )
    patch = np.ones((2, 2, 3), dtype=np.uint8) * 255
    state = {"predict_calls": 0, "client_requests": 0}
    force_refresh_flags: list[bool] = []

    class FakeClient:
        def predict(self, **kwargs):
            state["predict_calls"] += 1
            if state["predict_calls"] == 1:
                raise RuntimeError("Expired ZeroGPU proxy token")
            return {"annotations": []}

    def fake_get_client(*, force_refresh: bool = False, **kwargs):
        state["client_requests"] += 1
        force_refresh_flags.append(force_refresh)
        return FakeClient()

    invalidate_calls: list[str | None] = []

    def fake_invalidate(*, space: str | None = None, **kwargs):
        invalidate_calls.append(space)

    report_failure_calls: list[str] = []

    def fake_report_failure(space: str, exc: Exception, *, settings: Settings):
        report_failure_calls.append(space)

    monkeypatch.setattr(model_module.REMOTE_SEGMENTATION_CLIENTS, "get_client", fake_get_client)
    monkeypatch.setattr(model_module.REMOTE_SEGMENTATION_CLIENTS, "invalidate", fake_invalidate)
    monkeypatch.setattr(model_module.REMOTE_SEGMENTATION_PROVIDER_POOL, "report_failure", fake_report_failure)
    monkeypatch.setattr(
        "src.domain.inference.decode_remote_segmentation_result",
        lambda *args, **kwargs: np.ones((2, 2), dtype=bool),
    )

    with requests.Session() as session:
        session.headers.update({"User-Agent": "test"})
        mask, *_ = _predict_remote_patch_mask(
            patch,
            settings=settings,
            semantic_threshold=0.5,
            session=session,
        )

    assert np.array_equal(mask, np.ones((2, 2), dtype=bool))
    assert state["predict_calls"] == 2
    assert state["client_requests"] == 2
    assert force_refresh_flags == [False, True]
    assert invalidate_calls == ["provider-a"]
    assert report_failure_calls == []


def test_predict_remote_patch_mask_rotates_away_from_refreshable_provider_in_multi_provider_mode(monkeypatch) -> None:
    from src.domain import model as model_module

    settings = Settings(
        remote_segmentation_space="provider-a",
        remote_segmentation_spaces=("provider-a", "provider-b"),
        remote_segmentation_retries=3,
        remote_segmentation_client_refresh_retries=2,
        remote_segmentation_refreshable_provider_cooldown_sec=60,
        remote_segmentation_provider_patience_sec=60,
    )
    patch = np.ones((2, 2, 3), dtype=np.uint8) * 255
    predict_calls: list[str] = []
    invalidate_calls: list[str | None] = []

    pool = RemoteSegmentationProviderPool()

    class FakeClient:
        def __init__(self, space: str) -> None:
            self.space = space

        def predict(self, **kwargs):
            predict_calls.append(self.space)
            if self.space == "provider-a":
                raise RuntimeError("Expired ZeroGPU proxy token")
            return {"annotations": []}

    class FakeRegistry:
        def get_client(self, *, space: str, **kwargs):
            return FakeClient(space)

        def invalidate(self, *, space: str | None = None, **kwargs):
            invalidate_calls.append(space)

    monkeypatch.setattr(model_module, "REMOTE_SEGMENTATION_PROVIDER_POOL", pool)
    monkeypatch.setattr(model_module, "REMOTE_SEGMENTATION_CLIENTS", FakeRegistry())
    monkeypatch.setattr(
        "src.domain.inference.decode_remote_segmentation_result",
        lambda *args, **kwargs: np.ones((2, 2), dtype=bool),
    )

    with requests.Session() as session:
        session.headers.update({"User-Agent": "test"})
        mask, *_ = _predict_remote_patch_mask(
            patch,
            settings=settings,
            semantic_threshold=0.5,
            session=session,
        )

    assert np.array_equal(mask, np.ones((2, 2), dtype=bool))
    assert predict_calls == ["provider-a", "provider-b"]
    assert invalidate_calls == ["provider-a"]
    assert pool.get_ready_spaces(settings) == ["provider-b"]


def test_predict_remote_patch_mask_tries_other_providers_before_failing(monkeypatch) -> None:
    from src.domain import model as model_module

    settings = Settings(
        remote_segmentation_space="provider-a",
        remote_segmentation_spaces=("provider-a", "provider-b"),
        remote_segmentation_retries=1,
        remote_segmentation_failure_cooldown_sec=1,
        remote_segmentation_provider_patience_sec=30,
    )
    patch = np.ones((2, 2, 3), dtype=np.uint8) * 255
    predict_calls: list[str] = []

    pool = RemoteSegmentationProviderPool()

    class FakeClient:
        def __init__(self, space: str) -> None:
            self.space = space

        def predict(self, **kwargs):
            predict_calls.append(self.space)
            if self.space == "provider-a":
                raise RuntimeError("temporary provider failure")
            return {"annotations": []}

    class FakeRegistry:
        def get_client(self, *, space: str, **kwargs):
            return FakeClient(space)

        def invalidate(self, *, space: str | None = None, **kwargs):
            return None

    monkeypatch.setattr(model_module, "REMOTE_SEGMENTATION_PROVIDER_POOL", pool)
    monkeypatch.setattr(model_module, "REMOTE_SEGMENTATION_CLIENTS", FakeRegistry())
    monkeypatch.setattr(
        "src.domain.inference.decode_remote_segmentation_result",
        lambda *args, **kwargs: np.ones((2, 2), dtype=bool),
    )

    with requests.Session() as session:
        session.headers.update({"User-Agent": "test"})
        mask, *_ = _predict_remote_patch_mask(
            patch,
            settings=settings,
            semantic_threshold=0.5,
            session=session,
        )

    assert np.array_equal(mask, np.ones((2, 2), dtype=bool))
    assert predict_calls == ["provider-a", "provider-b"]


def test_run_tiled_inference_runs_scenes_concurrently(monkeypatch) -> None:
    settings = Settings(scene_segmentation_concurrency=2)
    arr_t1 = np.zeros((2, 2, 3), dtype=np.uint8)
    arr_t2 = np.ones((2, 2, 3), dtype=np.uint8)

    active = 0
    max_active = 0
    lock = threading.Lock()

    def fake_run_scene(scene_rgb, *, scene_label, **kwargs):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.1)
        with lock:
            active -= 1
        if scene_label == "t1":
            return np.full((2, 2), 0.2, dtype=np.float32), type("Diag", (), {
                "patch_count": 1,
                "patch_prepare_seconds": 0.1,
                "remote_seconds": 0.2,
                "mask_decode_seconds": 0.3,
            })()
        return np.full((2, 2), 0.8, dtype=np.float32), type("Diag", (), {
            "patch_count": 1,
            "patch_prepare_seconds": 0.4,
            "remote_seconds": 0.5,
            "mask_decode_seconds": 0.6,
        })()

    monkeypatch.setattr("src.domain.inference._run_scene_segmentation", fake_run_scene)
    probs, diag = run_tiled_inference(
        arr_t1,
        arr_t2,
        settings=settings,
        semantic_threshold=0.5,
    )

    assert max_active >= 2
    assert np.allclose(probs["t1_semantic_prediction"], 0.2)
    assert np.allclose(probs["t2_semantic_prediction"], 0.8)
    assert np.allclose(probs["change_prediction"], 0.6)
    assert diag.patch_count == 2


def test_scene_segmentation_reports_every_completed_patch(monkeypatch) -> None:
    settings = Settings(
        patch_size=2,
        stride=2,
        remote_segmentation_max_parallel_patches=2,
        remote_segmentation_spaces=("provider-a",),
    )
    scene = np.zeros((2, 4, 3), dtype=np.uint8)
    scene[:, :2, :] = 10
    scene[:, 2:, :] = 20
    messages: list[str] = []

    def fake_predict(patch_rgb, *args, **kwargs):
        if int(patch_rgb[0, 0, 0]) == 10:
            time.sleep(0.05)
        return np.ones((2, 2), dtype=bool), 0.1, 0.2, 0.3

    monkeypatch.setattr("src.domain.inference._predict_remote_patch_mask", fake_predict)

    _run_scene_segmentation(
        scene,
        scene_label="t1",
        settings=settings,
        semantic_threshold=0.5,
        progress_callback=messages.append,
    )

    assert len(messages) == 2
    assert "1/2" in messages[0]
    assert "2/2" in messages[1]
    assert all("Segmented t1 patch progress" in message for message in messages)


def test_scene_segmentation_failure_preserves_successful_patch_cache(tmp_path, monkeypatch) -> None:
    settings = Settings(
        patch_size=2,
        stride=2,
        remote_segmentation_max_parallel_patches=2,
        remote_segmentation_spaces=("provider-a",),
    )
    scene = np.zeros((2, 4, 3), dtype=np.uint8)
    scene[:, :2, :] = 10
    scene[:, 2:, :] = 20

    def fake_predict(patch_rgb, *args, **kwargs):
        if int(patch_rgb[0, 0, 0]) == 10:
            return np.ones((2, 2), dtype=bool), 0.1, 0.2, 0.3
        time.sleep(0.05)
        raise RuntimeError("provider failed")

    monkeypatch.setattr("src.domain.inference._predict_remote_patch_mask", fake_predict)

    try:
        _run_scene_segmentation(
            scene,
            scene_label="t1",
            settings=settings,
            semantic_threshold=0.5,
            cache_dir=tmp_path,
        )
    except RuntimeError as exc:
        assert "Partial t1 progress is cached" in str(exc)
    else:  # pragma: no cover - defensive
        raise AssertionError("Expected scene segmentation to fail.")

    cache_files = sorted((tmp_path / "remote_sam3_cache" / "t1").glob("patch_*.npz"))
    assert len(cache_files) == 1
