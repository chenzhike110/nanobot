"""Structured media assets used by channels, tools, and context building."""

from __future__ import annotations

import hashlib
import mimetypes
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, TypeAlias

from nanobot.utils.helpers import detect_image_mime

MediaPurpose: TypeAlias = Literal["for_model", "for_user", "both"]
MediaSource: TypeAlias = Literal["channel", "web", "mcp", "tool", "system"]
MediaKind: TypeAlias = Literal["image", "audio", "video", "file"]


@dataclass
class MediaAsset:
    """Structured reference to a media item."""

    id: str
    kind: MediaKind
    purpose: MediaPurpose = "both"
    source: MediaSource = "channel"
    path: str | None = None
    url: str | None = None
    mime_type: str | None = None
    size_bytes: int | None = None
    width: int | None = None
    height: int | None = None
    sha256: str | None = None
    caption: str | None = None
    ocr_text: str | None = None
    vision_summary: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    expires_at: str | None = None
    variants: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_image(self) -> bool:
        return self.kind == "image"

    @property
    def for_model(self) -> bool:
        return self.purpose in ("for_model", "both")

    @property
    def for_user(self) -> bool:
        return self.purpose in ("for_user", "both")

    def preferred_path(self, *, for_model: bool = False) -> str | None:
        """Return the best local path for the requested use."""
        if for_model:
            return self.variants.get("model_optimized") or self.path or self.variants.get("original")
        return self.variants.get("thumbnail") or self.path or self.variants.get("original")

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict."""
        data = {
            "id": self.id,
            "kind": self.kind,
            "purpose": self.purpose,
            "source": self.source,
            "path": self.path,
            "url": self.url,
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
            "width": self.width,
            "height": self.height,
            "sha256": self.sha256,
            "caption": self.caption,
            "ocr_text": self.ocr_text,
            "vision_summary": self.vision_summary,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "variants": self.variants,
            "metadata": self.metadata,
        }
        return {k: v for k, v in data.items() if v not in (None, {}, [])}

    def history_text(self) -> str:
        """Render a compact textual reference suitable for history/context."""
        label = self.caption or self.vision_summary or self.ocr_text or self.metadata.get("label") or self.id
        suffix: list[str] = []
        if self.vision_summary:
            suffix.append(self.vision_summary)
        elif self.ocr_text:
            suffix.append(f"OCR: {self.ocr_text[:180]}")
        return f"[{self.kind} {self.id}: {label}]" + (f" {' '.join(suffix)}" if suffix else "")


MediaInput: TypeAlias = str | dict[str, Any] | MediaAsset


def _coerce_purpose(value: Any, default: MediaPurpose) -> MediaPurpose:
    if value in ("for_model", "for_user", "both"):
        return value
    return default


def _coerce_source(value: Any, default: MediaSource) -> MediaSource:
    if value in ("channel", "web", "mcp", "tool", "system"):
        return value
    return default


def _guess_kind(mime_type: str | None, path: str | None) -> MediaKind:
    if mime_type:
        prefix = mime_type.split("/", 1)[0]
        if prefix in ("image", "audio", "video"):
            return prefix  # type: ignore[return-value]
    guessed = mimetypes.guess_type(path or "")[0] if path else None
    if guessed:
        prefix = guessed.split("/", 1)[0]
        if prefix in ("image", "audio", "video"):
            return prefix  # type: ignore[return-value]
    return "file"


def _populate_path_metadata(asset: MediaAsset) -> MediaAsset:
    if not asset.path:
        return asset
    path = Path(asset.path).expanduser()
    asset.path = str(path)
    if not path.is_file():
        return asset

    try:
        raw = path.read_bytes()
    except OSError:
        return asset

    asset.size_bytes = asset.size_bytes or len(raw)
    asset.sha256 = asset.sha256 or hashlib.sha256(raw).hexdigest()
    if not asset.mime_type:
        asset.mime_type = detect_image_mime(raw) or mimetypes.guess_type(path.name, strict=False)[0]
    if asset.kind == "file" and asset.mime_type:
        asset.kind = _guess_kind(asset.mime_type, asset.path)
    if not asset.variants:
        asset.variants = {"original": asset.path}
    elif "original" not in asset.variants:
        asset.variants["original"] = asset.path
    return asset


def _build_id(path: str | None, sha256: str | None, fallback: str) -> str:
    if sha256:
        return f"asset_{sha256[:12]}"
    if path:
        digest = hashlib.sha1(path.encode("utf-8")).hexdigest()[:12]
        return f"asset_{digest}"
    digest = hashlib.sha1(fallback.encode("utf-8")).hexdigest()[:12]
    return f"asset_{digest}"


def media_asset_from_input(
    item: MediaInput,
    *,
    default_source: MediaSource = "channel",
    default_purpose: MediaPurpose = "both",
) -> MediaAsset:
    """Convert a string/dict/media asset into a normalized MediaAsset."""
    if isinstance(item, MediaAsset):
        asset = item
        asset.source = _coerce_source(asset.source, default_source)
        asset.purpose = _coerce_purpose(asset.purpose, default_purpose)
        return _populate_path_metadata(asset)

    if isinstance(item, str):
        asset = MediaAsset(
            id="",
            kind="file",
            purpose=default_purpose,
            source=default_source,
            path=item,
        )
        asset = _populate_path_metadata(asset)
        asset.id = asset.id or _build_id(asset.path, asset.sha256, item)
        return asset

    if not isinstance(item, dict):
        raise TypeError(f"unsupported media input: {type(item).__name__}")

    variants = item.get("variants")
    asset = MediaAsset(
        id=str(item.get("id") or ""),
        kind=item.get("kind") or _guess_kind(item.get("mime_type"), item.get("path")),
        purpose=_coerce_purpose(item.get("purpose"), default_purpose),
        source=_coerce_source(item.get("source"), default_source),
        path=item.get("path"),
        url=item.get("url"),
        mime_type=item.get("mime_type"),
        size_bytes=item.get("size_bytes"),
        width=item.get("width"),
        height=item.get("height"),
        sha256=item.get("sha256"),
        caption=item.get("caption"),
        ocr_text=item.get("ocr_text"),
        vision_summary=item.get("vision_summary"),
        created_at=item.get("created_at") or datetime.now().isoformat(),
        expires_at=item.get("expires_at"),
        variants=dict(variants) if isinstance(variants, dict) else {},
        metadata=dict(item.get("metadata") or {}),
    )
    asset = _populate_path_metadata(asset)
    asset.id = asset.id or _build_id(asset.path, asset.sha256, str(item))
    return asset


def normalize_media_items(
    items: list[MediaInput] | None,
    *,
    default_source: MediaSource = "channel",
    default_purpose: MediaPurpose = "both",
) -> list[MediaAsset]:
    """Normalize a mixed media list into MediaAsset objects."""
    if not items:
        return []
    normalized: list[MediaAsset] = []
    seen: set[tuple[str, str | None]] = set()
    for raw in items:
        try:
            asset = media_asset_from_input(
                raw,
                default_source=default_source,
                default_purpose=default_purpose,
            )
        except Exception:
            continue
        key = (asset.id, asset.preferred_path(for_model=False) or asset.url)
        if key in seen:
            continue
        seen.add(key)
        normalized.append(asset)
    return normalized


def filter_media_by_purpose(
    items: list[MediaInput] | None,
    purpose: MediaPurpose,
    *,
    default_source: MediaSource = "channel",
    default_purpose: MediaPurpose = "both",
) -> list[MediaAsset]:
    """Return assets visible to the requested purpose."""
    assets = normalize_media_items(
        items,
        default_source=default_source,
        default_purpose=default_purpose,
    )
    if purpose == "for_model":
        return [asset for asset in assets if asset.for_model]
    if purpose == "for_user":
        return [asset for asset in assets if asset.for_user]
    return assets


def iter_media_paths(
    items: list[MediaInput] | None,
    *,
    purpose: MediaPurpose | None = None,
    prefer_model: bool = False,
    default_source: MediaSource = "channel",
    default_purpose: MediaPurpose = "both",
) -> list[str]:
    """Collect concrete local file paths from media items."""
    assets = normalize_media_items(
        items,
        default_source=default_source,
        default_purpose=default_purpose,
    )
    paths: list[str] = []
    for asset in assets:
        if purpose == "for_model" and not asset.for_model:
            continue
        if purpose == "for_user" and not asset.for_user:
            continue
        path = asset.preferred_path(for_model=prefer_model)
        if path:
            paths.append(path)
    return paths
