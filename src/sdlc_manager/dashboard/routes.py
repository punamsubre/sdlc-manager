"""FastAPI REST and SSE streaming endpoints for the SDLC Manager Dashboard."""

import asyncio
import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import BaseModel

# NOTE: the agent App is imported lazily inside the /agent/run path —
# a module-level import forced the helper (git/file REST only) to build
# all five LlmAgents at startup.
from ..tools import (
    apply_fixes,
    scan_secrets,
    terraform_plan,
    validate_file,
)
from ..tools._executor import run_git_command
from .auth import (
    GITHUB_COOKIE,
    UserSession,
    create_access_token,
    create_github_session_token,
    get_current_user,
    read_github_session,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["SDLC Manager API"])
session_service = InMemorySessionService()

_AGENT_RUNNER = None


def _agent_runner():
    global _AGENT_RUNNER
    if _AGENT_RUNNER is None:
        from ..agent import app as adk_app

        _AGENT_RUNNER = Runner(app=adk_app, session_service=session_service)
    return _AGENT_RUNNER


class GoogleAuthRequest(BaseModel):
    credential: str


class ValidateFileRequest(BaseModel):
    working_directory: str
    file_path: str
    dataset_map: str | None = None


class ApplyFixesRequest(BaseModel):
    working_directory: str
    file_path: str
    accepted_fix_ids: list[str]


class ChangedFilesRequest(BaseModel):
    working_directory: str = "."
    base_branch: str


class RunValidationRequest(BaseModel):
    working_directory: str = "."
    files: list[str]
    dataset_map: str | None = None


class GateRunRequest(BaseModel):
    working_directory: str = "."
    # Commit context: explicit working-tree files + the commit message.
    files: list[str] = []
    message: str | None = None
    # PR context: a branch pair — the server derives the changed files and
    # reads their content from the compare branch — plus the PR title.
    base_branch: str | None = None
    compare_branch: str | None = None
    title: str | None = None


class GateFixRequest(BaseModel):
    working_directory: str = "."
    file_path: str


class GateFormatRequest(BaseModel):
    working_directory: str = "."


class SuggestFixRequest(BaseModel):
    working_directory: str = "."
    file_path: str
    line: int | None = None
    message: str


class ApplySuggestionRequest(BaseModel):
    working_directory: str = "."
    file_path: str
    original_code: str
    proposed_code: str


class ReviewBrowseRequest(BaseModel):
    working_directory: str = "."
    path: str = ""


class ReviewInspectRequest(BaseModel):
    working_directory: str = "."
    file_path: str


class ReviewRunRequest(BaseModel):
    working_directory: str = "."
    file_path: str
    line_start: int = 1
    line_end: int = 0  # 0 = to end of file
    chunk_index: int = 1
    chunk_count: int = 1


class TerraformPlanRequest(BaseModel):
    working_directory: str
    var_file: str | None = None
    # The UI's optional subdirectory for the terraform root; resolved
    # under working_directory (confined like every other path input).
    terraform_dir: str | None = None


class GitCompareRequest(BaseModel):
    working_directory: str = "."
    base_branch: str
    compare_branch: str


class GitFileDiffRequest(BaseModel):
    working_directory: str = "."
    base_branch: str
    compare_branch: str
    file_path: str


_FETCH_STAMPS: dict[str, float] = {}
_FETCH_TTL_SECONDS = 30.0


async def _fetch_remote_if_stale(working_directory: str, args: list[str]) -> None:
    """Run a remote-tracking fetch at most once per TTL per repo — every
    branches/compare click was paying a full network round trip."""
    import time

    key = str(Path(working_directory).resolve()).lower()
    now = time.monotonic()
    if now - _FETCH_STAMPS.get(key, 0.0) < _FETCH_TTL_SECONDS:
        return
    _FETCH_STAMPS[key] = now
    await run_git_command(args, cwd=working_directory, quiet_fail=True)


_GENAI_CLIENT = None


def _genai_client():
    """One google-genai client per process — construction resolves credentials."""
    global _GENAI_CLIENT
    if _GENAI_CLIENT is None:
        from google.genai import Client

        api_key = os.getenv("GEMINI_API_KEY")
        http_options = types.HttpOptions(timeout=120_000)
        _GENAI_CLIENT = (
            Client(api_key=api_key, http_options=http_options)
            if api_key
            else Client(http_options=http_options)
        )
    return _GENAI_CLIENT


async def _generate_offloop(model: str, contents, config):
    """The sync SDK call in a worker thread — an LLM round trip must never
    stall every other request on the event loop."""
    return await asyncio.to_thread(
        _genai_client().models.generate_content, model=model, contents=contents, config=config
    )


def _parse_numstat(stdout: str) -> list[tuple[str, int, int]]:
    """Parse `git diff --numstat` output into (file, additions, deletions).
    Binary files report '-' and count as 0, matching all previous sites."""
    rows: list[tuple[str, int, int]] = []
    for line in (stdout or "").splitlines():
        parts = line.strip().split(maxsplit=2)
        if len(parts) == 3:
            adds = int(parts[0]) if parts[0].isdigit() else 0
            dels = int(parts[1]) if parts[1].isdigit() else 0
            rows.append((parts[2].strip(), adds, dels))
    return rows


def _parse_commit_log(stdout: str) -> list[dict[str, str]]:
    """Parse `git log --format=%h|%an|%ar|%s` lines. maxsplit=3 keeps commit
    messages that themselves contain '|' intact."""
    commits = []
    for line in (stdout or "").splitlines():
        p = line.strip().split("|", 3)
        if len(p) >= 4:
            commits.append({
                "hash": p[0].strip(),
                "author": p[1].strip(),
                "relative_time": p[2].strip(),
                "message": p[3].strip(),
            })
    return commits


def _require_branch(value: str, field: str) -> str:
    """Reject branch names git would parse as options or that carry
    control characters — argv arrays stop shell injection, not option
    injection."""
    v = (value or "").strip()
    if not v or v.startswith("-") or any(c in v for c in " \t\n\r"):
        raise HTTPException(status_code=400, detail=f"Invalid {field}: {value!r}")
    return v


def _resolve_inside(base_dir: str, rel_path: str) -> str:
    """Resolve a request-supplied relative path and refuse escapes: every file
    the API reads or writes must stay inside the served working directory."""
    base = Path(base_dir).resolve()
    # Hosted hardening (opt-in): when SDLC_WORKSPACE_ROOT is set, every
    # working_directory must live under it — a client-supplied absolute
    # path can't point the server at arbitrary host directories.
    root = os.getenv("SDLC_WORKSPACE_ROOT", "").strip()
    if root:
        root_path = Path(root).resolve()
        if base != root_path and root_path not in base.parents:
            raise HTTPException(
                status_code=400,
                detail="working_directory is outside this deployment's workspace root.",
            )
    target = (base / rel_path).resolve()
    if base != target and base not in target.parents:
        raise HTTPException(status_code=400, detail="file_path escapes the working directory.")
    return str(target)


class ApplyDiffBlocksRequest(BaseModel):
    working_directory: str = "."
    base_branch: str
    compare_branch: str
    file_path: str
    block_ids: list[int]
    commit_message: str = ""
    # Optional destination override: commit onto this branch instead of base_branch
    # (used when moving local changes onto a feature branch before raising a PR).
    commit_to_branch: str | None = None
    create_branch: bool = False
    push: bool = False
    # True when the UI gate just ran these exact checks and passed: the git
    # hooks would repeat the identical suite, so the commit/push skip them.
    gate_passed: bool = False


class GeneratePrDescriptionRequest(BaseModel):
    working_directory: str = "."
    base_branch: str
    compare_branch: str
    commits: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    diff_summary: str = ""


class CreatePrRequest(BaseModel):
    working_directory: str = "."
    base_branch: str
    compare_branch: str
    title: str
    description: str = ""
    draft: bool = False
    reviewers: list[str] = []
    # True when the UI gate just ran the pre-push suite and passed — the push
    # hooks would repeat the identical checks, so they are skipped.
    gate_passed: bool = False


class AgentChatRequest(BaseModel):
    message: str
    working_directory: str | None = None
    session_id: str | None = None


@router.get("/auth/config")
async def auth_config() -> dict[str, Any]:
    """Public sign-in configuration for the login page (no secrets here —
    a Google OAuth client ID is public by design)."""
    return {
        "auth_enabled": os.getenv("AUTH_ENABLED", "false").lower() in ("true", "1", "yes"),
        "google_client_id": (os.getenv("GOOGLE_OAUTH_CLIENT_ID") or "").strip(),
    }


@router.post("/auth/google")
async def auth_google(req: GoogleAuthRequest, request: Request, response: Response) -> dict[str, Any]:
    """Google OAuth ID token verification & JWT cookie issuance."""
    try:
        import google.auth.transport.requests
        import google.oauth2.id_token

        client_id = os.getenv("GOOGLE_OAUTH_CLIENT_ID")
        # Named distinctly — must not shadow the FastAPI `request` parameter.
        google_verifier = google.auth.transport.requests.Request()
        id_info = google.oauth2.id_token.verify_oauth2_token(
            req.credential, google_verifier, client_id
        )

        email = id_info.get("email", "")
        name = id_info.get("name", "")
        picture = id_info.get("picture", "")

        allowed_domains = os.getenv("ALLOWED_EMAIL_DOMAINS", "*")
        if allowed_domains != "*":
            domains = [d.strip() for d in allowed_domains.split(",")]
            user_domain = email.split("@")[-1] if "@" in email else ""
            if user_domain not in domains:
                raise HTTPException(
                    status_code=403, detail="Email domain not authorized."
                )

        token = create_access_token(user_email=email, name=name, picture=picture)
        is_https = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
        response.set_cookie(
            key="session_token",
            value=token,
            httponly=True,
            samesite="lax",
            secure=is_https,
            max_age=8 * 3600,
        )
        return {
            "status": "success",
            "token": token,
            "user": {"email": email, "name": name, "picture": picture},
        }
    except Exception as e:
        logger.exception("Google auth failed: %s", e)
        logger.warning("Google auth failure: %s", e)
        raise HTTPException(
            status_code=401, detail="Authentication failed. Please sign in again."
        ) from e


@router.get("/auth/me")
async def auth_me(user: UserSession = Depends(get_current_user)) -> dict[str, Any]:
    """Return active authenticated user."""
    return {"user": user.model_dump()}


@router.post("/auth/logout")
async def auth_logout(request: Request, response: Response) -> dict[str, str]:
    """Clear the session cookie AND revoke the token server-side."""
    from ..dashboard.auth import revoke_token

    token = request.cookies.get("session_token")
    if token:
        revoke_token(token)
    response.delete_cookie("session_token")
    return {"status": "logged_out"}


# --- Per-developer GitHub authorization (OAuth App web flow) ---
# Every GitHub action runs as the logged-in developer, so GitHub itself
# enforces which repositories and operations their account is allowed.

def _external_base_url(request) -> str:
    """Public base URL of this service, honoring the Cloud Run proxy headers."""
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}"


@router.get("/auth/github/login")
async def github_oauth_login(
    request: Request,
    user: UserSession = Depends(get_current_user),
):
    """Send the developer to GitHub to authorize this platform (one time)."""
    from urllib.parse import urlencode

    from fastapi.responses import RedirectResponse

    client_id = (os.getenv("GITHUB_OAUTH_CLIENT_ID") or "").strip()
    if not client_id:
        raise HTTPException(
            status_code=400,
            detail="GitHub sign-in is not configured on this deployment (GITHUB_OAUTH_CLIENT_ID is missing).",
        )
    import secrets as _secrets

    from ..dashboard.auth import create_state_token

    nonce = _secrets.token_urlsafe(24)
    state = create_state_token(user.email, nonce)
    params = urlencode({
        "client_id": client_id,
        "redirect_uri": f"{_external_base_url(request)}/api/v1/auth/github/callback",
        "scope": "repo",
        "state": state,
    })
    resp = RedirectResponse(f"https://github.com/login/oauth/authorize?{params}")
    # The nonce cookie binds the callback to THIS browser: a stolen or
    # replayed state fails without it (login-CSRF defense).
    resp.set_cookie(
        "gh_oauth_nonce", nonce, max_age=600, httponly=True, samesite="lax",
        secure=_external_base_url(request).startswith("https"),
    )
    return resp


@router.get("/auth/github/callback")
async def github_oauth_callback(request: Request, code: str = "", state: str = ""):
    """Exchange the authorization code for the developer's own GitHub token."""
    import httpx
    from fastapi.responses import RedirectResponse

    from ..dashboard.auth import verify_state_token

    nonce = request.cookies.get("gh_oauth_nonce")
    if not code or not verify_state_token(state, nonce):
        raise HTTPException(status_code=400, detail="GitHub authorization was rejected or the request expired. Try connecting again.")

    client_id = (os.getenv("GITHUB_OAUTH_CLIENT_ID") or "").strip()
    client_secret = (os.getenv("GITHUB_OAUTH_CLIENT_SECRET") or "").strip()
    if not client_id or not client_secret:
        raise HTTPException(status_code=400, detail="GitHub sign-in is not configured on this deployment.")

    async with httpx.AsyncClient(timeout=20.0) as client:
        token_res = await client.post(
            "https://github.com/login/oauth/access_token",
            headers={"Accept": "application/json"},
            data={"client_id": client_id, "client_secret": client_secret, "code": code},
        )
        token_data = token_res.json()
        gh_token = token_data.get("access_token")
        if not gh_token:
            raise HTTPException(status_code=400, detail=f"GitHub did not issue a token: {token_data.get('error_description', 'unknown error')}")

        user_res = await client.get(
            "https://api.github.com/user",
            headers={"Authorization": f"Bearer {gh_token}", "Accept": "application/vnd.github+json"},
        )
        login = (user_res.json() or {}).get("login", "")

    is_https = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
    response = RedirectResponse("/?github=connected")
    response.set_cookie(
        key=GITHUB_COOKIE,
        value=create_github_session_token(gh_token, login),
        httponly=True,
        samesite="lax",
        secure=is_https,
        max_age=30 * 86400,
    )
    return response


@router.get("/auth/github/status")
async def github_oauth_status(
    request: Request,
    user: UserSession = Depends(get_current_user),
) -> dict[str, Any]:
    """Report how the current developer is authenticated to GitHub, if at all."""
    from ..tools.github_tools import local_git_credential

    gh_token, login = read_github_session(request)
    if gh_token:
        return {"connected": True, "source": "oauth", "login": login}
    local_token, local_user = await local_git_credential()
    if local_token:
        return {"connected": True, "source": "local_git", "login": local_user}
    return {
        "connected": False,
        "source": None,
        "login": None,
        "oauth_configured": bool(os.getenv("GITHUB_OAUTH_CLIENT_ID")),
    }


@router.post("/auth/github/disconnect")
async def github_oauth_disconnect(response: Response) -> dict[str, str]:
    """Forget the developer's GitHub authorization on this browser."""
    response.delete_cookie(GITHUB_COOKIE)
    return {"status": "disconnected", "message": "GitHub disconnected. You can revoke the app under GitHub Settings → Applications."}


@router.get("/dashboard/summary")
async def dashboard_summary(
    working_directory: str = ".",
    user: UserSession = Depends(get_current_user),
) -> dict[str, Any]:
    """Return repository health metrics and an explainable quality score.

    The score always ships with its own breakdown so the UI never shows a
    number it cannot justify. Scans run on the local workspace only.
    """
    from ..tools.schema_validation import validate_syntax

    status_res = await run_git_command(["status", "--short"], cwd=working_directory)

    # Folders whose secret findings are deliberate fixtures (default: tests/).
    exclude_dirs = [d.strip() for d in os.getenv("SECRET_SCAN_EXCLUDE", "tests").split(",") if d.strip()]
    secrets_res = await scan_secrets(working_directory=working_directory, exclude_dirs=exclude_dirs)
    findings = secrets_res.get("findings", []) or []

    score = 100
    breakdown: list[dict[str, Any]] = [
        {"label": "Base score", "delta": 100, "detail": "Every workspace starts at 100."},
    ]

    if findings:
        affected = sorted({f.get("file", "?") for f in findings})
        score -= 40
        breakdown.append({
            "label": f"Hardcoded secrets: {len(findings)} finding(s) in {len(affected)} file(s)",
            "delta": -40,
            "detail": "Commits are blocked until the secrets are removed.",
        })

    # Hygiene: uncommitted changes (gentle, capped).
    changed_lines = [line for line in (status_res.get("stdout") or "").splitlines() if line.strip()]
    changed_files = [line[3:].strip() for line in changed_lines]
    hygiene_penalty = min(len(changed_files), 10)
    if hygiene_penalty:
        score -= hygiene_penalty
        breakdown.append({
            "label": f"Uncommitted changes: {len(changed_files)} file(s)",
            "delta": -hygiene_penalty,
            "detail": "-1 per uncommitted file, capped at -10. Commit or discard to recover these points.",
        })

    # Syntax: quick check of changed text files (capped so the dashboard stays fast).
    syntax_checked = 0
    async def _syntax_one(fname: str):
        fpath = _resolve_inside(working_directory, fname)
        try:
            if not os.path.isfile(fpath) or os.path.getsize(fpath) > 1_000_000:
                return None
            with open(fpath, encoding="utf-8", errors="replace") as fh:
                content = fh.read()
        except OSError:
            return None
        return fname, await validate_syntax(fname, content)

    candidates = [
        f for f in changed_files[:20]
        if f.lower().endswith((".sql", ".py", ".sh", ".ksh", ".json", ".yaml", ".yml"))
    ]
    # Up to 20 checks: concurrent, in deterministic input order.
    import asyncio as _asyncio

    outcomes = await _asyncio.gather(*(_syntax_one(f) for f in candidates))
    for outcome in outcomes:
        if outcome is None:
            continue
        fname, result = outcome
        syntax_checked += 1
        if result.get("status") == "failed" and result.get("errors"):
            score -= 10
            first = result["errors"][0]
            breakdown.append({
                "label": f"Syntax error in {fname}",
                "delta": -10,
                "detail": (first.get("message") or "")[:140],
            })

    # Secrets are a hard blocker: never score above 50 while any exist.
    if findings and score > 50:
        breakdown.append({
            "label": "Cap while secrets are present",
            "delta": 50 - score,
            "detail": "The score cannot exceed 50 until the secret scan is clean.",
        })
        score = 50
    score = max(score, 0)

    return {
        "status": "success",
        "working_directory": working_directory,
        "git": status_res,
        "secrets": secrets_res,
        "quality_score": score,
        "score_breakdown": breakdown,
        "scan_scope": {
            "workspace": os.path.abspath(working_directory),
            "scanned_files": secrets_res.get("scanned_files_count", 0),
            "excluded_dirs": exclude_dirs,
            "excluded_files": secrets_res.get("excluded_files_count", 0),
            "syntax_checked_files": syntax_checked,
        },
    }


@router.post("/validate/file")
async def api_validate_file(
    req: ValidateFileRequest,
    user: UserSession = Depends(get_current_user),
) -> dict[str, Any]:
    """Deterministic validation on a single file with proposed diffs."""
    return await validate_file(
        working_directory=req.working_directory,
        file_path=req.file_path,
        dataset_map=req.dataset_map,
    )


@router.post("/validate/apply")
async def api_apply_fixes(
    req: ApplyFixesRequest,
    user: UserSession = Depends(get_current_user),
) -> dict[str, Any]:
    """Apply accepted fix proposals to disk."""
    return await apply_fixes(
        working_directory=req.working_directory,
        file_path=req.file_path,
        accepted_fix_ids=req.accepted_fix_ids,
    )


# Extensions the validator knows how to check beyond the always-on secret scan.
_VALIDATABLE_EXTS = (".sql", ".py", ".sh", ".ksh", ".bash", ".json", ".yaml", ".yml", ".tf", ".txt", ".md")
_MAX_VALIDATE_BYTES = 1_000_000


@router.post("/validate/changed-files")
async def api_validate_changed_files(
    req: ChangedFilesRequest,
    user: UserSession = Depends(get_current_user),
) -> dict[str, Any]:
    """List files changed between the local working state and a branch."""
    base_ref = await _resolve_git_ref(req.base_branch, req.working_directory)

    status_res = await run_git_command(["diff", "--name-status", base_ref], cwd=req.working_directory)
    if status_res.get("status") != "success" or "fatal" in (status_res.get("stderr") or ""):
        raise HTTPException(
            status_code=502,
            detail=f"Could not read changes against '{req.base_branch}': {(status_res.get('stderr') or 'git diff failed')[:200]}",
        )

    numstat_res = await run_git_command(["diff", "--numstat", base_ref], cwd=req.working_directory)
    stats: dict[str, tuple[int, int]] = {
        fname: (adds, dels)
        for fname, adds, dels in _parse_numstat(numstat_res.get("stdout"))
    }

    type_map = {"A": "added", "M": "modified", "D": "deleted", "R": "modified", "C": "added", "T": "modified"}
    files: list[dict[str, Any]] = []
    seen: set[str] = set()

    for line in (status_res.get("stdout") or "").splitlines():
        parts = line.strip().split("\t")
        if len(parts) < 2:
            continue
        code = parts[0].strip()
        fname = parts[-1].strip()  # renames: use the new path
        if not fname or fname in seen:
            continue
        seen.add(fname)
        change_type = type_map.get(code[:1], "modified")
        adds, dels = stats.get(fname, (0, 0))

        validatable = change_type != "deleted"
        reason = "File was deleted — there is nothing to validate." if not validatable else None
        if validatable:
            fpath = _resolve_inside(req.working_directory, fname)
            try:
                size = os.path.getsize(fpath)
                if size > _MAX_VALIDATE_BYTES:
                    validatable = False
                    reason = f"File is too large to validate ({size // 1024} KB — limit is {_MAX_VALIDATE_BYTES // 1024} KB)."
            except OSError:
                validatable = False
                reason = "File could not be read from disk."

        files.append({
            "file": fname,
            "change_type": change_type,
            "additions": adds,
            "deletions": dels,
            "validatable": validatable,
            "skip_reason": reason,
        })

    # Untracked files count as added local changes.
    untracked_res = await run_git_command(["ls-files", "--others", "--exclude-standard"], cwd=req.working_directory)
    for fname in (untracked_res.get("stdout") or "").splitlines():
        fname = fname.strip()
        if not fname or fname in seen:
            continue
        seen.add(fname)
        validatable = True
        reason = None
        adds = 0
        try:
            fpath = _resolve_inside(req.working_directory, fname)
            size = os.path.getsize(fpath)
            if size > _MAX_VALIDATE_BYTES:
                validatable = False
                reason = f"File is too large to validate ({size // 1024} KB — limit is {_MAX_VALIDATE_BYTES // 1024} KB)."
            else:
                with open(fpath, encoding="utf-8", errors="replace") as fh:
                    adds = sum(1 for _ in fh)
        except OSError:
            validatable = False
            reason = "File could not be read from disk."
        files.append({
            "file": fname,
            "change_type": "added",
            "additions": adds,
            "deletions": 0,
            "validatable": validatable,
            "skip_reason": reason,
        })

    return {
        "status": "success",
        "base_branch": req.base_branch,
        "base_ref": base_ref,
        "files": files,
        "total": len(files),
    }


@router.post("/validate/run")
async def api_validate_run(
    req: RunValidationRequest,
    user: UserSession = Depends(get_current_user),
) -> dict[str, Any]:
    """Run layered validation (secrets -> syntax -> schema -> fix proposals) on selected files only."""
    from ..tools._linter import lint_python_dag_content, lint_sql_content
    from ..tools._schema import parse_dataset_map
    from ..tools.schema_validation import (
        schema_cache_status,
        validate_sql_schema,
        validate_syntax,
    )
    from ..tools.validation_tools import scan_content_for_secrets

    if not req.files:
        raise HTTPException(status_code=400, detail="No files selected for validation.")

    dataset_map = req.dataset_map or os.getenv("SQL_DATASET_MAP")
    results: list[dict[str, Any]] = []

    for file_path in req.files[:50]:
        entry: dict[str, Any] = {"file": file_path}
        full_path = _resolve_inside(req.working_directory, file_path)

        if not os.path.isfile(full_path):
            entry.update({"status": "error", "message": "File no longer exists on disk. Refresh the changed-files list."})
            results.append(entry)
            continue
        try:
            size = os.path.getsize(full_path)
            if size > _MAX_VALIDATE_BYTES:
                entry.update({"status": "error", "message": f"File is too large to validate ({size // 1024} KB)."})
                results.append(entry)
                continue
            with open(full_path, encoding="utf-8", errors="replace") as fh:
                content = fh.read()
        except OSError as e:
            entry.update({"status": "error", "message": f"File could not be read: {e}"})
            results.append(entry)
            continue

        # Layer 1 — secret scan (hard block, always first).
        secrets = scan_content_for_secrets(content, file_path)
        entry["secrets"] = secrets
        if secrets:
            entry["status"] = "blocked"
            entry["message"] = f"{len(secrets)} hardcoded secret(s) found. This file is blocked until they are removed."
            results.append(entry)
            continue

        # Layer 2 — syntax.
        syntax = await validate_syntax(file_path, content)
        entry["syntax"] = syntax

        # Layer 3 — schema verification (SQL only, read-only metadata, fails open).
        if file_path.lower().endswith(".sql"):
            entry["schema"] = await validate_sql_schema(content, dataset_map)
        else:
            entry["schema"] = {"status": "not_applicable", "issues": [], "unverified": [], "tables_checked": []}

        # Layer 4 — rule-based fix proposals (advisory, accept/reject).
        fn = file_path.lower()
        fixes = []
        if fn.endswith(".sql"):
            raw_map = parse_dataset_map(dataset_map)
            param_map = {real: f"${{{var}}}" for var, real in raw_map.items()} if raw_map else None
            fixes = [f.to_dict() for f in lint_sql_content(content, file_path, dataset_param_map=param_map)]
        elif fn.endswith(".py"):
            fixes = [f.to_dict() for f in lint_python_dag_content(content, file_path)]
        entry["fixes"] = fixes

        schema_status = entry["schema"]["status"]
        has_syntax_errors = syntax.get("status") == "failed"
        has_schema_errors = schema_status in ("failed", "partial")

        if has_syntax_errors or has_schema_errors:
            entry["status"] = "failed"
        elif fixes:
            entry["status"] = "review"
        elif schema_status == "unverified":
            entry["status"] = "unverified"
        else:
            entry["status"] = "clean"
        results.append(entry)

    blocking = [
        r["file"] for r in results
        if r.get("status") in ("blocked", "failed") or r.get("status") == "error"
    ]
    return {
        "status": "success",
        "results": results,
        "blocking_files": blocking,
        "schema_cache": schema_cache_status(),
    }


async def _read_exact_file_at_ref(ref: str, file_path: str, cwd: str) -> tuple[bool, str]:
    """Byte-accurate file content at a git ref. The shared git executor strips
    stdout, which would erase the exact trailing whitespace and final newline
    the hygiene check judges — so this reads the raw stream."""
    proc = await asyncio.create_subprocess_exec(
        "git", "show", f"{ref}:{file_path}",
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, _err = await proc.communicate()
    if proc.returncode != 0:
        return False, ""
    return True, out.decode("utf-8", errors="replace")


@router.post("/gate/run")
async def api_gate_run(
    req: GateRunRequest,
    user: UserSession = Depends(get_current_user),
) -> dict[str, Any]:
    """Run the pre-commit / pre-PR quality gate: hygiene, secrets, and
    Conventional Commit message/title. A repository that carries a lefthook.yml
    defines its own gates instead — those run through the bridge and the native
    checks stand down, so rules are never duplicated."""
    from ..tools.lefthook_bridge import run_repo_checks
    from ..tools.quality_gate import run_gate

    files = list(req.files)
    file_contents: dict[str, str] = {}

    # A real compare branch means the gate judges branch content, not the
    # working tree. LOCAL_WORKTREE as compare means judge the files on disk.
    compare_ref = None
    if req.compare_branch and req.compare_branch.strip() != LOCAL_WORKTREE:
        compare_ref = await _resolve_git_ref(req.compare_branch, req.working_directory)

    # PR context: derive the changed files from the branch pair — the gate
    # judges everything the PR will carry.
    if req.base_branch:
        base_ref = await _resolve_git_ref(req.base_branch, req.working_directory)
        diff_res = await run_git_command(
            ["diff", "--name-only", f"{base_ref}...{compare_ref}"] if compare_ref
            else ["diff", "--name-only", base_ref],
            cwd=req.working_directory,
        )
        for fname in (diff_res.get("stdout") or "").splitlines():
            fname = fname.strip()
            if fname and fname not in files:
                files.append(fname)

    if compare_ref:
        # One git subprocess per file — run them concurrently (bounded)
        # instead of paying the spawn latency N times in sequence.
        sem = asyncio.Semaphore(8)

        async def _read_one(fname: str):
            async with sem:
                return fname, await _read_exact_file_at_ref(
                    compare_ref, fname, req.working_directory
                )

        reads = await asyncio.gather(*(_read_one(f) for f in list(files)))
        for fname, (exists, text) in reads:
            if exists:
                file_contents[fname] = text
            else:
                files.remove(fname)  # deleted on the compare branch — nothing to gate

    repo_result = await run_repo_checks(
        working_directory=req.working_directory,
        stage="pr" if req.base_branch else "commit",
        files=files,
        message=req.message,
        title=req.title,
    )
    if repo_result is not None:
        return repo_result

    # run_gate is synchronous (hygiene + secret scans over many files);
    # off-loop so one gate run can't stall every other request.
    return await asyncio.to_thread(
        run_gate,
        working_directory=req.working_directory,
        files=files,
        file_contents=file_contents,
        message=req.message,
        title=req.title,
    )


@router.get("/gate/repo-readiness")
async def api_gate_repo_readiness(
    working_directory: str = ".",
    user: UserSession = Depends(get_current_user),
) -> dict[str, Any]:
    """Whether the served repo is lefthook-governed, and if so, whether this
    machine has the governance files and tools its checks need."""
    from ..tools.lefthook_bridge import repo_readiness

    # Sync work (PATH probes, registry, condition subprocesses) — a
    # worker thread, not the event loop.
    return await asyncio.to_thread(repo_readiness, working_directory)


@router.post("/gate/run-setup")
async def api_gate_run_setup(
    req: GateFormatRequest,
    user: UserSession = Depends(get_current_user),
) -> dict[str, Any]:
    """Run the repository's own setup script (bootstrapping PowerShell 7 first
    when Windows lacks it) — explicit accept only."""
    from ..tools.lefthook_bridge import run_repo_setup

    return await run_repo_setup(req.working_directory)


@router.post("/gate/fix-format")
async def api_gate_fix_format(
    req: GateFormatRequest,
    user: UserSession = Depends(get_current_user),
) -> dict[str, Any]:
    """Run the repository's own formatter — explicit accept only."""
    from ..tools.lefthook_bridge import run_repo_format

    return await run_repo_format(req.working_directory)


@router.post("/gate/fix-hygiene")
async def api_gate_fix_hygiene(
    req: GateFixRequest,
    user: UserSession = Depends(get_current_user),
) -> dict[str, Any]:
    """Apply the deterministic hygiene fixes to one file — explicit accept only."""
    from ..tools.quality_gate import apply_hygiene_fixes

    return apply_hygiene_fixes(req.working_directory, req.file_path)


@router.post("/validate/suggest-fix")
async def api_suggest_fix(
    req: SuggestFixRequest,
    user: UserSession = Depends(get_current_user),
) -> dict[str, Any]:
    """Draft a suggested fix for a reported issue. The suggestion is only a proposal —
    nothing is written until the developer explicitly accepts it."""
    full_path = _resolve_inside(req.working_directory, req.file_path)
    if not os.path.isfile(full_path):
        raise HTTPException(status_code=404, detail="File no longer exists on disk.")
    with open(full_path, encoding="utf-8", errors="replace") as fh:
        content = fh.read()

    nl = chr(10)
    prompt = (
        "You are a precise code repair assistant. A validation check reported this issue:" + nl +
        f"File: {req.file_path}" + nl +
        (f"Line: {req.line}" + nl if req.line else "") +
        f"Issue: {req.message}" + nl + nl +
        "File content between the markers:" + nl +
        "<<<FILE" + nl + content[:8000] + nl + "FILE>>>" + nl + nl +
        "Propose the SMALLEST change that fixes exactly this issue." + nl +
        "Rules:" + nl +
        "- 'original_code' MUST be copied character-for-character from the file (one or more complete lines)." + nl +
        "- 'proposed_code' is the replacement for that exact snippet." + nl +
        "- Do not rewrite unrelated code. Do not add commentary inside the code." + nl +
        '- Return valid JSON: {"original_code": string, "proposed_code": string, "explanation": one plain-English sentence}' + nl
    )

    try:
        model_name = os.getenv("VALIDATION_AGENT_MODEL") or os.getenv("ORCHESTRATOR_MODEL", "gemini-3.7-flash")
        response = await _generate_offloop(
            model_name,
            prompt,
            types.GenerateContentConfig(response_mime_type="application/json", temperature=0.1),
        )
        data = json.loads(response.text)
        original = data.get("original_code", "")
        proposed = data.get("proposed_code", "")
        if not original or not proposed or original == proposed:
            raise ValueError("empty or unchanged suggestion")
        if original not in content:
            # One normalization attempt: strip trailing whitespace per line.
            normalized = "\n".join(line.rstrip() for line in original.splitlines())
            if normalized in content:
                original = normalized
            else:
                raise ValueError("suggestion does not match file content")
        return {
            "status": "success",
            "fix": {
                "original_code": original,
                "proposed_code": proposed,
                "explanation": data.get("explanation", "Suggested correction for the reported issue."),
            },
        }
    except Exception as e:
        logger.warning("Fix suggestion failed for %s: %s", req.file_path, e)
        raise HTTPException(
            status_code=422,
            detail="A reliable suggestion could not be drafted for this issue. Fix it manually, then validate again.",
        ) from e


@router.post("/validate/apply-suggestion")
async def api_apply_suggestion(
    req: ApplySuggestionRequest,
    user: UserSession = Depends(get_current_user),
) -> dict[str, Any]:
    """Write one explicitly-accepted suggestion to the local file."""
    full_path = _resolve_inside(req.working_directory, req.file_path)
    if not os.path.isfile(full_path):
        raise HTTPException(status_code=404, detail="File no longer exists on disk.")
    with open(full_path, encoding="utf-8", errors="replace") as fh:
        content = fh.read()

    if req.original_code not in content:
        raise HTTPException(
            status_code=409,
            detail="The file has changed since this suggestion was drafted. Validate again to get a fresh suggestion.",
        )

    new_content = content.replace(req.original_code, req.proposed_code, 1)
    with open(full_path, "w", encoding="utf-8", newline="") as fh:
        fh.write(new_content)
    return {
        "status": "success",
        "message": f"Fix applied to '{req.file_path}'.",
    }


@router.get("/validate/schema-cache")
async def api_schema_cache_status(
    user: UserSession = Depends(get_current_user),
) -> dict[str, Any]:
    """Report schema-source availability and cache freshness."""
    from ..tools.schema_validation import schema_cache_status

    return {"status": "success", "cache": schema_cache_status()}


@router.post("/validate/schema-cache/refresh")
async def api_schema_cache_refresh(
    user: UserSession = Depends(get_current_user),
) -> dict[str, Any]:
    """Clear cached schema metadata so the next validation re-fetches it."""
    from ..tools.schema_validation import clear_schema_cache, schema_cache_status

    removed = clear_schema_cache()
    return {
        "status": "success",
        "message": f"Schema cache cleared ({removed} table(s)). Fresh metadata will be fetched on the next validation.",
        "cache": schema_cache_status(),
    }


@router.get("/git/status")
async def api_git_status(
    working_directory: str = ".",
    user: UserSession = Depends(get_current_user),
) -> dict[str, Any]:
    """Fetch git status for working directory."""
    return await run_git_command(["status", "--short"], cwd=working_directory)


@router.get("/git/diff")
async def api_git_diff(
    working_directory: str = ".",
    staged: bool = False,
    user: UserSession = Depends(get_current_user),
) -> dict[str, Any]:
    """Fetch git diff for working directory."""
    args = ["diff", "--cached"] if staged else ["diff"]
    return await run_git_command(args, cwd=working_directory)


@router.get("/git/branches")
async def api_git_branches(
    working_directory: str = ".",
    user: UserSession = Depends(get_current_user),
) -> dict[str, Any]:
    """Fetch live repository branches (local & remote) sorted by latest commit."""
    # 0. Automatically sync remote tracking branches (quiet fetch)
    try:
        await _fetch_remote_if_stale(working_directory, ["fetch", "--all", "--prune", "--quiet"])
    except Exception as e:
        logger.debug("Background git fetch failed: %s", e)

    # 1. Fetch current checked-out branch
    current_res = await run_git_command(["branch", "--show-current"], cwd=working_directory)
    current_branch = current_res.get("stdout", "").strip() or "main"

    # 2. Fetch all local and remote branches sorted by commit date
    args = [
        "for-each-ref",
        "--sort=-committerdate",
        "--format=%(refname:short)|%(committerdate:relative)|%(committerdate:iso8601)|%(subject)|%(HEAD)",
        "refs/heads",
        "refs/remotes",
    ]
    ref_res = await run_git_command(args, cwd=working_directory)
    raw_lines = (ref_res.get("stdout") or "").splitlines()

    branches = []
    seen = set()
    default_branch = "main"

    for line in raw_lines:
        line = line.strip()
        if not line:
            continue
        parts = line.split("|")
        refname = parts[0].strip() if len(parts) > 0 else ""
        rel_time = parts[1].strip() if len(parts) > 1 else ""
        iso_time = parts[2].strip() if len(parts) > 2 else ""
        subject = parts[3].strip() if len(parts) > 3 else ""
        head_marker = parts[4].strip() if len(parts) > 4 else ""

        # Skip raw 'origin' or 'origin/HEAD'
        if refname in ("origin", "origin/HEAD") or "HEAD" in refname:
            continue

        clean_name = refname.replace("origin/", "") if refname.startswith("origin/") else refname
        is_remote = refname.startswith("origin/")

        # Deduplicate local vs remote tracking branches while preserving metadata
        if clean_name in seen:
            continue
        seen.add(clean_name)

        is_cur = (clean_name == current_branch) or (head_marker == "*")

        if clean_name in ("main", "master"):
            default_branch = clean_name

        branches.append({
            "name": clean_name,
            "ref": refname,
            "is_remote": is_remote,
            "is_current": is_cur,
            "relative_time": rel_time or "recently",
            "iso_time": iso_time,
            "last_commit": subject or "Latest commit",
        })

    if not branches:
        branches.append({
            "name": current_branch or "main",
            "ref": current_branch or "main",
            "is_remote": False,
            "is_current": True,
            "relative_time": "active",
            "iso_time": "",
            "last_commit": "Initial repository branch",
        })

    return {
        "status": "success",
        "current_branch": current_branch,
        "default_branch": default_branch,
        "branches": branches,
    }


# Sentinel branch name representing the uncommitted working tree.
LOCAL_WORKTREE = "__local__"


def _read_worktree_file(cwd: str, file_path: str) -> tuple[bool, str]:
    """Return (exists, content) for a file in the working tree."""
    path = _resolve_inside(cwd, file_path)
    if not os.path.isfile(path):
        return False, ""
    with open(path, encoding="utf-8", errors="replace") as fh:
        return True, fh.read()


async def _compare_local_to_branch(base_branch: str, cwd: str) -> dict[str, Any]:
    """Compare the uncommitted working tree (tracked + untracked) against a branch."""
    base_ref = await _resolve_git_ref(base_branch, cwd)

    numstat_res = await run_git_command(["diff", "--numstat", base_ref], cwd=cwd)
    files = []
    total_adds = 0
    total_dels = 0
    for fname, adds, dels in _parse_numstat(numstat_res.get("stdout")):
        total_adds += adds
        total_dels += dels
        files.append({"file": fname, "additions": adds, "deletions": dels})

    # Untracked files count as local changes too.
    untracked_res = await run_git_command(["ls-files", "--others", "--exclude-standard"], cwd=cwd)
    for fname in (untracked_res.get("stdout") or "").splitlines():
        fname = fname.strip()
        if not fname:
            continue
        adds = 0
        try:
            path = _resolve_inside(cwd, fname)
            if os.path.getsize(path) <= 1_000_000:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    adds = sum(1 for _ in fh)
        except OSError:
            pass
        total_adds += adds
        files.append({"file": fname, "additions": adds, "deletions": 0})

    diff_res = await run_git_command(["diff", base_ref], cwd=cwd)
    diff_raw = diff_res.get("stdout") or ""
    diff_preview = diff_raw[:8000] + ("... [diff truncated]" if len(diff_raw) > 8000 else "")

    return {
        "status": "success",
        "is_identical": len(files) == 0,
        "base_branch": base_branch,
        "compare_branch": LOCAL_WORKTREE,
        "base_ref": base_ref,
        "compare_ref": LOCAL_WORKTREE,
        "commits": [],
        "files": files,
        "stats": {
            "files_changed": len(files),
            "additions": total_adds,
            "deletions": total_dels,
        },
        "diff_preview": diff_preview,
    }


async def _resolve_git_ref(ref: str, cwd: str) -> str:
    """Resolve a branch name to the freshest safe ref. Prefers the
    remote-tracking branch when it is strictly ahead of the local one
    (a teammate pushed and this clone hasn't pulled) — but never when the
    local branch carries commits of its own, which the remote would hide."""
    ref = ref.strip()
    if not ref:
        return "HEAD"
    if ref.startswith("origin/"):
        return ref

    check = await run_git_command(["rev-parse", "--verify", ref], cwd=cwd, quiet_fail=True)
    local_ok = check.get("status") == "success" and check.get("stdout") and "fatal" not in check.get("stderr", "")
    remote = f"origin/{ref}"
    check_remote = await run_git_command(["rev-parse", "--verify", remote], cwd=cwd, quiet_fail=True)
    remote_ok = check_remote.get("status") == "success" and check_remote.get("stdout") and "fatal" not in check_remote.get("stderr", "")

    if not local_ok:
        return remote if remote_ok else ref
    if remote_ok and check.get("stdout") != check_remote.get("stdout"):
        ahead = await run_git_command(
            ["merge-base", "--is-ancestor", ref, remote], cwd=cwd, quiet_fail=True
        )
        if ahead.get("status") == "success":
            return remote
    return ref


@router.post("/git/compare")
async def api_git_compare(
    req: GitCompareRequest,
    user: UserSession = Depends(get_current_user),
) -> dict[str, Any]:
    """Compare two branches and return commits, file stats, and diff summary."""
    raw_base = req.base_branch.strip()
    raw_compare = req.compare_branch.strip()

    if raw_base == raw_compare:
        return {
            "status": "success",
            "is_identical": True,
            "message": "Base and compare branches are the same — nothing to compare.",
            "commits": [],
            "files": [],
            "stats": {"files_changed": 0, "additions": 0, "deletions": 0},
            "diff_preview": "",
        }

    if raw_base == LOCAL_WORKTREE:
        raise HTTPException(status_code=400, detail="Local changes can only be the 'From' side of a comparison.")

    # Teammates' pushes and GitHub-web edits are invisible to a stale clone —
    # refresh remote-tracking refs before comparing. Offline or auth failures
    # are non-fatal: the comparison then reflects local knowledge only.
    await _fetch_remote_if_stale(req.working_directory, ["fetch", "origin", "--prune"])

    if raw_compare == LOCAL_WORKTREE:
        result = await _compare_local_to_branch(raw_base, req.working_directory)
        result["stale_branches"] = [
            lag for lag in [await _local_branch_lag(raw_base, req.working_directory)] if lag
        ]
        return result

    # Resolve refs and lag checks concurrently: each is 4+ git spawns,
    # and the four groups are independent of one another.
    import asyncio as _aio

    base_ref, compare_ref, base_lag, compare_lag = await _aio.gather(
        _resolve_git_ref(raw_base, req.working_directory),
        _resolve_git_ref(raw_compare, req.working_directory),
        _local_branch_lag(raw_base, req.working_directory),
        _local_branch_lag(raw_compare, req.working_directory),
    )
    stale_branches = [lag for lag in (base_lag, compare_lag) if lag]

    # 1. Fetch commits between base..compare (or symmetric difference)
    log_res = await run_git_command(
        ["log", f"{base_ref}..{compare_ref}", "--format=%h|%an|%ar|%s", "-n", "50"],
        cwd=req.working_directory,
    )
    commits = _parse_commit_log(log_res.get("stdout"))

    # 2. Fetch numstat for per-file additions/deletions
    numstat_res = await run_git_command(
        ["diff", "--numstat", f"{base_ref}...{compare_ref}"],
        cwd=req.working_directory,
    )
    files = []
    total_adds = 0
    total_dels = 0
    for fname, adds, dels in _parse_numstat(numstat_res.get("stdout")):
        total_adds += adds
        total_dels += dels
        files.append({"file": fname, "additions": adds, "deletions": dels})

    # 3. Fetch capped diff preview
    diff_res = await run_git_command(
        ["diff", f"{base_ref}...{compare_ref}"],
        cwd=req.working_directory,
    )
    diff_raw = diff_res.get("stdout") or ""
    diff_preview = diff_raw[:8000] + ("... [diff truncated]" if len(diff_raw) > 8000 else "")

    is_identical = (len(commits) == 0 and len(files) == 0)

    return {
        "status": "success",
        "is_identical": is_identical,
        "base_branch": raw_base,
        "compare_branch": raw_compare,
        "base_ref": base_ref,
        "compare_ref": compare_ref,
        "commits": commits,
        "files": files,
        "stats": {
            "files_changed": len(files),
            "additions": total_adds,
            "deletions": total_dels,
        },
        "diff_preview": diff_preview,
        "stale_branches": stale_branches,
    }


async def _local_branch_lag(branch: str, cwd: str) -> dict[str, Any] | None:
    """How far a local branch is behind its remote counterpart. None when it
    is up to date, absent locally, or carries unpushed commits (in which case
    a plain fast-forward would be unsafe and the developer resolves it)."""
    if not branch or branch == LOCAL_WORKTREE or branch.startswith("origin/"):
        return None
    local = await run_git_command(["rev-parse", "--verify", branch], cwd=cwd, quiet_fail=True)
    remote = await run_git_command(["rev-parse", "--verify", f"origin/{branch}"], cwd=cwd, quiet_fail=True)
    if local.get("status") != "success" or remote.get("status") != "success":
        return None
    if local.get("stdout") == remote.get("stdout"):
        return None
    ahead = await run_git_command(
        ["merge-base", "--is-ancestor", branch, f"origin/{branch}"], cwd=cwd, quiet_fail=True
    )
    if ahead.get("status") != "success":
        return None
    count = await run_git_command(
        ["rev-list", "--count", f"{branch}..origin/{branch}"], cwd=cwd, quiet_fail=True
    )
    behind_raw = (count.get("stdout") or "").strip()
    behind = int(behind_raw) if behind_raw.isdigit() else 0
    return {"branch": branch, "behind": behind} if behind else None


class GitPullBranchRequest(BaseModel):
    working_directory: str = "."
    branch: str


@router.post("/git/pull-branch")
async def api_git_pull_branch(
    req: GitPullBranchRequest,
    user: UserSession = Depends(get_current_user),
) -> dict[str, Any]:
    """Fast-forward a local branch to its remote counterpart. Never merges:
    if histories diverged, the developer resolves that themselves."""
    branch = req.branch.strip()
    if (
        not branch
        or branch == LOCAL_WORKTREE
        or branch.startswith(("origin/", "-"))
        or any(c in branch for c in " ~^:?*[\\")
    ):
        raise HTTPException(status_code=400, detail="Invalid branch name.")

    head = await run_git_command(["rev-parse", "--abbrev-ref", "HEAD"], cwd=req.working_directory)
    current = (head.get("stdout") or "").strip()
    if current == branch:
        # Checked-out branch: pull updates both the ref and the working tree.
        res = await run_git_command(["pull", "--ff-only", "origin", branch], cwd=req.working_directory)
    else:
        # Not checked out: fast-forward the ref without touching the working tree.
        res = await run_git_command(["fetch", "origin", f"{branch}:{branch}"], cwd=req.working_directory)

    if res.get("status") != "success":
        detail = (res.get("stderr") or "").strip().splitlines()
        raise HTTPException(
            status_code=409,
            detail=f"Could not fast-forward '{branch}': "
            + (detail[-1][:300] if detail else "histories diverged — resolve manually with git."),
        )

    if current == branch or not current or current == "HEAD":
        return {
            "status": "success",
            "branch": branch,
            "message": f"Local '{branch}' is now up to date with origin/{branch} — the changes are in your working folder.",
        }

    # The developer is on another branch: merge the freshened branch into it
    # so the update is actually visible in their working folder. A conflict
    # aborts cleanly — the tool never leaves a half-merged tree behind.
    merge = await run_git_command(["merge", "--no-edit", branch], cwd=req.working_directory)
    if merge.get("status") != "success":
        await run_git_command(["merge", "--abort"], cwd=req.working_directory, quiet_fail=True)
        return {
            "status": "partial",
            "branch": branch,
            "message": f"Local '{branch}' was updated, but merging it into '{current}' hit a conflict "
            f"with your work. Resolve it manually: git merge {branch}.",
        }
    return {
        "status": "success",
        "branch": branch,
        "merged_into": current,
        "message": f"Local '{branch}' updated and merged into '{current}' — the changes are now in your working folder.",
    }


@router.get("/git/recent-commits")
async def api_git_recent_commits(
    working_directory: str = ".",
    branch: str = "",
    limit: int = 8,
    user: UserSession = Depends(get_current_user),
) -> dict[str, Any]:
    """Latest commits on a branch (default: the checked-out one), for the
    dashboard history panel. Resolves to the remote-tracking ref when it is
    ahead, so teammates' pushes appear without a local pull."""
    if branch.strip():
        ref = await _resolve_git_ref(branch, working_directory)
        display = branch.strip()
    else:
        head = await run_git_command(["rev-parse", "--abbrev-ref", "HEAD"], cwd=working_directory)
        ref = display = (head.get("stdout") or "").strip() or "HEAD"
    log_res = await run_git_command(
        ["log", ref, "-n", str(max(1, min(limit, 20))), "--format=%h|%an|%ar|%s"],
        cwd=working_directory,
    )
    commits = []
    commits.extend(_parse_commit_log(log_res.get("stdout")))
    return {"status": "success", "branch": display, "commits": commits}


async def _read_file_at_ref(ref: str, file_path: str, cwd: str) -> tuple[bool, str]:
    """Return (exists, content) for a file at a given git ref."""
    res = await run_git_command(["show", f"{ref}:{file_path}"], cwd=cwd, quiet_fail=True)
    stderr = res.get("stderr", "") or ""
    if res.get("status") != "success" or "fatal" in stderr or "exists on disk, but not in" in stderr:
        return False, ""
    return True, res.get("stdout") or ""


def _build_diff_blocks(base_lines: list[str], compare_lines: list[str]) -> list[dict[str, Any]]:
    """Build aligned side-by-side change blocks via difflib opcodes (deterministic)."""
    import difflib

    matcher = difflib.SequenceMatcher(a=base_lines, b=compare_lines, autojunk=False)
    blocks = []
    for idx, (tag, i1, i2, j1, j2) in enumerate(matcher.get_opcodes()):
        blocks.append({
            "id": idx,
            "type": tag,  # equal | replace | delete | insert
            "base_start": i1 + 1,
            "base_lines": base_lines[i1:i2],
            "compare_start": j1 + 1,
            "compare_lines": compare_lines[j1:j2],
        })
    return blocks


@router.post("/git/file-diff")
async def api_git_file_diff(
    req: GitFileDiffRequest,
    user: UserSession = Depends(get_current_user),
) -> dict[str, Any]:
    """Return a structured side-by-side diff of one file between two branches (or the working tree)."""
    if req.base_branch.strip() == LOCAL_WORKTREE:
        raise HTTPException(status_code=400, detail="Local changes can only be the 'From' side of a comparison.")

    base_ref = await _resolve_git_ref(req.base_branch, req.working_directory)
    base_exists, base_text = await _read_file_at_ref(base_ref, req.file_path, req.working_directory)

    if req.compare_branch.strip() == LOCAL_WORKTREE:
        compare_exists, compare_text = _read_worktree_file(req.working_directory, req.file_path)
    else:
        compare_ref = await _resolve_git_ref(req.compare_branch, req.working_directory)
        compare_exists, compare_text = await _read_file_at_ref(compare_ref, req.file_path, req.working_directory)

    if not base_exists and not compare_exists:
        raise HTTPException(status_code=404, detail=f"File '{req.file_path}' not found on either branch.")

    base_lines = base_text.splitlines() if base_exists else []
    compare_lines = compare_text.splitlines() if compare_exists else []
    blocks = _build_diff_blocks(base_lines, compare_lines)
    change_count = sum(1 for b in blocks if b["type"] != "equal")

    return {
        "status": "success",
        "file_path": req.file_path,
        "base_branch": req.base_branch,
        "compare_branch": req.compare_branch,
        "base_exists": base_exists,
        "compare_exists": compare_exists,
        "blocks": blocks,
        "change_count": change_count,
    }


@router.post("/git/apply-diff-blocks")
async def api_apply_diff_blocks(
    req: ApplyDiffBlocksRequest,
    user: UserSession = Depends(get_current_user),
) -> dict[str, Any]:
    """Apply selected change blocks from the compare branch onto the base branch as a commit."""
    import shutil
    import tempfile

    if not req.block_ids:
        raise HTTPException(status_code=400, detail="No change blocks selected.")
    if req.base_branch.strip() == LOCAL_WORKTREE:
        raise HTTPException(status_code=400, detail="Local changes can only be the 'From' side of a merge.")

    base_ref = await _resolve_git_ref(req.base_branch, req.working_directory)
    base_exists, base_text = await _read_file_at_ref(base_ref, req.file_path, req.working_directory)

    if req.compare_branch.strip() == LOCAL_WORKTREE:
        compare_exists, compare_text = _read_worktree_file(req.working_directory, req.file_path)
    else:
        compare_ref = await _resolve_git_ref(req.compare_branch, req.working_directory)
        compare_exists, compare_text = await _read_file_at_ref(compare_ref, req.file_path, req.working_directory)
    if not base_exists and not compare_exists:
        raise HTTPException(status_code=404, detail=f"File '{req.file_path}' not found on either branch.")

    base_lines = base_text.splitlines() if base_exists else []
    compare_lines = compare_text.splitlines() if compare_exists else []
    blocks = _build_diff_blocks(base_lines, compare_lines)

    selected = set(req.block_ids)
    merged_lines: list[str] = []
    applied = 0
    for block in blocks:
        if block["type"] == "equal":
            merged_lines.extend(block["base_lines"])
        elif block["id"] in selected:
            merged_lines.extend(block["compare_lines"])
            applied += 1
        else:
            merged_lines.extend(block["base_lines"])

    merged_content = "\n".join(merged_lines)
    if merged_content and not merged_content.endswith("\n"):
        merged_content += "\n"

    # Destination branch: base branch by default, or an explicit (possibly new) feature branch.
    target_branch = _require_branch(
        req.commit_to_branch or req.base_branch, "destination branch"
    )
    if target_branch == LOCAL_WORKTREE:
        raise HTTPException(status_code=400, detail="Destination must be a real branch.")

    source_label = "local changes" if req.compare_branch.strip() == LOCAL_WORKTREE else req.compare_branch
    message = req.commit_message.strip() or (
        f"merge({req.file_path}): apply {applied} change block(s) from {source_label} into {target_branch}"
    )

    if req.create_branch:
        check = await run_git_command(["rev-parse", "--verify", f"refs/heads/{target_branch}"], cwd=req.working_directory, quiet_fail=True)
        if check.get("status") == "success" and (check.get("stdout") or "").strip() and "fatal" not in (check.get("stderr") or ""):
            raise HTTPException(status_code=409, detail=f"Branch '{target_branch}' already exists — pick another name or choose 'existing branch'.")

    # Determine whether the destination branch is the currently checked-out branch.
    current_res = await run_git_command(["branch", "--show-current"], cwd=req.working_directory)
    current_branch = (current_res.get("stdout") or "").strip()

    if not req.create_branch and current_branch == target_branch:
        # Guard against clobbering uncommitted local edits to this file.
        dirty = await run_git_command(["status", "--porcelain", "--", req.file_path], cwd=req.working_directory)
        if (dirty.get("stdout") or "").strip():
            raise HTTPException(
                status_code=409,
                detail=(
                    f"'{req.file_path}' has uncommitted local edits and '{target_branch}' is the branch currently "
                    "checked out. Pick a different destination branch, or commit/stash the file first."
                ),
            )
        target_file = _resolve_inside(req.working_directory, req.file_path)
        os.makedirs(os.path.dirname(target_file) or ".", exist_ok=True)
        with open(target_file, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(merged_content)
        await run_git_command(["add", req.file_path], cwd=req.working_directory)
        commit_args = ["commit", "-m", message] + (["--no-verify"] if req.gate_passed else [])
        commit_res = await run_git_command(commit_args, cwd=req.working_directory, timeout=300)
    else:
        # Commit onto the destination branch without disturbing the current checkout via a temp worktree.
        wt_path = os.path.join(tempfile.gettempdir(), f"sdlc-merge-{uuid.uuid4().hex[:8]}")
        if req.create_branch:
            # New feature branch cut from the base (To) branch.
            add_res = await run_git_command(
                ["worktree", "add", wt_path, "-b", target_branch, base_ref],
                cwd=req.working_directory,
            )
        else:
            add_res = await run_git_command(["worktree", "add", wt_path, target_branch], cwd=req.working_directory)
            if add_res.get("status") != "success" or "fatal" in (add_res.get("stderr") or ""):
                # Destination may only exist as a remote tracking branch — create a local branch from it.
                add_res = await run_git_command(
                    ["worktree", "add", wt_path, "-b", target_branch, f"origin/{target_branch}"],
                    cwd=req.working_directory,
                )
        if add_res.get("status") != "success" or "fatal" in (add_res.get("stderr") or ""):
            raise HTTPException(status_code=500, detail=f"Could not prepare merge worktree: {add_res.get('stderr')}")

        try:
            target_file = _resolve_inside(wt_path, req.file_path)
            os.makedirs(os.path.dirname(target_file) or wt_path, exist_ok=True)
            with open(target_file, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(merged_content)
            await run_git_command(["add", req.file_path], cwd=wt_path)
            commit_args = ["commit", "-m", message] + (["--no-verify"] if req.gate_passed else [])
            commit_res = await run_git_command(commit_args, cwd=wt_path, timeout=300)
        finally:
            await run_git_command(["worktree", "remove", "--force", wt_path], cwd=req.working_directory)
            await run_git_command(["worktree", "prune"], cwd=req.working_directory)
            shutil.rmtree(wt_path, ignore_errors=True)

    stderr = commit_res.get("stderr") or ""
    stdout = commit_res.get("stdout") or ""
    if commit_res.get("status") != "success" or "fatal" in stderr:
        raise HTTPException(status_code=500, detail=f"Commit failed: {stderr or stdout}")
    if "nothing to commit" in stdout or "nothing to commit" in stderr:
        return {
            "status": "success",
            "code": "no_changes",
            "message": f"Selected blocks produced no content change on '{target_branch}'.",
            "applied_blocks": 0,
            "branch": target_branch,
        }

    # Optionally push the destination branch so it is PR-ready on GitHub.
    push_note = ""
    if req.push:
        push_args = ["push", "-u", "origin", target_branch] + (["--no-verify"] if req.gate_passed else [])
        push_res = await run_git_command(push_args, cwd=req.working_directory, timeout=900)
        push_err = (push_res.get("stderr") or "").strip()
        if push_res.get("status") == "success" and "error" not in push_err.lower() and "fatal" not in push_err.lower():
            push_note = " Branch pushed to origin."
        else:
            push_note = f" (Push failed: {push_err[:200]})"

    hash_res = await run_git_command(["rev-parse", "--short", target_branch], cwd=req.working_directory)
    return {
        "status": "success",
        "code": "blocks_applied",
        "message": f"Applied {applied} change block(s) to '{target_branch}'.{push_note}",
        "applied_blocks": applied,
        "branch": target_branch,
        "commit": (hash_res.get("stdout") or "").strip(),
        "commit_message": message,
    }


@router.post("/git/generate-pr-description")
async def api_generate_pr_description(
    req: GeneratePrDescriptionRequest,
    user: UserSession = Depends(get_current_user),
) -> dict[str, Any]:
    """Generate AI PR title and structured description grounded strictly in diff and commit data."""
    nl = chr(10)
    commit_lines = [f"- {c.get('message', '')} (by {c.get('author', '')})" for c in req.commits[:15]]
    file_lines = [f"- {f.get('file', '')} (+{f.get('additions', 0)} / -{f.get('deletions', 0)})" for f in req.files[:20]]
    commit_texts = nl.join(commit_lines)
    file_texts = nl.join(file_lines)

    prompt = (
        "You are an enterprise software release assistant." + nl +
        f"Generate a concise, professional Pull Request title and structured Markdown description for merging '{req.compare_branch}' into '{req.base_branch}'." + nl + nl +
        "Strict Rules:" + nl +
        "- Ground your description strictly in the provided commits and changed files." + nl +
        "- Do NOT invent features, bugs, or fixes not evidenced in the changes." + nl +
        "- Tone: concise, professional, enterprise-grade." + nl +
        "- Format title as a Conventional Commit (e.g., feat(scope): description or fix(scope): description)." + nl +
        "- Format the description with EXACTLY these markdown sections, in this order:" + nl +
        "  ## What — a factual summary of what changed (files, features, behavior)." + nl +
        "  ## Why — the motivation or problem being solved, inferred only from commit messages and diff evidence." + nl +
        "  ## How — how the change is implemented (approach, key code paths, notable decisions)." + nl + nl +
        "Commits:" + nl + (commit_texts or "No commit messages provided.") + nl + nl +
        "Files Modified:" + nl + (file_texts or "No files listed.") + nl + nl +
        "Diff Excerpt:" + nl + (req.diff_summary[:2500] if req.diff_summary else "No diff summary provided.") + nl + nl +
        'Return a valid JSON object with keys:' + nl +
        '"title": string (the PR title),' + nl +
        '"description": string (the markdown PR body)' + nl
    )

    try:
        model_name = os.getenv("REVIEW_AGENT_MODEL", "gemini-3.1-pro-preview")
        response = await _generate_offloop(
            model_name,
            prompt,
            types.GenerateContentConfig(response_mime_type="application/json", temperature=0.2),
        )
        data = json.loads(response.text)
        return {
            "status": "success",
            "title": data.get("title", f"Merge {req.compare_branch} into {req.base_branch}"),
            "description": data.get("description", "Automated Pull Request draft."),
        }
    except Exception as e:
        logger.warning("LLM PR generation fallback: %s", e)
        primary_msg = req.commits[0]["message"] if req.commits else f"Changes from {req.compare_branch}"
        title = primary_msg.capitalize() if not primary_msg.startswith(("feat", "fix", "chore", "refactor")) else primary_msg
        description = (
            "## What" + nl +
            f"Merges `{req.compare_branch}` into `{req.base_branch}` — {len(req.commits)} commit(s) across {len(req.files)} file(s)." + nl +
            (file_texts or "- Various repository updates.") + nl + nl +
            "## Why" + nl +
            (commit_texts or "- " + primary_msg) + nl + nl +
            "## How" + nl +
            "Changes were staged block-by-block in the Merge Studio and verified against automated quality guardrails prior to PR submission." + nl
        )
        return {
            "status": "success",
            "title": title,
            "description": description,
        }


def _parse_github_remote(remote_url: str) -> tuple[str, str] | None:
    """Extract (owner, repo) from an https or ssh GitHub remote URL."""
    import re

    url = remote_url.strip()
    match = re.search(r"github\.com[:/]([^/]+)/([^/\s]+?)(?:\.git)?/?$", url)
    if not match:
        return None
    return match.group(1), match.group(2)


@router.post("/git/create-pr")
async def api_create_pr(
    req: CreatePrRequest,
    request: Request,
    user: UserSession = Depends(get_current_user),
) -> dict[str, Any]:
    """Create a real pull request on GitHub as the logged-in developer."""
    from ..tools.github_tools import github_create_pr, github_request_reviewers

    _require_branch(req.base_branch, "base branch")
    _require_branch(req.compare_branch, "compare branch")
    if LOCAL_WORKTREE in (req.compare_branch.strip(), req.base_branch.strip()):
        raise HTTPException(
            status_code=400,
            detail="A pull request needs a real branch. Move your local changes to a branch first, then create the PR from it.",
        )

    # The developer's own GitHub identity: OAuth cookie (hosted) or, when this
    # runs on their machine (local server / helper), their stored git credentials.
    gh_token, gh_login = read_github_session(request)

    # Resolve owner/repo: env override first, otherwise auto-detect from origin remote.
    owner = os.getenv("GITHUB_REPO_OWNER") or os.getenv("GITHUB_OWNER", "")
    repo = os.getenv("GITHUB_REPO_NAME") or os.getenv("GITHUB_REPO", "")
    if not owner or not repo:
        remote_res = await run_git_command(["remote", "get-url", "origin"], cwd=req.working_directory)
        parsed = _parse_github_remote(remote_res.get("stdout") or "")
        if parsed:
            owner, repo = parsed
    if not owner or not repo:
        raise HTTPException(
            status_code=400,
            detail=(
                "Could not determine the GitHub repository. Set GITHUB_REPO_OWNER and "
                "GITHUB_REPO_NAME, or add a GitHub 'origin' remote to this workspace."
            ),
        )

    # Push both branches so GitHub sees the state you reviewed locally.
    push_warnings = []
    for branch in {req.compare_branch, req.base_branch}:
        exists = await run_git_command(["rev-parse", "--verify", f"refs/heads/{branch}"], cwd=req.working_directory, quiet_fail=True)
        if exists.get("status") != "success" or "fatal" in (exists.get("stderr") or ""):
            continue  # remote-only branch, nothing local to push
        push_args = ["push", "origin", branch] + (["--no-verify"] if req.gate_passed else [])
        push_res = await run_git_command(push_args, cwd=req.working_directory, timeout=900)
        if push_res.get("status") != "success" or "error" in (push_res.get("stderr") or "").lower().replace("error:", "error"):
            stderr = (push_res.get("stderr") or "").strip()
            if stderr and "Everything up-to-date" not in stderr and "->" not in stderr:
                push_warnings.append(f"Could not push '{branch}': {stderr[:200]}")

    res = await github_create_pr(
        tool_context=None,
        owner=owner,
        repo=repo,
        title=req.title,
        head_branch=req.compare_branch,
        base_branch=req.base_branch,
        body=req.description,
        draft=req.draft,
        token=gh_token,
    )

    if res.get("status") != "success":
        detail = res.get("message", "GitHub rejected the pull request.")
        if push_warnings:
            detail += " | " + " | ".join(push_warnings)
        raise HTTPException(status_code=502, detail=detail)

    # Request reviewers (best effort — PR already exists at this point).
    reviewers = [r.strip().lstrip("@") for r in req.reviewers if r.strip()]
    reviewer_note = ""
    # GitHub rejects the whole reviewer request if it includes the PR author.
    if gh_login and any(r.lower() == gh_login.lower() for r in reviewers):
        reviewers = [r for r in reviewers if r.lower() != gh_login.lower()]
        reviewer_note = " (Skipped you as reviewer — GitHub does not allow the PR author to review their own PR.)"
    if reviewers:
        rev_res = await github_request_reviewers(
            tool_context=None,
            owner=owner,
            repo=repo,
            pr_number=res["pr_number"],
            reviewers=reviewers,
            token=gh_token,
        )
        if rev_res.get("status") == "success":
            reviewer_note = f" Review requested from {', '.join(reviewers)}."
        else:
            reviewer_note = f" (Could not add reviewers: {rev_res.get('message', 'unknown error')})"

    res["message"] = (res.get("message", "") + reviewer_note).strip()
    if push_warnings:
        res["push_warnings"] = push_warnings
    res["repository"] = f"{owner}/{repo}"
    return res


@router.post("/terraform/plan")
async def api_terraform_plan(
    req: TerraformPlanRequest,
    user: UserSession = Depends(get_current_user),
) -> dict[str, Any]:
    """Run terraform plan."""
    # The UI's terraform_dir is a subfolder of the workspace; resolve it with
    # the same confinement as every other client-supplied path.
    tf_root = req.working_directory
    if req.terraform_dir:
        tf_root = _resolve_inside(req.working_directory, req.terraform_dir)
    return await terraform_plan(
        working_directory=tf_root,
        var_file=req.var_file,
    )


# --- Architecture Review: whole-file, advisory-only recommendations ---------
# This screen never writes to disk. Large files are reviewed in sections
# (driven by the client using the chunk plan from /review/inspect) so the
# COMPLETE file is always analyzed — never silently truncated.

_REVIEW_TYPES = {
    ".py": "Python", ".sql": "SQL", ".js": "JavaScript", ".ts": "TypeScript",
    ".tsx": "TypeScript (React)", ".jsx": "JavaScript (React)", ".sh": "Shell",
    ".ksh": "Shell (ksh)", ".bash": "Shell", ".tf": "Terraform", ".yaml": "YAML",
    ".yml": "YAML", ".json": "JSON", ".java": "Java", ".go": "Go", ".cs": "C#",
    ".cpp": "C++", ".c": "C", ".h": "C/C++ header", ".rb": "Ruby", ".php": "PHP",
    ".html": "HTML", ".css": "CSS", ".scss": "SCSS", ".md": "Markdown",
}
_REVIEW_MAX_BYTES = 1_000_000
_REVIEW_CHUNK_LINES = 500

# The review runs as MULTIPLE mandatory passes per section. Each pass owns a
# fixed set of categories and must return a verdict for every one of them —
# so an empty category is a real conclusion, never skipped attention. The
# checklists encode bug classes that fail silently (wrong results, no error),
# which matter more than anything that fails loudly.
_REVIEW_PASSES = [
    {
        "categories": ["Correctness & Logic", "Security & Safety"],
        "checklist": (
            "Correctness & Logic — check EVERY item:\n"
            "- LEFT/RIGHT JOIN whose semantics are changed by WHERE filters on the outer table's columns "
            "(an equality/inequality filter silently converts it to an INNER JOIN; an IS NULL anti-join mixed "
            "with other outer joins deserves scrutiny of whether NULL-extended rows are handled as intended)\n"
            "- CASE or IF expressions with no ELSE/default branch (silent NULLs for unmatched rows)\n"
            "- NOT IN against a subquery that may contain NULLs (silently returns no rows)\n"
            "- Division without a zero guard\n"
            "- Implicit type coercion in comparisons or join keys (unquoted literals, string-vs-date/number compares)\n"
            "- Aggregations over columns that may contain NULLs without explicit handling\n"
            "- GROUP BY using ordinal positions (breaks silently if columns are reordered)\n"
            "- Filters applied after aggregation that should be applied before it\n"
            "Security & Safety — check EVERY item:\n"
            "- Hardcoded credentials, API keys, or tokens in the code\n"
            "- Exposure of sensitive/PII columns without evident need or masking\n"
            "- Injection-prone string building or unsafe input handling\n"
            "- Risky patterns that widen access or leak data"
        ),
    },
    {
        "categories": ["Performance & Efficiency", "Structure & Maintainability"],
        "checklist": (
            "Performance & Efficiency — check EVERY item:\n"
            "- SELECT * where explicit columns are appropriate\n"
            "- The same expression computed repeatedly instead of once\n"
            "- The same table scanned multiple times where one scan (or a CTE) would do\n"
            "- Missing partition/cluster filters on large-table access\n"
            "- Implicit or cross joins (comma-separated FROM lists)\n"
            "- Correlated or deeply nested subqueries that could be joins or CTEs\n"
            "- Unnecessary DISTINCT or sorting\n"
            "Structure & Maintainability — check EVERY item:\n"
            "- Hardcoded environment/dataset/schema names with no parameterization\n"
            "- Duplicated query blocks that should be shared (CTE/view)\n"
            "- Magic numbers embedded in calculations (rates, thresholds, dates)\n"
            "- Deep subquery nesting where CTEs would clarify"
        ),
    },
    {
        "categories": ["Standards & Consistency", "Documentation & Readability"],
        "checklist": (
            "Standards & Consistency — check EVERY item:\n"
            "- Inconsistent naming (mixed case styles, mixed conventions across objects/columns)\n"
            "- Non-idiomatic or inconsistent formatting for the language\n"
            "- Implicit shorthand that violates the language's conventions\n"
            "Documentation & Readability — check EVERY item:\n"
            "- No file-level or block-level comments at all\n"
            "- Undocumented business logic: magic numbers or non-obvious calculations with no explanation\n"
            "- Names so unclear the implementation must be read to understand intent"
        ),
    },
]

_REVIEW_CATEGORIES = [
    "Correctness & Logic",
    "Performance & Efficiency",
    "Structure & Maintainability",
    "Standards & Consistency",
    "Security & Safety",
    "Documentation & Readability",
]

_REVIEW_SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", "dist", ".claude", ".agents"}


def _sql_outer_join_where_findings(all_lines: list[str], line_start: int, line_end: int) -> list[dict[str, Any]]:
    """Deterministic backstop for the highest-stakes silent-failure class in SQL:
    a WHERE clause filtering on columns of a LEFT/RIGHT-joined table. Equality
    filters silently convert the outer join to an INNER JOIN; IS NULL anti-joins
    must be confirmed intentional. Model attention may miss this; this check cannot.
    """
    import re

    section = all_lines[line_start - 1 : line_end]
    # alias -> line where the outer join is declared
    outer_aliases: dict[str, int] = {}
    join_re = re.compile(r"\b(LEFT|RIGHT|FULL)\s+(OUTER\s+)?JOIN\s+([\w.`\[\]]+)\s+(?:AS\s+)?(\w+)", re.IGNORECASE)
    in_where = False
    findings: list[dict[str, Any]] = []

    for offset, raw in enumerate(section):
        line_no = line_start + offset
        line = raw.strip()
        for m in join_re.finditer(raw):
            outer_aliases[m.group(4).lower()] = line_no
        upper = line.upper()
        if upper.startswith("WHERE") or " WHERE " in f" {upper} ":
            in_where = True
        if re.match(r"^(GROUP\s+BY|ORDER\s+BY|HAVING|LIMIT|UNION|\)|;)", upper):
            in_where = False
        if in_where and outer_aliases:
            for alias, join_line in outer_aliases.items():
                for m in re.finditer(rf"\b{re.escape(alias)}\.(\w+)", raw, re.IGNORECASE):
                    col = m.group(1)
                    is_null_test = bool(re.search(rf"\b{re.escape(alias)}\.{col}\s+IS\s+(NOT\s+)?NULL", raw, re.IGNORECASE))
                    findings.append({
                        "category": "Correctness & Logic",
                        "priority": "High",
                        "line_start": line_no,
                        "line_end": line_no,
                        "summary": f"WHERE clause filters on outer-joined table '{alias}' — outer-join semantics at risk",
                        "why": (
                            f"'{alias}' is joined with an outer join (line {join_line}), but the WHERE clause references "
                            f"{alias}.{col}. " + (
                                "An IS NULL / IS NOT NULL test here is an anti-join pattern: it silently drops or keeps "
                                "NULL-extended rows and produces wrong results if the join key itself can be NULL — confirm this is intentional."
                                if is_null_test else
                                "A comparison filter here silently discards the NULL-extended rows, converting the outer join "
                                "into INNER JOIN behavior — results will be wrong without any error."
                            )
                        ),
                        "recommendation": (
                            f"If rows without a '{alias}' match must be kept, move this condition into the JOIN's ON clause "
                            f"(e.g. ON ... AND {alias}.{col} = ...). If this is a deliberate anti-join, state it with a comment "
                            "and consider NOT EXISTS, which handles NULLs explicitly."
                        ),
                    })
    # One finding per alias is enough — keep the first occurrence for each.
    deduped: dict[str, dict[str, Any]] = {}
    for f in findings:
        alias_key = f["summary"]
        if alias_key not in deduped:
            deduped[alias_key] = f
    return list(deduped.values())


def _review_resolve(working_directory: str, rel_path: str) -> str:
    """Resolve a path inside the workspace — delegates to the single shared
    guard so there is exactly one confinement semantics in this module."""
    return _resolve_inside(working_directory, rel_path or ".")


@router.post("/review/browse")
async def api_review_browse(
    req: ReviewBrowseRequest,
    user: UserSession = Depends(get_current_user),
) -> dict[str, Any]:
    """List one folder of the workspace for the file picker."""
    target = _review_resolve(req.working_directory, req.path)
    if not os.path.isdir(target):
        raise HTTPException(status_code=404, detail="Folder not found.")

    root = os.path.abspath(req.working_directory)
    dirs: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    try:
        for name in sorted(os.listdir(target), key=str.lower):
            full = os.path.join(target, name)
            rel = os.path.relpath(full, root).replace("\\", "/")
            if os.path.isdir(full):
                if name in _REVIEW_SKIP_DIRS or name.startswith("."):
                    continue
                dirs.append({"name": name, "path": rel})
            else:
                ext = os.path.splitext(name)[1].lower()
                if ext in _REVIEW_TYPES:
                    files.append({
                        "name": name,
                        "path": rel,
                        "type": _REVIEW_TYPES[ext],
                        "size": os.path.getsize(full),
                    })
    except OSError as e:
        raise HTTPException(status_code=400, detail=f"Folder could not be read: {e}") from e

    parent = os.path.relpath(os.path.dirname(target), root).replace("\\", "/") if target != root else None
    return {
        "status": "success",
        "path": req.path or ".",
        "parent": None if parent in (None, ".") and (req.path in ("", ".")) else (parent if parent != "." else ""),
        "dirs": dirs,
        "files": files,
    }


@router.post("/review/inspect")
async def api_review_inspect(
    req: ReviewInspectRequest,
    user: UserSession = Depends(get_current_user),
) -> dict[str, Any]:
    """Validate a file before review and return its chunk plan."""
    target = _review_resolve(req.working_directory, req.file_path)

    if not os.path.exists(target):
        raise HTTPException(status_code=404, detail=f"File not found: {req.file_path}")
    if os.path.isdir(target):
        raise HTTPException(status_code=400, detail="That path is a folder. This screen reviews one file at a time.")

    ext = os.path.splitext(target)[1].lower()
    if ext not in _REVIEW_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"'{ext or 'no extension'}' files are not supported for review. Supported: {', '.join(sorted(set(_REVIEW_TYPES.values())))}.",
        )
    size = os.path.getsize(target)
    if size > _REVIEW_MAX_BYTES:
        raise HTTPException(status_code=400, detail=f"File is too large to review ({size // 1024} KB — limit is {_REVIEW_MAX_BYTES // 1024} KB).")
    try:
        with open(target, encoding="utf-8", errors="replace") as fh:
            line_count = sum(1 for _ in fh)
    except OSError as e:
        raise HTTPException(status_code=400, detail=f"File could not be read: {e}") from e

    chunks = []
    start = 1
    while start <= max(line_count, 1):
        end = min(start + _REVIEW_CHUNK_LINES - 1, line_count)
        chunks.append({"line_start": start, "line_end": end})
        start = end + 1

    return {
        "status": "success",
        "file_path": req.file_path,
        "type": _REVIEW_TYPES[ext],
        "size_bytes": size,
        "line_count": line_count,
        "chunks": chunks,
        "chunked": len(chunks) > 1,
    }


@router.post("/review/run")
async def api_review_run(
    req: ReviewRunRequest,
    user: UserSession = Depends(get_current_user),
) -> dict[str, Any]:
    """Review one section of a file (the whole file when it fits in one section).

    Advisory only: returns grounded recommendations, never diffs, never writes.
    """
    target = _review_resolve(req.working_directory, req.file_path)
    if not os.path.isfile(target):
        raise HTTPException(status_code=404, detail="File no longer exists on disk.")
    with open(target, encoding="utf-8", errors="replace") as fh:
        all_lines = fh.read().splitlines()

    line_start = max(req.line_start, 1)
    line_end = req.line_end if req.line_end and req.line_end <= len(all_lines) else len(all_lines)
    section = all_lines[line_start - 1 : line_end]
    numbered = "\n".join(f"{line_start + i:5d}| {line}" for i, line in enumerate(section))

    ext = os.path.splitext(target)[1].lower()
    language = _REVIEW_TYPES.get(ext, "code")
    nl = chr(10)

    section_note = ""
    if req.chunk_count > 1:
        header_context = "\n".join(f"{i + 1:5d}| {line}" for i, line in enumerate(all_lines[:30]))
        section_note = (
            f"This is section {req.chunk_index} of {req.chunk_count} of a large file "
            f"({len(all_lines)} lines total). For context, the file begins with:{nl}{header_context}{nl}{nl}"
            "Only report findings for lines INSIDE the section below." + nl
        )

    base_rules = (
        "Rules:" + nl +
        "- Every finding MUST reference specific line numbers within the shown range and be tied to this exact code. "
        "No generic advice that could apply to any file." + nl +
        "- Cover the ENTIRE range shown — issues near the end of the code matter exactly as much as issues at the top." + nl +
        "- Priority criteria: High = incorrect behavior, data/security risk, or major cost if ignored; "
        "Medium = meaningful degradation of performance or maintainability, moderate effort to address; "
        "Low = polish, consistency, readability." + nl +
        "- Silent failures outrank loud ones: code that produces WRONG RESULTS without erroring is always High." + nl +
        "- 'why' must state the concrete consequence, not restate the rule." + nl +
        "- 'recommendation' is prose or a short illustrative snippet, never a patch." + nl +
        "- Be concise: 'why' at most 2 sentences; 'recommendation' at most 4 sentences or a 5-line snippet." + nl
    )

    def _pass_prompt(pass_spec: dict[str, Any], include_summary: bool) -> str:
        cats = pass_spec["categories"]
        return (
            f"You are a senior engineer reviewing one {language} file for a regulated financial enterprise. "
            "Produce prioritized, grounded recommendations. This is ADVISORY guidance the developer applies "
            "manually — do NOT produce patches or diffs." + nl + nl +
            f"File: {req.file_path}" + nl + section_note + nl +
            f"Code (line-numbered, lines {line_start}-{line_end}):" + nl + numbered + nl + nl +
            "You are responsible ONLY for these categories, and you MUST evaluate EVERY one of them "
            "independently and completely: " + ", ".join(cats) + "." + nl + nl +
            "Work through this checklist item by item against the code — do not stop at the first findings:" + nl +
            pass_spec["checklist"] + nl + nl + base_rules + nl +
            'Return valid JSON: {' +
            ('"summary": {"purpose": string (what this file does, 1-2 sentences), "condition": string (its general state, 1 sentence)}, ' if include_summary else '') +
            '"categories": [one entry PER category listed above: {"category": string, '
            '"conclusion": "issues_found" or "clean" (clean means you checked every checklist item and found nothing), '
            '"findings": [{"priority": "High"|"Medium"|"Low", "line_start": int, "line_end": int, '
            '"summary": string, "why": string, "recommendation": string}]}]}'
        )

    client = _genai_client()
    models = [
        os.getenv("REVIEW_AGENT_MODEL", "gemini-3.1-pro-preview"),
        os.getenv("AI_FALLBACK_MODEL", "gemini-3.7-flash"),
    ]

    def _run_pass_sync(pass_spec: dict[str, Any], include_summary: bool) -> dict[str, Any]:
        last_error: Exception | None = None
        for model_name in models:
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=_pass_prompt(pass_spec, include_summary),
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        temperature=0.2,
                        max_output_tokens=16384,
                    ),
                )
                return json.loads(response.text)
            except Exception as e:
                last_error = e
                logger.warning("Review pass %s via %s failed for %s: %s",
                               pass_spec["categories"][0], model_name, req.file_path, e)
        raise RuntimeError(str(last_error)[:180])

    import asyncio as _asyncio

    try:
        pass_results = await _asyncio.gather(
            *[_asyncio.to_thread(_run_pass_sync, spec, idx == 0) for idx, spec in enumerate(_REVIEW_PASSES)]
        )
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"The review could not be completed: {str(e)[:180]}. Try again.",
        ) from e

    # Merge passes; enforce grounding (valid line refs) and category ownership;
    # dedupe near-identical findings across passes.
    findings: list[dict[str, Any]] = []
    seen: set[tuple[str, int, str]] = set()
    summary: dict[str, Any] = {}
    for idx, data in enumerate(pass_results):
        if idx == 0:
            summary = data.get("summary") or {}
        allowed = set(_REVIEW_PASSES[idx]["categories"])
        for cat_entry in data.get("categories", []) or []:
            category = cat_entry.get("category")
            if category not in allowed:
                continue
            for f in cat_entry.get("findings", []) or []:
                try:
                    fs, fe = int(f.get("line_start", 0)), int(f.get("line_end", 0))
                except (TypeError, ValueError):
                    continue
                if fs < line_start or fs > line_end:
                    continue
                fe = min(max(fe, fs), line_end)
                priority = str(f.get("priority", "")).capitalize()
                if priority not in ("High", "Medium", "Low"):
                    priority = "Medium"
                if not f.get("summary") or not f.get("recommendation"):
                    continue
                key = (category, fs, str(f["summary"])[:40].lower())
                if key in seen:
                    continue
                seen.add(key)
                findings.append({
                    "category": category,
                    "priority": priority,
                    "line_start": fs,
                    "line_end": fe,
                    "summary": str(f["summary"])[:200],
                    "why": str(f.get("why", ""))[:600],
                    "recommendation": str(f["recommendation"])[:1500],
                })

    # Deterministic backstop for the must-detect outer-join/WHERE class (SQL only):
    # merged in unless the model already reported the same lines in the same category.
    if req.file_path.lower().endswith(".sql"):
        reported_correctness_lines = {
            f["line_start"] for f in findings if f["category"] == "Correctness & Logic"
        }
        for bf in _sql_outer_join_where_findings(all_lines, line_start, line_end):
            if bf["line_start"] not in reported_correctness_lines:
                findings.append(bf)

    # Coverage signal: on a long section, findings clustered only in the opening
    # lines indicate the later portion was not genuinely analyzed.
    section_len = line_end - line_start + 1
    coverage_warning = False
    if findings and section_len > 200:
        last_ref = max(f["line_start"] for f in findings)
        coverage_warning = last_ref < line_start + int(section_len * 0.35)

    return {
        "status": "success",
        "file_path": req.file_path,
        "chunk_index": req.chunk_index,
        "chunk_count": req.chunk_count,
        "summary": summary,
        "findings": findings,
        "coverage_warning": coverage_warning,
    }


@router.get("/dashboard/audit")
async def dashboard_audit(
    limit: int = 50,
    user: UserSession = Depends(get_current_user),
) -> dict[str, Any]:
    """Return platform audit trail of recent agent actions."""
    return {
        "status": "success",
        "events": [],
        "message": "Audit trail ready. Events populate during agent sessions.",
        "limit": limit,
    }


@router.post("/agent/run")
async def api_agent_run(
    req: AgentChatRequest,
    request: Request,
    user: UserSession = Depends(get_current_user),
) -> StreamingResponse:
    """Stream ADK Orchestrator & Sub-Agent turns via Server-Sent Events (SSE)."""
    # Run the App, not the bare agent: the App carries the plugins
    # (global instruction with the workspace rule, logging). The Runner
    # is cached — one per process.
    runner = _agent_runner()

    user_id = user.email
    session_id = req.session_id or f"web-session-{uuid.uuid4().hex[:8]}"

    from ..agent import app as adk_app

    session = await session_service.get_session(
        app_name=adk_app.name, user_id=user_id, session_id=session_id
    )
    if not session:
        session = await session_service.create_session(
            app_name=adk_app.name, user_id=user_id, session_id=session_id
        )

    content = types.Content(
        role="user",
        parts=[types.Part.from_text(text=req.message)],
    )

    async def event_generator():
        try:
            import time as _time

            last_beat = _time.monotonic()
            async for turn in runner.run_async(
                user_id=user_id,
                session_id=session_id,
                new_message=content,
            ):
                # Stop consuming the runner when the browser is gone —
                # otherwise the agent keeps burning tokens for nobody.
                if await request.is_disconnected():
                    logger.info("SSE client disconnected — stopping agent turn")
                    break
                if _time.monotonic() - last_beat > 15:
                    last_beat = _time.monotonic()
                    yield ": keepalive\n\n"
                # ADK events carry text and tool calls inside content.parts.
                if getattr(turn, "content", None):
                    for part in turn.content.parts:
                        if getattr(part, "text", None):
                            yield f"data: {json.dumps({'type': 'token', 'text': part.text})}\n\n"
                        fc = getattr(part, "function_call", None)
                        if fc:
                            # Values are truncated: a tool arg can carry a whole
                            # diff, and the chip only needs to identify the call.
                            args = {
                                k: (v if len(str(v)) <= 80 else str(v)[:77] + "...")
                                for k, v in (fc.args or {}).items()
                            }
                            yield (
                                "data: "
                                + json.dumps(
                                    {"type": "tool_call", "name": fc.name, "args": args}
                                )
                                + "\n\n"
                            )
        except Exception as e:
            logger.exception("Agent turn failed: %s", e)
            yield (
                "data: "
                + json.dumps({
                    'type': 'error',
                    'message': 'The agent run failed — see the server logs for details.',
                })
                + "\n\n"
            )

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
