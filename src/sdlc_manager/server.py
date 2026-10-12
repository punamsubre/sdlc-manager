"""FastAPI server for SDLC Manager with Web Dashboard and ADK."""

import os
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from google.adk.cli.fast_api import get_fast_api_app

from .config import ServerEnv, initialize_environment
from .dashboard.middleware import setup_middleware
from .dashboard.routes import router as dashboard_router
from .observability import configure_otel_resource, setup_opentelemetry

env = initialize_environment(ServerEnv)

configure_otel_resource(
    agent_name=env.agent_name,
    project_id=env.google_cloud_project,
)

AGENT_DIR = os.getenv("AGENT_DIR", str(Path(__file__).resolve().parent.parent))
STATIC_DIR = Path(__file__).resolve().parent / "dashboard" / "static"

app: FastAPI = get_fast_api_app(
    agents_dir=AGENT_DIR,
    session_service_uri=env.session_service_uri,
    artifact_service_uri=env.artifact_service_uri,
    memory_service_uri=env.memory_service_uri,
    allow_origins=env.allow_origins_list,
    web=env.serve_web_interface,
    reload_agents=env.reload_agents,
)

# Apply security headers & CORS
setup_middleware(app)

_STARTUP_DONE = False


@app.on_event("startup")
async def _production_startup() -> None:
    """Runs under ANY launch mode (python -m, `uvicorn server:app`, gunicorn):
    the auth guard and telemetry must not depend on which entry point started
    the app — previously they only ran under main()."""
    global _STARTUP_DONE
    if _STARTUP_DONE:
        return
    _STARTUP_DONE = True
    from .dashboard.auth import assert_production_auth_config

    assert_production_auth_config()
    setup_opentelemetry(
        project_id=env.google_cloud_project,
        agent_name=env.agent_name,
        log_level=env.log_level,
        app=app,  # enables FastAPI HTTP instrumentation
    )

# Paths that must work without a session: the login flow, health probes, the
# public bootstrap script, and static assets. Everything else — including the
# ADK-generated agent/session API that get_fast_api_app mounted above, which
# carries no auth of its own — requires the dashboard session when auth is on.
_PUBLIC_PATHS = (
    "/", "/login", "/health", "/setup.ps1", "/setup.sh", "/static", "/api/v1/auth", "/favicon",
)

# Simple in-process token buckets. Honest limitation: per-instance state, so
# effective limits scale with instance count — acceptable at max 2 instances,
# revisit with a shared store if scaling out.
_RATE_BUCKETS: dict[str, tuple[float, float]] = {}
_AI_PATHS = ("/api/v1/validate/suggest-fix", "/api/v1/review/run",
             "/api/v1/git/generate-pr-description", "/api/v1/agent/run")


def _per_minute(limit: str, fallback: int) -> int:
    """Parse config's '60/minute' style limit strings."""
    try:
        return max(1, int(str(limit).split("/", 1)[0].strip()))
    except (ValueError, AttributeError):
        return fallback


_API_LIMIT = _per_minute(env.rate_limit_api, 60)
_AI_LIMIT = _per_minute(env.rate_limit_ai_review, 10)


def _rate_ok(key: str, per_minute: int) -> bool:
    import time

    now = time.monotonic()
    tokens, last = _RATE_BUCKETS.get(key, (float(per_minute), now))
    tokens = min(float(per_minute), tokens + (now - last) * (per_minute / 60.0))
    if tokens < 1.0:
        _RATE_BUCKETS[key] = (tokens, now)
        return False
    _RATE_BUCKETS[key] = (tokens - 1.0, now)
    return True


@app.middleware("http")
async def auth_and_rate_limit(request: Request, call_next):
    from fastapi.responses import JSONResponse

    path = request.url.path
    is_public = path == "/" or any(
        path == p or path.startswith(p + "/") or (p != "/" and path.startswith(p))
        for p in _PUBLIC_PATHS if p != "/"
    )

    if _auth_enabled() and not is_public:
        from .dashboard.auth import decode_access_token

        token = request.cookies.get("session_token") or (
            request.headers.get("authorization", "").removeprefix("Bearer ").strip()
            or None
        )
        if not token or decode_access_token(token) is None:
            return JSONResponse(
                status_code=401, content={"detail": "Not authenticated."}
            )

    if path.startswith("/api/") or path.startswith("/run"):
        client_host = request.client.host if request.client else "anon"
        who = request.cookies.get("session_token", client_host)[:64]
        if path in _AI_PATHS:
            if not _rate_ok(f"ai:{who}", _AI_LIMIT):
                return JSONResponse(
                    status_code=429,
                    content={
                        "detail": "AI request rate limit reached — try again shortly."
                    },
                )
        elif not _rate_ok(f"api:{who}", _API_LIMIT):
            return JSONResponse(
                status_code=429,
                content={"detail": "API rate limit reached — try again shortly."},
            )

    return await call_next(request)


# Include Dashboard REST and SSE APIs
app.include_router(dashboard_router)

# Mount Dashboard Static Files
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _auth_enabled() -> bool:
    return os.getenv("AUTH_ENABLED", "false").lower() in ("true", "1", "yes")


@app.get("/", include_in_schema=False, response_model=None)
async def serve_dashboard(
    request: Request,
) -> FileResponse | RedirectResponse | dict[str, str]:
    """Serve the main Dashboard SPA (sign-in required when auth is enabled)."""
    if _auth_enabled():
        from .dashboard.auth import decode_access_token

        token = request.cookies.get("session_token")
        if not token or decode_access_token(token) is None:
            return RedirectResponse("/login")
    index_file = STATIC_DIR / "index.html"
    if index_file.is_file():
        return FileResponse(str(index_file))
    return {"status": "ok", "message": "SDLC Manager API Running"}


@app.get("/login", include_in_schema=False, response_model=None)
async def serve_login() -> FileResponse | dict[str, str]:
    """Serve the Google OAuth login page."""
    login_file = STATIC_DIR / "login.html"
    if login_file.is_file():
        return FileResponse(str(login_file))
    return {"status": "ok", "message": "Login page"}


def _render_setup_script(request: Request, filename: str):
    """One-command developer bootstrap, rendered per platform. Public by
    design: the scripts contain no secrets — the package-registry token
    comes from the developer's own gcloud login at run time."""
    script = STATIC_DIR / filename
    if not script.is_file():
        return {"status": "error", "message": "Setup script not available."}
    # One image serves every environment: the script's self-reference URL
    # is rewritten to whichever host the developer actually fetched from,
    # and the package registry comes from deploy-time config.
    from fastapi.responses import PlainTextResponse

    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    body = script.read_text(encoding="utf-8")
    body = body.replace("__SERVICE_BASE_URL__", f"{proto}://{host}")
    body = body.replace(
        "__PYTHON_REGISTRY__",
        os.getenv("SDLC_PYTHON_REGISTRY", ""),
    )
    return PlainTextResponse(body)


@app.get("/setup.ps1", include_in_schema=False, response_model=None)
async def serve_setup_script(request: Request):
    """Windows onboarding: irm <base>/setup.ps1 | iex"""
    return _render_setup_script(request, "setup.ps1")


@app.get("/setup.sh", include_in_schema=False, response_model=None)
async def serve_setup_script_sh(request: Request):
    """macOS/Linux onboarding: curl -fsSL <base>/setup.sh | bash"""
    return _render_setup_script(request, "setup.sh")


@app.get("/health")
async def health() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "ok", "agent": env.agent_name}


def main() -> None:
    """Run the FastAPI server."""
    from .config import enable_utf8_output

    enable_utf8_output()

    # Auth guard + telemetry run in the app's startup hook under every launch
    # mode — main() only starts the server.
    uvicorn.run(
        app,
        host=env.host,
        port=env.port,
    )


if __name__ == "__main__":
    main()
