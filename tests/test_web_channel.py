from __future__ import annotations

import asyncio
import base64
from pathlib import Path

import pytest

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.web import WebChannel
from nanobot.config.schema import WebConfig


_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+b8Z0AAAAASUVORK5CYII="
)


def _make_channel() -> WebChannel:
    return WebChannel(WebConfig(enabled=True, port=0), MessageBus())


def test_send_queues_serialized_messages(tmp_path: Path) -> None:
    channel = _make_channel()
    image_path = tmp_path / "image.png"
    image_path.write_bytes(_PNG_1X1)

    payload = OutboundMessage(
        channel="web",
        chat_id="chat-1",
        content="hello",
        media=[str(image_path)],
    )

    asyncio.run(channel.send(payload))
    messages = channel._drain_outbound("chat-1")

    assert messages[0]["role"] == "assistant"
    assert messages[0]["content"] == "hello"
    assert messages[0]["media"][0]["path"] == str(image_path)


def test_stream_send_broadcasts_without_writing_history() -> None:
    channel = _make_channel()
    subscriber = channel._subscribe("chat-1")

    payload = OutboundMessage(
        channel="web",
        chat_id="chat-1",
        content="he",
        metadata={"_stream": True, "_stream_kind": "text_delta", "_stream_id": "s1"},
    )

    asyncio.run(channel.send(payload))
    item = subscriber.get(timeout=1.0)
    messages = channel._drain_outbound("chat-1")

    assert item["metadata"]["_stream"] is True
    assert item["content"] == "he"
    assert messages == []
    channel._unsubscribe("chat-1", subscriber)


@pytest.mark.asyncio
async def test_publish_inbound_supports_inline_base64_media(tmp_path: Path) -> None:
    channel = _make_channel()
    channel._loop = asyncio.get_running_loop()
    captured = {}

    async def _fake_handle_message(**kwargs):
        captured.update(kwargs)

    channel._handle_message = _fake_handle_message  # type: ignore[method-assign]
    channel._publish_inbound(
        {
            "sender_id": "u1",
            "chat_id": "chat-1",
            "content": "look",
            "media": [
                {
                    "mime_type": "image/png",
                    "content_base64": base64.b64encode(_PNG_1X1).decode("utf-8"),
                    "purpose": "for_model",
                    "filename": "inline.png",
                }
            ],
        }
    )
    await asyncio.sleep(0)

    history, cursor = channel._full_history("chat-1")
    assert captured["chat_id"] == "chat-1"
    assert captured["media"][0]["source"] == "web"
    assert history[0]["role"] == "user"
    assert cursor == history[0]["id"]


def test_history_since_returns_incremental_messages(tmp_path: Path) -> None:
    channel = _make_channel()
    first = channel._append_history(
        "chat-1",
        channel._serialize_message(
            role="assistant",
            chat_id="chat-1",
            content="one",
            media=[],
            metadata={},
        ),
    )
    second = channel._append_history(
        "chat-1",
        channel._serialize_message(
            role="assistant",
            chat_id="chat-1",
            content="two",
            media=[],
            metadata={},
        ),
    )

    messages, cursor = channel._history_since("chat-1", since=first["id"])

    assert len(messages) == 1
    assert messages[0]["content"] == "two"
    assert cursor == second["id"]


def test_frontend_helpers_point_to_dist_and_missing_page() -> None:
    channel = _make_channel()
    dist_dir = channel._frontend_dist_dir()
    page = channel._missing_frontend_page()

    assert dist_dir.name == "dist"
    assert "npm run build" in page
    assert "Web UI not built" in page
