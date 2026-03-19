from __future__ import annotations

import asyncio
import base64
import json
import sys
from types import ModuleType, SimpleNamespace

import pytest

from nanobot.agent.tools.mcp import MCPToolWrapper
from nanobot.agent.tools.base import ToolExecutionResult
from nanobot.media.assets import MediaAsset


class _FakeTextContent:
    def __init__(self, text: str) -> None:
        self.text = text


@pytest.fixture(autouse=True)
def _fake_mcp_module(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = ModuleType("mcp")
    mod.types = SimpleNamespace(TextContent=_FakeTextContent)
    monkeypatch.setitem(sys.modules, "mcp", mod)


def _make_wrapper(session: object, *, timeout: float = 0.1) -> MCPToolWrapper:
    tool_def = SimpleNamespace(
        name="demo",
        description="demo tool",
        inputSchema={"type": "object", "properties": {}},
    )
    return MCPToolWrapper(session, "test", tool_def, tool_timeout=timeout)


class _FakeBinaryContent:
    def __init__(self, *, mimeType: str, data: str, annotations: dict | None = None) -> None:
        self.mimeType = mimeType
        self.data = data
        self.annotations = annotations or {}


@pytest.mark.asyncio
async def test_execute_returns_text_blocks() -> None:
    async def call_tool(_name: str, arguments: dict) -> object:
        assert arguments == {"value": 1}
        return SimpleNamespace(content=[_FakeTextContent("hello"), 42])

    wrapper = _make_wrapper(SimpleNamespace(call_tool=call_tool))

    result = await wrapper.execute(value=1)

    assert result == "hello\n42"


@pytest.mark.asyncio
async def test_execute_returns_timeout_message() -> None:
    async def call_tool(_name: str, arguments: dict) -> object:
        await asyncio.sleep(1)
        return SimpleNamespace(content=[])

    wrapper = _make_wrapper(SimpleNamespace(call_tool=call_tool), timeout=0.01)

    result = await wrapper.execute()

    assert result == "(MCP tool call timed out after 0.01s)"


@pytest.mark.asyncio
async def test_execute_handles_server_cancelled_error() -> None:
    async def call_tool(_name: str, arguments: dict) -> object:
        raise asyncio.CancelledError()

    wrapper = _make_wrapper(SimpleNamespace(call_tool=call_tool))

    result = await wrapper.execute()

    assert result == "(MCP tool call was cancelled)"


@pytest.mark.asyncio
async def test_execute_re_raises_external_cancellation() -> None:
    started = asyncio.Event()

    async def call_tool(_name: str, arguments: dict) -> object:
        started.set()
        await asyncio.sleep(60)
        return SimpleNamespace(content=[])

    wrapper = _make_wrapper(SimpleNamespace(call_tool=call_tool), timeout=10)
    task = asyncio.create_task(wrapper.execute())
    await started.wait()

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_execute_handles_generic_exception() -> None:
    async def call_tool(_name: str, arguments: dict) -> object:
        raise RuntimeError("boom")

    wrapper = _make_wrapper(SimpleNamespace(call_tool=call_tool))

    result = await wrapper.execute()

    assert result == "(MCP tool call failed: RuntimeError)"


@pytest.mark.asyncio
async def test_execute_parses_structured_media_payload() -> None:
    payload = {
        "content": "generated image",
        "media": [
            {
                "id": "asset_demo",
                "kind": "image",
                "purpose": "for_user",
                "source": "mcp",
                "path": "/tmp/demo.png",
                "mime_type": "image/png",
            }
        ],
    }

    async def call_tool(_name: str, arguments: dict) -> object:
        return SimpleNamespace(content=[_FakeTextContent(json.dumps(payload))])

    wrapper = _make_wrapper(SimpleNamespace(call_tool=call_tool))
    result = await wrapper.execute()

    assert isinstance(result, ToolExecutionResult)
    assert result.content == "generated image"
    assert isinstance(result.media[0], MediaAsset)
    assert result.media[0].id == "asset_demo"


@pytest.mark.asyncio
async def test_execute_converts_audio_block_to_media_asset() -> None:
    audio_b64 = base64.b64encode(b"RIFF....WAVE").decode("utf-8")

    async def call_tool(_name: str, arguments: dict) -> object:
        return SimpleNamespace(
            content=[
                _FakeTextContent("ok"),
                _FakeBinaryContent(
                    mimeType="audio/wav",
                    data=audio_b64,
                    annotations={"audience": "for_user"},
                ),
            ]
        )

    wrapper = _make_wrapper(SimpleNamespace(call_tool=call_tool))
    result = await wrapper.execute()

    assert isinstance(result, ToolExecutionResult)
    assert result.content == "ok"
    assert len(result.media) == 1
    assert result.media[0].kind == "audio"
