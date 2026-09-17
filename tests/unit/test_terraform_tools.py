"""Unit tests for Terraform tools and Executor."""

from unittest.mock import AsyncMock, patch

import pytest

from sdlc_manager.tools._executor import (
    run_cli_command,
)
from sdlc_manager.tools.terraform_tools import (
    terraform_fmt,
    terraform_init,
    terraform_plan,
    terraform_validate,
)


@pytest.mark.asyncio
async def test_run_cli_command_not_found():
    res = await run_cli_command("non_existent_binary_xyz_123", ["status"])
    assert res["status"] == "error"
    assert res["code"] == "command_not_found"


@pytest.mark.asyncio
async def test_terraform_init_args():
    with patch(
        "sdlc_manager.tools.terraform_tools.run_terraform_command",
        new_callable=AsyncMock,
    ) as mock_run:
        mock_run.return_value = {"status": "success", "code": "command_completed"}
        res = await terraform_init(working_directory="/test/dir", upgrade=True)
        assert res["status"] == "success"
        mock_run.assert_called_once_with(
            # 600s: provider downloads on a cold dir routinely exceed the
            # generic 120s default and must be slow successes, not timeouts.
            ["init", "-input=false", "-upgrade"], cwd="/test/dir", timeout=600
        )


@pytest.mark.asyncio
async def test_terraform_validate_args():
    with patch(
        "sdlc_manager.tools.terraform_tools.run_terraform_command",
        new_callable=AsyncMock,
    ) as mock_run:
        mock_run.return_value = {"status": "success", "code": "command_completed"}
        res = await terraform_validate(working_directory="/test/dir")
        assert res["status"] == "success"
        mock_run.assert_called_once_with(["validate", "-json"], cwd="/test/dir")


@pytest.mark.asyncio
async def test_terraform_plan_args():
    with patch(
        "sdlc_manager.tools.terraform_tools.run_terraform_command",
        new_callable=AsyncMock,
    ) as mock_run:
        mock_run.return_value = {"status": "success", "code": "command_completed"}
        res = await terraform_plan(working_directory="/test/dir", var_file="dev.tfvars")
        assert res["status"] == "success"
        mock_run.assert_called_once_with(
            ["plan", "-input=false", "-no-color", "-var-file", "dev.tfvars"],
            cwd="/test/dir",
            timeout=180,
        )


@pytest.mark.asyncio
async def test_terraform_fmt_args():
    with patch(
        "sdlc_manager.tools.terraform_tools.run_terraform_command",
        new_callable=AsyncMock,
    ) as mock_run:
        mock_run.return_value = {"status": "success", "code": "command_completed"}
        res = await terraform_fmt(
            working_directory="/test/dir", check_only=True, recursive=True
        )
        assert res["status"] == "success"
        mock_run.assert_called_once_with(
            ["fmt", "-check", "-recursive"], cwd="/test/dir"
        )
