# Environment Variables Reference

Complete reference for all environment variables used by **SDLC Manager**. All variables are typed, runtime-validated by Pydantic (`ServerEnv`), and follow a **zero-hardcoding** architecture.

---

## 1. Google Cloud Core

| Variable | Description | Default | Required? | Where to Set |
|---|---|---|:---:|---|
| `GOOGLE_CLOUD_PROJECT` | GCP Project ID for Vertex AI, Cloud Trace, and Cloud Logging. |  -  | **Yes** | Local `.env`, Cloud Run |
| `GOOGLE_CLOUD_LOCATION` | Vertex AI region (e.g. `global`, `us-central1`). | `global` | No | Local `.env`, Cloud Run |
| `AGENT_NAME` | Unique service identifier for telemetry and session routing. | `sdlc-manager` | No | Local `.env`, Cloud Run |

---

## 2. Dynamic Model Selection

Switch any model at runtime without modifying code:

| Variable | Description | Default | Recommended Models |
|---|---|---|---|
| `ORCHESTRATOR_MODEL` | Model for the top-level router agent. Fast intent classification. | `gemini-2.5-flash` | `gemini-2.5-flash`, `gemini-2.0-flash` |
| `GIT_AGENT_MODEL` | Model for Git CLI, branching, and GitHub PR workflows. | `gemini-2.5-flash` | `gemini-2.5-flash`, `gemini-2.0-flash` |
| `VALIDATION_AGENT_MODEL` | Model for AST validation, secret scanning, and diff proposals. | `gemini-2.5-flash` | `gemini-2.5-flash` |
| `REVIEW_AGENT_MODEL` | Model for deep architectural code review & optimization. | `gemini-2.5-pro` | `gemini-2.5-pro`, `gemini-1.5-pro` |
| `TERRAFORM_AGENT_MODEL` | Model for Terraform CLI commands and plan analysis. | `gemini-2.5-flash` | `gemini-2.5-flash` |
| `AI_REVIEW_MODEL` | Fallback backend model for automated review generation. | `gemini-2.5-flash` | `gemini-2.5-flash` |
| `AI_FALLBACK_MODEL` | Fallback model when primary LLM encounters 429/503 errors. | `gemini-2.5-flash` | `gemini-2.5-flash` |

---

## 3. Server & Network

| Variable | Description | Default | Where to Set |
|---|---|---|---|
| `HOST` | IP address to bind uvicorn server (`127.0.0.1` local, `0.0.0.0` container). | `127.0.0.1` | Local `.env`, Dockerfile |
| `PORT` | HTTP server port. | `8000` | Local `.env`, Cloud Run |
| `LOG_LEVEL` | Logging verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`). | `INFO` | Local `.env`, Cloud Run |
| `SERVE_WEB_INTERFACE` | Whether to serve the ADK built-in developer UI. | `FALSE` | Local `.env` |
| `ALLOW_ORIGINS` | JSON array string of allowed CORS origins. | `["http://127.0.0.1:8000", "http://localhost:8000"]` | Local `.env`, Cloud Run |

---

## 4. Authentication & Security

| Variable | Description | Default | Required? |
|---|---|---|:---:|
| `AUTH_ENABLED` | Toggle Google OAuth 2.0 & JWT authentication requirement. | `FALSE` | No |
| `GOOGLE_OAUTH_CLIENT_ID` | Google OAuth 2.0 Web Client ID. | `None` | If `AUTH_ENABLED=TRUE` |
| `GOOGLE_OAUTH_CLIENT_SECRET` | Google OAuth 2.0 Web Client Secret. | `None` | If `AUTH_ENABLED=TRUE` |
| `JWT_SECRET_KEY` | Symmetric key used to sign session JWTs. | `change-this...` | In Production |
| `JWT_EXPIRY_HOURS` | Lifetime of JWT session tokens in hours. | `8` | No |
| `ALLOWED_EMAIL_DOMAINS` | Comma-separated list of authorized email domains (`*` for all). | `*` | No |

---

## 5. GitHub Integration

| Variable | Description | Default | Required? |
|---|---|---|:---:|
| `GITHUB_TOKEN` | Personal Access Token with `repo` scope for PR creation and merging. | `None` | For PR ops |
| `GIT_DEFAULT_BRANCH` | Default target branch for repository operations. | `main` | No |
| `GIT_COMMAND_TIMEOUT` | Timeout in seconds for Git subprocess commands. | `30` | No |

---

## 6. Code Quality & FinOps

| Variable | Description | Default |
|---|---|---|
| `FINOPS_MAX_BYTES_THRESHOLD` | Maximum bytes scanned by BigQuery dry-run before blocking (Default: 10 GB). | `10737418240` |
| `SQL_DATASET_MAP` | Parameterization mappings (e.g. `AEDW_DB=DB_AEDWD2,STG_DB=DB_STG2`). | `None` |
| `SCHEMA_CACHE_TTL` | In-memory schema cache expiration in seconds. | `300` |
| `TERRAFORM_COMMAND_TIMEOUT` | Timeout in seconds for Terraform plan/validate subprocesses. | `120` |

---

## 7. OpenTelemetry

| Variable | Description | Default | Required? |
|---|---|---|:---:|
| `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` | Capture full LLM message payload in OpenTelemetry trace spans. | `TRUE` | **Yes** |