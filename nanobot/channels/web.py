"""Built-in web channel with a richer chat UI and polling API."""

from __future__ import annotations

import asyncio
import json
import mimetypes
import queue
import threading
import time
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.paths import get_data_dir
from nanobot.config.schema import WebConfig
from nanobot.media.assets import MediaAsset, normalize_media_items
from nanobot.media.store import AssetStore


class WebChannel(BaseChannel):
    """HTTP channel for browser-based chat and uploads."""

    name = "web"
    display_name = "Web"

    def __init__(self, config: WebConfig, bus: MessageBus):
        super().__init__(config, bus)
        self.config: WebConfig = config
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_future: asyncio.Future[None] | None = None
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._asset_store = AssetStore()
        self._history: dict[str, list[dict[str, Any]]] = {}
        self._history_lock = threading.Lock()
        self._subscribers: dict[str, list[queue.Queue[dict[str, Any] | None]]] = {}
        self._subscriber_lock = threading.Lock()
        self._next_event_id = 0
        self._data_dir = get_data_dir().resolve()
        # Optional callback to expose current tool definitions to the web UI.
        # Signature: () -> list[dict[str, Any]]
        self._tools_getter: Callable[[], list[dict[str, Any]]] | None = None
        # Optional callback to expose latest camera preview payload.
        # Signature: (camera_id: str, max_age_ms: int) -> dict[str, Any] | None
        self._camera_preview_getter: Callable[[str, int], dict[str, Any] | None] | None = None

    def set_tools_getter(self, getter: Callable[[], list[dict[str, Any]]]) -> None:
        """Inject a callable that returns current tool definitions for /tools."""
        self._tools_getter = getter

    def set_camera_preview_getter(
        self,
        getter: Callable[[str, int], dict[str, Any] | None],
    ) -> None:
        """Inject a callable that returns latest camera preview payload."""
        self._camera_preview_getter = getter

    def _cors_origin(self) -> str:
        origins = self.config.cors_origins or ["*"]
        return "*" if "*" in origins else origins[0]

    def _append_history(self, chat_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._history_lock:
            self._next_event_id += 1
            item = dict(payload)
            item["id"] = self._next_event_id
            self._history.setdefault(chat_id, []).append(item)
            return item

    def _history_since(self, chat_id: str, since: int = 0) -> tuple[list[dict[str, Any]], int]:
        with self._history_lock:
            entries = list(self._history.get(chat_id, []))
        messages = [item for item in entries if int(item.get("id", 0)) > since]
        next_cursor = int(entries[-1]["id"]) if entries else since
        return messages, next_cursor

    def _full_history(self, chat_id: str) -> tuple[list[dict[str, Any]], int]:
        return self._history_since(chat_id, since=0)

    def _drain_outbound(self, chat_id: str) -> list[dict[str, Any]]:
        messages, _ = self._full_history(chat_id)
        return messages

    def _subscribe(self, chat_id: str) -> queue.Queue[dict[str, Any] | None]:
        q: queue.Queue[dict[str, Any] | None] = queue.Queue()
        with self._subscriber_lock:
            self._subscribers.setdefault(chat_id, []).append(q)
        return q

    def _unsubscribe(self, chat_id: str, q: queue.Queue[dict[str, Any] | None]) -> None:
        with self._subscriber_lock:
            listeners = self._subscribers.get(chat_id, [])
            self._subscribers[chat_id] = [item for item in listeners if item is not q]
            if not self._subscribers[chat_id]:
                self._subscribers.pop(chat_id, None)

    def _broadcast(self, chat_id: str, payload: dict[str, Any]) -> None:
        with self._subscriber_lock:
            listeners = list(self._subscribers.get(chat_id, []))
        for q in listeners:
            try:
                q.put_nowait(payload)
            except queue.Full:
                continue

    @staticmethod
    def _write_sse(handler: BaseHTTPRequestHandler, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False)
        handler.wfile.write(f"data: {body}\n\n".encode("utf-8"))
        handler.wfile.flush()

    def _serialize_asset(self, asset: MediaAsset) -> dict[str, Any]:
        payload = asset.to_dict()
        path = asset.preferred_path(for_model=False)
        if path:
            resolved = Path(path).expanduser().resolve(strict=False)
            try:
                rel = resolved.relative_to(self._data_dir)
                payload["web_url"] = f"/files/{rel.as_posix()}"
            except ValueError:
                pass
        return payload

    def _serialize_message(
        self,
        *,
        role: str,
        chat_id: str,
        content: str,
        media: list[dict[str, Any]],
        metadata: dict[str, Any] | None,
    ) -> dict[str, Any]:
        return {
            "role": role,
            "chat_id": chat_id,
            "content": content,
            "media": media,
            "metadata": metadata or {},
            "timestamp": datetime.now().isoformat(),
        }

    def _serialize_outbound(self, msg: OutboundMessage) -> dict[str, Any]:
        assets = normalize_media_items(msg.media, default_source="tool", default_purpose="both")
        return self._serialize_message(
            role="assistant",
            chat_id=msg.chat_id,
            content=msg.content,
            media=[self._serialize_asset(asset) for asset in assets if asset.for_user],
            metadata=msg.metadata,
        )

    def _serialize_camera_preview(self, payload: dict[str, Any]) -> dict[str, Any]:
        asset_payload = payload.get("asset")
        media: list[dict[str, Any]] = []
        if isinstance(asset_payload, dict):
            try:
                media.append(self._serialize_asset(MediaAsset(**asset_payload)))
            except Exception:
                media.append(dict(asset_payload))
        return {
            "camera_id": payload.get("camera_id"),
            "frame_id": payload.get("frame_id"),
            "captured_at": payload.get("captured_at"),
            "media": media,
        }

    def _serialize_inbound(
        self,
        *,
        chat_id: str,
        content: str,
        media: list[dict[str, Any]],
        metadata: dict[str, Any] | None,
    ) -> dict[str, Any]:
        return self._serialize_message(
            role="user",
            chat_id=chat_id,
            content=content,
            media=media,
            metadata=metadata,
        )

    def _frontend_dist_dir(self) -> Path:
        """Return the built frontend dist directory."""
        return Path(__file__).resolve().parents[1] / "webui" / "dist"

    def _missing_frontend_page(self) -> str:
        """Return a small fallback page when the frontend has not been built."""
        return (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{self.config.static_title}</title></head><body style='font-family:sans-serif;padding:32px'>"
            "<h1>Web UI not built</h1>"
            "<p>Run <code>cd frontend && npm install && npm run build</code> to generate static assets.</p>"
            "</body></html>"
        )

    def _send_frontend_index(self, handler: BaseHTTPRequestHandler) -> None:
        """Serve the built frontend index.html, or a fallback page if missing."""
        dist_dir = self._frontend_dist_dir()
        index_path = dist_dir / "index.html"
        if not index_path.is_file():
            self._send_html(handler, self._missing_frontend_page())
            return
        data = index_path.read_bytes()
        handler.send_response(HTTPStatus.OK)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(data)))
        handler.send_header("Access-Control-Allow-Origin", self._cors_origin())
        handler.end_headers()
        handler.wfile.write(data)

    def _send_frontend_asset(self, handler: BaseHTTPRequestHandler, rel_path: str) -> None:
        """Serve a built frontend static asset."""
        dist_dir = self._frontend_dist_dir()
        target = (dist_dir / rel_path).resolve(strict=False)
        try:
            target.relative_to(dist_dir)
        except ValueError:
            self._send_json(handler, HTTPStatus.FORBIDDEN, {"error": "forbidden"})
            return
        if not target.is_file():
            self._send_json(handler, HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        data = target.read_bytes()
        content_type = mimetypes.guess_type(target.name, strict=False)[0] or "application/octet-stream"
        handler.send_response(HTTPStatus.OK)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(data)))
        handler.send_header("Access-Control-Allow-Origin", self._cors_origin())
        handler.end_headers()
        handler.wfile.write(data)

    def _read_json(self, handler: BaseHTTPRequestHandler) -> dict[str, Any]:
        length = int(handler.headers.get("Content-Length", "0") or "0")
        raw = handler.rfile.read(length) if length > 0 else b"{}"
        return json.loads(raw.decode("utf-8") or "{}")

    def _send_json(self, handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Access-Control-Allow-Origin", self._cors_origin())
        handler.end_headers()
        handler.wfile.write(body)

    def _send_html(self, handler: BaseHTTPRequestHandler, body: str) -> None:
        data = body.encode("utf-8")
        handler.send_response(HTTPStatus.OK)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(data)))
        handler.send_header("Access-Control-Allow-Origin", self._cors_origin())
        handler.end_headers()
        handler.wfile.write(data)

    def _send_file(self, handler: BaseHTTPRequestHandler, rel_path: str) -> None:
        target = (self._data_dir / rel_path).resolve(strict=False)
        try:
            target.relative_to(self._data_dir)
        except ValueError:
            self._send_json(handler, HTTPStatus.FORBIDDEN, {"error": "forbidden"})
            return
        if not target.is_file():
            self._send_json(handler, HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        data = target.read_bytes()
        content_type = mimetypes.guess_type(target.name, strict=False)[0] or "application/octet-stream"
        handler.send_response(HTTPStatus.OK)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(data)))
        handler.send_header("Access-Control-Allow-Origin", self._cors_origin())
        handler.end_headers()
        handler.wfile.write(data)

    def _normalize_inbound_media(self, media: list[Any]) -> list[dict[str, Any]]:
        normalized_media: list[dict[str, Any]] = []
        for item in media:
            if isinstance(item, dict) and item.get("content_base64") and item.get("mime_type"):
                raw = item["content_base64"]
                try:
                    size = len(raw.encode("utf-8")) * 3 // 4
                except Exception:
                    size = self.config.max_upload_bytes + 1
                if size > self.config.max_upload_bytes:
                    raise ValueError("upload exceeds maxUploadBytes")
                asset = self._asset_store.write_base64(
                    raw,
                    mime_type=item["mime_type"],
                    source="web",
                    purpose=item.get("purpose", "both"),
                    filename=item.get("filename"),
                    caption=item.get("caption"),
                    kind=item.get("kind"),
                )
                normalized_media.append(asset.to_dict())
            elif isinstance(item, dict):
                normalized_media.append(item)
        return normalized_media

    def _publish_inbound(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self._loop:
            raise RuntimeError("web channel loop is not initialized")

        sender_id = str(payload.get("sender_id") or payload.get("chat_id") or "web-user")
        chat_id = str(payload.get("chat_id") or sender_id)
        metadata = dict(payload.get("metadata") or {})
        normalized_media = self._normalize_inbound_media(payload.get("media") or [])
        entry = self._append_history(
            chat_id,
            self._serialize_inbound(
                chat_id=chat_id,
                content=str(payload.get("content") or ""),
                media=normalized_media,
                metadata=metadata,
            ),
        )

        coro = self._handle_message(
            sender_id=sender_id,
            chat_id=chat_id,
            content=str(payload.get("content") or ""),
            media=normalized_media,
            metadata=metadata,
            session_key=payload.get("session_key"),
        )
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None

        if current_loop is self._loop:
            self._loop.create_task(coro)
            return entry

        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        fut.result(timeout=15)
        return entry

    def _make_handler(self):
        channel = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
                logger.debug("Web channel: " + format, *args)

            def do_OPTIONS(self) -> None:  # noqa: N802
                self.send_response(HTTPStatus.NO_CONTENT)
                self.send_header("Access-Control-Allow-Origin", channel._cors_origin())
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
                self.end_headers()

            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                if parsed.path == "/":
                    channel._send_frontend_index(self)
                    return
                if parsed.path == "/health":
                    channel._send_json(self, HTTPStatus.OK, {"ok": True})
                    return
                if parsed.path == "/config":
                    channel._send_json(
                        self,
                        HTTPStatus.OK,
                        {
                            "title": channel.config.static_title,
                            "host": channel.config.host,
                            "port": channel.config.port,
                            "max_upload_bytes": channel.config.max_upload_bytes,
                        },
                    )
                    return
                if parsed.path == "/tools":
                    # Expose current tool definitions (if available) to the web UI.
                    if channel._tools_getter is None:
                        channel._send_json(self, HTTPStatus.NOT_FOUND, {"error": "tools endpoint not configured"})
                        return
                    try:
                        tools = channel._tools_getter() or []
                    except Exception as exc:  # pragma: no cover - defensive
                        logger.error("Web channel /tools getter failed: {}", exc)
                        channel._send_json(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "failed to load tools"})
                        return
                    channel._send_json(self, HTTPStatus.OK, {"tools": tools})
                    return
                if parsed.path == "/camera/latest":
                    if channel._camera_preview_getter is None:
                        channel._send_json(self, HTTPStatus.NOT_FOUND, {"error": "camera preview endpoint not configured"})
                        return
                    qs = parse_qs(parsed.query)
                    camera_id = str((qs.get("camera_id") or [""])[0]).strip()
                    max_age_ms = int((qs.get("max_age_ms") or ["500"])[0] or "500")
                    if not camera_id:
                        channel._send_json(self, HTTPStatus.BAD_REQUEST, {"error": "camera_id is required"})
                        return
                    payload = channel._camera_preview_getter(camera_id, max_age_ms=max_age_ms)
                    if payload is None:
                        channel._send_json(self, HTTPStatus.NOT_FOUND, {"error": "no fresh frame"})
                        return
                    channel._send_json(self, HTTPStatus.OK, channel._serialize_camera_preview(payload))
                    return
                if parsed.path == "/camera/events":
                    if channel._camera_preview_getter is None:
                        channel._send_json(self, HTTPStatus.NOT_FOUND, {"error": "camera preview endpoint not configured"})
                        return
                    qs = parse_qs(parsed.query)
                    camera_id = str((qs.get("camera_id") or [""])[0]).strip()
                    max_age_ms = int((qs.get("max_age_ms") or ["500"])[0] or "500")
                    if not camera_id:
                        channel._send_json(self, HTTPStatus.BAD_REQUEST, {"error": "camera_id is required"})
                        return
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "keep-alive")
                    self.send_header("X-Accel-Buffering", "no")
                    self.send_header("Access-Control-Allow-Origin", channel._cors_origin())
                    self.end_headers()
                    last_frame_id = -1
                    try:
                        while channel._running:
                            payload = channel._camera_preview_getter(camera_id, max_age_ms=max_age_ms)
                            if payload is not None:
                                frame_id = int(payload.get("frame_id") or 0)
                                if frame_id > last_frame_id:
                                    channel._write_sse(self, channel._serialize_camera_preview(payload))
                                    last_frame_id = frame_id
                            else:
                                self.wfile.write(b": waiting fresh frame\n\n")
                                self.wfile.flush()
                            time.sleep(0.2)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    return
                if parsed.path.startswith("/assets/"):
                    channel._send_frontend_asset(self, parsed.path.lstrip("/"))
                    return
                if parsed.path in ("/favicon.svg", "/vite.svg"):
                    channel._send_frontend_asset(self, parsed.path.lstrip("/"))
                    return
                if parsed.path.startswith("/files/"):
                    channel._send_file(self, parsed.path[len("/files/"):])
                    return
                if parsed.path == "/history":
                    qs = parse_qs(parsed.query)
                    chat_id = str((qs.get("chat_id") or ["default"])[0])
                    messages, next_cursor = channel._full_history(chat_id)
                    channel._send_json(self, HTTPStatus.OK, {"messages": messages, "next_cursor": next_cursor})
                    return
                if parsed.path == "/events":
                    qs = parse_qs(parsed.query)
                    chat_id = str((qs.get("chat_id") or ["default"])[0])
                    since = int((qs.get("since") or ["0"])[0] or "0")
                    subscriber = channel._subscribe(chat_id)
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "keep-alive")
                    self.send_header("X-Accel-Buffering", "no")
                    self.send_header("Access-Control-Allow-Origin", channel._cors_origin())
                    self.end_headers()
                    try:
                        backlog, _ = channel._history_since(chat_id, since=since)
                        for item in backlog:
                            channel._write_sse(self, item)
                        while channel._running:
                            try:
                                item = subscriber.get(timeout=15.0)
                            except queue.Empty:
                                self.wfile.write(b": keep-alive\n\n")
                                self.wfile.flush()
                                continue
                            if item is None:
                                break
                            channel._write_sse(self, item)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    finally:
                        channel._unsubscribe(chat_id, subscriber)
                    return
                if parsed.path == "/poll":
                    qs = parse_qs(parsed.query)
                    chat_id = str((qs.get("chat_id") or ["default"])[0])
                    since = int((qs.get("since") or ["0"])[0] or "0")
                    messages, next_cursor = channel._history_since(chat_id, since=since)
                    channel._send_json(self, HTTPStatus.OK, {"messages": messages, "next_cursor": next_cursor})
                    return
                channel._send_frontend_index(self)

            def do_POST(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                try:
                    payload = channel._read_json(self)
                    if parsed.path == "/upload":
                        asset = channel._asset_store.write_base64(
                            payload["content_base64"],
                            mime_type=payload["mime_type"],
                            source="web",
                            purpose=payload.get("purpose", "both"),
                            filename=payload.get("filename"),
                            caption=payload.get("caption"),
                            kind=payload.get("kind"),
                        )
                        channel._send_json(self, HTTPStatus.OK, {"asset": channel._serialize_asset(asset)})
                        return
                    if parsed.path == "/message":
                        entry = channel._publish_inbound(payload)
                        channel._send_json(self, HTTPStatus.ACCEPTED, {"ok": True, "message": entry})
                        return
                    channel._send_json(self, HTTPStatus.NOT_FOUND, {"error": "not found"})
                except Exception as exc:
                    channel._send_json(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})

        return Handler

    async def start(self) -> None:
        """Start the HTTP server and keep running until stop() is called."""
        self._loop = asyncio.get_running_loop()
        self._stop_future = self._loop.create_future()
        self._server = ThreadingHTTPServer((self.config.host, self.config.port), self._make_handler())
        self._server.daemon_threads = True

        def _serve() -> None:
            assert self._server is not None
            self._server.serve_forever(poll_interval=0.2)

        self._thread = threading.Thread(target=_serve, name="nanobot-web-channel", daemon=True)
        self._thread.start()
        self._running = True
        logger.info("Web channel listening on http://{}:{}", self.config.host, self.config.port)
        assert self._stop_future is not None
        await self._stop_future

    async def stop(self) -> None:
        """Stop the HTTP server."""
        self._running = False
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        self._thread = None
        if self._stop_future and not self._stop_future.done():
            self._stop_future.set_result(None)

    async def send(self, msg: OutboundMessage) -> None:
        """Queue outbound messages for polling clients."""
        payload = self._serialize_outbound(msg)
        if not msg.metadata.get("_stream"):
            payload = self._append_history(msg.chat_id, payload)
        self._broadcast(msg.chat_id, payload)
