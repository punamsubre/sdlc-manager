# SDLC Manager - production environment
#
# One-time values come from this environment's bootstrap stack:
#   terraform -chdir=../../bootstrap/prod output
#
# Secrets are Secret Manager secret NAMES - create the secrets first:
#   printf %s "<value>" | gcloud secrets create sdlc-jwt-secret --data-file=-
#
# Usage (from this directory):
#   terragrunt init
#   terragrunt plan  -var="docker_image=<region>-docker.pkg.dev/<project>/<repo>/sdlc-manager@sha256:<digest>"
#   terragrunt apply -var="docker_image=..."

terraform {
  source = "../../main"

  extra_arguments "backend_bucket" {
    commands  = ["init"]
    arguments = ["-backend-config=bucket=${local.terraform_state_bucket}"]
  }
}

locals {
  terraform_state_bucket = "REPLACE_ME-terraform-state-prod" # bootstrap output: terraform_state_bucket
}

inputs = {
  environment            = "prod"
  project                = "REPLACE_ME-gcp-project-id"
  region                 = "us-central1"
  zone                   = "us-central1-a"
  google_cloud_location  = "global"
  agent_name             = "sdlc-manager"
  terraform_state_bucket = local.terraform_state_bucket

  # bootstrap output: workload_identity_pool_principal_identifier
  workload_identity_pool_principal_identifier = "REPLACE_ME-wif-principal"

  # Production dashboard: Google sign-in required, domain-restricted.
  auth_enabled           = "TRUE"
  allowed_email_domains  = "REPLACE_ME-yourcompany.com"
  google_oauth_client_id = "REPLACE_ME.apps.googleusercontent.com"
  github_oauth_client_id = "REPLACE_ME-github-oauth-app-id"

  # Secret Manager secret names (values are read at runtime, never stored in state).
  jwt_secret_name                 = "sdlc-jwt-secret"
  google_oauth_client_secret_name = "sdlc-google-oauth-secret"
  github_oauth_client_secret_name = "sdlc-github-oauth-secret"

  # Developer one-command onboarding installs from this registry.
  sdlc_python_registry = "us-central1-python.pkg.dev/REPLACE_ME-gcp-project-id/sdlc-python/simple/"

  # Startup does real work (agent loading, service wiring) - keep one instance warm.
  min_instance_count = 1

  # Optional model/behavior overrides:
  # extra_env = {
  #   ORCHESTRATOR_MODEL = "gemini-3.7-flash"
  #   REVIEW_AGENT_MODEL = "gemini-3.1-pro-preview"
  #   SQL_DATASET_MAP    = "AEDW_DB=DB_AEDWD2"
  # }
}
