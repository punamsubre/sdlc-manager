# SDLC Manager - dev environment
#
# One-time values come from this environment's bootstrap stack:
#   terraform -chdir=../../bootstrap/dev output
#
# Usage (from this directory; Terraform >= 1.14 required):
#   TG_TF_PATH=terraform terragrunt init
#   TG_TF_PATH=terraform terragrunt plan  -var="docker_image=<region>-docker.pkg.dev/<project>/<repo>/sdlc-manager@sha256:<digest>"
#   TG_TF_PATH=terraform terragrunt apply -var="docker_image=..."
# Omitting docker_image reuses the previously deployed image.

terraform {
  source = "../../main"

  extra_arguments "backend_bucket" {
    commands  = ["init"]
    arguments = ["-backend-config=bucket=${local.terraform_state_bucket}"]
  }
}

locals {
  terraform_state_bucket = "REPLACE_ME-terraform-state-dev" # bootstrap output: terraform_state_bucket
}

inputs = {
  environment            = "dev"
  project                = "REPLACE_ME-gcp-project-id"
  region                 = "us-central1"
  zone                   = "us-central1-a"
  google_cloud_location  = "global"
  agent_name             = "sdlc-manager"
  terraform_state_bucket = local.terraform_state_bucket

  # bootstrap output: workload_identity_pool_principal_identifier
  workload_identity_pool_principal_identifier = "REPLACE_ME-wif-principal"

  # Developer one-command onboarding installs from this registry.
  sdlc_python_registry = "REPLACE_ME-region-python.pkg.dev/REPLACE_ME-gcp-project-id/sdlc-python/simple/"

  # Dev: open dashboard, scale to zero when idle.
  auth_enabled       = "FALSE"
  min_instance_count = 0

  # Capability switches for restricted projects - each maps to a permission
  # the applying identity may lack:
  #   smoke_enabled=false     when iam.serviceAccounts.setIamPolicy is denied
  #   database_enabled=false  when servicenetworking.services.addPeering is
  #                           denied (sessions are then in-memory)
  smoke_enabled    = true
  database_enabled = true

  # When project-IAM writes are denied, run as a pre-provisioned service
  # account that already holds the module's app_iam_roles:
  # app_service_account_email = "REPLACE_ME-sa@REPLACE_ME-gcp-project-id.iam.gserviceaccount.com"
}
