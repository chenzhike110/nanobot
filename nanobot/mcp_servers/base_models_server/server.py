from __future__ import annotations

import base64
import os
from typing import Any, Literal

import httpx
import numpy as np
from mcp import types
from mcp.server.fastmcp import FastMCP

from nanobot.media.store import AssetStore

from .utils import (
    encode_image, 
    encode_depth,
    decode_mask,
    load_image,
    load_depth,
    visualize_mask_on_image,
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
    description="""
Run SAM3 single-image segmentation; 

Arguments:
- image: image path
- points: list of points, each point is a dictionary with 'x', 'y', and 'label'(1 for positive, 0 for negative)
- box: list of 4 integers, representing the bounding box [x_left_top, y_left_top, x_right_bottom, y_right_bottom]
- english_text_prompt: text prompt for segmentation (name of the object to segment in English)

returns overlay image and mask file paths in text.

Attention:
- point, box and text should be provided only one of them.
- the coordinates of the box and point are normalized to [0, 1000] 
- text prompt should be in English
"""
)
async def sam3_segment(
    *,
    image: str,
    points: list[dict[str, float]] | None = None,
    box: list[int] | None = None,
    english_text_prompt: str | None = None
) -> list[types.ContentBlock]:
    # Services expect JPEG base64 strings, matching rpc/utils.py encode_image()
    if box is not None:
        box = {
            "x1": int(box[0]), 
            "y1": int(box[1]), 
            "x2": int(box[2]), 
            "y2": int(box[3])
        }
    
    if os.path.exists(image):
        image = load_image(image)
    else:
        raise ValueError(f"Image file not found: {image}")
        
    payload: dict[str, Any] = {
        "image": encode_image(image),
        "points": points,
        "box": box,
        "text_prompt": english_text_prompt,
        "mode": "interactive",
        "multimask_output": False
    }
    async with httpx.AsyncClient(timeout=60) as client:
        res = await client.post(f"{SAM3_URL}/v1/vision/segment", json=payload)
        res.raise_for_status()
        data = res.json()

    mask_b64 = data["masks"][0]
    scores = data["scores"][0]

    mask = decode_mask(mask_b64, target_shape=(image.shape[0], image.shape[1]))

    overlay = visualize_mask_on_image(image=image, mask=mask)
    overlay_b64 = encode_image(overlay)

    text_lines = "SAM3 segmentation done. scores={scores}"

    return [
        types.TextContent(type="text", text="\n".join(text_lines)),
        types.ImageContent(type="image", data=overlay_b64, mimeType="image/png", annotations=_anno("user")),
    ]


@server.tool(
    name="foundationpose_initialize",
    description="""
Initialize FoundationPose session; returns pose (text) session_id (for tracking) and axes overlay image (for_user).

Arguments:
- mesh_file: mesh file path
- rgb: rgb image path
- depth: depth image path
- mask: mask image path
- K: camera intrinsic matrix

returns pose (text) session_id (for tracking) and axes overlay image (for_user).
"""
)
async def foundationpose_initialize(
    *,
    mesh_file: str,
    rgb: str,
    depth: str,
    mask: str,
    K: list[list[float]],
) -> list[types.ContentBlock]:

    session_id = str(uuid.uuid4())
    mesh_filename = f"model_{session_id}.obj"
    # Step 1: upload mesh as multipart form data (mirrors FoundationPoseClient.upload_mesh)
    async with httpx.AsyncClient(timeout=60) as client:
        mesh_f = open(mesh_file, 'rb')
        upload_res = await client.post(
            f"{FOUNDATIONPOSE_URL}/foundationpose/upload_mesh",
            files={"file": (mesh_filename, mesh_f)},
            data={"filename": mesh_filename},
        )
        upload_res.raise_for_status()
        server_mesh_path: str = upload_res.json()["message"].split("saved to ")[-1]
    # Step 2: initialize with converted image formats matching rpc/utils.py:
    #   rgb / mask → JPEG base64  (encode_image)
    #   depth      → uint16 mm PNG base64  (encode_depth)

    rgb = load_image(rgb)
    depth = load_depth(depth)
    mask = load_image(mask)

    payload: dict[str, Any] = {
        "session_id": session_id,
        "mesh_file": server_mesh_path,
        "rgb": encode_image(rgb),
        "depth": encode_depth(depth),
        "mask": encode_image(mask),
        "K": K,
        "iteration": 2
    }

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
            text=f"FoundationPose initialize success={success}\nmessage={message}\npose={pose}\nsession_id={session_id}",
        )
    ]

    if pose and success:
        img = decode_image_base64(rgb)
        axes = draw_pose_axes_on_image(
            image_rgba=img,
            K=np.array(K, dtype=np.float64),
            pose_cam_obj=np.array(pose, dtype=np.float64),
            style=PoseAxesStyle(axis_length=0.1),
        )
        axes_b64 = encode_png_base64(axes)
        blocks.append(
            types.ImageContent(type="image", data=axes_b64, mimeType="image/png", annotations=_anno("for_user"))
        )
    return blocks


@server.tool(
    name="foundationpose_track",
    description="""
Track FoundationPose session; returns pose (text) and axes overlay image (for_user).

Arguments:
- session_id: session id
- rgb: rgb image path
- depth: depth image path
- K: camera intrinsic matrix
""",
)
async def foundationpose_track(
    *,
    session_id: str,
    rgb: str,
    depth: str,
    K: list[list[float]],
) -> list[types.ContentBlock]:
    # rgb → JPEG base64, depth → uint16 mm PNG base64 (mirrors rpc/utils.py)
    payload: dict[str, Any] = {
        "session_id": session_id,
        "rgb": load_image_b64(rgb),
        "depth": load_depth_b64(depth),
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
            style=PoseAxesStyle(axis_length=0.1),
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

