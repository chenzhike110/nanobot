from __future__ import annotations

import os
from typing import Literal
from mcp import types
from mcp.server.fastmcp import FastMCP

from nanobot.media.store import AssetStore


def _env(name: str, default: str) -> str:
    return (os.getenv(name) or default).rstrip("/")


server = FastMCP("nanobot-base-models")
_asset_store = AssetStore()


def _anno(purpose: Literal["for_model", "for_user", "both"]) -> types.Annotations:
    if purpose == "for_user":
        return types.Annotations(audience=["user"])
    elif purpose == "for_model":
        return types.Annotations(audience=["assistant"])
    else:  # "both"
        return types.Annotations(audience=["user", "assistant"])
