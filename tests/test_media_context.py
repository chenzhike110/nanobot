from __future__ import annotations

import base64
from pathlib import Path

from nanobot.agent.context import ContextBuilder
from nanobot.media.assets import normalize_media_items
from nanobot.media.store import AssetStore


_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+b8Z0AAAAASUVORK5CYII="
)


def _write_png(path: Path) -> None:
    path.write_bytes(_PNG_1X1)


def test_asset_store_can_rehydrate_by_id(tmp_path: Path) -> None:
    store = AssetStore(tmp_path / "assets")
    image_path = tmp_path / "sample.png"
    _write_png(image_path)

    asset = store.register_path(image_path, source="web", purpose="both", kind="image")
    loaded = store.get(asset.id)

    assert loaded is not None
    assert loaded.id == asset.id
    assert loaded.path == str(image_path)


def test_context_builder_replaces_excess_images_with_text_summary(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    builder = ContextBuilder(workspace)

    media = []
    for idx in range(4):
        image_path = tmp_path / f"image-{idx}.png"
        _write_png(image_path)
        media.append(str(image_path))

    content = builder._build_user_content("compare these", media)

    assert isinstance(content, list)
    image_blocks = [block for block in content if block.get("type") == "image_url"]
    text_blocks = [block for block in content if block.get("type") == "text"]
    assert len(image_blocks) == 3
    assert any("Referenced assets kept as text history" in block.get("text", "") for block in text_blocks)
    assert text_blocks[-1]["text"] == "compare these"


def test_normalize_media_items_keeps_structured_purpose(tmp_path: Path) -> None:
    image_path = tmp_path / "demo.png"
    _write_png(image_path)

    media = normalize_media_items([
        {
            "id": "asset_demo",
            "kind": "image",
            "purpose": "for_model",
            "source": "tool",
            "path": str(image_path),
            "caption": "demo",
        }
    ])

    assert media[0].id == "asset_demo"
    assert media[0].for_model is True
    assert media[0].for_user is False
