"""ADK Orchestrator and Sub-Agent configurations for SDLC Manager."""

import os

from google.adk.agents import LlmAgent
from google.adk.apps import App
from google.adk.plugins.global_instruction_plugin import GlobalInstructionPlugin
from google.adk.plugins.logging_plugin import LoggingPlugin
from google.adk.tools import FunctionTool, load_memory

from .callbacks import LoggingCallbacks, add_session_to_memory, require_user_approval
from .config import ServerEnv, initialize_environment
from .prompt import (
    GIT_AGENT_INSTRUCTION,
    ORCHESTRATOR_INSTRUCTION,
    REVIEW_AGENT_INSTRUCTION,
    ROOT_AGENT_DESCRIPTION,
    TERRAFORM_AGENT_INSTRUCTION,
    VALIDATION_AGENT_INSTRUCTION,
    return_global_instruction,
)
from .tools import (
    apply_fixes,
    check_bigquery_schema,
    check_readiness,
    generate_pr_description,
    get_quality_score,
    git_add,
    git_blame,
    git_branch_list,
    git_checkout_branch,
    git_clone,
    git_commit,
    git_diff,
    git_fetch,
    git_log,
    git_merge,
    git_pull,
    git_push,
    git_rebase,
    git_remote_list,
    git_reset,
    git_show_commit,
    git_stash,
    git_stash_list,
    git_status,
    git_tag,
    github_create_pr,
    github_list_prs,
    github_merge_pr,
    github_pr_status,
    review_code,
    review_pr_diff,
    scan_secrets,
    terraform_fmt,
    terraform_init,
    terraform_plan,
    terraform_validate,
    validate_directory,
    validate_file,
    validate_sql_syntax,
    wire_repo_hooks,
)

env = initialize_environment(ServerEnv, print_config=False)
logging_callbacks = LoggingCallbacks()

git_agent = LlmAgent(
    name="git_agent",
    description="Handles Git CLI, branching, commits, and GitHub PR workflows",
    model=env.git_agent_model,
    instruction=GIT_AGENT_INSTRUCTION,
    tools=[
        FunctionTool(git_status),
        FunctionTool(git_log),
        FunctionTool(git_diff),
        FunctionTool(git_branch_list),
        FunctionTool(git_remote_list),
        FunctionTool(git_stash_list),
        FunctionTool(git_show_commit),
        FunctionTool(git_blame),
        FunctionTool(git_clone),
        FunctionTool(git_checkout_branch),
        FunctionTool(git_add),
        FunctionTool(git_commit),
        FunctionTool(git_push),
        FunctionTool(git_pull),
        FunctionTool(git_merge),
        FunctionTool(git_stash),
        FunctionTool(git_rebase),
        FunctionTool(git_reset),
        FunctionTool(git_tag),
        FunctionTool(git_fetch),
        FunctionTool(wire_repo_hooks),
        FunctionTool(github_create_pr),
        FunctionTool(github_list_prs),
        FunctionTool(github_pr_status),
        FunctionTool(github_merge_pr),
    ],
    before_tool_callback=[require_user_approval, logging_callbacks.before_tool],
    after_tool_callback=logging_callbacks.after_tool,
)

validation_agent = LlmAgent(
    name="validation_agent",
    description="Performs code quality checks, AST linting, and secret scanning",
    model=env.validation_agent_model,
    instruction=VALIDATION_AGENT_INSTRUCTION,
    tools=[
        FunctionTool(scan_secrets),
        FunctionTool(validate_file),
        FunctionTool(validate_sql_syntax),
        FunctionTool(check_bigquery_schema),
        FunctionTool(validate_directory),
        FunctionTool(apply_fixes),
        FunctionTool(check_readiness),
    ],
    before_tool_callback=[logging_callbacks.before_tool],
    after_tool_callback=logging_callbacks.after_tool,
)

review_agent = LlmAgent(
    name="review_agent",
    description="Provides deep AI architectural code reviews and scorecards",
    model=env.review_agent_model,
    instruction=REVIEW_AGENT_INSTRUCTION,
    tools=[
        FunctionTool(review_code),
        FunctionTool(get_quality_score),
        FunctionTool(review_pr_diff),
        FunctionTool(generate_pr_description),
        FunctionTool(check_readiness),
    ],
    before_tool_callback=[logging_callbacks.before_tool],
    after_tool_callback=logging_callbacks.after_tool,
)

terraform_agent = LlmAgent(
    name="terraform_agent",
    description="Manages Terraform formatting, validation, and plan preview",
    model=env.terraform_agent_model,
    instruction=TERRAFORM_AGENT_INSTRUCTION,
    tools=[
        FunctionTool(terraform_init),
        FunctionTool(terraform_validate),
        FunctionTool(terraform_fmt),
        FunctionTool(terraform_plan),
    ],
    before_tool_callback=[require_user_approval, logging_callbacks.before_tool],
    after_tool_callback=logging_callbacks.after_tool,
)

root_agent = LlmAgent(
    name=env.agent_name,
    description=ROOT_AGENT_DESCRIPTION,
    model=env.orchestrator_model,
    instruction=ORCHESTRATOR_INSTRUCTION,
    sub_agents=[git_agent, validation_agent, review_agent, terraform_agent],
    tools=[
        load_memory,
    ],
    before_agent_callback=logging_callbacks.before_agent,
    after_agent_callback=[logging_callbacks.after_agent, add_session_to_memory],
    before_model_callback=logging_callbacks.before_model,
    after_model_callback=logging_callbacks.after_model,
)

from google.adk.agents.context_cache_config import ContextCacheConfig

# LoggingPlugin prints raw event traces straight to stdout — that IS the
# server's Cloud Run log stream, but it buries answers in a developer
# terminal. Interactive surfaces opt out with SDLC_CONSOLE_TRACE=0.
_plugins = [GlobalInstructionPlugin(return_global_instruction)]
if os.getenv("SDLC_CONSOLE_TRACE", "1") != "0":
    _plugins.append(LoggingPlugin())

app = App(
    name=env.agent_name,
    root_agent=root_agent,
    plugins=_plugins,
    # The 24 tool declarations + instructions are re-sent on every model
    # call; caching that prefix cuts input tokens and per-call latency
    # without changing a single prompt or response.
    context_cache_config=ContextCacheConfig(
        min_tokens=2048,
        ttl_seconds=600,
        cache_intervals=10,
    ),
)
