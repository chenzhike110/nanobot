import httpx
import base64
from typing import Any
from mcp import types
from .server import server, _env

PIPER_URL = _env("NANOBOT_BASE_PIPER_URL", "http://localhost:16060")

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
