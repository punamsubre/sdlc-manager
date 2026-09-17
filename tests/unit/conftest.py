"""Pytest unit test configuration and mocks."""

import os
from unittest.mock import MagicMock

import pytest


def pytest_configure() -> None:
    """Mock environment and credentials before tests run."""
    os.environ["GOOGLE_CLOUD_PROJECT"] = "test-project"
    os.environ["GOOGLE_CLOUD_LOCATION"] = "global"
    os.environ["AGENT_NAME"] = "sdlc_manager"
    os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] = "true"


@pytest.fixture
def mock_tool_context():
    """Create a mock ToolContext."""
    ctx = MagicMock()
    ctx.agent_name = "test_agent"
    ctx.invocation_id = "test-inv-123"
    ctx.state = {}
    return ctx
