"""Send this agent's telemetry to Microsoft Agent 365.

The agent runs as the Entra agent identity A365_AGENT_ID. It has no secret of
its own: a Google service account proves who it is, and the blueprint's
federated identity credential trusts that Google token.

    Google ID token (service account key)
      -> blueprint token for the agent identity (client_credentials + fmi_path)
      -> agent identity token for the Agent 365 observability API

That app-only token is what the S2S export route expects.

ADK has no automatic instrumentation in the Microsoft OpenTelemetry Distro, so
A365TelemetryPlugin records the three scopes Agent 365 requires
(invoke_agent, chat, execute_tool) from ADK plugin callbacks.
"""

import base64
import json
import logging
import os
import threading
import time
from typing import Any, Optional

import requests
from google.adk.plugins.base_plugin import BasePlugin
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import service_account
from microsoft.opentelemetry.a365.core import (
    AgentDetails,
    Channel,
    ExecuteToolScope,
    InferenceCallDetails,
    InferenceOperationType,
    InferenceScope,
    InvokeAgentScope,
    InvokeAgentScopeDetails,
    Request,
    SpanDetails,
    ToolCallDetails,
    ToolType,
)
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

logger = logging.getLogger(__name__)

OBSERVABILITY_RESOURCE = "9b975845-388f-4429-889e-eab1ef63949c"
OBSERVABILITY_SCOPE = f"api://{OBSERVABILITY_RESOURCE}/.default"
TOKEN_EXCHANGE_AUDIENCE = "api://AzureADTokenExchange"
JWT_BEARER = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
# Refresh tokens this long before they expire.
REFRESH_MARGIN_SECONDS = 300


def jwt_claims(token: str) -> dict[str, Any]:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


class AgentIdentityTokenProvider:
    """Gets and caches the agent identity's token for the observability API."""

    def __init__(self, tenant_id: str, blueprint_id: str, agent_id: str, key_file: str):
        self.tenant_id = tenant_id
        self.blueprint_id = blueprint_id
        self.agent_id = agent_id
        self._google = service_account.IDTokenCredentials.from_service_account_file(
            key_file, target_audience=TOKEN_EXCHANGE_AUDIENCE
        )
        self._token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
        self._token: Optional[str] = None
        self._expires_at = 0.0
        # The exporter calls the resolver from its own background thread.
        self._lock = threading.Lock()

    def get_token(self) -> str:
        with self._lock:
            if self._token is None or time.time() > self._expires_at - REFRESH_MARGIN_SECONDS:
                self._token = self._fetch()
                self._expires_at = float(jwt_claims(self._token)["exp"])
            return self._token

    def exchange_token(self) -> str:
        """Returns a token the agent identity can use as its client assertion.

        The blueprint signs in with the Google token and asks for an exchange
        token scoped to this agent identity (fmi_path). Both the S2S and the
        OBO flows start from it.
        """
        self._google.refresh(GoogleAuthRequest())
        return self.post({
            "grant_type": "client_credentials",
            "client_id": self.blueprint_id,
            "scope": f"{TOKEN_EXCHANGE_AUDIENCE}/.default",
            "client_assertion_type": JWT_BEARER,
            "client_assertion": self._google.token,
            "fmi_path": self.agent_id,
        })

    def post(self, form: dict[str, str]) -> str:
        resp = requests.post(self._token_url, data=form, timeout=30)
        body = resp.json()
        if resp.status_code != 200:
            raise RuntimeError(f"{body.get('error')}: {body.get('error_description', '').splitlines()[0]}")
        return body["access_token"]

    def _fetch(self) -> str:
        token = self.post({
            "grant_type": "client_credentials",
            "client_id": self.agent_id,
            "scope": OBSERVABILITY_SCOPE,
            "client_assertion_type": JWT_BEARER,
            "client_assertion": self.exchange_token(),
        })
        # The observability service only accepts the final app-only token for
        # this agent; never send it a blueprint or user token.
        claims = jwt_claims(token)
        if claims.get("idtyp") != "app" or "scp" in claims:
            raise RuntimeError("Observability token is not app-only")
        if claims.get("aud") not in (OBSERVABILITY_RESOURCE, f"api://{OBSERVABILITY_RESOURCE}"):
            raise RuntimeError(f"Observability token has unexpected audience {claims.get('aud')}")
        return token

    def resolve(self, agent_id: str, tenant_id: str) -> Optional[str]:
        if agent_id != self.agent_id or tenant_id != self.tenant_id:
            return None
        return self.get_token()


def _register_exporter(provider: AgentIdentityTokenProvider) -> None:
    # use_microsoft_opentelemetry() builds and registers its own global
    # TracerProvider, but `adk web` registers one before it imports agent.py and
    # OpenTelemetry allows only one. So build the distro's Agent 365 span
    # processors and add them to whichever provider is already active.
    # _append_a365_components is private; microsoft-opentelemetry is pinned.
    from microsoft.opentelemetry._distro import _append_a365_components

    otel_kwargs: dict[str, Any] = {}
    # Export is still off unless ENABLE_A365_OBSERVABILITY_EXPORTER=true.
    _append_a365_components(
        True,
        otel_kwargs,
        token_resolver=provider.resolve,
        use_s2s_endpoint=True,
    )

    tracer_provider = trace.get_tracer_provider()
    if not isinstance(tracer_provider, TracerProvider):
        tracer_provider = TracerProvider()
        trace.set_tracer_provider(tracer_provider)
    for processor in otel_kwargs.get("span_processors", []):
        tracer_provider.add_span_processor(processor)


def content_text(content: Any) -> str:
    """Joins the parts of a google.genai Content into one readable string.

    Besides text, a model turn can hold function calls and the next request
    can hold only function responses, so those are written out too.
    """
    lines = []
    for part in getattr(content, "parts", None) or []:
        if part.text:
            lines.append(part.text)
        elif part.function_call:
            call = part.function_call
            lines.append(f"[tool call] {call.name}({json.dumps(call.args or {}, ensure_ascii=False, default=str)})")
        elif part.function_response:
            resp = part.function_response
            lines.append(f"[tool result] {resp.name}: {json.dumps(resp.response, ensure_ascii=False, default=str)}")
    return "\n".join(lines)


class A365TelemetryPlugin(BasePlugin):
    """Records each ADK run as Agent 365 invoke_agent, chat and execute_tool spans.

    Spans carry the agent and tenant IDs themselves (no baggage), so only these
    spans are exported. ADK's own spans use the same operation names but have no
    agent identity, so the exporter drops them instead of sending duplicates.
    """

    def __init__(self, agent_details: AgentDetails):
        super().__init__(name="a365_telemetry")
        self._agent = agent_details
        # Open scopes, keyed by invocation ID (and function call ID for tools).
        self._invocations: dict[str, InvokeAgentScope] = {}
        self._requests: dict[str, Request] = {}
        self._outputs: dict[str, str] = {}
        self._inferences: dict[str, InferenceScope] = {}
        self._tools: dict[str, ExecuteToolScope] = {}

    def _child(self, invocation_id: str) -> SpanDetails:
        scope = self._invocations.get(invocation_id)
        return SpanDetails(parent_context=scope.get_context() if scope else None)

    async def before_run_callback(self, *, invocation_context):
        session_id = invocation_context.session.id
        text = content_text(invocation_context.user_content)
        request = Request(
            content=text,
            session_id=session_id,
            conversation_id=session_id,
            channel=Channel(name="adk"),
        )
        scope = InvokeAgentScope.start(request, InvokeAgentScopeDetails(), self._agent)
        scope.record_input_messages([text])
        self._requests[invocation_context.invocation_id] = request
        self._invocations[invocation_context.invocation_id] = scope
        return None

    async def on_event_callback(self, *, invocation_context, event):
        # Take the answer from the event the user receives rather than from the
        # model, so a reply that a later plugin replaced (e.g. a DLP block) is
        # recorded as what was actually shown.
        if event.is_final_response() and event.content:
            text = content_text(event.content)
            if text:
                self._outputs[invocation_context.invocation_id] = text
        return None

    async def after_run_callback(self, *, invocation_context):
        invocation_id = invocation_context.invocation_id
        self._requests.pop(invocation_id, None)
        # A later plugin can answer in place of the model; ADK then skips the
        # after_model callbacks, so close the inference scope here.
        inference = self._inferences.pop(invocation_id, None)
        if inference:
            inference.dispose()
        scope = self._invocations.pop(invocation_id, None)
        if scope:
            output = self._outputs.pop(invocation_id, "")
            if output:
                scope.record_output_messages([output])
            scope.dispose()

    async def before_model_callback(self, *, callback_context, llm_request):
        invocation_id = callback_context.invocation_id
        request = self._requests.get(invocation_id)
        if request is None:
            return None
        scope = InferenceScope.start(
            request,
            InferenceCallDetails(
                operationName=InferenceOperationType.CHAT,
                model=llm_request.model or "",
                providerName="gcp.vertex_ai",
            ),
            self._agent,
            span_details=self._child(invocation_id),
        )
        # The full history goes to Gemini; record only the newest turn.
        if llm_request.contents:
            scope.record_input_messages([content_text(llm_request.contents[-1])])
        self._inferences[invocation_id] = scope
        return None

    async def after_model_callback(self, *, callback_context, llm_response):
        # With streaming, ADK calls this for every partial chunk too.
        if llm_response.partial:
            return None
        invocation_id = callback_context.invocation_id
        scope = self._inferences.pop(invocation_id, None)
        if scope is None:
            return None
        text = content_text(llm_response.content)
        if text:
            scope.record_output_messages([text])
        usage = llm_response.usage_metadata
        if usage:
            if usage.prompt_token_count is not None:
                scope.record_input_tokens(usage.prompt_token_count)
            if usage.candidates_token_count is not None:
                scope.record_output_tokens(usage.candidates_token_count)
        if llm_response.finish_reason:
            scope.record_finish_reasons([str(llm_response.finish_reason.value).lower()])
        scope.dispose()
        return None

    async def on_model_error_callback(self, *, callback_context, llm_request, error):
        scope = self._inferences.pop(callback_context.invocation_id, None)
        if scope:
            scope.record_error(error)
            scope.dispose()
        return None

    async def before_tool_callback(self, *, tool, tool_args, tool_context):
        invocation_id = tool_context.invocation_id
        request = self._requests.get(invocation_id)
        if request is None:
            return None
        scope = ExecuteToolScope.start(
            request,
            ToolCallDetails(
                tool_name=tool.name,
                arguments=tool_args,
                tool_call_id=tool_context.function_call_id,
                description=tool.description,
                # Every tool here comes from an MCP server.
                tool_type=ToolType.EXTENSION.value,
            ),
            self._agent,
            span_details=self._child(invocation_id),
        )
        self._tools[tool_context.function_call_id] = scope
        return None

    async def after_tool_callback(self, *, tool, tool_args, tool_context, result):
        scope = self._tools.pop(tool_context.function_call_id, None)
        if scope:
            scope.record_response(json.dumps(result, ensure_ascii=False, default=str))
            scope.dispose()
        return None

    async def on_tool_error_callback(self, *, tool, tool_args, tool_context, error):
        scope = self._tools.pop(tool_context.function_call_id, None)
        if scope:
            scope.record_error(error)
            scope.dispose()
        return None


def load_identity() -> Optional[AgentIdentityTokenProvider]:
    """Returns the agent identity from the A365_* settings, or None if any is missing."""
    settings = {
        name: os.getenv(name)
        for name in ("A365_TENANT_ID", "A365_BLUEPRINT_ID", "A365_AGENT_ID", "A365_GCP_SA_KEY_FILE")
    }
    missing = [name for name, value in settings.items() if not value]
    if missing:
        logger.info("Agent 365 disabled; missing %s", ", ".join(missing))
        return None
    return AgentIdentityTokenProvider(
        tenant_id=settings["A365_TENANT_ID"],
        blueprint_id=settings["A365_BLUEPRINT_ID"],
        agent_id=settings["A365_AGENT_ID"],
        key_file=os.path.expandvars(settings["A365_GCP_SA_KEY_FILE"]),
    )


def create_plugin(
    identity: AgentIdentityTokenProvider, agent_name: str, agent_description: str
) -> A365TelemetryPlugin:
    """Registers the Agent 365 exporter and returns the plugin that feeds it."""
    _register_exporter(identity)
    return A365TelemetryPlugin(
        AgentDetails(
            agent_id=identity.agent_id,
            agent_name=agent_name,
            agent_description=agent_description,
            agent_blueprint_id=identity.blueprint_id,
            tenant_id=identity.tenant_id,
        )
    )
