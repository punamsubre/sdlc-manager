"""Unit tests for ServerEnv configuration."""

import pytest
from pydantic import ValidationError

from sdlc_manager.config import ServerEnv


def test_server_env_defaults():
    env_dict = {
        "GOOGLE_CLOUD_PROJECT": "my-gcp-project",
        "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "true",
    }
    config = ServerEnv.model_validate(env_dict)
    assert config.google_cloud_project == "my-gcp-project"
    assert config.orchestrator_model == "gemini-3.7-flash"
    assert config.review_agent_model == "gemini-3.1-pro-preview"
    assert config.git_default_branch == "main"
    assert config.allow_origins_list == [
        "http://127.0.0.1:8000",
        "http://localhost:8000",
    ]


def test_server_env_invalid_allow_origins():
    env_dict = {
        "GOOGLE_CLOUD_PROJECT": "my-gcp-project",
        "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "true",
        "ALLOW_ORIGINS": "not-valid-json",
    }
    with pytest.raises(ValidationError):
        ServerEnv.model_validate(env_dict)
