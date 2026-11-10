###############################################################################
# Cloud Run service hosting the Streamlit web app (production)                  #
###############################################################################

resource "google_cloud_run_v2_service" "app" {
  name                = var.service_name
  project             = var.project_id
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = false

  template {
    service_account                  = google_service_account.runtime.email
    timeout                          = "${var.cloud_run_timeout_seconds}s"
    max_instance_request_concurrency = var.cloud_run_concurrency
    # Streamlit's session_state lives in the Python process; pin a browser to
    # one instance so file-upload XHRs and the websocket land together.
    session_affinity = true

    scaling {
      min_instance_count = var.cloud_run_min_instances
      max_instance_count = var.cloud_run_max_instances
    }

    containers {
      image = var.image

      ports {
        container_port = 8080
      }

      # "Report a problem" bundles (see error_reports.tf / gui/error_report.py).
      env {
        name  = "PRIMERCAST_REPORT_BUCKET"
        value = google_storage_bucket.error_reports.name
      }
      env {
        name  = "PRIMERCAST_REPORT_PREFIX"
        value = "prod"
      }

      # Scheduled monitors (see monitor.tf / src/primercast/monitor_schedule.py).
      env {
        name  = "PRIMERCAST_MONITOR_BUCKET"
        value = google_storage_bucket.monitor.name
      }
      env {
        name  = "PRIMERCAST_MONITOR_PREFIX"
        value = "prod"
      }
      env {
        name  = "PRIMERCAST_MONITOR_TZ"
        value = var.monitor_time_zone
      }
      # The first run of a new schedule executes on the monitor job, not here.
      env {
        name  = "PRIMERCAST_MONITOR_JOB"
        value = "projects/${var.project_id}/locations/${var.region}/jobs/${local.monitor_jobs.prod.name}"
      }
      env {
        name  = "PRIMERCAST_PUBLIC_URL"
        value = var.custom_domain != "" ? "https://${var.custom_domain}" : ""
      }
      env {
        name  = "PRIMERCAST_EMAIL_SENDER"
        value = var.monitor_email_sender
      }
      env {
        name  = "PRIMERCAST_SMTP_HOST"
        value = var.monitor_smtp_host
      }
      env {
        name  = "PRIMERCAST_SMTP_PORT"
        value = tostring(var.monitor_smtp_port)
      }
      env {
        name  = "PRIMERCAST_SMTP_USER"
        value = var.monitor_smtp_user
      }
      dynamic "env" {
        for_each = var.monitor_email_enabled ? [1] : []
        content {
          name = "PRIMERCAST_EMAIL_PASSWORD"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.monitor_smtp_key.secret_id
              version = "latest"
            }
          }
        }
      }

      resources {
        limits = {
          cpu    = var.cloud_run_cpu
          memory = var.cloud_run_memory
        }
        # CPU always allocated (not throttled between requests): the pipeline
        # runs snakemake as a subprocess whose work must keep progressing while
        # the Streamlit request that launched it is in flight. (carmen can idle-
        # throttle because it barely computes; this app cannot.)
        cpu_idle          = false
        startup_cpu_boost = true
      }
    }
  }

  # CI deploys new images via `gcloud run deploy --image ...`; terraform owns the
  # service shell + non-image config. The image tag drifts from this default
  # between releases — that's expected and harmless.
  lifecycle {
    ignore_changes = [
      client,
      client_version,
      template[0].containers[0].image,
      # Service-level scaling block (not template.scaling) drifts to an empty
      # manual count via gcloud deploys; updating it only spawns a no-op revision.
      scaling,
    ]
  }

  # The revision must be able to read the SMTP-key secret when it is created.
  depends_on = [
    google_project_service.services,
    google_secret_manager_secret_iam_member.monitor_smtp_key,
  ]
}

# Public site: ingress=ALL above lets anyone reach the service; this binding
# lets anyone invoke. There is no auth tier — the team accepts a public,
# shared-tenant model (results are not sensitive and are not retained durably).
resource "google_cloud_run_v2_service_iam_member" "public_invoker" {
  project  = google_cloud_run_v2_service.app.project
  location = google_cloud_run_v2_service.app.location
  name     = google_cloud_run_v2_service.app.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

###############################################################################
# Custom domain mapping (Cloud Run v1 domain-mapping API, works with v2       #
# services). GCP auto-provisions and renews the TLS cert once the DNS records  #
# below are in place.                                                          #
#                                                                              #
# Prerequisite: the GCP identity running terraform must be a verified owner    #
# of sabeti.broadinstitute.org in Google Search Console. One-time step per     #
# domain, not per service.                                                      #
###############################################################################

resource "google_cloud_run_domain_mapping" "custom_domain" {
  count    = var.custom_domain != "" ? 1 : 0
  name     = var.custom_domain
  location = var.region
  project  = var.project_id

  metadata {
    namespace = var.project_id
  }

  spec {
    route_name = google_cloud_run_v2_service.app.name
  }

  depends_on = [google_cloud_run_v2_service.app]
}
