"""Agent lifecycle callback functions for monitoring, memory, and approval."""

import logging
import re
from typing import Any

from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.context import Context
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.tools import ToolContext
from google.adk.tools.base_tool import BaseTool
from opentelemetry import trace

logger = logging.getLogger(__name__)

# Tools that modify repository or infrastructure state and require user approval
WRITE_TOOLS: frozenset[str] = frozenset(
    {
        # Git write operations
        "git_clone",
        "git_checkout_branch",
        "git_add",
        "git_commit",
        "git_push",
        "git_pull",
        "git_merge",
        "git_stash",
        "git_rebase",
        "git_reset",
        "git_tag",
        "git_fetch",
        "wire_repo_hooks",
        # GitHub write operations
        "github_create_pr",
        "github_merge_pr",
        # Terraform write operations
        "terraform_plan",
        "terraform_init",
    }
)

PENDING_APPROVAL_KEY = "pending_tool_approval"


_MEMORY_UNAVAILABLE = False


async def add_session_to_memory(callback_context: CallbackContext) -> None:
    """Automatically save completed sessions to memory bank."""
    global _MEMORY_UNAVAILABLE
    if _MEMORY_UNAVAILABLE:
        return  # no memory service here — one warning was enough
    try:
        await callback_context.add_session_to_memory()
    except ValueError as e:
        _MEMORY_UNAVAILABLE = True
        logger.warning("Memory service unavailable (%s) — skipping for this process.", e)
    except Exception as e:
        logger.warning(f"Failed to add session to memory: {type(e).__name__}: {e}")


_APPROVAL_RE = re.compile(
    r"^\s*(yes|y|ok(ay)?|approve[d]?|go ahead|proceed|confirm(ed)?|sure|do it)\b",
    re.IGNORECASE,
)
_DENIAL_RE = re.compile(
    r"^\s*(no|nope|cancel|stop|abort|don'?t|reject)\b", re.IGNORECASE
)
# 'no problem, go ahead' must not read as denial; 'sure, but what does it
# do?' must not read as consent.
_POSITIVE_ANYWHERE_RE = re.compile(
    r"\b(go ahead|proceed|approve[d]?|yes|do it|continue)\b", re.IGNORECASE
)
_HESITATION_RE = re.compile(
    r"\?|\b(but|wait|first|before|except|hold)\b", re.IGNORECASE
)


def _user_consents(text: str) -> bool | None:
    """True = consent, False = denial, None = ambiguous (re-prompt)."""
    if _DENIAL_RE.match(text) and not _POSITIVE_ANYWHERE_RE.search(text):
        return False
    if _APPROVAL_RE.match(text) and not _HESITATION_RE.search(text):
        return True
    if _POSITIVE_ANYWHERE_RE.search(text) and not _HESITATION_RE.search(text):
        return True
    return None


def _current_user_text(tool_context: ToolContext) -> str | None:
    """The user message of the current invocation, if the runtime exposes it."""
    content = getattr(tool_context, "user_content", None)
    if content is None:
        return None
    parts = getattr(content, "parts", None) or []
    return " ".join(p.text for p in parts if getattr(p, "text", None)) or None


def require_user_approval(
    tool: BaseTool,
    args: dict[str, Any],
    tool_context: ToolContext,
) -> dict[str, Any] | None:
    """Block write tools until user confirms the operation.

    Approval is bound to the EXACT tool call: the re-invocation must carry the
    same arguments the user saw, and the user's current message must actually
    consent — otherwise a confused or prompt-injected model could get one call
    approved and execute a different one (e.g. adding force/no_verify, or
    switching branch) or proceed after the user said no.
    """
    tool_name = tool.name

    if tool_name not in WRITE_TOOLS:
        return None

    state = tool_context.state
    call_args = {k: v for k, v in args.items() if k != "tool_context"}

    pending = state.get(PENDING_APPROVAL_KEY)
    if isinstance(pending, dict) and pending.get("tool") == tool_name:
        user_text = _current_user_text(tool_context)
        verdict = _user_consents(user_text) if user_text is not None else None
        if user_text is not None and verdict is False:
            state[PENDING_APPROVAL_KEY] = None
            return {
                "status": "blocked",
                "code": "approval_denied",
                "message": (
                    f"Operation '{tool_name}' cancelled — you declined the approval."
                ),
            }
        if pending.get("args") != call_args:
            # The call changed after the user saw it — re-request with the new args.
            logger.warning(
                "Tool '%s' re-invoked with DIFFERENT args after approval request "
                "(approved=%s, attempted=%s) — requiring fresh approval.",
                tool_name, pending.get("args"), call_args,
            )
        elif user_text is None or verdict is True:
            # Same call, and the user's own words consent (or the runtime hides
            # the message — args identity is then the strongest available check).
            state[PENDING_APPROVAL_KEY] = None
            logger.info(
                "AUDIT approval granted: tool=%s args=%s", tool_name, call_args
            )
            return None

    approval_request = {"tool": tool_name, "args": call_args}
    state[PENDING_APPROVAL_KEY] = approval_request

    args_display = ", ".join(f"{k}={v!r}" for k, v in call_args.items())

    logger.info(
        "Approval requested for tool '%s' with args: %s", tool_name, args_display
    )

    return {
        "status": "pending_approval",
        "code": "approval_required",
        "message": (
            f"⚠️ This operation requires your approval:\n\n"
            f"**Tool:** `{tool_name}`\n"
            f"**Arguments:** {args_display}\n\n"
            f"Please confirm by saying 'yes', 'approve', or 'go ahead' "
            f"to proceed, or 'no'/'cancel' to abort."
        ),
        "tool": tool_name,
        "args": approval_request["args"],
    }


class LoggingCallbacks:
    """Provides observability callbacks for ADK agent lifecycle events."""

    def __init__(self, logger_instance: logging.Logger | None = None) -> None:
        if logger_instance is None:
            logger_instance = logging.getLogger(self.__class__.__module__)
        self.logger = logger_instance

    def _log_state_debug(self, ctx: Context) -> None:
        # to_dict() serializes the whole session state — pay that only
        # when DEBUG output is actually enabled.
        if not self.logger.isEnabledFor(logging.DEBUG):
            return
        state = ctx.state.to_dict()
        active_keys = [k for k, v in state.items() if v]
        self.logger.debug(f"Active state keys: {active_keys}")

    def before_agent(self, callback_context: CallbackContext) -> None:
        self.logger.info(
            f"*** Starting agent '{callback_context.agent_name}' "
            f"with invocation_id '{callback_context.invocation_id}' ***"
        )
        self._log_state_debug(callback_context)

    def after_agent(self, callback_context: CallbackContext) -> None:
        self.logger.info(
            f"*** Leaving agent '{callback_context.agent_name}' "
            f"with invocation_id '{callback_context.invocation_id}' ***"
        )
        self._log_state_debug(callback_context)

    def before_model(
        self,
        callback_context: CallbackContext,
        llm_request: LlmRequest,
    ) -> None:
        self.logger.info(
            f"*** Before LLM call for agent '{callback_context.agent_name}' "
            f"with invocation_id '{callback_context.invocation_id}' ***"
        )
        self._log_state_debug(callback_context)

    def after_model(
        self,
        callback_context: CallbackContext,
        llm_response: LlmResponse,
    ) -> None:
        self.logger.info(
            f"*** After LLM call for agent '{callback_context.agent_name}' "
            f"with invocation_id '{callback_context.invocation_id}' ***"
        )
        self._log_state_debug(callback_context)

        if usage := llm_response.usage_metadata:
            token_info: dict[str, int] = {
                key: value
                for key, value in {
                    "prompt_tokens": usage.prompt_token_count,
                    "response_tokens": usage.candidates_token_count,
                    "cached_tokens": usage.cached_content_token_count,
                    "reasoning_tokens": usage.thoughts_token_count,
                    "tool_use_tokens": usage.tool_use_prompt_token_count,
                }.items()
                if value is not None
            }
            if token_info:
                self.logger.info(f"Token usage: {token_info}")

            span = trace.get_current_span()
            if usage.cached_content_token_count is not None:
                span.set_attribute(
                    "gen_ai.usage.cache_read.input_tokens",
                    usage.cached_content_token_count,
                )

    def before_tool(
        self,
        tool: BaseTool,
        args: dict[str, Any],
        tool_context: ToolContext,
    ) -> None:
        self.logger.info(
            f"*** Before invoking tool '{tool.name}' in agent "
            f"'{tool_context.agent_name}' ***"
        )
        self._log_state_debug(tool_context)

    def after_tool(
        self,
        tool: BaseTool,
        args: dict[str, Any],
        tool_context: ToolContext,
        tool_response: dict[str, Any],
    ) -> None:
        self.logger.info(
            f"*** After invoking tool '{tool.name}' in agent "
            f"'{tool_context.agent_name}' ***"
        )
        self._log_state_debug(tool_context)
