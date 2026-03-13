"""Tool registry for dynamic tool management."""

from typing import Any

from nanobot.agent.tools.base import Tool, ToolExecutionResult


class ToolRegistry:
    """
    Registry for agent tools.

    Allows dynamic registration and execution of tools.
    """

    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """Register a tool."""
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        """Unregister a tool by name."""
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool | None:
        """Get a tool by name."""
        return self._tools.get(name)

    def has(self, name: str) -> bool:
        """Check if a tool is registered."""
        return name in self._tools

    def get_definitions(self) -> list[dict[str, Any]]:
        """Get all tool definitions in OpenAI format."""
        return [tool.to_schema() for tool in self._tools.values()]

    async def execute_with_result(self, name: str, params: dict[str, Any]) -> ToolExecutionResult:
        """Execute a tool and preserve structured media artifacts when available."""
        _HINT = "\n\n[Analyze the error above and try a different approach.]"

        tool = self._tools.get(name)
        if not tool:
            return ToolExecutionResult(
                content=f"Error: Tool '{name}' not found. Available: {', '.join(self.tool_names)}"
            )

        try:
            # Attempt to cast parameters to match schema types
            params = tool.cast_params(params)
            
            # Validate parameters
            errors = tool.validate_params(params)
            if errors:
                return ToolExecutionResult(
                    content=f"Error: Invalid parameters for tool '{name}': " + "; ".join(errors) + _HINT
                )
            result = await tool.execute(**params)
            envelope = result if isinstance(result, ToolExecutionResult) else ToolExecutionResult(content=str(result))
            if envelope.content.startswith("Error"):
                return ToolExecutionResult(
                    content=envelope.content + _HINT,
                    media=envelope.media,
                    metadata=envelope.metadata,
                )
            return envelope
        except Exception as e:
            return ToolExecutionResult(content=f"Error executing {name}: {str(e)}" + _HINT)

    async def execute(self, name: str, params: dict[str, Any]) -> str:
        """Execute a tool by name with given parameters."""
        return (await self.execute_with_result(name, params)).content

    @property
    def tool_names(self) -> list[str]:
        """Get list of registered tool names."""
        return list(self._tools.keys())

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools
