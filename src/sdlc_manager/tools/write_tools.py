"""Write Git tools that modify the repository.

These tools perform destructive or state-changing operations and require
user approval via the before_tool_callback before execution.
"""

import logging
import re
from typing import Any

from google.adk.tools import ToolContext

from ._executor import CLONE_TIMEOUT, run_git_command, safe_git_value

logger = logging.getLogger(__name__)


async def git_clone(
    tool_context: ToolContext,
    repository_url: str,
    destination_path: str,
    branch: str | None = None,
    depth: int | None = None,
) -> dict[str, Any]:
    """Clone a Git repository to a local directory.

    Args:
        repository_url: URL of the repository to clone (HTTPS or SSH).
        destination_path: Absolute path where the repository should be cloned.
        branch: Optional specific branch to clone.
        depth: Optional shallow clone depth (e.g., 1 for latest commit only).

    Returns:
        Structured result with clone outcome.
    """
    args = ["clone"]
    if branch:
        args.extend(["--branch", safe_git_value(branch, "branch")])
    if depth is not None:
        args.extend(["--depth", str(depth)])
    url = str(repository_url).strip()
    if not re.match(r"^(https?://|git@|ssh://)", url):
        return {
            "status": "error",
            "message": (
                f"Refused: '{url}' is not a recognized git URL (https, ssh, git@)."
            ),
        }
    args.extend([url, safe_git_value(destination_path, "path")])
    return await run_git_command(args, timeout=CLONE_TIMEOUT)


async def git_checkout_branch(
    tool_context: ToolContext,
    working_directory: str,
    branch_name: str,
    create_new: bool = False,
    start_point: str | None = None,
) -> dict[str, Any]:
    """Switch to an existing branch or create a new one.

    Args:
        working_directory: Absolute path to the Git repository.
        branch_name: Name of the branch to switch to or create.
        create_new: If True, create a new branch with this name.
        start_point: Starting point for the new branch (e.g., main, a commit hash).

    Returns:
        Structured result with checkout outcome.
    """
    args = ["checkout"]
    if create_new:
        args.append("-b")
    args.append(safe_git_value(branch_name, "branch"))
    if create_new and start_point:
        args.append(safe_git_value(start_point, "ref"))
    return await run_git_command(args, cwd=working_directory)


async def git_add(
    tool_context: ToolContext,
    working_directory: str,
    file_paths: list[str] | None = None,
    add_all: bool = False,
) -> dict[str, Any]:
    """Stage files for the next commit.

    Args:
        working_directory: Absolute path to the Git repository.
        file_paths: List of file paths to stage (relative to repo root).
        add_all: If True, stage all modified and new files.

    Returns:
        Structured result with staging outcome.
    """
    args = ["add"]
    if add_all:
        args.append("--all")
    elif file_paths:
        args.extend(["--", *(safe_git_value(f, "path") for f in file_paths)])
    else:
        return {
            "status": "error",
            "code": "invalid_arguments",
            "message": "Provide file_paths or set add_all=True.",
        }
    return await run_git_command(args, cwd=working_directory)


async def git_commit(
    tool_context: ToolContext,
    working_directory: str,
    message: str,
    amend: bool = False,
    no_verify: bool = False,
) -> dict[str, Any]:
    """Commit staged changes with a message.

    Args:
        working_directory: Absolute path to the Git repository.
        message: Commit message. Should follow conventional commit format.
        amend: If True, amend the previous commit.
        no_verify: If True, skip the repository's git hooks. Only on the
            user's explicit instruction — say clearly which checks are skipped.

    Returns:
        Structured result with commit outcome.
    """
    args = ["commit", "-m", message]
    if amend:
        args.append("--amend")
    governance = "bypassed"
    if no_verify:
        logger.warning(
            "AUDIT hooks bypassed: git commit --no-verify in %s", working_directory
        )
        args.append("--no-verify")
    else:
        from .lefthook_bridge import governance_state

        governance = governance_state(working_directory)
        if governance == "unwired":
            # Governed repo, hooks never installed: git would commit with
            # zero checks while looking governed. Refuse instead of letting
            # governance silently not happen.
            return {
                "status": "blocked",
                "code": "hooks_not_wired",
                "message": (
                    "This repository is governed by lefthook.yml, but its git "
                    "hooks are not installed — the commit would skip every "
                    "repository check. Wire them first (wire_repo_hooks), "
                    "then retry the commit."
                ),
            }
    if governance == "wired":
        # This line reaches the terminal only when the tool actually executes
        # (post-approval); the hooks then run for minutes with no other output.
        print("  ⏳ Committing — running the repository's checks (may take minutes)...")
    # A commit runs the repo's wired pre-commit hooks (lefthook), which can
    # legitimately take minutes — the default read-command timeout kills them.
    return await run_git_command(args, cwd=working_directory, timeout=300)


async def git_push(
    tool_context: ToolContext,
    working_directory: str,
    remote: str = "origin",
    branch: str | None = None,
    set_upstream: bool = False,
    force: bool = False,
    tags: bool = False,
    no_verify: bool = False,
) -> dict[str, Any]:
    """Push local commits to a remote repository.

    Args:
        working_directory: Absolute path to the Git repository.
        remote: Remote name (default: origin).
        branch: Branch to push. None pushes current branch.
        set_upstream: If True, set the upstream tracking branch.
        force: If True, force push (DANGEROUS  -  use with caution).
        tags: If True, push tags along with commits.
        no_verify: If True, skip the repository's git hooks. Only on the
            user's explicit instruction — say clearly which checks are skipped.

    Returns:
        Structured result with push outcome.
    """
    args = ["push"]
    if set_upstream:
        args.append("--set-upstream")
    if force:
        args.append("--force-with-lease")
    if tags:
        args.append("--tags")
    governance = "bypassed"
    if no_verify:
        logger.warning(
            "AUDIT hooks bypassed: git push --no-verify in %s", working_directory
        )
        args.append("--no-verify")
    else:
        from .lefthook_bridge import governance_state

        governance = governance_state(working_directory)
        if governance == "unwired":
            return {
                "status": "blocked",
                "code": "hooks_not_wired",
                "message": (
                    "This repository is governed by lefthook.yml, but its git "
                    "hooks are not installed — the push would skip every "
                    "repository check. Wire them first (wire_repo_hooks), "
                    "then retry the push."
                ),
            }
    args.append(safe_git_value(remote, "remote"))
    if branch:
        args.append(safe_git_value(branch, "branch"))
    if governance == "wired":
        print("  ⏳ Pushing — running the repository's checks (may take minutes)...")
    # A push runs the repo's wired pre-push hooks (validate/test suites).
    return await run_git_command(args, cwd=working_directory, timeout=900)


async def git_pull(
    tool_context: ToolContext,
    working_directory: str,
    remote: str = "origin",
    branch: str | None = None,
    rebase: bool = False,
) -> dict[str, Any]:
    """Pull changes from a remote repository.

    Args:
        working_directory: Absolute path to the Git repository.
        remote: Remote name (default: origin).
        branch: Branch to pull from. None pulls current branch's upstream.
        rebase: If True, rebase instead of merge when pulling.

    Returns:
        Structured result with pull outcome.
    """
    args = ["pull"]
    if rebase:
        args.append("--rebase")
    args.append(safe_git_value(remote, "remote"))
    if branch:
        args.append(safe_git_value(branch, "branch"))
    return await run_git_command(args, cwd=working_directory, timeout=60)


async def git_merge(
    tool_context: ToolContext,
    working_directory: str,
    branch_name: str,
    no_ff: bool = False,
    message: str | None = None,
) -> dict[str, Any]:
    """Merge a branch into the current branch.

    Args:
        working_directory: Absolute path to the Git repository.
        branch_name: Name of the branch to merge into the current branch.
        no_ff: If True, create a merge commit even for fast-forward merges.
        message: Optional custom merge commit message.

    Returns:
        Structured result with merge outcome.
    """
    args = ["merge"]
    if no_ff:
        args.append("--no-ff")
    if message:
        args.extend(["-m", message])
    args.append(safe_git_value(branch_name, "branch"))
    return await run_git_command(args, cwd=working_directory)


async def git_stash(
    tool_context: ToolContext,
    working_directory: str,
    action: str = "push",
    message: str | None = None,
    stash_index: int | None = None,
) -> dict[str, Any]:
    """Stash or restore working directory changes.

    Args:
        working_directory: Absolute path to the Git repository.
        action: Stash action  -  "push" (save), "pop" (restore and remove),
            "apply" (restore and keep), "drop" (remove).
        message: Optional message for the stash entry (only for "push").
        stash_index: Index of the stash entry for pop/apply/drop operations.

    Returns:
        Structured result with stash outcome.
    """
    if action not in ("push", "pop", "apply", "list", "drop", "show"):
        return {"status": "error", "message": f"Invalid stash action: {action!r}."}
    args = ["stash", action]
    if action == "push" and message:
        args.extend(["-m", message])
    if stash_index is not None and action in ("pop", "apply", "drop"):
        args.append(f"stash@{{{stash_index}}}")
    return await run_git_command(args, cwd=working_directory)


async def git_rebase(
    tool_context: ToolContext,
    working_directory: str,
    onto_branch: str,
    interactive: bool = False,
    abort: bool = False,
    continue_rebase: bool = False,
) -> dict[str, Any]:
    """Rebase current branch onto another branch.

    Args:
        working_directory: Absolute path to the Git repository.
        onto_branch: Branch to rebase onto (ignored if abort or continue_rebase).
        interactive: If True, start interactive rebase.
        abort: If True, abort an in-progress rebase.
        continue_rebase: If True, continue an in-progress rebase.

    Returns:
        Structured result with rebase outcome.
    """
    args = ["rebase"]
    if abort:
        args.append("--abort")
    elif continue_rebase:
        args.append("--continue")
    else:
        if interactive:
            # There is no TTY here: --interactive would spawn an editor that
            # can never return, hang, and die on timeout. Refuse it cleanly.
            return {
                "status": "error",
                "message": "Interactive rebase is not supported in this environment "
                "— run it in your own terminal, or use a non-interactive rebase.",
            }
        args.append(safe_git_value(onto_branch, "branch"))
    return await run_git_command(args, cwd=working_directory)


async def git_reset(
    tool_context: ToolContext,
    working_directory: str,
    mode: str = "mixed",
    target: str = "HEAD",
) -> dict[str, Any]:
    """Reset current HEAD to a specified state.

    WARNING: --hard mode discards all uncommitted changes permanently.

    Args:
        working_directory: Absolute path to the Git repository.
        mode: Reset mode  -  "soft" (keep staged), "mixed" (unstage, keep changes),
            "hard" (discard all changes).
        target: Commit reference to reset to (default HEAD).

    Returns:
        Structured result with reset outcome.
    """
    if mode not in ("soft", "mixed", "hard"):
        return {
            "status": "error",
            "message": f"Invalid reset mode: {mode!r} (soft|mixed|hard).",
        }
    args = ["reset", f"--{mode}", safe_git_value(target, "ref")]
    return await run_git_command(args, cwd=working_directory)


async def git_tag(
    tool_context: ToolContext,
    working_directory: str,
    tag_name: str | None = None,
    message: str | None = None,
    delete: bool = False,
    list_tags: bool = False,
) -> dict[str, Any]:
    """Create, list, or delete tags.

    Args:
        working_directory: Absolute path to the Git repository.
        tag_name: Name of the tag to create or delete.
        message: Optional annotation message (creates annotated tag).
        delete: If True, delete the specified tag.
        list_tags: If True, list all tags.

    Returns:
        Structured result with tag outcome.
    """
    args = ["tag"]
    if list_tags:
        args.append("--list")
    elif delete and tag_name:
        args.extend(["-d", tag_name])
    elif tag_name:
        if message:
            args.extend(["-a", tag_name, "-m", message])
        else:
            args.append(tag_name)
    else:
        # Default to listing
        args.append("--list")
    return await run_git_command(args, cwd=working_directory)


async def git_fetch(
    tool_context: ToolContext,
    working_directory: str,
    remote: str = "origin",
    prune: bool = False,
    all_remotes: bool = False,
) -> dict[str, Any]:
    """Fetch changes from remote without merging.

    Args:
        working_directory: Absolute path to the Git repository.
        remote: Remote name (default: origin).
        prune: If True, remove remote-tracking references that no longer exist.
        all_remotes: If True, fetch from all configured remotes.

    Returns:
        Structured result with fetch outcome.
    """
    args = ["fetch"]
    if all_remotes:
        args.append("--all")
    else:
        args.append(safe_git_value(remote, "remote"))
    if prune:
        args.append("--prune")
    return await run_git_command(args, cwd=working_directory, timeout=60)
