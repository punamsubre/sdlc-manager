# Read own previous deployment (for docker_image default)
data "terraform_remote_state" "main" {
  backend = "gcs"

  config = {
    bucket = var.terraform_state_bucket
    prefix = "main"
  }
}

locals {
  # Run app service account roles
  app_iam_roles = toset([
    "roles/aiplatform.user",
    "roles/cloudsql.client",
    "roles/cloudsql.instanceUser",
    "roles/cloudtrace.agent",
    "roles/logging.logWriter",
    "roles/serviceusage.serviceUsageConsumer",
    "roles/storage.bucketViewer",
    "roles/storage.objectUser",
    "roles/telemetry.tracesWriter",
  ])

  # Prepare for future regional Cloud Run redundancy
  locations = toset([var.region])

  # Cloud Run service environment variables. Optional dashboard settings are
  # merged only when provided, so unset vars never appear as empty strings.
  run_app_env = merge(
    {
      ADK_SUPPRESS_EXPERIMENTAL_FEATURE_WARNINGS         = coalesce(var.adk_suppress_experimental_feature_warnings, "TRUE")
      AGENT_NAME                                         = var.agent_name
      ALLOW_ORIGINS                                      = jsonencode(["http://127.0.0.1:8000", "http://localhost:8000"]) # Localhost-only for gcloud proxy access (add client service origins when UI is deployed)
      ARTIFACT_SERVICE_URI                               = google_storage_bucket.artifact_service.url
      GOOGLE_CLOUD_LOCATION                              = var.google_cloud_location
      GOOGLE_CLOUD_PROJECT                               = var.project
      GOOGLE_GENAI_USE_VERTEXAI                          = "TRUE"
      LOG_LEVEL                                          = coalesce(var.log_level, "INFO")
      MEMORY_SERVICE_URI                                 = "agentengine://${google_vertex_ai_reasoning_engine.session_and_memory.id}"
      OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT = coalesce(var.otel_instrumentation_genai_capture_message_content, "FALSE")
      RELOAD_AGENTS                                      = "FALSE"
      SERVE_WEB_INTERFACE                                = coalesce(var.serve_web_interface, "FALSE")
      TELEMETRY_NAMESPACE                                = var.environment
    },
    var.database_enabled ? {
      SESSION_SERVICE_URI = "postgresql+asyncpg://${google_sql_user.app[0].name}:@localhost:5432/${google_sql_database.sessions[0].name}"
    } : {},
    { for k, v in {
      AUTH_ENABLED           = var.auth_enabled
      ALLOWED_EMAIL_DOMAINS  = var.allowed_email_domains
      GOOGLE_OAUTH_CLIENT_ID = var.google_oauth_client_id
      GITHUB_OAUTH_CLIENT_ID = var.github_oauth_client_id
      SDLC_PYTHON_REGISTRY   = var.sdlc_python_registry
    } : k => v if v != null },
    var.extra_env,
  )

  # Secret Manager-backed env vars: values never touch Terraform state or
  # the service spec - Cloud Run resolves them at instance start.
  run_app_secret_env = { for k, v in {
    JWT_SECRET_KEY             = var.jwt_secret_name
    GOOGLE_OAUTH_CLIENT_SECRET = var.google_oauth_client_secret_name
    GITHUB_OAUTH_CLIENT_SECRET = var.github_oauth_client_secret_name
  } : k => v if v != null }

  # Cloud SQL Auth Proxy args  -  shared between bastion and Cloud Run sidecar
  cloud_sql_proxy_args = var.database_enabled ? [
    google_sql_database_instance.sessions[0].connection_name,
    "--private-ip",
    "--port=5432",
    "--auto-iam-authn",
    "--structured-logs",
  ] : []

  # Either the module-created service account or the pre-provisioned one.
  app_sa_email  = var.app_service_account_email != null ? var.app_service_account_email : google_service_account.app[0].email
  app_sa_member = "serviceAccount:${local.app_sa_email}"
  app_sa_name   = "projects/${var.project}/serviceAccounts/${local.app_sa_email}"

  # Create a unique Agent resource name per deployment environment
  resource_name = "${var.agent_name}-${var.environment}"

  # Service account ID 30-char limit: truncate prefix to preserve environment suffix
  sa_suffix_app = "-${var.environment}"
  sa_prefix_app = substr(var.agent_name, 0, 30 - length(local.sa_suffix_app))
  sa_id_app     = "${local.sa_prefix_app}${local.sa_suffix_app}"

  # Create labels for billing organization
  labels = {
    application = var.agent_name
    environment = var.environment
  }

  # Recycle docker_image from previous deployment if not provided
  docker_image = coalesce(var.docker_image, try(data.terraform_remote_state.main.outputs.app_deployed_image, null))
}

resource "google_service_account" "app" {
  count        = var.app_service_account_email == null ? 1 : 0
  account_id   = local.sa_id_app
  display_name = "${local.resource_name} Service Account"
  description  = "Service account attached to the ${local.resource_name} Cloud Run service"
}

resource "google_project_iam_member" "app" {
  for_each = var.app_service_account_email == null ? local.app_iam_roles : toset([])
  project  = var.project
  role     = each.key
  member   = local.app_sa_member
}

# The app service account may read exactly the secrets wired into the service.
resource "google_secret_manager_secret_iam_member" "app" {
  for_each  = local.run_app_secret_env
  secret_id = each.value
  role      = "roles/secretmanager.secretAccessor"
  member    = local.app_sa_member
}

resource "google_vertex_ai_reasoning_engine" "session_and_memory" {
  display_name = "${local.resource_name} Sessions and Memory"
  description  = "Managed Session and Memory Bank Service for the ${local.resource_name} app"
  region       = var.region

  # Prevent plan and apply diffs with an empty spec for managed sessions and memory bank only (no runtime code)
  spec {}
}

resource "random_id" "bucket_suffix" {
  byte_length = 4
}

resource "google_storage_bucket" "artifact_service" {
  name     = "${local.resource_name}-artifact-service-${random_id.bucket_suffix.hex}"
  location = "US"

  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  versioning {
    enabled = true
  }
}

resource "google_cloud_run_v2_service" "app" {
  for_each            = local.locations
  name                = local.resource_name
  location            = each.key
  deletion_protection = false
  launch_stage        = "GA"
  ingress             = var.ingress
  labels              = local.labels

  # Service-level scaling (updates without creating new revisions)
  scaling {
    # 1+ in production: startup does real work (agent loading, service wiring),
    # so a cold start is user-visible.
    min_instance_count = var.min_instance_count
    max_instance_count = 100
  }

  template {
    service_account       = local.app_sa_email
    timeout               = "300s"
    execution_environment = "EXECUTION_ENVIRONMENT_GEN2"

    containers {
      image = local.docker_image

      ports {
        name           = "http1"
        container_port = 8000
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "2Gi"
        }
        # true = Request-based billing, false = instance-based billing
        # https://cloud.google.com/run/docs/configuring/billing-settings#setting
        cpu_idle = true
      }

      startup_probe {
        failure_threshold     = 5
        initial_delay_seconds = 20
        timeout_seconds       = 15
        period_seconds        = 20
        http_get {
          path = "/health"
          port = 8000
        }
      }

      dynamic "env" {
        for_each = local.run_app_env
        content {
          name  = env.key
          value = env.value
        }
      }

      dynamic "env" {
        for_each = local.run_app_secret_env
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = env.value
              version = "latest"
            }
          }
        }
      }
    }

    # Cloud SQL Auth Proxy sidecar (no startup probe  -  Cloud Run restarts on crash)
    dynamic "containers" {
      for_each = var.database_enabled ? [1] : []
      content {
        image = "gcr.io/cloud-sql-connectors/cloud-sql-proxy:2"
        args  = concat(local.cloud_sql_proxy_args, ["--exit-zero-on-sigterm"])
      }
    }

    # Direct VPC egress for Cloud SQL private IP connectivity
    vpc_access {
      network_interfaces {
        network    = google_compute_network.main.id
        subnetwork = google_compute_subnetwork.main.id
      }
      egress = "PRIVATE_RANGES_ONLY"
    }

    # Explicitly set the concurrency (defaults to 80 for CPU >= 1).
    max_instance_request_concurrency = 100
  }
}

# Browser-facing dashboard: without this binding a fresh deployment answers
# 403 to every user. The app performs its own Google sign-in when
# auth_enabled=TRUE; environments fronted by IAP/LB auth set public_access=false.
resource "google_cloud_run_v2_service_iam_member" "public_invoker" {
  for_each = var.public_access ? local.locations : toset([])
  name     = google_cloud_run_v2_service.app[each.key].name
  location = each.key
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# Read Cloud Run service state after resource modification completes to work around GCP API eventual
# consistency - Terraform's dependency graph ensures this data source is read after the resource is
# updated, guaranteeing outputs reflect the actual deployed revision rather than stale cached data.
data "google_cloud_run_v2_service" "app" {
  for_each = local.locations
  name     = google_cloud_run_v2_service.app[each.key].name
  location = each.key
}

# Unpack deployed Cloud Run service environment variables for Terraform output
locals {
  # Select any deployed service  -  they share env config across regions
  app_service_any = values(data.google_cloud_run_v2_service.app)[0]

  # Filter to the container using the deployed image
  app_container_any = one([for c in local.app_service_any.template[0].containers : c if c.image == local.docker_image])

  # Project the container env objects to a key, value map (secret env values display as empty strings)
  app_environment_variables = { for e in local.app_container_any.env : e.name => e.value }
}
