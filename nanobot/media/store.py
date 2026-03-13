"""Runtime asset store for tool- and web-generated media."""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
from pathlib import Path

from nanobot.config.paths import get_assets_dir
from nanobot.media.assets import MediaAsset, MediaKind, MediaPurpose, MediaSource, media_asset_from_input
from nanobot.utils.helpers import ensure_dir, safe_filename


class AssetStore:
    """Persist or register media assets under a shared runtime directory."""

    def __init__(self, base_dir: Path | None = None):
        self.base_dir = ensure_dir(base_dir or get_assets_dir())
        self._index_path = self.base_dir / "index.json"

    def _load_index(self) -> dict[str, dict]:
        if not self._index_path.exists():
            return {}
        try:
            return json.loads(self._index_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_index(self, data: dict[str, dict]) -> None:
        self._index_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _remember(self, asset: MediaAsset) -> MediaAsset:
        index = self._load_index()
        index[asset.id] = asset.to_dict()
        self._save_index(index)
        return asset

    def get(self, asset_id: str) -> MediaAsset | None:
        payload = self._load_index().get(asset_id)
        if not payload:
            return None
        return media_asset_from_input(payload, default_source=payload.get("source", "tool"))

    def rehydrate(self, asset_ids: list[str]) -> list[MediaAsset]:
        return [asset for asset_id in asset_ids if (asset := self.get(asset_id)) is not None]

    def register_path(
        self,
        path: str | Path,
        *,
        source: MediaSource,
        purpose: MediaPurpose,
        kind: MediaKind | None = None,
        caption: str | None = None,
    ) -> MediaAsset:
        """Wrap an existing local file as a structured asset."""
        payload = {
            "path": str(Path(path).expanduser()),
            "source": source,
            "purpose": purpose,
            "caption": caption,
        }
        if kind:
            payload["kind"] = kind
        return self._remember(
            media_asset_from_input(payload, default_source=source, default_purpose=purpose)
        )

    def write_bytes(
        self,
        data: bytes,
        *,
        mime_type: str,
        source: MediaSource,
        purpose: MediaPurpose,
        filename: str | None = None,
        caption: str | None = None,
        kind: MediaKind | None = None,
    ) -> MediaAsset:
        """Persist bytes and return a structured asset."""
        digest = hashlib.sha256(data).hexdigest()
        ext = mimetypes.guess_extension(mime_type, strict=False) or ""
        base_name = safe_filename(filename or f"{digest[:12]}{ext}" or digest[:12])
        target = self.base_dir / source / f"{digest[:12]}_{base_name}"
        ensure_dir(target.parent)
        if not target.exists():
            target.write_bytes(data)
        return self.register_path(
            target,
            source=source,
            purpose=purpose,
            kind=kind,
            caption=caption,
        )

    def write_base64(
        self,
        payload: str,
        *,
        mime_type: str,
        source: MediaSource,
        purpose: MediaPurpose,
        filename: str | None = None,
        caption: str | None = None,
        kind: MediaKind | None = None,
    ) -> MediaAsset:
        """Decode base64 payload and persist it as an asset."""
        return self.write_bytes(
            base64.b64decode(payload),
            mime_type=mime_type,
            source=source,
            purpose=purpose,
            filename=filename,
            caption=caption,
            kind=kind,
        )
