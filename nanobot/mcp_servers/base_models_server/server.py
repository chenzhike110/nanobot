from __future__ import annotations

import base64
import os
from typing import Any, Literal

import httpx
import numpy as np
from mcp import types
from mcp.server.fastmcp import FastMCP

from nanobot.media.store import AssetStore

from .render import (
    PoseAxesStyle,
    decode_image_base64,
    decode_mask_base64,
    draw_pose_axes_on_image,
    encode_png_base64,
    overlay_masks_rgba,
)


def _env(name: str, default: str) -> str:
    return (os.getenv(name) or default).rstrip("/")


SAM3_URL = _env("NANOBOT_BASE_SAM3_URL", "http://localhost:16001")
FOUNDATIONPOSE_URL = _env("NANOBOT_BASE_FOUNDATIONPOSE_URL", "http://localhost:16030")
PIPER_URL = _env("NANOBOT_BASE_PIPER_URL", "http://localhost:16060")


server = FastMCP("nanobot-base-models")
_asset_store = AssetStore()


def _anno(purpose: Literal["for_model", "for_user", "both"]) -> types.Annotations:
    if purpose == "for_user":
        return types.Annotations(audience=["user"])
    elif purpose == "for_model":
        return types.Annotations(audience=["assistant"])
    else:  # "both"
        return types.Annotations(audience=["user", "assistant"])


@server.tool(
    name="sam3_segment",
    description="Run SAM3 single-image segmentation; returns overlay image (both) and mask file paths in text.",
)
async def sam3_segment(
    *,
    image: str,
    points: list[dict[str, Any]] | None = None,
    box: dict[str, float] | None = None,
    text_prompt: str | None = None,
    mask_input: str | None = None,
    multimask_output: bool = True,
    mode: Literal["interactive", "automatic"] = "interactive",
    overlay_alpha: int = 110,
) -> list[types.ContentBlock]:
    payload: dict[str, Any] = {
        "image": image,
        "points": points,
        "box": box,
        "text_prompt": text_prompt,
        "mask_input": mask_input,
        "multimask_output": multimask_output,
        "mode": mode,
    }
    async with httpx.AsyncClient(timeout=60) as client:
        res = await client.post(f"{SAM3_URL}/v1/vision/segment", json=payload)
        res.raise_for_status()
        data = res.json()

    masks_b64: list[str] = list(data.get("masks") or [])
    scores: list[float] = list(data.get("scores") or [])

    mask_paths: list[str] = []
    for i, m in enumerate(masks_b64):
        raw_bytes = base64.b64decode(m.encode("utf-8"))
        asset = _asset_store.write_bytes(
            raw_bytes,
            mime_type="image/png",
            source="mcp",
            purpose="for_model",
            filename=f"sam3_mask_{i}.png",
            caption=f"SAM3 mask {i}",
            kind="image",
        )
        mask_paths.append(asset.preferred_path(for_model=True) or asset.path or "")

    img = decode_image_base64(image)
    masks = [decode_mask_base64(m) for m in masks_b64]
    overlay = overlay_masks_rgba(image_rgba=img, masks_l=masks, alpha=int(overlay_alpha))
    overlay_b64 = encode_png_base64(overlay)

    text_lines = ["SAM3 segmentation done.", f"num_masks={len(masks_b64)}"]
    if scores:
        text_lines.append(f"scores={scores}")
    for i, p in enumerate(mask_paths):
        text_lines.append(f"mask_{i}_path={p}")

    return [
        types.TextContent(type="text", text="\n".join(text_lines)),
        types.ImageContent(type="image", data=overlay_b64, mimeType="image/png", annotations=_anno("both")),
    ]


@server.tool(
    name="foundationpose_initialize",
    description="Initialize FoundationPose session; returns pose (text) and axes overlay image (for_user).",
)
async def foundationpose_initialize(
    *,
    session_id: str,
    mesh_file: str,
    rgb: str,
    depth: str,
    mask: str,
    K: list[list[float]],
    iteration: int | None = None,
    axis_length: float = 0.1,
) -> list[types.ContentBlock]:
    payload: dict[str, Any] = {
        "session_id": session_id,
        "mesh_file": mesh_file,
        "rgb": rgb,
        "depth": depth,
        "mask": mask,
        "K": K,
    }
    if iteration is not None:
        payload["iteration"] = iteration

    async with httpx.AsyncClient(timeout=120) as client:
        res = await client.post(f"{FOUNDATIONPOSE_URL}/foundationpose/initialize", json=payload)
        res.raise_for_status()
        data = res.json()

    pose = data.get("pose")
    success = bool(data.get("success", True))
    message = data.get("message")

    blocks: list[types.ContentBlock] = [
        types.TextContent(
            type="text",
            text=f"FoundationPose initialize success={success}\nmessage={message}\npose={pose}",
        )
    ]

    if pose and success:
        img = decode_image_base64(rgb)
        axes = draw_pose_axes_on_image(
            image_rgba=img,
            K=np.array(K, dtype=np.float64),
            pose_cam_obj=np.array(pose, dtype=np.float64),
            style=PoseAxesStyle(axis_length=float(axis_length)),
        )
        axes_b64 = encode_png_base64(axes)
        blocks.append(
            types.ImageContent(type="image", data=axes_b64, mimeType="image/png", annotations=_anno("for_user"))
        )
    return blocks


@server.tool(
    name="foundationpose_track",
    description="Track FoundationPose session; returns pose (text) and axes overlay image (for_user).",
)
async def foundationpose_track(
    *,
    session_id: str,
    rgb: str,
    depth: str,
    K: list[list[float]],
    iteration: int | None = None,
    axis_length: float = 0.1,
) -> list[types.ContentBlock]:
    payload: dict[str, Any] = {
        "session_id": session_id,
        "rgb": rgb,
        "depth": depth,
        "K": K,
    }
    if iteration is not None:
        payload["iteration"] = iteration

    async with httpx.AsyncClient(timeout=120) as client:
        res = await client.post(f"{FOUNDATIONPOSE_URL}/foundationpose/track", json=payload)
        res.raise_for_status()
        data = res.json()

    pose = data.get("pose")
    success = bool(data.get("success", True))
    message = data.get("message")

    blocks: list[types.ContentBlock] = [
        types.TextContent(
            type="text",
            text=f"FoundationPose track success={success}\nmessage={message}\npose={pose}",
        )
    ]
    if pose and success:
        img = decode_image_base64(rgb)
        axes = draw_pose_axes_on_image(
            image_rgba=img,
            K=np.array(K, dtype=np.float64),
            pose_cam_obj=np.array(pose, dtype=np.float64),
            style=PoseAxesStyle(axis_length=float(axis_length)),
        )
        axes_b64 = encode_png_base64(axes)
        blocks.append(
            types.ImageContent(type="image", data=axes_b64, mimeType="image/png", annotations=_anno("for_user"))
        )
    return blocks


@server.tool(
    name="piper_tts",
    description="Call remote Piper TTS and return WAV audio (for_user).",
)
async def piper_tts(
    *,
    text: str
) -> list[types.ContentBlock]:
    payload: dict[str, Any] = {"text": text, "output": "wav"}

    async with httpx.AsyncClient(timeout=120) as client:
        res = await client.post(f"{PIPER_URL}/v1/audio/tts", json=payload)
        res.raise_for_status()
        audio_bytes = res.content

    mime = "audio/wav"
    b64 = base64.b64encode(audio_bytes).decode("utf-8")
    return [
        types.TextContent(type="text", text=f"Piper TTS ok"),
        types.AudioContent(type="audio", data=b64, mimeType=mime, annotations=_anno("for_user")),
    ]

