from __future__ import annotations

import base64
from dataclasses import dataclass
from io import BytesIO
from typing import Iterable

import numpy as np
from PIL import Image, ImageColor, ImageDraw


def _b64_to_bytes(data_b64: str) -> bytes:
    return base64.b64decode(data_b64.encode("utf-8"))


def _bytes_to_b64(data: bytes) -> str:
    return base64.b64encode(data).decode("utf-8")


def decode_image_base64(image_b64: str) -> Image.Image:
    img = Image.open(BytesIO(_b64_to_bytes(image_b64)))
    return img.convert("RGBA")


def decode_mask_base64(mask_b64: str) -> Image.Image:
    """
    Decode a binary mask payload (base64 of an image file) into an 8-bit mask.
    Non-zero pixels are treated as foreground.
    """
    mask = Image.open(BytesIO(_b64_to_bytes(mask_b64)))
    return mask.convert("L")


def overlay_masks_rgba(
    *,
    image_rgba: Image.Image,
    masks_l: Iterable[Image.Image],
    colors: list[str] | None = None,
    alpha: int = 110,
) -> Image.Image:
    if image_rgba.mode != "RGBA":
        image_rgba = image_rgba.convert("RGBA")

    base = image_rgba.copy()
    w, h = base.size
    palette = colors or ["#ff3355", "#22c55e", "#3b82f6", "#f59e0b", "#a855f7"]

    for idx, mask in enumerate(masks_l):
        if mask.mode != "L":
            mask = mask.convert("L")
        if mask.size != (w, h):
            mask = mask.resize((w, h), resample=Image.NEAREST)

        color = ImageColor.getrgb(palette[idx % len(palette)])
        layer = Image.new("RGBA", (w, h), color + (0,))
        mask_arr = np.array(mask, dtype=np.uint8)
        a = (mask_arr > 0).astype(np.uint8) * np.uint8(alpha)
        layer_arr = np.array(layer, dtype=np.uint8)
        layer_arr[..., 3] = a
        layer = Image.fromarray(layer_arr, mode="RGBA")
        base = Image.alpha_composite(base, layer)

    return base


@dataclass(frozen=True)
class PoseAxesStyle:
    axis_length: float = 0.1
    line_width: int = 4
    x_color: str = "#ff3355"
    y_color: str = "#22c55e"
    z_color: str = "#3b82f6"


def draw_pose_axes_on_image(
    *,
    image_rgba: Image.Image,
    K: np.ndarray,
    pose_cam_obj: np.ndarray,
    style: PoseAxesStyle = PoseAxesStyle(),
) -> Image.Image:
    if image_rgba.mode != "RGBA":
        image_rgba = image_rgba.convert("RGBA")
    img = image_rgba.copy()
    draw = ImageDraw.Draw(img)

    K = np.asarray(K, dtype=np.float64).reshape(3, 3)
    T = np.asarray(pose_cam_obj, dtype=np.float64).reshape(4, 4)

    L = float(style.axis_length)
    pts_obj = np.array(
        [
            [0.0, 0.0, 0.0, 1.0],
            [L, 0.0, 0.0, 1.0],
            [0.0, L, 0.0, 1.0],
            [0.0, 0.0, L, 1.0],
        ],
        dtype=np.float64,
    ).T

    pts_cam = (T @ pts_obj)
    X = pts_cam[:3, :]
    z = X[2, :]
    if np.any(z <= 1e-6):
        return img

    uvw = (K @ X)
    u = uvw[0, :] / uvw[2, :]
    v = uvw[1, :] / uvw[2, :]

    o = (float(u[0]), float(v[0]))
    x_end = (float(u[1]), float(v[1]))
    y_end = (float(u[2]), float(v[2]))
    z_end = (float(u[3]), float(v[3]))

    lw = int(style.line_width)
    draw.line([o, x_end], fill=style.x_color, width=lw)
    draw.line([o, y_end], fill=style.y_color, width=lw)
    draw.line([o, z_end], fill=style.z_color, width=lw)
    r = max(2, lw)
    draw.ellipse([o[0] - r, o[1] - r, o[0] + r, o[1] + r], fill="#ffffff")

    return img


def encode_png_base64(image_rgba: Image.Image) -> str:
    buf = BytesIO()
    image_rgba.save(buf, format="PNG")
    return _bytes_to_b64(buf.getvalue())

