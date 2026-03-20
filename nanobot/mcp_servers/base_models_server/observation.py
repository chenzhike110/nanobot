from __future__ import annotations

from dataclasses import dataclass

import cv2
from mcp import types

from nanobot.media.store import AssetStore

from .cameras.frame_hub import CameraFrame, get_frame_hub
from .cameras.rs_capture import RSCapture
from .server import _anno, server
from .utils import encode_depth, encode_image

_asset_store = AssetStore()


@dataclass
class _PreviewCacheItem:
    frame_id: int
    payload: dict


_preview_cache: dict[str, _PreviewCacheItem] = {}

@server.tool(
    name="list_cameras",
    description="""
List all cameras;

Return camera ids
"""
)
async def list_cameras() -> list[types.ContentBlock]:
    camera_ids = RSCapture.get_device_serial_numbers()
    return [
        types.TextContent(type="text", text=f"Camera ids: {camera_ids}"),
    ]


@server.tool(
    name="get_observation",
    description="""
Run observation;

Return Camera Image, Depth Image
"""
)
async def get_observation(
    *,
    camera_ids: list[str],
    include_depth: bool = False,
    max_age_ms: int = 300,
) -> list[types.ContentBlock]:
    hub = get_frame_hub()
    hub.ensure_cameras(camera_ids)

    blocks: list[types.ContentBlock] = []
    stale_cameras: list[str] = []
    for camera_id in camera_ids:
        frame = hub.get_latest(camera_id, max_age_ms=max_age_ms)
        if frame is None:
            stale_cameras.append(camera_id)
            continue

        rgb_b64 = encode_image(frame.rgb)
        blocks.append(
            types.ImageContent(
                type="image",
                data=rgb_b64,
                mimeType="image/jpeg",
                annotations=_anno("for_model"),
            )
        )
        if include_depth and frame.depth is not None:
            depth_b64 = encode_depth(frame.depth)
            blocks.append(
                types.ImageContent(
                    type="image",
                    data=depth_b64,
                    mimeType="image/png",
                    annotations=_anno("for_model"),
                )
            )

    status = (
        f"observation done; cameras={camera_ids}; fresh={len(camera_ids) - len(stale_cameras)};"
        f" stale={stale_cameras}; include_depth={include_depth}; max_age_ms={max_age_ms}"
    )
    return [types.TextContent(type="text", text=status), *blocks]


def get_camera_preview_payload(camera_id: str, *, max_age_ms: int = 500) -> dict | None:
    """Return preview payload for web channel consumers."""
    hub = get_frame_hub()
    hub.ensure_camera(camera_id)
    frame = hub.get_latest(camera_id, max_age_ms=max_age_ms)
    if frame is None:
        return None

    cached = _preview_cache.get(camera_id)
    if cached is not None and cached.frame_id == frame.frame_id:
        return cached.payload

    payload = _write_preview_asset(frame)
    _preview_cache[camera_id] = _PreviewCacheItem(frame_id=frame.frame_id, payload=payload)
    return payload


def _write_preview_asset(frame: CameraFrame) -> dict:
    ok, encoded = cv2.imencode(".jpg", frame.rgb)
    if not ok:
        raise ValueError(f"encode preview image failed for camera={frame.camera_id}")
    asset = _asset_store.write_bytes(
        encoded.tobytes(),
        mime_type="image/jpeg",
        source="mcp",
        purpose="for_user",
        filename=f"camera_{frame.camera_id}_{frame.frame_id}.jpg",
        caption=f"camera {frame.camera_id} frame {frame.frame_id}",
        kind="image",
    )
    return {
        "camera_id": frame.camera_id,
        "frame_id": frame.frame_id,
        "captured_at": frame.captured_at,
        "asset": asset.to_dict(),
    }
