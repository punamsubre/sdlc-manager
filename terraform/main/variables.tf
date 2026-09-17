variable "environment" {
  description = "Deployment environment (dev, stage, prod)"
  type        = string
  validation {
    condition     = contains(["dev", "stage", "prod"], var.environment)
    error_message = "Must be dev, stage, or prod"
  }
}

variable "project" {
  description = "Google Cloud project ID"
  type        = string
}

variable "region" {
  description = "Google Cloud Compute region"
  type        = string
}

variable "zone" {
  description = "Google Cloud Compute zone"
  type        = string
  validation {
    condition     = startswith(var.zone, var.region)
    error_message = "Zone must be within region (e.g., region=us-central1, zone=us-central1-a)"
  }
}

variable "google_cloud_location" {
  description = "Vertex AI model endpoint location"
  type        = string
}

variable "agent_name" {
  description = "Agent name to identify cloud resources and logs"
  type        = string
}

variable "terraform_state_bucket" {
  description = "Terraform state GCS bucket name"
  type        = string
}

variable "workload_identity_pool_principal_identifier" {
  description = "WIF principal identifier for additional IAM role bindings"
  type        = string
}

variable "docker_image" {
  description = "Docker image URI to deploy (defaults to previous deployment)"
  type        = string
  nullable    = true
  default     = null
}

# Optional app runtime environment variables
variable "log_level" {
  description = "Agent app logging verbosity"
  type        = string
  nullable    = true
  default     = null
}

variable "otel_instrumentation_genai_capture_message_content" {
  description = "Capture LLM message content in OpenTelemetry traces (TRUE/FALSE)"
  type        = string
  nullable    = true
  default     = null
}

variable "serve_web_interface" {
  description = "Enable web UI"
  type        = string
  nullable    = true
  default     = null
}

variable "adk_suppress_experimental_feature_warnings" {
  description = "Suppress ADK experimental feature warnings"
  type        = string
  nullable    = true
  default     = null
}

# ---- Dashboard authentication & identity -------------------------------------

variable "auth_enabled" {
  description = "Require Google sign-in on the dashboard (TRUE/FALSE). Production must be TRUE."
  type        = string
  nullable    = true
  default     = null
}

variable "allowed_email_domains" {
  description = "Comma-separated email domains allowed to sign in (e.g. 'example.com')"
  type        = string
  nullable    = true
  default     = null
}

variable "google_oauth_client_id" {
  description = "Google OAuth client ID for dashboard sign-in (public by design)"
  type        = string
  nullable    = true
  default     = null
}

variable "github_oauth_client_id" {
  description = "GitHub OAuth app client ID for per-developer GitHub connect"
  type        = string
  nullable    = true
  default     = null
}

# Secret Manager secret NAMES (not values) - the service reads them at runtime.
variable "jwt_secret_name" {
  description = "Secret Manager secret holding JWT_SECRET_KEY (required when auth_enabled=TRUE)"
  type        = string
  nullable    = true
  default     = null
}

variable "google_oauth_client_secret_name" {
  description = "Secret Manager secret holding the Google OAuth client secret"
  type        = string
  nullable    = true
  default     = null
}

variable "github_oauth_client_secret_name" {
  description = "Secret Manager secret holding the GitHub OAuth client secret"
  type        = string
  nullable    = true
  default     = null
}

# ---- Distribution & scaling ---------------------------------------------------

variable "sdlc_python_registry" {
  description = "Artifact Registry python repo serving the CLI/helper package (host/project/repo/simple/ path)"
  type        = string
  nullable    = true
  default     = null
}

variable "min_instance_count" {
  description = "Minimum warm Cloud Run instances (set 1+ in production to avoid cold starts)"
  type        = number
  default     = 0
}

variable "ingress" {
  description = "Cloud Run ingress. Org policy run.allowedIngress may forbid INGRESS_TRAFFIC_ALL - use INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER behind a load balancer."
  type        = string
  default     = "INGRESS_TRAFFIC_ALL"
  validation {
    condition = contains([
      "INGRESS_TRAFFIC_ALL",
      "INGRESS_TRAFFIC_INTERNAL_ONLY",
      "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER",
    ], var.ingress)
    error_message = "Must be a valid Cloud Run ingress setting"
  }
}

variable "app_service_account_email" {
  description = "Run the service as this pre-provisioned service account instead of creating one. For projects where the applying identity cannot modify project IAM - the given account must already hold the roles in app_iam_roles."
  type        = string
  nullable    = true
  default     = null
}

variable "database_enabled" {
  description = "Provision the private Cloud SQL session store (VPC peering, instance, bastion). Requires servicenetworking.services.addPeering; disable where that permission or private services access is unavailable - sessions are then in-memory."
  type        = bool
  default     = true
}

variable "smoke_enabled" {
  description = "Provision the CI smoke-test identity (SA + impersonation binding). Requires the applying identity to hold iam.serviceAccounts.setIamPolicy; disable in environments without CI-driven smoke tests."
  type        = bool
  default     = true
}

variable "public_access" {
  description = "Grant run.invoker to allUsers so browsers can reach the dashboard (the app enforces its own Google sign-in). FALSE when fronted by IAP/LB auth."
  type        = bool
  default     = true
}

variable "extra_env" {
  description = "Additional plain env vars (model overrides, rate limits, SQL_DATASET_MAP, ...)"
  type        = map(string)
  default     = {}
}
