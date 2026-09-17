"""Unit tests for approval callbacks."""

from unittest.mock import MagicMock

from sdlc_manager.callbacks import PENDING_APPROVAL_KEY, require_user_approval


def test_require_user_approval_for_write_tool(mock_tool_context):
    tool = MagicMock()
    tool.name = "git_commit"
    args = {"message": "feat: test"}

    res = require_user_approval(tool, args, mock_tool_context)
    assert res is not None
    assert res["status"] == "pending_approval"
    assert mock_tool_context.state[PENDING_APPROVAL_KEY]["tool"] == "git_commit"


def test_require_user_approval_passes_read_tool(mock_tool_context):
    tool = MagicMock()
    tool.name = "git_status"
    args = {}

    res = require_user_approval(tool, args, mock_tool_context)
    assert res is None
