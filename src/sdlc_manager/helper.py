
"""SDLC Manager Local Helper — the developer-machine agent for the hosted dashboard.

The hosted (Cloud Run) dashboard cannot reach a developer's local files: browsers
and servers are sandboxed away from the client filesystem. This helper closes that
gap. It runs on the developer's machine, bound strictly to 127.0.0.1, and exposes
the same /api/v1 surface as the main server so the dashboard page (served from the
cloud) can perform git and file operations against the LOCAL repository.

Security model:
  - Binds to 127.0.0.1 only — never reachable from the network.
  - Every request must carry the pairing token (X-Helper-Token header). The token
    is generated once on first run and stored in ~/.sdlc-helper.json.
  - The developer pastes the token into the dashboard once ("Connect local helper");
    the dashboard stores it in the browser and sends it automatically afterwards.

One-time setup on a developer machine (from the project folder):
    uv run helper                     # prints the pairing token, serves on :8765
    uv run helper --install-autostart # also start automatically with Windows

Recurring experience: none. Open the dashboard URL, log in — it just works.
"""

import argparse
import json
import os
import secrets
import sys
from pathlib import Path
from typing import Any

HELPER_VERSION = "1.0.0"
DEFAULT_PORT = 8765
CONFIG_PATH = Path.home() / ".sdlc-helper.json"


def _load_config() -> dict[str, Any]:
    if CONFIG_PATH.is_file():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return {}


def _save_config(cfg: dict[str, Any]) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def _get_or_create_token(reset: bool = False) -> str:
    cfg = _load_config()
    if reset or not cfg.get("token"):
        cfg["token"] = secrets.token_urlsafe(24)
        _save_config(cfg)
    return cfg["token"]


def create_app(token: str, workspace: str):
    """Build the helper FastAPI app: same API router, token auth, permissive CORS."""
    # The helper trusts the local developer — session auth is replaced by the
    # pairing token, enforced by middleware below.
    os.environ["AUTH_ENABLED"] = "false"
    # A GCP project is optional for helper duties: schema checks fail open.
    # When none is configured, resolve the developer's own gcloud/ADC default —
    # an empty GOOGLE_CLOUD_PROJECT would be taken literally by google.auth
    # and turn every BigQuery URL into projects//... 404s.
    if not (os.getenv("GOOGLE_CLOUD_PROJECT") or "").strip():
        try:
            import google.auth

            _, _adc_project = google.auth.default()
        except Exception:
            _adc_project = None
        os.environ["GOOGLE_CLOUD_PROJECT"] = (_adc_project or "").strip()
    # The genai client reads the raw environment at call time: without the
    # Vertex flag it demands a GEMINI_API_KEY and every AI feature served
    # from the helper (PR descriptions, reviews) falls back to templates.
    os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "TRUE")
    os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "global")

    from fastapi import FastAPI, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse

    from .dashboard.routes import router

    app = FastAPI(title="SDLC Manager Local Helper", version=HELPER_VERSION)

    @app.middleware("http")
    async def require_pairing_token(request: Request, call_next):
        # Preflight and health checks pass without the token; everything else pairs.
        if request.method == "OPTIONS" or request.url.path == "/helper/health":
            return await call_next(request)
        supplied = request.headers.get("x-helper-token", "")
        if not secrets.compare_digest(supplied, token):
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing or invalid helper pairing token."},
            )
        return await call_next(request)

    # The dashboard page is served from the hosted origin; browsers allow it to
    # call 127.0.0.1 (loopback is exempt from mixed-content rules in Chromium).
    # allow_private_network is REQUIRED: Chrome sends a Private-Network preflight
    # for public->local requests, and starlette rejects it with 400 otherwise.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        allow_private_network=True,
    )

    @app.get("/helper/health")
    async def helper_health(request: Request) -> dict[str, Any]:
        supplied = request.headers.get("x-helper-token", "")
        paired = secrets.compare_digest(supplied, token)
        body: dict[str, Any] = {
            "status": "ok",
            "app": "sdlc-helper",
            "version": HELPER_VERSION,
            "paired": paired,
        }
        # The workspace path is the developer's filesystem layout — shown
        # only to a caller that already holds the pairing token.
        if paired:
            body["workspace"] = workspace
        return body

    app.include_router(router)
    return app


def _startup_bat_path() -> Path:
    return (
        Path(os.environ.get("APPDATA", str(Path.home())))
        / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
        / "sdlc-helper.bat"
    )


def install_autostart(port: int, workspace: str) -> Path:
    """Register the helper to launch (minimized) when Windows starts."""
    bat = _startup_bat_path()
    bat.parent.mkdir(parents=True, exist_ok=True)
    bat.write_text(
        "@echo off\n"
        f'cd /d "{workspace}"\n'
        f'start "sdlc-helper" /min "{sys.executable}" -m sdlc_manager.helper --port {port}\n',
        encoding="utf-8",
    )
    return bat


def remove_autostart() -> bool:
    bat = _startup_bat_path()
    if bat.is_file():
        bat.unlink()
        return True
    return False


def stop_running_helper(port: int) -> bool:
    """Terminate the helper process listening on the given local port (Windows)."""
    import subprocess

    try:
        out = subprocess.run(
            ["netstat", "-ano"], capture_output=True, text=True, check=True
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return False

    pids = set()
    for line in out.splitlines():
        if "LISTENING" not in line:
            continue
        cols = line.split()
        # Exact local-address match: substring matching made :8898 also match
        # :88981 and could kill an unrelated process.
        if len(cols) >= 5 and cols[1] == f"127.0.0.1:{port}":
            pids.add(cols[-1])
    stopped = False
    for pid in pids:
        result = subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)
        stopped = stopped or result.returncode == 0
    return stopped


def main() -> None:
    from .config import enable_utf8_output

    enable_utf8_output()

    parser = argparse.ArgumentParser(description="SDLC Manager Local Helper")
    parser.add_argument("--port", type=int, default=None, help=f"Port to listen on (default {DEFAULT_PORT})")
    parser.add_argument("--workspace", default=".", help="Repository folder to serve (default: current directory)")
    parser.add_argument("--show-token", action="store_true", help="Print the pairing token and exit")
    parser.add_argument("--reset-token", action="store_true", help="Generate a new pairing token")
    parser.add_argument("--install-autostart", action="store_true", help="Start the helper automatically with Windows")
    parser.add_argument("--remove-autostart", action="store_true", help="Remove the Windows autostart entry")
    parser.add_argument("--stop", action="store_true", help="Stop the running helper")
    args = parser.parse_args()

    cfg = _load_config()
    port = args.port or cfg.get("port") or DEFAULT_PORT
    workspace = str(Path(args.workspace).resolve())
    token = _get_or_create_token(reset=args.reset_token)
    cfg = _load_config()
    cfg.update({"port": port, "workspace": workspace})
    _save_config(cfg)

    if args.stop:
        print("Helper stopped." if stop_running_helper(port) else f"No helper running on port {port}.")
        return
    if args.show_token:
        print(token)
        return
    if args.remove_autostart:
        print("Autostart removed." if remove_autostart() else "No autostart entry found.")
        return
    if args.install_autostart:
        bat = install_autostart(port, workspace)
        print(f"Autostart installed: {bat}")
        print("The helper will start minimized on next login. Starting it now as well...")

    os.chdir(workspace)

    # If a helper is already running on this port, don't crash with a bind
    # error — tell the developer they're already set up.
    try:
        import urllib.request

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/helper/health", timeout=2) as res:
            existing = json.loads(res.read().decode("utf-8"))
        if existing.get("app") == "sdlc-helper":
            print()
            print("A helper is already running on this machine — you're all set.")
            print(f"  Serving : {existing.get('workspace')}")
            print(f"  Address : http://127.0.0.1:{port}")
            print(f"  Pairing token : {token}")
            print("(To restart it, stop the running one first or use --port for a second instance.)")
            return
    except Exception:
        pass  # nothing on the port — start normally

    print()
    print("=" * 62)
    print("  SDLC Manager Local Helper")
    print(f"  Serving repository : {workspace}")
    print(f"  Listening on       : http://127.0.0.1:{port}  (localhost only)")
    print()
    print("  PAIRING TOKEN (paste into the dashboard once):")
    print(f"      {token}")
    print("=" * 62)
    print()

    import uvicorn

    uvicorn.run(create_app(token, workspace), host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
