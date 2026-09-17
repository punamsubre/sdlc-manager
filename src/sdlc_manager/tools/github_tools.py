"""GitHub API tools for pull request management.

Uses httpx for async HTTP requests to the GitHub REST API v3.

Authentication is always PER USER — there is no shared token:
  1. An explicit token (the logged-in developer's OAuth token, passed by the
     dashboard routes), or
  2. The developer's own stored git credentials on this machine (Windows
     credential manager / git credential helper) — the same identity they use
     for `git push`.
GitHub then enforces that every action is limited to what THAT user's account
is allowed to do.
"""

import asyncio
import logging
import os
import time
from typing import Any

from google.adk.tools import ToolContext

ERROR_STATUS = "error"
SUCCESS_STATUS = "success"

logger = logging.getLogger(__name__)

_HTTP_CLIENT = None


def _http_client():
    """One pooled client per process: connection/TLS reuse across GitHub calls."""
    global _HTTP_CLIENT
    if _HTTP_CLIENT is None:
        import httpx

        _HTTP_CLIENT = httpx.AsyncClient(timeout=30.0)
    return _HTTP_CLIENT

GITHUB_API_BASE = "https://api.github.com"

# Short-lived cache for the local git credential lookup: (token, username, expiry)
_LOCAL_CRED_CACHE: tuple[str | None, str | None, float] = (None, None, 0.0)


_CRED_LOCK = asyncio.Lock()


async def local_git_credential() -> tuple[str | None, str | None]:
    """Fetch the developer's own GitHub credential from the local git credential helper.

    Returns (token, username). Never prompts interactively.
    """
    global _LOCAL_CRED_CACHE
    token, user, expires = _LOCAL_CRED_CACHE
    if time.time() < expires:
        return token, user

    # Single flight: concurrent callers share one helper subprocess.
    async with _CRED_LOCK:
        token, user, expires = _LOCAL_CRED_CACHE
        if time.time() < expires:
            return token, user
        return await _local_git_credential_locked()


async def _local_git_credential_locked() -> tuple[str | None, str | None]:
    global _LOCAL_CRED_CACHE
    proc = None
    result: tuple[str | None, str | None] = (None, None)
    try:
        env = dict(os.environ)
        env["GCM_INTERACTIVE"] = "never"
        env["GIT_TERMINAL_PROMPT"] = "0"
        proc = await asyncio.create_subprocess_exec(
            "git", "credential", "fill",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        out, _ = await asyncio.wait_for(
            proc.communicate(b"protocol=https\nhost=github.com\n\n"), timeout=10
        )
        fields = dict(
            line.split("=", 1) for line in out.decode("utf-8", "replace").splitlines() if "=" in line
        )
        if fields.get("password"):
            result = (fields["password"], fields.get("username"))
    except Exception as e:
        logger.debug("Local git credential lookup failed: %s", e)
        # A timed-out helper must be reaped, not abandoned.
        if proc is not None and proc.returncode is None:
            import contextlib

            with contextlib.suppress(Exception):
                proc.kill()
                await proc.wait()

    _LOCAL_CRED_CACHE = (result[0], result[1], time.time() + 120)
    return result


def _headers_for(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def _github_api_request(
    method: str,
    endpoint: str,
    json_body: dict[str, Any] | None = None,
    token: str | None = None,
) -> dict[str, Any]:
    """Make an authenticated request to the GitHub API as the current developer.

    Args:
        method: HTTP method (GET, POST, PUT, PATCH, DELETE).
        endpoint: API endpoint path (e.g., /repos/owner/repo/pulls).
        json_body: Optional JSON request body.
        token: The developer's own token (OAuth). If omitted, the developer's
            local git credentials are used.

    Returns:
        Structured result with API response.
    """
    import httpx

    if not token:
        token, _ = await local_git_credential()
    if not token:
        return {
            "status": ERROR_STATUS,
            "code": "not_connected",
            "message": (
                "No GitHub identity available. Connect GitHub in the dashboard, "
                "or sign in to GitHub with git on this machine (e.g. run a git push once)."
            ),
        }
    headers = _headers_for(token)

    url = f"{GITHUB_API_BASE}{endpoint}"
    logger.info("GitHub API %s %s", method, endpoint)

    try:
        response = await _http_client().request(
            method=method,
            url=url,
            headers=headers,
            json=json_body,
        )

        if response.status_code >= 400:
            try:
                error_data = response.json() if response.content else {}
            except ValueError:
                error_data = {"message": response.text[:200]}
            return {
                "status": ERROR_STATUS,
                "code": "api_error",
                "message": error_data.get("message", f"HTTP {response.status_code}"),
                "status_code": response.status_code,
                "errors": error_data.get("errors", []),
            }

        try:
            response_data = response.json() if response.content else {}
        except ValueError:
            response_data = {"raw": response.text[:500]}
        return {
            "status": SUCCESS_STATUS,
            "code": "api_success",
            "data": response_data,
            "status_code": response.status_code,
        }

    except httpx.TimeoutException:
        return {
            "status": ERROR_STATUS,
            "code": "timeout",
            "message": f"GitHub API request timed out: {method} {endpoint}",
        }
    except httpx.HTTPError as e:
        return {
            "status": ERROR_STATUS,
            "code": "http_error",
            "message": f"GitHub API request failed: {e}",
        }


async def github_create_pr(
    tool_context: ToolContext,
    owner: str,
    repo: str,
    title: str,
    head_branch: str,
    base_branch: str = "main",
    body: str = "",
    draft: bool = False,
    token: str | None = None,
) -> dict[str, Any]:
    """Create a pull request on GitHub as the current developer.

    Args:
        owner: Repository owner (username or organization).
        repo: Repository name.
        title: Pull request title.
        head_branch: Branch containing changes (source).
        base_branch: Branch to merge into (target, default: main).
        body: Pull request description in Markdown.
        draft: If True, create as draft PR.
        token: Developer's OAuth token; omitted -> local git credentials.

    Returns:
        Structured result with PR URL and number.
    """
    result = await _github_api_request(
        "POST",
        f"/repos/{owner}/{repo}/pulls",
        json_body={
            "title": title,
            "head": head_branch,
            "base": base_branch,
            "body": body,
            "draft": draft,
        },
        token=token,
    )

    if result["status"] == SUCCESS_STATUS:
        pr_data = result["data"]
        return {
            "status": SUCCESS_STATUS,
            "code": "pr_created",
            "message": f"Pull request #{pr_data['number']} created successfully.",
            "pr_number": pr_data["number"],
            "pr_url": pr_data["html_url"],
            "pr_title": pr_data["title"],
            "pr_state": pr_data["state"],
        }
    return result


# Deliberately NOT agent-registered (absent from tools/__init__ exports):
# reviewer assignment is a dashboard-only action so the reviewer list is
# always the one the user saw and confirmed in the PR form.
async def github_request_reviewers(
    tool_context: ToolContext,
    owner: str,
    repo: str,
    pr_number: int,
    reviewers: list[str],
    token: str | None = None,
) -> dict[str, Any]:
    """Request reviewers on an existing pull request.

    Args:
        owner: Repository owner (username or organization).
        repo: Repository name.
        pr_number: Pull request number.
        reviewers: GitHub usernames to request review from.
        token: Developer's OAuth token; omitted -> local git credentials.

    Returns:
        Structured result with requested reviewer outcome.
    """
    result = await _github_api_request(
        "POST",
        f"/repos/{owner}/{repo}/pulls/{pr_number}/requested_reviewers",
        json_body={"reviewers": reviewers},
        token=token,
    )

    if result["status"] == SUCCESS_STATUS:
        return {
            "status": SUCCESS_STATUS,
            "code": "reviewers_requested",
            "message": f"Requested review from: {', '.join(reviewers)}.",
            "reviewers": reviewers,
        }
    return result


async def github_list_prs(
    tool_context: ToolContext,
    owner: str,
    repo: str,
    state: str = "open",
    head_branch: str | None = None,
    base_branch: str | None = None,
    max_count: int = 10,
) -> dict[str, Any]:
    """List pull requests on a GitHub repository.

    Args:
        owner: Repository owner (username or organization).
        repo: Repository name.
        state: Filter by state  -  "open", "closed", or "all".
        head_branch: Filter by head branch name.
        base_branch: Filter by base branch name.
        max_count: Maximum number of PRs to return (default 10).

    Returns:
        Structured result with list of pull requests.
    """
    from urllib.parse import urlencode

    # URL-encode every filter (branch names may carry &/#) and clamp
    # per_page to GitHub's hard maximum of 100.
    query: dict[str, str] = {"state": state, "per_page": str(min(int(max_count), 100))}
    if head_branch:
        query["head"] = f"{owner}:{head_branch}"
    if base_branch:
        query["base"] = base_branch
    params = "?" + urlencode(query)

    result = await _github_api_request(
        "GET",
        f"/repos/{owner}/{repo}/pulls{params}",
    )

    if result["status"] == SUCCESS_STATUS:
        prs = result["data"]
        pr_summaries = [
            {
                "number": pr["number"],
                "title": pr["title"],
                "state": pr["state"],
                "author": pr["user"]["login"],
                "head": pr["head"]["ref"],
                "base": pr["base"]["ref"],
                "url": pr["html_url"],
                "created_at": pr["created_at"],
                "draft": pr.get("draft", False),
            }
            for pr in prs
        ]
        return {
            "status": SUCCESS_STATUS,
            "code": "prs_listed",
            "message": f"Found {len(pr_summaries)} pull request(s).",
            "pull_requests": pr_summaries,
            "total_count": len(pr_summaries),
        }
    return result


async def github_pr_status(
    tool_context: ToolContext,
    owner: str,
    repo: str,
    pr_number: int,
) -> dict[str, Any]:
    """Get the status and details of a specific pull request.

    Args:
        owner: Repository owner (username or organization).
        repo: Repository name.
        pr_number: Pull request number.

    Returns:
        Structured result with PR details, status checks, and review status.
    """
    result = await _github_api_request(
        "GET",
        f"/repos/{owner}/{repo}/pulls/{pr_number}",
    )

    if result["status"] == SUCCESS_STATUS:
        pr = result["data"]
        return {
            "status": SUCCESS_STATUS,
            "code": "pr_status_retrieved",
            "message": f"PR #{pr_number} status retrieved.",
            "pr_number": pr["number"],
            "title": pr["title"],
            "state": pr["state"],
            "merged": pr.get("merged", False),
            "mergeable": pr.get("mergeable"),
            "author": pr["user"]["login"],
            "head": pr["head"]["ref"],
            "base": pr["base"]["ref"],
            "url": pr["html_url"],
            "additions": pr.get("additions", 0),
            "deletions": pr.get("deletions", 0),
            "changed_files": pr.get("changed_files", 0),
            "created_at": pr["created_at"],
            "updated_at": pr["updated_at"],
            "draft": pr.get("draft", False),
        }
    return result


async def github_merge_pr(
    tool_context: ToolContext,
    owner: str,
    repo: str,
    pr_number: int,
    merge_method: str = "squash",
    commit_message: str | None = None,
) -> dict[str, Any]:
    """Merge a pull request on GitHub.

    Args:
        owner: Repository owner (username or organization).
        repo: Repository name.
        pr_number: Pull request number to merge.
        merge_method: Merge method  -  "merge", "squash", or "rebase".
        commit_message: Optional custom merge commit message.

    Returns:
        Structured result with merge outcome.
    """
    body: dict[str, Any] = {"merge_method": merge_method}
    if commit_message:
        body["commit_message"] = commit_message

    result = await _github_api_request(
        "PUT",
        f"/repos/{owner}/{repo}/pulls/{pr_number}/merge",
        json_body=body,
    )

    if result["status"] == SUCCESS_STATUS:
        merge_data = result["data"]
        return {
            "status": SUCCESS_STATUS,
            "code": "pr_merged",
            "message": merge_data.get("message", f"PR #{pr_number} merged."),
            "sha": merge_data.get("sha", ""),
            "merged": merge_data.get("merged", True),
        }
    return result
