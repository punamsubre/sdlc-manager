"""Environment configuration models for SDLC Manager.

Provides Pydantic models for type-safe environment variable validation
and configuration management across Orchestrator and all Sub-Agents.
"""

import json
import os
import re
from typing import Literal

from dotenv import find_dotenv, load_dotenv
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)


def enable_utf8_output() -> None:
    """Windows consoles default to cp1252; emoji in agent logs would crash
    every turn without UTF-8 output streams."""
    import sys

    for stream in (sys.stdout, sys.stderr):
        if stream and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


class ConfigurationError(RuntimeError):
    """Invalid environment configuration — catchable by importers."""


_ENV_INSTANCES: dict[type, BaseModel] = {}


def initialize_environment[T: BaseModel](
    model_class: type[T],
    override_dotenv: bool = True,
    print_config: bool = True,
) -> T:
    """Initialize and validate environment configuration.

    Idempotent per model class: server startup, agent import and the CLI
    all call this — the dotenv disk walk and validation run once.
    """
    cached = _ENV_INSTANCES.get(model_class)
    if cached is not None:
        return cached  # type: ignore[return-value]
    # Search from the CURRENT directory (the repo being served), not from the
    # installed package location - required for the packaged helper to find .env.
    load_dotenv(find_dotenv(usecwd=True), override=override_dotenv)

    try:
        env = model_class.model_validate(os.environ)
    except ValidationError as e:
        # Raise, don't sys.exit: importers (tests, the ADK loader) must see a
        # catchable error; entry points convert it to a clean exit themselves.
        raise ConfigurationError(f"Environment validation failed:\n{e}") from e

    # The genai client reads the raw environment at model-call time, so the
    # validated defaults must be written back — otherwise running from a repo
    # without a .env silently flips the client from Vertex to API-key mode.
    if isinstance(env, ServerEnv):
        os.environ.setdefault(
            "GOOGLE_GENAI_USE_VERTEXAI",
            "TRUE" if env.google_genai_use_vertexai else "FALSE",
        )
        os.environ.setdefault("GOOGLE_CLOUD_LOCATION", env.google_cloud_location)

    if print_config and hasattr(env, "print_config"):
        env.print_config()

    _ENV_INSTANCES[model_class] = env
    return env


class ServerEnv(BaseModel):
    """Environment configuration for SDLC Manager."""

    google_genai_use_vertexai: bool = Field(
        default=True,
        alias="GOOGLE_GENAI_USE_VERTEXAI",
        description="Use Vertex AI for model authentication",
    )

    gemini_api_key: str | None = Field(
        default=None,
        alias="GEMINI_API_KEY",
        description="Gemini API Key if using Gemini Developer API",
    )

    google_cloud_project: str = Field(
        ...,
        alias="GOOGLE_CLOUD_PROJECT",
        description="GCP project ID for authentication and observability",
    )

    google_cloud_location: str = Field(
        default="global",
        alias="GOOGLE_CLOUD_LOCATION",
        description="Vertex AI region (e.g., global, us-central1)",
    )

    agent_name: str = Field(
        default="sdlc_manager",
        alias="AGENT_NAME",
        description="Unique agent identifier for resources and logs",
    )

    # === Sub-Agent Model Configuration ===
    orchestrator_model: str = Field(
        default="gemini-3.7-flash",
        alias="ORCHESTRATOR_MODEL",
        description="Model for the top-level SDLC router/orchestrator agent",
    )

    git_agent_model: str = Field(
        default="gemini-3.7-flash",
        alias="GIT_AGENT_MODEL",
        description="Model for the Git & GitHub operations sub-agent",
    )

    validation_agent_model: str = Field(
        default="gemini-3.7-flash",
        alias="VALIDATION_AGENT_MODEL",
        description="Model for the AST & Code Quality validation sub-agent",
    )

    review_agent_model: str = Field(
        default="gemini-3.1-pro-preview",
        alias="REVIEW_AGENT_MODEL",
        description="Model for the deep AI code review sub-agent",
    )

    terraform_agent_model: str = Field(
        default="gemini-3.7-flash",
        alias="TERRAFORM_AGENT_MODEL",
        description="Model for the Terraform operations sub-agent",
    )

    ai_review_model: str = Field(
        default="gemini-3.7-flash",
        alias="AI_REVIEW_MODEL",
        description="Dedicated model for automated file review generation",
    )

    ai_fallback_model: str = Field(
        default="gemini-3.7-flash",
        alias="AI_FALLBACK_MODEL",
        description="Fallback model on primary model 429/503 errors",
    )

    # === AI Review Parameters ===
    ai_max_output_tokens: int = Field(
        default=32768,
        alias="AI_MAX_OUTPUT_TOKENS",
        description="Max token budget per file review",
    )

    ai_full_rewrite_max_lines: int = Field(
        default=1200,
        alias="AI_FULL_REWRITE_MAX_LINES",
        description="Max file line count before falling back to advisory-only review",
    )

    ai_max_retries: int = Field(
        default=3,
        alias="AI_MAX_RETRIES",
        description="Max retry attempts for AI review calls",
    )

    # === Server Settings ===
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO",
        alias="LOG_LEVEL",
        description="Logging verbosity level",
    )

    serve_web_interface: bool = Field(
        default=False,
        alias="SERVE_WEB_INTERFACE",
        description="Whether to serve the ADK web interface",
    )

    reload_agents: bool = Field(
        default=False,
        alias="RELOAD_AGENTS",
        description="Whether to reload agents on file changes",
    )

    session_service_uri: str | None = Field(
        default=None,
        alias="SESSION_SERVICE_URI",
        description="Session service URI",
    )

    memory_service_uri: str | None = Field(
        default=None,
        alias="MEMORY_SERVICE_URI",
        description="Memory service URI",
    )

    artifact_service_uri: str | None = Field(
        default=None,
        alias="ARTIFACT_SERVICE_URI",
        description="GCS bucket URI for artifact storage",
    )

    allow_origins: str = Field(
        default='["http://127.0.0.1:8000", "http://localhost:8000"]',
        alias="ALLOW_ORIGINS",
        description="JSON array string of allowed CORS origins",
    )

    host: str = Field(
        default="127.0.0.1",
        alias="HOST",
        description="Network interface for uvicorn to bind",
    )

    port: int = Field(
        default=8000,
        alias="PORT",
        description="Server port",
    )

    otel_capture_content: bool = Field(
        default=True,
        alias="OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT",
        description="OpenTelemetry message content capture setting",
    )

    # === Authentication ===
    auth_enabled: bool = Field(
        default=False,
        alias="AUTH_ENABLED",
        description="Whether Google OAuth and JWT authentication is required",
    )

    google_oauth_client_id: str | None = Field(
        default=None,
        alias="GOOGLE_OAUTH_CLIENT_ID",
        description="Google OAuth 2.0 Web Client ID",
    )

    google_oauth_client_secret: str | None = Field(
        default=None,
        alias="GOOGLE_OAUTH_CLIENT_SECRET",
        description="Google OAuth 2.0 Web Client Secret",
    )

    jwt_secret_key: str | None = Field(
        default=None,
        alias="JWT_SECRET_KEY",
        description="Secret key for signing session JWTs (required when AUTH_ENABLED)",
    )

    @field_validator("jwt_secret_key")
    @classmethod
    def _jwt_required_when_auth_on(cls, v, info):
        # Model-level enforcement: every entry path (server, uvicorn import,
        # helper) validates this — not just server.main().
        auth_on = str(info.data.get("auth_enabled", False)).lower() in ("true", "1")
        if auth_on and not v:
            raise ValueError("JWT_SECRET_KEY is required when AUTH_ENABLED is true")
        return v

    jwt_expiry_hours: int = Field(
        default=8,
        alias="JWT_EXPIRY_HOURS",
        description="Session JWT token lifetime in hours",
    )

    allowed_email_domains: str = Field(
        default="*",
        alias="ALLOWED_EMAIL_DOMAINS",
        description="Comma-separated allowed Google login email domains (* for all)",
    )

    # === GitHub Integration ===
    # No shared token: GitHub actions run as each developer via their own
    # OAuth authorization (hosted) or their local git credentials (helper).
    github_oauth_client_id: str | None = Field(
        default=None,
        alias="GITHUB_OAUTH_CLIENT_ID",
        description="GitHub OAuth App client ID for per-developer authorization",
    )

    git_default_branch: str = Field(
        default="main",
        alias="GIT_DEFAULT_BRANCH",
        description="Default branch name for Git operations",
    )

    git_command_timeout: int = Field(
        default=30,
        alias="GIT_COMMAND_TIMEOUT",
        description="Default timeout for Git commands in seconds",
    )

    # === Validation & FinOps ===
    finops_max_bytes_threshold: int = Field(
        default=10737418240,
        alias="FINOPS_MAX_BYTES_THRESHOLD",
        description="Max bytes allowed for BigQuery dry run before blocking",
    )

    sql_dataset_map: str | None = Field(
        default=None,
        alias="SQL_DATASET_MAP",
        description="Template variable to real dataset mappings",
    )

    sql_param_columns: str = Field(
        default="etl_batch_sk",
        alias="SQL_PARAM_COLUMNS",
        description="Comma-separated columns that must be parameterized",
    )

    schema_cache_ttl: int = Field(
        default=900,  # matches the consumer fallback in tools/schema_validation
        alias="SCHEMA_CACHE_TTL",
        description="TTL in seconds for cached table schemas",
    )

    # === Terraform ===
    terraform_command_timeout: int = Field(
        default=120,
        alias="TERRAFORM_COMMAND_TIMEOUT",
        description="Timeout in seconds for terraform plan and validate commands",
    )

    # === Rate Limiting ===
    rate_limit_api: str = Field(
        default="60/minute",
        alias="RATE_LIMIT_API",
        description="API request rate limit",
    )

    rate_limit_ai_review: str = Field(
        default="10/minute",
        alias="RATE_LIMIT_AI_REVIEW",
        description="Rate limit for heavy AI code review calls",
    )

    model_config = ConfigDict(
        populate_by_name=True,
        extra="ignore",
    )

    @field_validator("agent_name")
    @classmethod
    def sanitize_agent_name(cls, v: str) -> str:
        """ADK agent names must be valid Python identifiers. The helper loads
        the SERVED repository's .env, which may define AGENT_NAME for its own
        purposes (e.g. 'my-repo-agent') — normalize instead of crashing."""
        cleaned = re.sub(r"\W", "_", v.strip())
        return cleaned if cleaned.isidentifier() else "sdlc_manager"

    @field_validator("allow_origins")
    @classmethod
    def validate_allow_origins_format(cls, v: str) -> str:
        """Validate allow_origins is valid JSON array with at least one string."""
        try:
            origins = json.loads(v)
        except json.JSONDecodeError as e:
            msg = f"ALLOW_ORIGINS must be valid JSON: {e}"
            raise ValueError(msg) from e

        if not isinstance(origins, list) or not origins:
            msg = "ALLOW_ORIGINS must be a non-empty JSON array"
            raise ValueError(msg)

        if not all(isinstance(o, str) and o.strip() for o in origins):
            msg = "ALLOW_ORIGINS must be an array of non-empty strings"
            raise ValueError(msg)

        return v

    @property
    def allow_origins_list(self) -> list[str]:
        """Parse allow_origins JSON string to list."""
        result = json.loads(self.allow_origins)
        if not isinstance(result, list):  # pragma: no cover
            raise TypeError("Invalid allow_origins format")
        return result

    def print_config(self) -> None:
        """Print server configuration for verification."""
        print("\n\n✅ SDLC Manager Configuration Loaded:")
        print(f"  Project / Agent:     {self.google_cloud_project} / {self.agent_name}")
        print(f"  Orchestrator Model:  {self.orchestrator_model}")
        print(f"  Git Agent Model:     {self.git_agent_model}")
        print(f"  Validation Model:    {self.validation_agent_model}")
        print(f"  Review Agent Model:  {self.review_agent_model}")
        print(f"  Terraform Model:     {self.terraform_agent_model}")
        print(f"  AI Review Model:     {self.ai_review_model}")
        print(f"  Auth Enabled:        {self.auth_enabled}")
        print(f"  GitHub Auth:         {'OAuth app configured' if self.github_oauth_client_id else 'per-developer local git credentials'}\n\n")
