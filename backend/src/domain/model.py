from __future__ import annotations

from dataclasses import dataclass
import inspect
import threading
import time
from gradio_client import Client

from src.config import Settings
from src.utils.logging import get_logger


LOGGER = get_logger(__name__)

_CLIENT_ACCEPTS_TOKEN = "token" in inspect.signature(Client).parameters


class RemoteSegmentationClientRegistry:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._clients: dict[tuple[str, int, str, str], Client] = {}

    def get_client(
        self,
        *,
        space: str,
        timeout_sec: int,
        hf_token: str | None = None,
        x_ip_token: str | None = None,
        force_refresh: bool = False,
    ) -> Client:
        normalized_hf_token = hf_token or ""
        normalized_x_ip_token = x_ip_token or ""
        key = (space, timeout_sec, normalized_hf_token, normalized_x_ip_token)
        with self._lock:
            if force_refresh or key not in self._clients:
                headers: dict[str, str] = {}
                if normalized_x_ip_token:
                    headers["x-ip-token"] = normalized_x_ip_token
                if normalized_hf_token:
                    headers["Authorization"] = f"Bearer {normalized_hf_token}"

                client_kwargs = {
                    "verbose": False,
                    "download_files": False,
                    "httpx_kwargs": {"timeout": timeout_sec},
                    "headers": headers or None,
                }
                if _CLIENT_ACCEPTS_TOKEN:
                    client_kwargs["token"] = normalized_hf_token or None
                self._clients[key] = Client(
                    space,
                    **client_kwargs,
                )
                LOGGER.info(
                    "Initialized remote segmentation client for %s (hf_auth=%s, x_ip_forwarding=%s, client_token_arg=%s)",
                    space,
                    "enabled" if normalized_hf_token else "disabled",
                    "enabled" if normalized_x_ip_token else "disabled",
                    "enabled" if _CLIENT_ACCEPTS_TOKEN and normalized_hf_token else "disabled",
                )
            return self._clients[key]

    def invalidate(
        self,
        *,
        space: str | None = None,
        timeout_sec: int | None = None,
        hf_token: str | None = None,
        x_ip_token: str | None = None,
    ) -> None:
        with self._lock:
            if space is None and timeout_sec is None and hf_token is None and x_ip_token is None:
                self._clients = {}
                LOGGER.warning("Reset remote segmentation client cache.")
                return

            normalized_hf_token = hf_token or ""
            normalized_x_ip_token = x_ip_token or ""
            keys_to_delete = [
                key
                for key in self._clients
                if (space is None or key[0] == space)
                and (timeout_sec is None or key[1] == timeout_sec)
                and (hf_token is None or key[2] == normalized_hf_token)
                and (x_ip_token is None or key[3] == normalized_x_ip_token)
            ]
            for key in keys_to_delete:
                del self._clients[key]
            if keys_to_delete:
                LOGGER.warning("Reset remote segmentation client cache for %s.", space or "matching providers")


def is_quota_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return "exceeded your gpu quota" in message or "0s left" in message


def is_invalid_provider_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return (
        "api_name" in message
        or "404" in message
        or "space is in error" in message
        or "not found" in message
        or "cannot find api" in message
    )


def is_refreshable_provider_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return (
        "expired zerogpu proxy token" in message
        or ("proxy token" in message and "expired" in message)
        or ("zerogpu" in message and "token" in message and "expired" in message)
    )


@dataclass
class ProviderState:
    cooldown_until: float = 0.0
    consecutive_failures: int = 0
    last_error: str | None = None
    active_requests: int = 0


class RemoteSegmentationProviderPool:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._states: dict[str, ProviderState] = {}
        self._cursor = 0
        self._active_requests_total = 0

    def _ensure_states(self, spaces: tuple[str, ...]) -> None:
        for space in spaces:
            self._states.setdefault(space, ProviderState())

    def get_ready_spaces(self, settings: Settings) -> list[str]:
        spaces = settings.remote_segmentation_spaces
        with self._lock:
            self._ensure_states(spaces)
            now = time.monotonic()
            ready = [space for space in spaces if self._states[space].cooldown_until <= now]
            if not ready:
                return []
            start = self._cursor % len(ready)
            return ready[start:] + ready[:start]

    def try_acquire(self, space: str, *, settings: Settings) -> bool:
        with self._lock:
            self._ensure_states(settings.remote_segmentation_spaces)
            if self._active_requests_total >= max(1, settings.remote_segmentation_max_parallel_patches):
                return False
            state = self._states[space]
            if state.cooldown_until > time.monotonic():
                return False
            if state.active_requests >= max(1, settings.remote_segmentation_provider_max_concurrent_requests):
                return False
            state.active_requests += 1
            self._active_requests_total += 1
            return True

    def release(self, space: str, *, settings: Settings) -> None:
        with self._lock:
            self._ensure_states(settings.remote_segmentation_spaces)
            state = self._states[space]
            if state.active_requests > 0:
                state.active_requests -= 1
            if self._active_requests_total > 0:
                self._active_requests_total -= 1

    def next_retry_delay(self, settings: Settings) -> float:
        spaces = settings.remote_segmentation_spaces
        with self._lock:
            self._ensure_states(spaces)
            now = time.monotonic()
            future_delays = [
                state.cooldown_until - now
                for state in (self._states[space] for space in spaces)
                if state.cooldown_until > now
            ]
            if not future_delays:
                return float(settings.remote_segmentation_failure_cooldown_sec)
            return max(1.0, min(future_delays))

    def report_success(self, space: str, *, settings: Settings) -> None:
        with self._lock:
            self._ensure_states(settings.remote_segmentation_spaces)
            state = self._states[space]
            state.cooldown_until = 0.0
            state.consecutive_failures = 0
            state.last_error = None
            if space in settings.remote_segmentation_spaces:
                self._cursor = (settings.remote_segmentation_spaces.index(space) + 1) % len(
                    settings.remote_segmentation_spaces
                )

    def active_request_counts(self, settings: Settings) -> dict[str, int]:
        with self._lock:
            self._ensure_states(settings.remote_segmentation_spaces)
            return {space: self._states[space].active_requests for space in settings.remote_segmentation_spaces}

    @property
    def active_requests_total(self) -> int:
        with self._lock:
            return self._active_requests_total

    def report_failure(self, space: str, exc: Exception, *, settings: Settings) -> None:
        with self._lock:
            self._ensure_states(settings.remote_segmentation_spaces)
            state = self._states[space]
            state.consecutive_failures += 1
            state.last_error = f"{type(exc).__name__}: {exc}"
            if is_quota_error(exc):
                cooldown_sec = settings.remote_segmentation_quota_cooldown_sec
            elif is_invalid_provider_error(exc):
                cooldown_sec = settings.remote_segmentation_invalid_provider_cooldown_sec
            elif is_refreshable_provider_error(exc):
                cooldown_sec = settings.remote_segmentation_refreshable_provider_cooldown_sec
            else:
                cooldown_sec = settings.remote_segmentation_failure_cooldown_sec
            state.cooldown_until = max(state.cooldown_until, time.monotonic() + cooldown_sec)


REMOTE_SEGMENTATION_CLIENTS = RemoteSegmentationClientRegistry()
REMOTE_SEGMENTATION_PROVIDER_POOL = RemoteSegmentationProviderPool()
