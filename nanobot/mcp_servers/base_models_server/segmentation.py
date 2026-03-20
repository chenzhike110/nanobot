import httpx
from typing import Any
from mcp import types
from .server import server, _env
from .utils import load_image, encode_image, decode_mask, visualize_mask_on_image

SAM3_URL = _env("NANOBOT_BASE_SAM3_URL", "http://localhost:16001")

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
    
    image = load_image(image)
        
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
        types.ImageContent(type="image", data=overlay_b64, mimeType="image/png", annotations=_anno("for_both")),
    ]