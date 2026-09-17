"""Terraform CLI tools for infrastructure validation, formatting, and planning."""

from typing import Any

from google.adk.tools import ToolContext

from ._executor import run_terraform_command


async def terraform_init(
    working_directory: str,
    upgrade: bool = False,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Initialize Terraform working directory and download provider plugins."""
    args = ["init", "-input=false"]
    if upgrade:
        args.append("-upgrade")
    return await run_terraform_command(args, cwd=working_directory, timeout=600)


async def terraform_validate(
    working_directory: str,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Validate the syntax and consistency of Terraform configuration files."""
    args = ["validate", "-json"]
    return await run_terraform_command(args, cwd=working_directory)


async def terraform_fmt(
    working_directory: str,
    check_only: bool = True,
    recursive: bool = True,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Check or format Terraform configuration files to canonical format."""
    args = ["fmt"]
    if check_only:
        args.append("-check")
    if recursive:
        args.append("-recursive")
    return await run_terraform_command(args, cwd=working_directory)


async def terraform_plan(
    working_directory: str,
    var_file: str | None = None,
    target: str | None = None,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Generate and display an execution plan showing Terraform actions."""
    args = ["plan", "-input=false", "-no-color"]
    if var_file:
        args.extend(["-var-file", var_file])
    if target:
        args.extend(["-target", target])
    return await run_terraform_command(args, cwd=working_directory, timeout=180)
