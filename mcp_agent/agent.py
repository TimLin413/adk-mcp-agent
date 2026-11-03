"""ADK agent that gets its tools from MCP servers.

The agent does not define any tools itself. Each McpToolset launches an MCP
server (via npx) as a subprocess, asks it which tools it offers, and hands
those tools to Gemini. When Gemini decides to call a tool, ADK forwards the
call to the MCP server over stdio and returns the result.

- filesystem: official MCP filesystem server, sandboxed to ./workspace
- m365: Softeria ms-365-mcp-server, reading Outlook mail via Microsoft Graph

When the A365_* settings are present, the agent also runs as its Entra agent
identity: it reports each run to Microsoft Agent 365 (a365_observability.py),
searches the web through the Agent 365 tooling gateway (a365_tooling.py), and
has prompts and replies checked by Purview DLP (a365_purview.py).
"""

import os
from pathlib import Path

from google.adk.agents import LlmAgent
from google.adk.apps import App
from google.adk.tools.mcp_tool import McpToolset, StdioConnectionParams
from mcp import StdioServerParameters

from .a365_observability import create_plugin, load_identity
from .a365_purview import create_dlp_plugin
from .a365_tooling import create_gateway_toolset

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

# Only the tools needed to read mail. Filtering on the server side as well as
# with ADK's tool_filter limits the Graph scopes it requests to Mail.Read.
M365_MAIL_TOOLS = [
    "verify-login",
    "list-mail-folders",
    "list-mail-messages",
    "list-mail-folder-messages",
    "get-mail-message",
    "list-mail-attachments",
]

# Read-only mail access. The server signs in with the Entra app below and
# reuses the token cached by `npx @softeria/ms-365-mcp-server --login`.
m365_mcp = McpToolset(
    connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(
            command="npx",
            args=["-y", "@softeria/ms-365-mcp-server", "--read-only"],
            # The MCP client only passes a few system variables to the
            # subprocess, so the Entra app settings must be forwarded here.
            env={
                "MS365_MCP_CLIENT_ID": os.environ["MS365_MCP_CLIENT_ID"],
                "MS365_MCP_TENANT_ID": os.environ["MS365_MCP_TENANT_ID"],
                # Passed as an env var rather than --enabled-tools: on Windows
                # npx runs through cmd.exe, which treats "|" as a pipe.
                "ENABLED_TOOLS": f"^({'|'.join(M365_MAIL_TOOLS)})$",
            },
        ),
        timeout=60,
    ),
    # The server always adds account tools (login, logout, remove-account...);
    # hide them so the agent cannot sign the user out.
    tool_filter=M365_MAIL_TOOLS,
)

instruction = (
    "You are a helpful assistant with these tools.\n"
    "1. Microsoft 365 mail tools (read-only): list, search, and read the "
    "signed-in user's Outlook mail. When listing messages, request only the "
    "fields you need (such as subject, from, receivedDateTime, isRead, "
    "bodyPreview) and limit the count, so responses stay small. Open a "
    "full message with get-mail-message only when the user asks for details.\n"
    "   If a mail tool reports that the user is not signed in, tell the user "
    "to run `npx @softeria/ms-365-mcp-server --login` in a terminal.\n"
    f"2. File tools: you may only access files inside {WORKSPACE_DIR}.\n"
)
tools = [m365_mcp, filesystem_mcp]
MAX_WEB_SEARCHES = 4

# With the A365_* settings, the agent runs as its Entra agent identity: it
# reports telemetry to Agent 365 and can use Agent 365 MCP servers.
a365_identity = load_identity()
if a365_identity:
    tools.append(create_gateway_toolset(
        a365_identity,
        "mcp_WebSearchTools",
        scopes=["McpServers.WebSearch.All", "McpServersMetadata.Read.All"],
    ))
    instruction += (
        "3. Web search (SearchWeb, through Agent 365): use it for news, current "
        "events and other questions about the web, and cite the URLs it returns. "
        "It is not a general search engine: it summarizes the pages at the URLs "
        "you pass, and rejects calls without URLs. So pass the most specific "
        "pages you know rather than home pages. It cannot run JavaScript, so "
        "pages that load their content with scripts come back empty (the CWA "
        "typhoon and warning pages, Yahoo tag pages, Bing results). Pages that "
        "work: https://news.ltn.com.tw/list/breakingnews/weather and "
        "https://www.cna.com.tw for Taiwan news and weather, "
        "https://learn.microsoft.com for Microsoft products. You get "
        f"{MAX_WEB_SEARCHES} searches per question; if they don't have the "
        "detail asked for, answer with what you found, say what is missing, and "
        "link the best source.\n"
        "   If it fails with a sign-in error, tell the user to run `az login`.\n"
    )
instruction += (
    "Use the mail and file tools only when the request is about the user's "
    "mail or files.\n"
    "Show times in Asia/Taipei. Reply in the same language the user writes in."
)

def limit_web_searches(tool, args, tool_context):
    """Stops SearchWeb after MAX_WEB_SEARCHES calls in one turn.

    When pages come back empty, Gemini keeps trying other sites and then digs
    through mail and files, ignoring a limit stated in the instruction. Returning
    a result here skips the real call.
    """
    if tool.name != "SearchWeb":
        return None
    # "temp:" state lasts for the current invocation only.
    count = tool_context.state.get("temp:web_searches", 0) + 1
    tool_context.state["temp:web_searches"] = count
    if count > MAX_WEB_SEARCHES:
        return {
            "error": "Search limit reached for this question. Do not call SearchWeb "
            "or look in mail or files for this; answer the user now with what the "
            "earlier searches found, say what is missing, and link the best source."
        }
    return None


root_agent = LlmAgent(
    name="mcp_agent",
    model=os.getenv("MODEL", "gemini-3.5-flash"),
    description="An assistant that reads Outlook mail, searches the web, and manages local files through MCP servers.",
    instruction=instruction,
    tools=tools,
    before_tool_callback=limit_web_searches,
)

AGENT_NAME = "GCP-agent-test"

# `adk web` and `adk run` load `app` before `root_agent`; plugins only attach
# through an App. The name must match this folder.
# Telemetry goes first: ADK stops at the first plugin that answers, so it must
# see the turn before Purview DLP can replace a blocked prompt or reply.
app = App(
    name="mcp_agent",
    root_agent=root_agent,
    plugins=[
        create_plugin(a365_identity, agent_name=AGENT_NAME, agent_description=root_agent.description),
        create_dlp_plugin(a365_identity, agent_name=AGENT_NAME),
    ] if a365_identity else [],
)
