import httpx
import uuid
from typing import Any
from mcp import types
from .server import server, _env
from .utils import load_image, load_depth, encode_image, encode_depth, draw_coordinate_frame

FOUNDATIONPOSE_URL = _env("NANOBOT_BASE_FOUNDATIONPOSE_URL", "http://localhost:16030")

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
        image = draw_coordinate_frame(
            image=rgb,
            pose=pose,
            K=K,
            axis_length=0.1,
            thickness=3
        )
        axes_b64 = encode_image(image)
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
    rgb = load_image(rgb)
    depth = load_depth(depth)
    payload: dict[str, Any] = {
        "session_id": session_id,
        "rgb": encode_image(rgb),
        "depth": encode_depth(depth),
        "K": K,
        "iteration": 2
    }

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
        image = draw_coordinate_frame(
            image=rgb,
            pose=pose,
            K=K,
            axis_length=0.1,
            thickness=3
        )
        axes_b64 = encode_image(image)
        blocks.append(
            types.ImageContent(type="image", data=axes_b64, mimeType="image/png", annotations=_anno("for_user"))
        )
    return blocks
      