"""MCP client: connects to MCP servers and wraps their tools as native nanobot tools."""

import asyncio
import json
from contextlib import AsyncExitStack
from typing import Any

import httpx
from loguru import logger

from nanobot.agent.tools.base import Tool, ToolExecutionResult
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.media.assets import normalize_media_items
from nanobot.media.store import AssetStore


class MCPToolWrapper(Tool):
    """Wraps a single MCP server tool as a nanobot Tool."""

    def __init__(self, session, server_name: str, tool_def, tool_timeout: int = 30):
        self._session = session
        self._server_name = server_name
        self._original_name = tool_def.name
        self._name = f"mcp_{server_name}_{tool_def.name}"
        self._description = tool_def.description or tool_def.name
        self._parameters = tool_def.inputSchema or {"type": "object", "properties": {}}
        self._tool_timeout = tool_timeout
        self._asset_store = AssetStore()

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._parameters

    def _purpose_from_annotations(self, block: Any) -> str:
        annotations = getattr(block, "annotations", None)
        if isinstance(annotations, dict):
            candidate = annotations.get("purpose") or annotations.get("audience")
            if candidate in ("for_model", "for_user", "both"):
                return candidate
        return "for_model"

    def _asset_from_block(self, block: Any):
        mime_type = getattr(block, "mimeType", None) or getattr(block, "mime_type", None)
        data = getattr(block, "data", None)
        if not mime_type or not str(mime_type).startswith("image/") or data is None:
            return None
        purpose = self._purpose_from_annotations(block)
        if isinstance(data, str):
            return self._asset_store.write_base64(
                data,
                mime_type=mime_type,
                source="mcp",
                purpose=purpose,  # type: ignore[arg-type]
                filename=f"{self._server_name}_{self._original_name}.bin",
            )
        if isinstance(data, (bytes, bytearray)):
            return self._asset_store.write_bytes(
                bytes(data),
                mime_type=mime_type,
                source="mcp",
                purpose=purpose,  # type: ignore[arg-type]
                filename=f"{self._server_name}_{self._original_name}.bin",
            )
        return None

    @staticmethod
    def _structured_text_result(text: str) -> dict[str, Any] | None:
        try:
            payload = json.loads(text)
        except Exception:
            return None
        if isinstance(payload, dict) and ("content" in payload or "media" in payload):
            return payload
        return None

    async def execute(self, **kwargs: Any) -> str | ToolExecutionResult:
        from mcp import types

        try:
            result = await asyncio.wait_for(
                self._session.call_tool(self._original_name, arguments=kwargs),
                timeout=self._tool_timeout,
            )
        except asyncio.TimeoutError:
            logger.warning("MCP tool '{}' timed out after {}s", self._name, self._tool_timeout)
            return f"(MCP tool call timed out after {self._tool_timeout}s)"
        except asyncio.CancelledError:
            # MCP SDK's anyio cancel scopes can leak CancelledError on timeout/failure.
            # Re-raise only if our task was externally cancelled (e.g. /stop).
            task = asyncio.current_task()
            if task is not None and task.cancelling() > 0:
                raise
            logger.warning("MCP tool '{}' was cancelled by server/SDK", self._name)
            return "(MCP tool call was cancelled)"
        except Exception as exc:
            logger.exception(
                "MCP tool '{}' failed: {}: {}",
                self._name,
                type(exc).__name__,
                exc,
            )
            return f"(MCP tool call failed: {type(exc).__name__})"

        parts = []
        media = []
        for block in result.content:
            if isinstance(block, types.TextContent):
                parts.append(block.text)
            else:
                asset = self._asset_from_block(block)
                if asset is not None:
                    media.append(asset)
                else:
                    parts.append(str(block))

        text = "\n".join(parts) or "(no output)"
        structured = self._structured_text_result(text)
        if structured is not None:
            structured_media = normalize_media_items(
                structured.get("media"),
                default_source="mcp",
                default_purpose="for_model",
            )
            return ToolExecutionResult(
                content=str(structured.get("content") or "(no output)"),
                media=[*structured_media, *media],
                metadata=dict(structured.get("metadata") or {}),
            )
        if media:
            return ToolExecutionResult(content=text, media=media)
        return text


async def connect_mcp_servers(
    mcp_servers: dict, registry: ToolRegistry, stack: AsyncExitStack
) -> None:
    """Connect to configured MCP servers and register their tools."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.sse import sse_client
    from mcp.client.stdio import stdio_client
    from mcp.client.streamable_http import streamable_http_client

    for name, cfg in mcp_servers.items():
        try:
            transport_type = cfg.type
            if not transport_type:
                if cfg.command:
                    transport_type = "stdio"
                elif cfg.url:
                    # Convention: URLs ending with /sse use SSE transport; others use streamableHttp
                    transport_type = (
                        "sse" if cfg.url.rstrip("/").endswith("/sse") else "streamableHttp"
                    )
                else:
                    logger.warning("MCP server '{}': no command or url configured, skipping", name)
                    continue

            if transport_type == "stdio":
                params = StdioServerParameters(
                    command=cfg.command, args=cfg.args, env=cfg.env or None
                )
                read, write = await stack.enter_async_context(stdio_client(params))
            elif transport_type == "sse":
                def httpx_client_factory(
                    headers: dict[str, str] | None = None,
                    timeout: httpx.Timeout | None = None,
                    auth: httpx.Auth | None = None,
                ) -> httpx.AsyncClient:
                    merged_headers = {**(cfg.headers or {}), **(headers or {})}
                    return httpx.AsyncClient(
                        headers=merged_headers or None,
                        follow_redirects=True,
                        timeout=timeout,
                        auth=auth,
                    )

                read, write = await stack.enter_async_context(
                    sse_client(cfg.url, httpx_client_factory=httpx_client_factory)
                )
            elif transport_type == "streamableHttp":
                # Always provide an explicit httpx client so MCP HTTP transport does not
                # inherit httpx's default 5s timeout and preempt the higher-level tool timeout.
                http_client = await stack.enter_async_context(
                    httpx.AsyncClient(
                        headers=cfg.headers or None,
                        follow_redirects=True,
                        timeout=None,
                    )
                )
                read, write, _ = await stack.enter_async_context(
                    streamable_http_client(cfg.url, http_client=http_client)
                )
            else:
                logger.warning("MCP server '{}': unknown transport type '{}'", name, transport_type)
                continue

            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()

            tools = await session.list_tools()
            for tool_def in tools.tools:
                wrapper = MCPToolWrapper(session, name, tool_def, tool_timeout=cfg.tool_timeout)
                registry.register(wrapper)
                logger.debug("MCP: registered tool '{}' from server '{}'", wrapper.name, name)

            logger.info("MCP server '{}': connected, {} tools registered", name, len(tools.tools))
        except Exception as e:
            logger.error("MCP server '{}': failed to connect: {}", name, e)
