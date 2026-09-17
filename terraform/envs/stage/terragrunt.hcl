# SDLC Manager - stage environment
#
# One-time values come from this environment's bootstrap stack:
#   terraform -chdir=../../bootstrap/stage output
#
# Usage (from this directory):
#   terragrunt init
#   terragrunt plan  -var="docker_image=<region>-docker.pkg.dev/<project>/<repo>/sdlc-manager@sha256:<digest>"
#   terragrunt apply -var="docker_image=..."
# Omitting docker_image reuses the previously deployed image.

terraform {
  source = "../../main"

  extra_arguments "backend_bucket" {
    commands  = ["init"]
    arguments = ["-backend-config=bucket=${local.terraform_state_bucket}"]
  }
}

locals {
  terraform_state_bucket = "REPLACE_ME-terraform-state-stage" # bootstrap output: terraform_state_bucket
}

inputs = {
  environment            = "stage"
  project                = "REPLACE_ME-gcp-project-id"
  region                 = "us-central1"
  zone                   = "us-central1-a"
  google_cloud_location  = "global"
  agent_name             = "sdlc-manager"
  terraform_state_bucket = local.terraform_state_bucket

  # bootstrap output: workload_identity_pool_principal_identifier
  workload_identity_pool_principal_identifier = "REPLACE_ME-wif-principal"

  # Stage mirrors the production auth posture for realistic pre-prod testing.
  auth_enabled       = "TRUE"
  min_instance_count = 0
}
