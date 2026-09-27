###############################################################################
# "Report a problem" error reports                                             #
#                                                                              #
# The web app (gui/error_report.py) uploads a .tar.gz bundle per report to     #
# this bucket, then prints a structured log line                                #
# (jsonPayload.event = "error_report_submitted"). The log-based alert below    #
# turns that line into an email to the team. No SMTP credentials are needed   #
# in the container.                                                            #
###############################################################################

resource "google_storage_bucket" "error_reports" {
  name     = var.error_report_bucket_name
  project  = var.project_id
  location = var.region

  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false

  lifecycle_rule {
    condition {
      age = var.error_report_retention_days
    }
    action {
      type = "Delete"
    }
  }

  depends_on = [google_project_service.services]
}

# Create-only: the public app can add reports but cannot read, list, overwrite
# or delete them, so a misbehaving client can't see other users' reports.
resource "google_storage_bucket_iam_member" "runtime_report_writer" {
  bucket = google_storage_bucket.error_reports.name
  role   = "roles/storage.objectCreator"
  member = "serviceAccount:${google_service_account.runtime.email}"
}

resource "google_monitoring_notification_channel" "error_report_email" {
  for_each     = toset(var.error_report_notification_emails)
  project      = var.project_id
  display_name = "${var.service_name} error reports — ${each.value}"
  type         = "email"
  labels = {
    email_address = each.value
  }

  depends_on = [google_project_service.services]
}

resource "google_monitoring_alert_policy" "error_report_submitted" {
  project      = var.project_id
  display_name = "${var.service_name}: user submitted an error report"
  combiner     = "OR"

  conditions {
    display_name = "error_report_submitted log entry"
    condition_matched_log {
      filter = join(" AND ", [
        "resource.type=\"cloud_run_revision\"",
        "resource.labels.service_name=~\"^${var.service_name}\"",
        "jsonPayload.event=\"error_report_submitted\"",
      ])
      label_extractors = {
        service       = "EXTRACT(resource.labels.service_name)"
        run_id        = "EXTRACT(jsonPayload.run_id)"
        report_id     = "EXTRACT(jsonPayload.report_id)"
        location      = "EXTRACT(jsonPayload.location)"
        return_code   = "EXTRACT(jsonPayload.return_code)"
        contact_email = "EXTRACT(jsonPayload.contact_email)"
        user_message  = "EXTRACT(jsonPayload.user_message)"
      }
    }
  }

  # Log-based alerts require a rate limit; reports within the same 5 minutes
  # are folded into one email (all of them are still in the bucket and logs).
  alert_strategy {
    notification_rate_limit {
      period = "300s"
    }
    auto_close = "1800s"
  }

  documentation {
    mime_type = "text/markdown"
    subject   = "[${var.service_name}] Error report for run $${log.extracted_label.run_id}"
    content   = <<-EOT
      A user submitted an error report from the web app.

      - **Service:** $${log.extracted_label.service}
      - **Run ID:** $${log.extracted_label.run_id}
      - **Report ID:** $${log.extracted_label.report_id}
      - **Pipeline exit code:** $${log.extracted_label.return_code}
      - **Contact email:** $${log.extracted_label.contact_email}
      - **Message:** $${log.extracted_label.user_message}

      Bundle: `$${log.extracted_label.location}`

      Download: `gcloud storage cp $${log.extracted_label.location} .`
      (Reports are deleted after ${var.error_report_retention_days} days.)
    EOT
  }

  notification_channels = [
    for channel in google_monitoring_notification_channel.error_report_email : channel.id
  ]
}
