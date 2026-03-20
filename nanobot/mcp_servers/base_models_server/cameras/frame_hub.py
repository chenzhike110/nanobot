from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from typing import Any

import numpy as np

from .rs_capture import RSCapture


@dataclass
class CameraFrame:
    camera_id: str
    frame_id: int
    captured_at: float
    rgb: np.ndarray
    depth: np.ndarray | None


@dataclass
class CameraFrameEvent:
    camera_id: str
    frame_id: int
    captured_at: float


class CameraFrameHub:
    """Single-producer camera hub with latest-frame cache."""

    def __init__(self, *, target_fps: int = 15, queue_size: int = 2):
        self._target_fps = max(1, int(target_fps))
        self._queue_size = max(1, int(queue_size))
        self._lock = threading.Lock()
        self._running = True
        self._captures: dict[str, RSCapture] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._latest: dict[str, CameraFrame] = {}
        self._subscribers: dict[str, list[queue.Queue[CameraFrameEvent | None]]] = {}
        self._next_frame_id = 0

    def ensure_camera(self, camera_id: str) -> None:
        with self._lock:
            if camera_id in self._threads:
                return
            capture = RSCapture(name=f"camera-{camera_id}", serial_number=camera_id)
            self._captures[camera_id] = capture
            thread = threading.Thread(
                target=self._capture_loop,
                args=(camera_id,),
                name=f"nanobot-camera-{camera_id}",
                daemon=True,
            )
            self._threads[camera_id] = thread
            thread.start()

    def ensure_cameras(self, camera_ids: list[str]) -> None:
        for camera_id in camera_ids:
            self.ensure_camera(camera_id)

    def get_latest(self, camera_id: str, *, max_age_ms: int = 300) -> CameraFrame | None:
        with self._lock:
            frame = self._latest.get(camera_id)
        if frame is None:
            return None
        max_age_s = max(0, max_age_ms) / 1000.0
        if max_age_s > 0 and (time.time() - frame.captured_at) > max_age_s:
            return None
        return frame

    def subscribe(self, camera_id: str) -> queue.Queue[CameraFrameEvent | None]:
        q: queue.Queue[CameraFrameEvent | None] = queue.Queue(maxsize=self._queue_size)
        with self._lock:
            self._subscribers.setdefault(camera_id, []).append(q)
        return q

    def unsubscribe(self, camera_id: str, q: queue.Queue[CameraFrameEvent | None]) -> None:
        with self._lock:
            listeners = self._subscribers.get(camera_id, [])
            self._subscribers[camera_id] = [item for item in listeners if item is not q]
            if not self._subscribers[camera_id]:
                self._subscribers.pop(camera_id, None)

    def shutdown(self) -> None:
        self._running = False
        with self._lock:
            captures = list(self._captures.values())
            subscribers = {
                key: list(items)
                for key, items in self._subscribers.items()
            }
            self._subscribers.clear()
        for _, items in subscribers.items():
            for q in items:
                try:
                    q.put_nowait(None)
                except queue.Full:
                    pass
        for capture in captures:
            try:
                capture.close()
            except Exception:
                continue

    def _capture_loop(self, camera_id: str) -> None:
        sleep_s = 1.0 / float(self._target_fps)
        while self._running:
            with self._lock:
                capture = self._captures.get(camera_id)
            if capture is None:
                return
            try:
                ret, rgb, depth = capture.read()
            except Exception:
                time.sleep(0.2)
                continue
            if not ret or rgb is None:
                time.sleep(0.05)
                continue

            with self._lock:
                self._next_frame_id += 1
                frame = CameraFrame(
                    camera_id=camera_id,
                    frame_id=self._next_frame_id,
                    captured_at=time.time(),
                    rgb=rgb,
                    depth=depth,
                )
                self._latest[camera_id] = frame
                listeners = list(self._subscribers.get(camera_id, []))

            event = CameraFrameEvent(
                camera_id=camera_id,
                frame_id=frame.frame_id,
                captured_at=frame.captured_at,
            )
            for q in listeners:
                try:
                    q.put_nowait(event)
                except queue.Full:
                    try:
                        q.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        q.put_nowait(event)
                    except queue.Full:
                        pass
            time.sleep(sleep_s)


_frame_hub: CameraFrameHub | None = None
_frame_hub_lock = threading.Lock()


def get_frame_hub() -> CameraFrameHub:
    global _frame_hub
    if _frame_hub is not None:
        return _frame_hub
    with _frame_hub_lock:
        if _frame_hub is None:
            _frame_hub = CameraFrameHub()
    return _frame_hub


def get_camera_preview(camera_id: str, *, max_age_ms: int = 300) -> dict[str, Any] | None:
    """Small helper for web adapters."""
    frame = get_frame_hub().get_latest(camera_id, max_age_ms=max_age_ms)
    if frame is None:
        return None
    return {
        "camera_id": frame.camera_id,
        "frame_id": frame.frame_id,
        "captured_at": frame.captured_at,
    }
