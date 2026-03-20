"""MCP server for remote base-model services (SAM3 / FoundationPose / Piper)."""

from .server import server
from .observation import get_observation, list_cameras
from .track import foundationpose_initialize, foundationpose_track
from .segmentation import sam3_segment
from .tts import piper_tts

__all__ = [
    "server",
    "list_cameras",
    "get_observation",
    "foundationpose_initialize",
    "foundationpose_track",
    "sam3_segment",
    "piper_tts",
]