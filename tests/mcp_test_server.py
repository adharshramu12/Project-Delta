"""Tiny MCP server used by test_addons_mcp.py (runs as a subprocess over stdio)."""

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

server = MCPServer("delta-test")


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
def add(a: int, b: int) -> str:
    """Add two numbers."""
    return str(a + b)


@server.tool()
def create_note(title: str) -> str:
    """Create a note (no annotations: Delta must treat it as a write)."""
    return f"created {title}"


if __name__ == "__main__":
    server.run()
