"""ADK agent that gets its tools from an MCP server.

The agent does not define any tools itself. Instead, McpToolset launches the
official filesystem MCP server (via npx) as a subprocess, asks it which tools
it offers, and hands those tools to Gemini. When Gemini decides to call a tool,
ADK forwards the call to the MCP server over stdio and returns the result.
"""

import os
from pathlib import Path

from google.adk.agents import LlmAgent
from google.adk.tools.mcp_tool import McpToolset, StdioConnectionParams
from mcp import StdioServerParameters

# The MCP server can only read or write files inside this folder.
WORKSPACE_DIR = Path(__file__).resolve().parent.parent / "workspace"

filesystem_mcp = McpToolset(
    connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(
            command="npx",
            args=[
                "-y",
                "@modelcontextprotocol/server-filesystem",
                str(WORKSPACE_DIR),
            ],
        ),
        # The first npx run downloads the package, so allow extra time.
        timeout=60,
    ),
)

root_agent = LlmAgent(
    name="mcp_file_agent",
    model=os.getenv("MODEL", "gemini-3.5-flash"),
    description="An assistant that manages files through an MCP filesystem server.",
    instruction=(
        "You are a helpful assistant that can work with files. "
        f"You may only access files inside {WORKSPACE_DIR}. "
        "Use the available tools to list, read, search, and write files when the "
        "user asks. Reply in the same language the user writes in."
    ),
    tools=[filesystem_mcp],
)
