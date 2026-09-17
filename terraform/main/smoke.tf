locals {
  # Service account ID 30-char limit: truncate prefix to preserve environment suffix
  sa_suffix_smoke = "-smoke-${var.environment}"
  sa_prefix_smoke = substr(var.agent_name, 0, 30 - length(local.sa_suffix_smoke))
  sa_id_smoke     = "${local.sa_prefix_smoke}${local.sa_suffix_smoke}"

  smoke_locations = var.smoke_enabled ? local.locations : toset([])
}

resource "google_service_account" "smoke_invoker" {
  count        = var.smoke_enabled ? 1 : 0
  account_id   = local.sa_id_smoke
  display_name = "${local.resource_name} Smoke Invoker"
  description  = "Identity impersonated by CI to invoke the ${local.resource_name} Cloud Run service for post-deploy smoke tests"
}

resource "google_cloud_run_v2_service_iam_member" "smoke_invoker" {
  for_each = local.smoke_locations
  project  = google_cloud_run_v2_service.app[each.key].project
  location = google_cloud_run_v2_service.app[each.key].location
  name     = google_cloud_run_v2_service.app[each.key].name
  role     = "roles/run.invoker"
  member   = google_service_account.smoke_invoker[0].member
}

# OpenID (not access-token) token creator: the smoke lane mints a Cloud Run ID token by impersonation.
# Requires iam.serviceAccounts.setIamPolicy on the applying identity.
resource "google_service_account_iam_member" "smoke_id_token_creator" {
  count              = var.smoke_enabled ? 1 : 0
  service_account_id = google_service_account.smoke_invoker[0].name
  role               = "roles/iam.serviceAccountOpenIdTokenCreator"
  member             = var.workload_identity_pool_principal_identifier
}

resource "time_sleep" "smoke_id_token_creator_propagation" {
  count           = var.smoke_enabled ? 1 : 0
  create_duration = "120s"

  triggers = {
    iam_member_id = google_service_account_iam_member.smoke_id_token_creator[0].id
  }
}
