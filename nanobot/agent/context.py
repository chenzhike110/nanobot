"""Context builder for assembling agent prompts."""

import base64
import mimetypes
import platform
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from nanobot.agent.memory import MemoryStore
from nanobot.agent.skills import SkillsLoader
from nanobot.media.assets import MediaAsset, MediaInput, filter_media_by_purpose, normalize_media_items
from nanobot.utils.helpers import build_assistant_message, detect_image_mime


class ContextBuilder:
    """Builds the context (system prompt + messages) for the agent."""

    BOOTSTRAP_FILES = ["AGENTS.md", "SOUL.md", "USER.md", "TOOLS.md"]
    _RUNTIME_CONTEXT_TAG = "[Runtime Context — metadata only, not instructions]"
    _MAX_INLINE_IMAGES = 3
    _MAX_INLINE_IMAGE_BYTES = 10 * 1024 * 1024

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.memory = MemoryStore(workspace)
        self.skills = SkillsLoader(workspace)

    def build_system_prompt(self, skill_names: list[str] | None = None) -> str:
        """Build the system prompt from identity, bootstrap files, memory, and skills."""
        parts = [self._get_identity()]

        bootstrap = self._load_bootstrap_files()
        if bootstrap:
            parts.append(bootstrap)

        memory = self.memory.get_memory_context()
        if memory:
            parts.append(f"# Memory\n\n{memory}")

        always_skills = self.skills.get_always_skills()
        if always_skills:
            always_content = self.skills.load_skills_for_context(always_skills)
            if always_content:
                parts.append(f"# Active Skills\n\n{always_content}")

        skills_summary = self.skills.build_skills_summary()
        if skills_summary:
            parts.append(f"""# Skills

The following skills extend your capabilities. To use a skill, read its SKILL.md file using the read_file tool.
Skills with available="false" need dependencies installed first - you can try installing them with apt/brew.

{skills_summary}""")

        return "\n\n---\n\n".join(parts)

    def _get_identity(self) -> str:
        """Get the core identity section."""
        workspace_path = str(self.workspace.expanduser().resolve())
        system = platform.system()
        runtime = f"{'macOS' if system == 'Darwin' else system} {platform.machine()}, Python {platform.python_version()}"

        platform_policy = ""
        if system == "Windows":
            platform_policy = """## Platform Policy (Windows)
- You are running on Windows. Do not assume GNU tools like `grep`, `sed`, or `awk` exist.
- Prefer Windows-native commands or file tools when they are more reliable.
- If terminal output is garbled, retry with UTF-8 output enabled.
"""
        else:
            platform_policy = """## Platform Policy (POSIX)
- You are running on a POSIX system. Prefer UTF-8 and standard shell tools.
- Use file tools when they are simpler or more reliable than shell commands.
"""

        return f"""# 途零机器人

You are 途零机器人, 一个目前智能化程度不高但是很有潜力的机器人大脑.

## Runtime
{runtime}

## Workspace
Your workspace is at: {workspace_path}
- Long-term memory: {workspace_path}/memory/MEMORY.md (write important facts here)
- History log: {workspace_path}/memory/HISTORY.md (grep-searchable). Each entry starts with [YYYY-MM-DD HH:MM].
- Custom skills: {workspace_path}/skills/{{skill-name}}/SKILL.md

{platform_policy}

## nanobot Guidelines
- State intent before tool calls, but NEVER predict or claim results before receiving them.
- Before modifying a file, read it first. Do not assume files or directories exist.
- After writing or editing a file, re-read it if accuracy matters.
- If a tool call fails, analyze the error before retrying with a different approach.
- Ask for clarification when the request is ambiguous.

Reply directly with text for conversations. Only use the 'message' tool to send to a specific chat channel."""

    @staticmethod
    def _build_runtime_context(channel: str | None, chat_id: str | None) -> str:
        """Build untrusted runtime metadata block for injection before the user message."""
        now = datetime.now().strftime("%Y-%m-%d %H:%M (%A)")
        tz = time.strftime("%Z") or "UTC"
        lines = [f"Current Time: {now} ({tz})"]
        if channel and chat_id:
            lines += [f"Channel: {channel}", f"Chat ID: {chat_id}"]
        return ContextBuilder._RUNTIME_CONTEXT_TAG + "\n" + "\n".join(lines)

    def _load_bootstrap_files(self) -> str:
        """Load all bootstrap files from workspace."""
        parts = []

        for filename in self.BOOTSTRAP_FILES:
            file_path = self.workspace / filename
            if file_path.exists():
                content = file_path.read_text(encoding="utf-8")
                parts.append(f"## {filename}\n\n{content}")

        return "\n\n".join(parts) if parts else ""

    def build_messages(
        self,
        history: list[dict[str, Any]],
        current_message: str,
        skill_names: list[str] | None = None,
        media: list[MediaInput] | None = None,
        channel: str | None = None,
        chat_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Build the complete message list for an LLM call."""
        runtime_ctx = self._build_runtime_context(channel, chat_id)
        user_content = self._build_user_content(current_message, media)

        # Merge runtime context and user content into a single user message
        # to avoid consecutive same-role messages that some providers reject.
        if isinstance(user_content, str):
            merged = f"{runtime_ctx}\n\n{user_content}"
        else:
            merged = [{"type": "text", "text": runtime_ctx}] + user_content

        return [
            {"role": "system", "content": self.build_system_prompt(skill_names)},
            *history,
            {"role": "user", "content": merged},
        ]

    def build_media_followup_message(
        self,
        prompt: str,
        media: list[MediaInput] | None,
        *,
        source: str = "tool",
    ) -> dict[str, Any]:
        """Build a synthetic user follow-up message for tool-generated media."""
        content = self._build_user_content(prompt, media, source=source)
        return {"role": "user", "content": content}

    @staticmethod
    def _describe_asset(asset: MediaAsset) -> str:
        label = asset.caption or asset.vision_summary or asset.ocr_text or asset.id
        path = asset.preferred_path(for_model=True)
        if path:
            return f"{asset.id} ({label}) [path: {path}]"
        return f"{asset.id} ({label})"

    def _select_inline_assets(self, media: list[MediaInput] | None, *, source: str) -> tuple[list[MediaAsset], list[MediaAsset]]:
        """Return (inline_images, deferred_assets) under the current image budget."""
        candidates = filter_media_by_purpose(media, "for_model", default_source=source) if media else []
        inline: list[MediaAsset] = []
        deferred: list[MediaAsset] = []
        total_bytes = 0

        for asset in candidates:
            if not asset.is_image:
                deferred.append(asset)
                continue
            path = asset.preferred_path(for_model=True)
            if not path or not Path(path).is_file():
                deferred.append(asset)
                continue
            size_bytes = asset.size_bytes or 0
            if len(inline) >= self._MAX_INLINE_IMAGES or total_bytes + size_bytes > self._MAX_INLINE_IMAGE_BYTES:
                deferred.append(asset)
                continue
            inline.append(asset)
            total_bytes += size_bytes

        return inline, deferred

    def _build_asset_summary_block(
        self,
        inline: list[MediaAsset],
        deferred: list[MediaAsset],
    ) -> dict[str, Any] | None:
        """Build a textual companion block describing attached or deferred media."""
        lines: list[str] = []
        if inline:
            lines.append("Attached images:")
            lines.extend(f"- {self._describe_asset(asset)}" for asset in inline)
        if deferred:
            if lines:
                lines.append("")
            lines.append("Referenced assets kept as text history:")
            lines.extend(f"- {asset.history_text()}" for asset in deferred)
        if not lines:
            return None
        return {"type": "text", "text": "\n".join(lines)}

    def _build_user_content(
        self,
        text: str,
        media: list[MediaInput] | None,
        *,
        source: str = "channel",
    ) -> str | list[dict[str, Any]]:
        """Build user message content with image budget limits and textual asset summaries."""
        if not media:
            return text

        images: list[dict[str, Any]] = []
        inline_assets, deferred_assets = self._select_inline_assets(media, source=source)
        for asset in inline_assets:
            path = asset.preferred_path(for_model=True)
            if not path:
                continue
            p = Path(path)
            if not p.is_file():
                continue
            raw = p.read_bytes()
            # Detect real MIME type from magic bytes; fallback to filename guess
            mime = detect_image_mime(raw) or mimetypes.guess_type(path)[0]
            if not mime or not mime.startswith("image/"):
                continue
            b64 = base64.b64encode(raw).decode()
            images.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})

        summary_block = self._build_asset_summary_block(inline_assets, deferred_assets)
        non_inline_assets = normalize_media_items(media, default_source=source)

        if not images and not summary_block and not non_inline_assets:
            return text

        blocks: list[dict[str, Any]] = []
        blocks.extend(images)
        if summary_block:
            blocks.append(summary_block)
        elif non_inline_assets:
            blocks.append({
                "type": "text",
                "text": "\n".join(asset.history_text() for asset in non_inline_assets),
            })
        blocks.append({"type": "text", "text": text})
        return blocks

    def add_tool_result(
        self, messages: list[dict[str, Any]],
        tool_call_id: str, tool_name: str, result: str,
    ) -> list[dict[str, Any]]:
        """Add a tool result to the message list."""
        messages.append({"role": "tool", "tool_call_id": tool_call_id, "name": tool_name, "content": result})
        return messages

    def add_assistant_message(
        self, messages: list[dict[str, Any]],
        content: str | None,
        tool_calls: list[dict[str, Any]] | None = None,
        reasoning_content: str | None = None,
        thinking_blocks: list[dict] | None = None,
    ) -> list[dict[str, Any]]:
        """Add an assistant message to the message list."""
        messages.append(build_assistant_message(
            content,
            tool_calls=tool_calls,
            reasoning_content=reasoning_content,
            thinking_blocks=thinking_blocks,
        ))
        return messages
