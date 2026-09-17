# SDLC Manager - one-command developer setup (Windows PowerShell)
#
# Run this FROM YOUR REPOSITORY FOLDER:
#   powershell -c "irm __SERVICE_BASE_URL__/setup.ps1 | iex"
#
# Verifies prerequisites (uv, Google Cloud CLI), signs you in to Google Cloud
# if needed, installs the SDLC Manager helper from the private registry, and
# starts it with Windows autostart. Contains no secrets and downloads nothing
# from third-party sites: the registry token comes from YOUR gcloud login at
# run time, and prerequisites are checked, never auto-installed.

$ErrorActionPreference = "Stop"
$RegistryHost = "__PYTHON_REGISTRY__"
if (-not $RegistryHost -or $RegistryHost -like "__PYTHON_*") {
    Write-Host "This deployment has no package registry configured (SDLC_PYTHON_REGISTRY)." -ForegroundColor Yellow
    Write-Host "Ask your administrator to set it on the service, then run this command again."
    exit 1
}

Write-Host ""
Write-Host "=== SDLC Manager developer setup ===" -ForegroundColor Cyan

# --- [1/4] uv (prerequisite - not auto-installed) ---------------------------
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "[1/4] uv is required but not installed." -ForegroundColor Yellow
    Write-Host "      Install it with:  pip install uv"
    Write-Host "      Then reopen PowerShell and run this command again."
    exit 1
} else {
    Write-Host "[1/4] uv found."
}

# --- [2/4] Google Cloud CLI + sign-in ---------------------------------------
if (-not (Get-Command gcloud -ErrorAction SilentlyContinue)) {
    Write-Host "[2/4] The Google Cloud CLI is required (it authorizes the package download)." -ForegroundColor Yellow
    Write-Host "      Opening the installer page - install it, reopen PowerShell, and run this command again."
    Start-Process "https://cloud.google.com/sdk/docs/install"
    exit 1
}
$acct = (& gcloud config get-value account 2>$null)
if (-not $acct -or $acct -eq "(unset)") {
    Write-Host "[2/4] Signing in to Google Cloud (a browser window will open)..."
    gcloud auth login
} else {
    Write-Host "[2/4] Google Cloud signed in as $acct."
}
$null = & gcloud auth application-default print-access-token 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "      Setting up application credentials (one more browser window)..."
    gcloud auth application-default login
}

# --- [3/4] Install the helper package ---------------------------------------
Write-Host "[3/4] Installing the SDLC Manager helper..."
# Org policy expires gcloud sessions periodically; a lapsed session would
# surface as a confusing 401/"package not found" from the registry.
$AccessToken = (& gcloud auth print-access-token 2>$null)
if ($LASTEXITCODE -ne 0 -or -not $AccessToken) {
    Write-Host ""
    Write-Host "Your Google Cloud sign-in has expired." -ForegroundColor Yellow
    Write-Host "Run:  gcloud auth login" -ForegroundColor Yellow
    Write-Host "Then run this setup command again."
    exit 1
}
$env:UV_EXTRA_INDEX_URL = "https://oauth2accesstoken:$AccessToken@$RegistryHost"
uv tool install sdlc-manager --python 3.13 --force
$env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
try { uv tool update-shell | Out-Null } catch { }

# --- [4/4] Start the helper in this folder ----------------------------------
if (-not (Test-Path ".git")) {
    Write-Host ""
    Write-Host "Installed. This folder is not a git repository, so the helper was not started." -ForegroundColor Yellow
    Write-Host "cd into your repository and run:  uv run helper --install-autostart"
    exit 0
}
Write-Host "[4/4] Starting the helper with Windows autostart..."
Write-Host "      Copy the PAIRING TOKEN below into the dashboard (helper pill -> Connect)." -ForegroundColor Cyan
sdlc-helper --install-autostart
