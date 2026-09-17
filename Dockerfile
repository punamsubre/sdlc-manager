# syntax=docker/dockerfile:1

# ============================================================================
# Builder Stage: Install dependencies with optimal caching
# ============================================================================
FROM python:3.13-slim AS builder

# Install uv from official distroless image (pinned for reproducible builds)
COPY --from=ghcr.io/astral-sh/uv:0.10.11 /uv /uvx /bin/

# Set working directory
WORKDIR /app

# Environment variables for optimal uv behavior
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never

# Copy dependency files - explicit cache invalidation when either file changes
COPY pyproject.toml uv.lock README.md ./

# Installs run without cache mounts so the same file builds everywhere;
# CI-level layer caching is handled by buildx's GHA cache in cd.yml.
RUN uv sync --locked --no-install-project --no-dev

# Copy only source code (documentation changes won't invalidate this layer)
COPY src ./src

# Build argument: set to "true" for editable install (local dev with file sync)
ARG editable=false

# Install the project itself into the venv (deps were installed above)
RUN \
    if [ "$editable" = "true" ]; then \
        uv sync --locked --no-dev; \
    else \
        uv sync --locked --no-editable --no-dev; \
    fi

# ============================================================================
# Runtime Stage: Minimal production image
# ============================================================================
FROM python:3.13-slim AS runtime

# Version/traceability stamped by CI (--build-arg); defaults for local builds.
ARG APP_VERSION=dev
ARG GIT_SHA=local
LABEL org.opencontainers.image.title="sdlc-manager" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.revision="${GIT_SHA}"

# Install git for Git operations at runtime
RUN apt-get update && apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/*

# Create non-root user for security
RUN groupadd -r app && useradd -r -g app app

# Set working directory
WORKDIR /app

# Copy application and virtual environment from builder
COPY --from=builder --chown=app:app /app .

# Set environment to use virtual environment
# AGENT_DIR=/app/src: the ADK loader reads agent definitions from source;
# the .venv install is still the import path for `python -m` — both are used.
# HOST=0.0.0.0: Bind to all interfaces for container networking
# PORT=8000: Explicitly set for consistent deployment
ENV VIRTUAL_ENV=/app/.venv \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    AGENT_DIR=/app/src \
    HOST=0.0.0.0 \
    PORT=8000

# Switch to non-root user
USER app

# Expose port (configurable via PORT env var, default 8000)
EXPOSE 8000

# Local/compose health probe (Cloud Run uses its own startup probe on /health).
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.getenv(\"PORT\",\"8000\")}/health', timeout=4)" || exit 1

# Run the FastAPI server via main() for unified startup logic (logging, etc.)
CMD ["python", "-m", "sdlc_manager.server"]
