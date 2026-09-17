# SDLC Manager Documentation

Technical documentation for **SDLC Manager** — the multi-agent AI platform for
governed development workflows. For the product overview and day-one usage,
start with the [main README](../README.md).

## First-Time Setup

- [Getting Started](getting-started.md) - Prerequisites, bootstrap, deploy, run
- [Environment Variables](environment-variables.md) - Complete configuration reference
- [Bootstrap](bootstrap.md) - Complete bootstrap setup for both deployment modes

## Development

- [Development](development.md) - Local workflow, Docker, testing, code quality
- [Testing Strategy](testing.md) - The unit-test guide and the 100% coverage gate
- [Code Quality](code-quality.md) - Tool usage and exclusion strategies
- [Docker Compose Workflow](docker-compose-workflow.md) - Watch mode, volumes, and configuration
- [Dockerfile Strategy](dockerfile-strategy.md) - Multi-stage builds and optimization
- [Cloud Backend Options](cloud-backend-options.md) - Advanced options for `uv run server` with cloud backends

## Deployment & Infrastructure

- [Infrastructure](infrastructure.md) - Deployment modes, CI/CD, protection strategies, IaC
- [Deployment Modes](deployment.md) - Multi-environment strategy and infrastructure
- [CI/CD Workflows](cicd.md) - Workflow architecture and mechanics
- [Protection Strategies](protection-strategies.md) - Branch, tag, environment protection
- [Cloud SQL Scaling and Reliability](cloud-sql.md) - Instance tiers, backups, HA, connection pooling, monitoring
- [Cloud Run Concurrency Tuning](cloud-run-concurrency-tuning.md) - Async runtime model and concurrency sizing
- [Image Digest Resolution](image-digest-resolution.md) - Index vs platform digests and provenance verification

## Security

- [Security Posture](security-posture.md) - Defense-in-depth rationale and architectural security decisions
- [ADK Origin Check Middleware](adk-origin-check-middleware.md) - Origin validation, CORS interaction, and `ALLOW_ORIGINS` configuration

## Operations

- [Observability](observability.md) - OpenTelemetry traces and logs
- [OpenTelemetry Architecture](opentelemetry-architecture.md) - ADK coexistence, instrumentation strategy, dependency management
- [Troubleshooting](troubleshooting.md) - Common issues and solutions
