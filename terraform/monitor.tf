###############################################################################
# Scheduled monitoring                                                         #
#                                                                              #
# The web app saves each scheduled monitor as schedules/<id>.json in the       #
# monitor bucket (src/primercast/monitor_schedule.py). Once a day Cloud        #
# Scheduler starts the `primercast-monitor` Cloud Run Job, which runs          #
# `adapt monitor-dispatch`: every due schedule is fetched + evaluated, its     #
# report emailed, and its outputs written to runs/<id>/<YYYYMMDD>/. The web    #
# app starts a new schedule's first run as a separate execution of the same    #
# job. The web service can scale to zero; nothing depends on it at run time.   #
#                                                                              #
# Email: Brevo SMTP relay (var.monitor_smtp_*). The From address is          #
# var.monitor_email_sender (a verified Brevo sender); the SMTP key lives in    #
# Secret Manager and is never in terraform state. Bootstrap:                   #
#   1. terraform apply                    (creates the empty secret)           #
#   2. printf '%s' 'SMTP_KEY' | gcloud secrets versions add \                  #
#        primercast-monitor-smtp-key --data-file=- --project=sabeti-adapt      #
#   3. set monitor_email_enabled = true and terraform apply again              #
# (Cloud Run refuses to start a revision that references a secret with no     #
# versions, hence the flag.)                                                   #
###############################################################################

resource "google_storage_bucket" "monitor" {
  name     = var.monitor_bucket_name
  project  = var.project_id
  location = var.region

  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false

  # No lifecycle rule: accession history must outlive any fixed age while a
  # schedule is active. The dispatcher deletes a schedule's data
  # 90 days after it is stopped.

  depends_on = [google_project_service.services]
}

resource "google_secret_manager_secret" "monitor_smtp_key" {
  secret_id = "${var.service_name}-monitor-smtp-key"
  project   = var.project_id

  replication {
    auto {}
  }

  depends_on = [google_project_service.services]
}

# --- Identities -------------------------------------------------------------

resource "google_service_account" "monitor_job" {
  account_id   = "${var.service_name}-monitor"
  display_name = "Runtime SA for the ${var.service_name} scheduled-monitor job"
  project      = var.project_id
}

resource "google_service_account" "monitor_scheduler" {
  account_id   = "${var.service_name}-scheduler"
  display_name = "Cloud Scheduler identity that starts the ${var.service_name} monitor job"
  project      = var.project_id
}

# The web app creates/stops schedules and reads run reports; the job
# reads schedules, reads/writes run data and purges stopped schedules.
resource "google_storage_bucket_iam_member" "monitor_bucket" {
  for_each = {
    runtime = google_service_account.runtime.email
    job     = google_service_account.monitor_job.email
  }
  bucket = google_storage_bucket.monitor.name
  role   = "roles/storage.objectUser"
  member = "serviceAccount:${each.value}"
}

# The job emails scheduled and first runs; the web app only sends when no job
# is configured (PRIMERCAST_MONITOR_JOB unset).
resource "google_secret_manager_secret_iam_member" "monitor_smtp_key" {
  for_each = {
    runtime = google_service_account.runtime.email
    job     = google_service_account.monitor_job.email
  }
  project   = var.project_id
  secret_id = google_secret_manager_secret.monitor_smtp_key.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${each.value}"
}

# --- Dispatcher jobs ----------------------------------------------------------

locals {
  monitor_jobs = {
    # Prod: triggered daily by Cloud Scheduler below.
    prod = { name = "${var.service_name}-monitor", prefix = "prod" }
    # Staging: no scheduler. CI points it at the latest branch image; run it by hand:
    #   gcloud run jobs execute primercast-monitor-staging --region us-central1
    staging = { name = "${var.service_name}-monitor-staging", prefix = "staging" }
  }
}

resource "google_cloud_run_v2_job" "monitor" {
  for_each            = local.monitor_jobs
  name                = each.value.name
  project             = var.project_id
  location            = var.region
  deletion_protection = false

  template {
    task_count = 1

    template {
      service_account = google_service_account.monitor_job.email
      timeout         = "${var.monitor_job_timeout_seconds}s"
      # A failed dispatch is safe to rerun: schedules that finished are recorded
      # and skipped.
      max_retries = 1

      containers {
        image   = var.image
        command = ["adapt"]
        args    = ["monitor-dispatch", "--purge-after-days", "90"]

        env {
          name  = "PRIMERCAST_MONITOR_BUCKET"
          value = google_storage_bucket.monitor.name
        }
        env {
          name  = "PRIMERCAST_MONITOR_PREFIX"
          value = each.value.prefix
        }
        env {
          name  = "PRIMERCAST_MONITOR_TZ"
          value = var.monitor_time_zone
        }
        # `adapt monitor` stamps run directories with the local date.
        env {
          name  = "TZ"
          value = var.monitor_time_zone
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
            cpu    = var.monitor_job_cpu
            memory = var.monitor_job_memory
          }
        }
      }
    }
  }

  # CI updates the image (`gcloud run jobs update --image ...`), like the services.
  lifecycle {
    ignore_changes = [
      client,
      client_version,
      template[0].template[0].containers[0].image,
    ]
  }

  depends_on = [
    google_project_service.services,
    google_secret_manager_secret_iam_member.monitor_smtp_key,
  ]
}

# The web app starts each new schedule's first run as its own execution
# (`monitor-dispatch --schedule-id <id>`), which needs runWithOverrides.
resource "google_cloud_run_v2_job_iam_member" "monitor_runtime_executor" {
  for_each = local.monitor_jobs
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_job.monitor[each.key].name
  role     = "roles/run.jobsExecutorWithOverrides"
  member   = "serviceAccount:${google_service_account.runtime.email}"
}

# --- Daily trigger (prod only) -------------------------------------------------

resource "google_cloud_run_v2_job_iam_member" "monitor_scheduler_invoker" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_job.monitor["prod"].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.monitor_scheduler.email}"
}

resource "google_cloud_scheduler_job" "monitor_daily" {
  name        = "${var.service_name}-monitor-daily"
  project     = var.project_id
  region      = var.region
  description = "Starts the ${var.service_name} monitor dispatcher, which runs every due scheduled monitor."
  schedule    = "0 0 * * *"
  time_zone   = var.monitor_time_zone

  retry_config {
    retry_count = 3
  }

  http_target {
    http_method = "POST"
    uri         = "https://run.googleapis.com/v2/projects/${var.project_id}/locations/${var.region}/jobs/${google_cloud_run_v2_job.monitor["prod"].name}:run"

    oauth_token {
      service_account_email = google_service_account.monitor_scheduler.email
    }
  }

  depends_on = [google_cloud_run_v2_job_iam_member.monitor_scheduler_invoker]
}
