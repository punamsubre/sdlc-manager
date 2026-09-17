"""Read-only Git tools that do not modify the repository.

These tools are safe to execute without user approval since they only
inspect the repository state without making changes.
"""

from typing import Any

from google.adk.tools import ToolContext

from ._executor import run_git_command, safe_git_value


async def git_status(
    tool_context: ToolContext,
    working_directory: str,
    short: bool = False,
) -> dict[str, Any]:
    """Show the working tree status of a Git repository.

    Displays modified, staged, and untracked files in the repository.

    Args:
        working_directory: Absolute path to the Git repository.
        short: If True, show output in short format.

    Returns:
        Structured result with repository status information.
    """
    args = ["status"]
    if short:
        args.append("--short")
    return await run_git_command(args, cwd=working_directory)


async def git_log(
    tool_context: ToolContext,
    working_directory: str,
    max_count: int = 10,
    oneline: bool = True,
    branch: str | None = None,
    file_path: str | None = None,
) -> dict[str, Any]:
    """Show commit history of the repository.

    Args:
        working_directory: Absolute path to the Git repository.
        max_count: Maximum number of commits to show (default 10).
        oneline: If True, show each commit on a single line.
        branch: Optional branch name to show history for.
        file_path: Optional file path to show history for a specific file.

    Returns:
        Structured result with commit history.
    """
    args = ["log", f"--max-count={max_count}"]
    if oneline:
        args.append("--oneline")
    else:
        args.append("--format=%H %ai %an%n  %s")
    if branch:
        args.append(safe_git_value(branch, "branch"))
    if file_path:
        args.extend(["--", safe_git_value(file_path, "path")])
    return await run_git_command(args, cwd=working_directory)


async def git_diff(
    tool_context: ToolContext,
    working_directory: str,
    staged: bool = False,
    branch_a: str | None = None,
    branch_b: str | None = None,
    file_path: str | None = None,
    stat_only: bool = False,
) -> dict[str, Any]:
    """Show differences between commits, working tree, or staged changes.

    Args:
        working_directory: Absolute path to the Git repository.
        staged: If True, show staged (cached) changes.
        branch_a: First branch/commit for comparison.
        branch_b: Second branch/commit for comparison.
        file_path: Optional file path to restrict diff to.
        stat_only: If True, show only file statistics (names and counts).

    Returns:
        Structured result with diff output.
    """
    args = ["diff"]
    if staged:
        args.append("--cached")
    if stat_only:
        args.append("--stat")
    if branch_a and branch_b:
        a = safe_git_value(branch_a, "branch")
        b = safe_git_value(branch_b, "branch")
        args.append(f"{a}...{b}")
    elif branch_a:
        args.append(safe_git_value(branch_a, "branch"))
    if file_path:
        args.extend(["--", safe_git_value(file_path, "path")])
    return await run_git_command(args, cwd=working_directory)


async def git_branch_list(
    tool_context: ToolContext,
    working_directory: str,
    show_remote: bool = False,
    show_all: bool = False,
) -> dict[str, Any]:
    """List branches in the repository.

    Args:
        working_directory: Absolute path to the Git repository.
        show_remote: If True, list remote-tracking branches.
        show_all: If True, list both local and remote branches.

    Returns:
        Structured result with branch listing.
    """
    args = ["branch", "-v"]
    if show_all:
        args.append("-a")
    elif show_remote:
        args.append("-r")
    return await run_git_command(args, cwd=working_directory)


async def git_remote_list(
    tool_context: ToolContext,
    working_directory: str,
) -> dict[str, Any]:
    """List configured remote repositories.

    Args:
        working_directory: Absolute path to the Git repository.

    Returns:
        Structured result with remote repository URLs.
    """
    return await run_git_command(["remote", "-v"], cwd=working_directory)


async def git_stash_list(
    tool_context: ToolContext,
    working_directory: str,
) -> dict[str, Any]:
    """List stashed changes in the repository.

    Args:
        working_directory: Absolute path to the Git repository.

    Returns:
        Structured result with stash entries.
    """
    return await run_git_command(["stash", "list"], cwd=working_directory)


async def git_show_commit(
    tool_context: ToolContext,
    working_directory: str,
    commit_ref: str = "HEAD",
    stat_only: bool = False,
) -> dict[str, Any]:
    """Show details of a specific commit.

    Args:
        working_directory: Absolute path to the Git repository.
        commit_ref: Commit hash, tag, or reference (default HEAD).
        stat_only: If True, show only file statistics.

    Returns:
        Structured result with commit details.
    """
    args = ["show", safe_git_value(commit_ref, "ref")]
    if stat_only:
        args.append("--stat")
    return await run_git_command(args, cwd=working_directory)


async def git_blame(
    tool_context: ToolContext,
    working_directory: str,
    file_path: str,
    line_start: int | None = None,
    line_end: int | None = None,
) -> dict[str, Any]:
    """Show line-by-line authorship of a file.

    Args:
        working_directory: Absolute path to the Git repository.
        file_path: Path to the file to blame (relative to repo root).
        line_start: Optional starting line number for the blame range.
        line_end: Optional ending line number for the blame range.

    Returns:
        Structured result with blame annotations.
    """
    args = ["blame"]
    if line_start is not None and line_end is not None:
        args.extend(["-L", f"{line_start},{line_end}"])
    args.extend(["--", safe_git_value(file_path, "path")])
    return await run_git_command(args, cwd=working_directory)
