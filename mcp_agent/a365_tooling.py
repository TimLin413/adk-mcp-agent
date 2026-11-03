"""Call Agent 365 MCP servers (the tooling gateway) on behalf of the user.

The gateway only accepts a delegated token, so the agent identity does an
on-behalf-of exchange:

    user token (aud = blueprint, scope access_agent)
    + blueprint exchange token for the agent identity
      -> agent identity OBO token for agent365.svc.cloud.microsoft

The admin consents the McpServers.* scopes on the blueprint, and the agent
identity inherits them.

There is no sign-in page in `adk web`, so the user token comes from the
developer's `az login`; the blueprint preauthorizes the Azure CLI for
access_agent. A deployed agent would get the user token from its caller.
"""

import asyncio
import shutil
import subprocess
import threading
import time
from typing import Optional

from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams

from .a365_observability import JWT_BEARER, REFRESH_MARGIN_SECONDS, AgentIdentityTokenProvider, jwt_claims

TOOLS_RESOURCE = "https://agent365.svc.cloud.microsoft"


class OboTokenProvider:
    """Gets and caches the agent identity's OBO token for one resource.

    Scopes are full URIs, e.g. https://agent365.svc.cloud.microsoft/McpServers.WebSearch.All.
    """

    def __init__(self, identity: AgentIdentityTokenProvider, scopes: list[str]):
        self._identity = identity
        self._scope = " ".join(scopes)
        self._token: Optional[str] = None
        self._expires_at = 0.0
        self._lock = threading.Lock()

    def _user_token(self) -> str:
        az = shutil.which("az")
        if az is None:
            raise RuntimeError("Azure CLI not found; run `az login` in a terminal that has az on PATH")
        result = subprocess.run(
            [az, "account", "get-access-token",
             "--scope", f"api://{self._identity.blueprint_id}/access_agent",
             "--query", "accessToken", "-o", "tsv"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"az could not get a user token; run `az login`. {result.stderr.strip()}")
        return result.stdout.strip()

    def get_token(self) -> str:
        with self._lock:
            if self._token is None or time.time() > self._expires_at - REFRESH_MARGIN_SECONDS:
                self._token = self._identity.post({
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "client_id": self._identity.agent_id,
                    "scope": self._scope,
                    "client_assertion_type": JWT_BEARER,
                    "client_assertion": self._identity.exchange_token(),
                    "assertion": self._user_token(),
                    "requested_token_use": "on_behalf_of",
                })
                self._expires_at = float(jwt_claims(self._token)["exp"])
            return self._token

    async def headers(self, _context) -> dict[str, str]:
        # Token requests and the az call block, so keep them off the event loop.
        token = await asyncio.to_thread(self.get_token)
        return {"Authorization": f"Bearer {token}"}


def create_gateway_toolset(
    identity: AgentIdentityTokenProvider, server_name: str, scopes: list[str]
) -> McpToolset:
    """Returns an ADK toolset for one Agent 365 MCP server, e.g. mcp_WebSearchTools."""
    tokens = OboTokenProvider(identity, [f"{TOOLS_RESOURCE}/{s}" for s in scopes])
    return McpToolset(
        connection_params=StreamableHTTPConnectionParams(
            url=f"{TOOLS_RESOURCE}/agents/servers/{server_name}",
            timeout=60,
        ),
        header_provider=tokens.headers,
    )
