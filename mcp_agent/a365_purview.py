"""Check prompts and replies with Microsoft Purview DLP.

Purview can't see a Gemini agent's conversation by itself, so the agent sends
each user prompt and final reply to the Graph Purview API:

- protectionScopes/compute: which activities (uploadText = prompt,
  downloadText = reply) the admin's policies cover for this agent, and whether
  they must be checked before answering (evaluateInline) or can be checked
  afterwards (evaluateOffline). Cached until the policy ETag changes.
- processContent: evaluates one piece of text. A restrictAccess/block action
  means the DLP policy matched; Purview records the interaction for audit.

The calls are made as the signed-in user (OBO), so they need the delegated
ProtectionScopes.Compute.User and Content.Process.User permissions on the agent
identity. The DLP policy must list the agent identity's app ID as a location.

If a Purview call fails (for example the permissions expired), the agent keeps
answering and logs a warning.
"""

import asyncio
import datetime
import logging
import uuid
from typing import Any, Optional

import requests
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from google.genai import types

from .a365_observability import AgentIdentityTokenProvider, content_text
from .a365_tooling import OboTokenProvider

logger = logging.getLogger(__name__)

GRAPH = "https://graph.microsoft.com"
PURVIEW_SCOPES = [f"{GRAPH}/ProtectionScopes.Compute.User", f"{GRAPH}/Content.Process.User"]
BLOCKED_MESSAGE = "此內容違反組織的資料保護政策（Microsoft Purview DLP），已被封鎖。"
PROMPT, REPLY = "uploadText", "downloadText"


class PurviewClient:
    """Calls the Graph Purview API as the signed-in user, for this agent."""

    def __init__(self, identity: AgentIdentityTokenProvider, agent_name: str):
        self._identity = identity
        self._agent_name = agent_name
        self._tokens = OboTokenProvider(identity, PURVIEW_SCOPES)
        self._scopes: Optional[list[dict[str, Any]]] = None
        self._etag: Optional[str] = None

    def _post(self, path: str, body: dict[str, Any], etag: Optional[str] = None) -> requests.Response:
        headers = {
            "Authorization": f"Bearer {self._tokens.get_token()}",
            "Client-Request-Id": str(uuid.uuid4()),
        }
        if etag:
            headers["If-None-Match"] = etag
        resp = requests.post(f"{GRAPH}/v1.0/me/dataSecurityAndGovernance/{path}", json=body, headers=headers, timeout=30)
        if resp.status_code >= 400:
            raise RuntimeError(f"{path} returned HTTP {resp.status_code}: {resp.text[:300]}")
        return resp

    def _location(self) -> dict[str, str]:
        return {"@odata.type": "microsoft.graph.policyLocationApplication", "value": self._identity.agent_id}

    def _execution_mode(self, activity: str) -> Optional[str]:
        """Returns evaluateInline/evaluateOffline, or None if no policy covers the activity."""
        if self._scopes is None:
            resp = self._post("protectionScopes/compute", {
                "activities": f"{PROMPT},{REPLY}",
                "locations": [self._location()],
            })
            self._scopes = resp.json().get("value", [])
            self._etag = resp.headers.get("ETag")
        modes = [
            scope.get("executionMode")
            for scope in self._scopes
            if activity in (scope.get("activities") or "").replace(" ", "").split(",")
        ]
        if "evaluateInline" in modes:
            return "evaluateInline"
        return modes[0] if modes else None

    def check(self, text: str, activity: str, conversation_id: str, sequence: int) -> bool:
        """Sends text to Purview and returns True if a DLP policy blocks it."""
        mode = self._execution_mode(activity)
        if mode is None:
            return False
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        resp = self._post("processContent", {
            "contentToProcess": {
                "contentEntries": [{
                    "@odata.type": "microsoft.graph.processConversationMetadata",
                    "identifier": str(uuid.uuid4()),
                    "content": {"@odata.type": "microsoft.graph.textContent", "data": text},
                    "agents": [{
                        "@odata.type": "microsoft.graph.aiAgentInfo",
                        "blueprintId": self._identity.blueprint_id,
                        "identifier": self._identity.agent_id,
                        "name": self._agent_name,
                    }],
                    "name": f"{self._agent_name} {'prompt' if activity == PROMPT else 'reply'}",
                    "correlationId": conversation_id,
                    "sequenceNumber": sequence,
                    "isTruncated": False,
                    "createdDateTime": now,
                    "modifiedDateTime": now,
                }],
                "activityMetadata": {"activity": activity},
                "protectedAppMetadata": {
                    "name": self._agent_name,
                    "version": "1.0",
                    "applicationLocation": self._location(),
                },
                "integratedAppMetadata": {"name": self._agent_name, "version": "1.0"},
            }
        }, etag=self._etag)
        body = resp.json() if resp.content else {}
        if body.get("protectionScopeState") == "modified":
            # The admin changed the policies; recompute scopes on the next call.
            self._scopes = None
        blocked = any(
            action.get("action") == "restrictAccess" and action.get("restrictionAction") == "block"
            for action in body.get("policyActions", [])
        )
        # Offline evaluation is for audit only; it doesn't hold up the reply.
        return blocked and mode == "evaluateInline"


def _blocked_response() -> LlmResponse:
    return LlmResponse(content=types.Content(role="model", parts=[types.Part(text=BLOCKED_MESSAGE)]))


class PurviewDlpPlugin(BasePlugin):
    """Blocks prompts and replies that a Purview DLP policy restricts.

    Add it after the telemetry plugin: ADK stops at the first plugin that
    returns a response, and telemetry should still see the model's turn.
    """

    def __init__(self, client: PurviewClient):
        super().__init__(name="purview_dlp")
        self._client = client

    async def _check(self, text: str, activity: str, callback_context) -> bool:
        session_id = callback_context.session.id
        sequence = callback_context.state.get("temp:purview_sequence", 0)
        callback_context.state["temp:purview_sequence"] = sequence + 1
        try:
            return await asyncio.to_thread(self._client.check, text, activity, session_id, sequence)
        except Exception as e:  # noqa: BLE001 - fail open, see module docstring
            logger.warning("Purview %s check failed; continuing without DLP: %s", activity, e)
            return False

    async def before_model_callback(self, *, callback_context, llm_request):
        # Check the user's prompt once, before the first model call of the turn.
        if callback_context.state.get("temp:purview_prompt_checked"):
            return None
        callback_context.state["temp:purview_prompt_checked"] = True
        text = content_text(callback_context.user_content)
        if text and await self._check(text, PROMPT, callback_context):
            logger.info("Purview DLP blocked the prompt")
            return _blocked_response()
        return None

    async def after_model_callback(self, *, callback_context, llm_response):
        # Only the final answer goes back to the user; skip tool calls and chunks.
        content = llm_response.content
        if llm_response.partial or not content or any(p.function_call for p in content.parts or []):
            return None
        text = content_text(content)
        if text and await self._check(text, REPLY, callback_context):
            logger.info("Purview DLP blocked the reply")
            return _blocked_response()
        return None


def create_dlp_plugin(identity: AgentIdentityTokenProvider, agent_name: str) -> PurviewDlpPlugin:
    return PurviewDlpPlugin(PurviewClient(identity, agent_name))
