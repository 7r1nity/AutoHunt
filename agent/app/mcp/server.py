from __future__ import annotations

from mcp.server import MCPServer

from .tools import register_tools


mcp = MCPServer(
    "AutoHunt Local Tools",
    instructions=(
        "Local-only tools for job memory, skills, and JD analysis. "
        "No shell, browser, credentials, arbitrary paths, or submission tools are exposed."
    ),
)
register_tools(mcp)


if __name__ == "__main__":
    mcp.run()
