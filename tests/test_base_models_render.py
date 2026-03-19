from __future__ import annotations

import base64
from io import BytesIO

import numpy as np
from PIL import Image

from mcp.base_models_server.render import (
    PoseAxesStyle,
    decode_image_base64,
    draw_pose_axes_on_image,
    encode_png_base64,
    overlay_masks_rgba,
)


def _img_to_b64_png(img: Image.Image) -> str:
    buf = BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def test_overlay_masks_outputs_png_same_size() -> None:
    rgb = Image.new("RGB", (8, 8), (10, 10, 10)).convert("RGBA")
    mask = Image.new("L", (8, 8), 0)
    mask.paste(255, (2, 2, 6, 6))

    out = overlay_masks_rgba(image_rgba=rgb, masks_l=[mask], alpha=120)
    assert out.size == (8, 8)

    out_b64 = encode_png_base64(out)
    raw = base64.b64decode(out_b64.encode("utf-8"))
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"


def test_draw_pose_axes_identity_pose_runs() -> None:
    rgb = Image.new("RGB", (64, 48), (0, 0, 0)).convert("RGBA")
    # Simple pinhole intrinsics
    K = np.array([[80.0, 0.0, 32.0], [0.0, 80.0, 24.0], [0.0, 0.0, 1.0]], dtype=np.float64)
    # Identity pose but translate forward to keep z>0
    T = np.eye(4, dtype=np.float64)
    T[2, 3] = 1.0

    out = draw_pose_axes_on_image(
        image_rgba=rgb,
        K=K,
        pose_cam_obj=T,
        style=PoseAxesStyle(axis_length=0.2, line_width=3),
    )
    assert out.size == (64, 48)

