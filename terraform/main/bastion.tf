locals {
  bastion_iam_roles = toset([
    "roles/cloudsql.client",
    "roles/logging.logWriter",
    "roles/monitoring.metricWriter",
  ])

  # Service account ID 30-char limit: truncate prefix to preserve environment suffix
  sa_suffix_bastion = "-bastion-${var.environment}"
  sa_prefix_bastion = substr(var.agent_name, 0, 30 - length(local.sa_suffix_bastion))
  sa_id_bastion     = "${local.sa_prefix_bastion}${local.sa_suffix_bastion}"

  # Conditional so the disabled branch never dereferences count-gated resources.
  bastion_user_data = var.database_enabled ? templatefile(
    "${path.module}/templates/bastion-cloud-init.yaml",
    {
      proxy_args          = join(" ", local.cloud_sql_proxy_args)
      app_service_account = local.app_sa_email
      app_sa_db_user      = google_sql_user.app[0].name
    }
  ) : ""
}

resource "google_service_account" "bastion" {
  count        = var.database_enabled ? 1 : 0
  account_id   = local.sa_id_bastion
  display_name = "${local.resource_name} Bastion Service Account"
  description  = "Service account attached to the ${local.resource_name} Cloud SQL Auth Proxy bastion"
}

resource "google_project_iam_member" "bastion" {
  for_each = var.database_enabled ? local.bastion_iam_roles : toset([])
  project  = var.project
  role     = each.key
  member   = google_service_account.bastion[0].member
}

# Bastion proxy impersonates the app SA for IAM database auth (--impersonate-service-account)
resource "google_service_account_iam_member" "bastion_impersonate_app" {
  count              = var.database_enabled ? 1 : 0
  service_account_id = local.app_sa_name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = google_service_account.bastion[0].member
}

resource "terraform_data" "bastion_user_data" {
  count            = var.database_enabled ? 1 : 0
  triggers_replace = sha256(local.bastion_user_data)
}

resource "google_compute_instance" "bastion" {
  count        = var.database_enabled ? 1 : 0
  name         = "${local.resource_name}-bastion"
  machine_type = "e2-micro"
  zone         = var.zone
  tags         = ["bastion"]
  labels       = local.labels

  boot_disk {
    initialize_params {
      image = "cos-cloud/cos-stable"
    }
  }

  network_interface {
    subnetwork = google_compute_subnetwork.main.id
    # No access_config  -  no public IP (IAP tunnel only)
  }

  metadata = {
    user-data                 = local.bastion_user_data
    enable-oslogin            = "TRUE"
    cos-update-strategy       = "update_enabled"
    google-logging-enabled    = "true"
    google-monitoring-enabled = "true"
  }

  service_account {
    email  = google_service_account.bastion[0].email
    scopes = ["cloud-platform"]
  }

  lifecycle {
    replace_triggered_by = [terraform_data.bastion_user_data[0]]
  }
}
