#!/usr/bin/env bash
# SDLC Manager - one-command developer setup (macOS / Linux)
#
# Run this FROM YOUR REPOSITORY FOLDER:
#   curl -fsSL __SERVICE_BASE_URL__/setup.sh | bash
#
# Verifies prerequisites (uv, Google Cloud CLI), signs you in to Google Cloud
# if needed, installs the SDLC Manager helper from the private registry, and
# starts it. Contains no secrets and downloads nothing from third-party
# sites: the registry token comes from YOUR gcloud login at run time, and
# prerequisites are checked, never auto-installed.

set -euo pipefail

REGISTRY_HOST="__PYTHON_REGISTRY__"
case "${REGISTRY_HOST}" in
  ""|__PYTHON_*)
    echo "This deployment has no package registry configured (SDLC_PYTHON_REGISTRY)."
    echo "Ask your administrator to set it on the service, then run this command again."
    exit 1
    ;;
esac

echo ""
echo "=== SDLC Manager developer setup ==="

# --- [1/4] uv (prerequisite - not auto-installed) ---------------------------
if ! command -v uv >/dev/null 2>&1; then
  echo "[1/4] uv is required but not installed."
  echo "      Install it with:  pip install uv"
  echo "      Then reopen your terminal and run this command again."
  exit 1
fi
echo "[1/4] uv found."

# --- [2/4] Google Cloud CLI + sign-in ---------------------------------------
if ! command -v gcloud >/dev/null 2>&1; then
  echo "[2/4] The Google Cloud CLI is required (it authorizes the package download)."
  echo "      Install it from: https://cloud.google.com/sdk/docs/install"
  echo "      Then reopen your terminal and run this command again."
  exit 1
fi
ACCT="$(gcloud config get-value account 2>/dev/null || true)"
if [ -z "${ACCT}" ] || [ "${ACCT}" = "(unset)" ]; then
  echo "[2/4] Signing in to Google Cloud (a browser window will open)..."
  gcloud auth login
else
  echo "[2/4] Google Cloud signed in as ${ACCT}."
fi
if ! gcloud auth application-default print-access-token >/dev/null 2>&1; then
  echo "      Setting up application credentials (one more browser window)..."
  gcloud auth application-default login
fi

# --- [3/4] Install the helper package ---------------------------------------
echo "[3/4] Installing the SDLC Manager helper..."
# Org policy expires gcloud sessions periodically; a lapsed session would
# surface as a confusing 401/"package not found" from the registry.
if ! ACCESS_TOKEN="$(gcloud auth print-access-token 2>/dev/null)" || [ -z "${ACCESS_TOKEN}" ]; then
  echo ""
  echo "Your Google Cloud sign-in has expired."
  echo "Run:  gcloud auth login"
  echo "Then run this setup command again."
  exit 1
fi
UV_EXTRA_INDEX_URL="https://oauth2accesstoken:${ACCESS_TOKEN}@${REGISTRY_HOST}" \
  uv tool install sdlc-manager --python 3.13 --force
export PATH="${HOME}/.local/bin:${PATH}"
uv tool update-shell >/dev/null 2>&1 || true

# --- [4/4] Start the helper in this folder ----------------------------------
if [ ! -d ".git" ]; then
  echo ""
  echo "Installed. This folder is not a git repository, so the helper was not started."
  echo "cd into your repository and run:  sdlc-helper"
  exit 0
fi
echo "[4/4] Starting the helper in the background..."
# No OS autostart here: --install-autostart is Windows-specific. The helper
# runs until logout; re-run 'sdlc-helper' after a reboot.
nohup sdlc-helper >/dev/null 2>&1 &
sleep 2
echo "      Copy the PAIRING TOKEN below into the dashboard (helper pill -> Connect)."
sdlc-helper --show-token
