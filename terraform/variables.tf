variable "project_id" {
  description = "GCP project that owns the Cloud Run services and Artifact Registry repo."
  type        = string
  default     = "sabeti-adapt"
}

variable "region" {
  description = "Region for the Cloud Run services and the (regional) Artifact Registry repo."
  type        = string
  default     = "us-central1"
}

variable "service_name" {
  description = "Cloud Run service name and the user-visible app slug."
  type        = string
  default     = "primercast"
}

variable "image" {
  description = "Container image used only when terraform first creates the Cloud Run services (both ignore image changes afterwards; CI deploys the real CPU-only GAR image). Defaults to Google's public placeholder so the services can be created before CI has pushed anything to the new GAR repo."
  type        = string
  default     = "us-docker.pkg.dev/cloudrun/container/hello"
}

variable "gar_repository_id" {
  description = "Artifact Registry repository ID for the primercast Docker image."
  type        = string
  default     = "primercast"
}

variable "cloud_run_min_instances" {
  description = "Minimum Cloud Run instances. 0 = scale to zero (accept cold starts for this low-frequency app)."
  type        = number
  default     = 0
}

variable "cloud_run_max_instances" {
  description = "Maximum Cloud Run instances. Caps blast radius of misuse."
  type        = number
  default     = 5
}

variable "cloud_run_cpu" {
  description = "CPU per Cloud Run instance. Heavier than carmen because the primer-design pipeline runs bowtie2 + MAFFT + torch in-process."
  type        = string
  default     = "8"
}

variable "cloud_run_memory" {
  description = "Memory per Cloud Run instance, shared by its concurrent sessions. Scratch files live in memory on Cloud Run. Every run uses all cores, so memory grows with CPU."
  type        = string
  default     = "16Gi"
}

variable "cloud_run_timeout_seconds" {
  description = "Per-request timeout, which also caps how long a Streamlit websocket (one browser session) stays connected before it reconnects. 3600s is the Cloud Run maximum."
  type        = number
  default     = 3600
}

variable "cloud_run_concurrency" {
  description = "Max concurrent requests per container. Streamlit's websocket and each file-upload PUT are separate Cloud Run requests, and the browser sends a multi-file upload as parallel PUTs. When an instance is at this limit, Cloud Run overrides session affinity and routes the overflow to another instance, which doesn't know the session and returns 400 (\"AxiosError ... status code 400\" in the GUI). Keep it well above the number of files a user uploads at once. Instances still scale out on CPU load."
  type        = number
  default     = 80
}

variable "custom_domain" {
  description = "Custom domain to map to the production Cloud Run service. Leave empty to skip domain mapping."
  type        = string
  default     = "primercast.sabeti.broadinstitute.org"
}

variable "error_report_bucket_name" {
  description = "GCS bucket (globally unique name) for \"Report a problem\" bundles from the web app."
  type        = string
  default     = "sabeti-adapt-primercast-error-reports"
}

variable "error_report_retention_days" {
  description = "Error report bundles are deleted this many days after upload."
  type        = number
  default     = 90
}

variable "error_report_notification_emails" {
  description = "Addresses emailed (via a Cloud Logging alert) when a user submits an error report."
  type        = list(string)
  default = [
    "khsu@broadinstitute.org",
    "baekseun@broadinstitute.org",
  ]
}

variable "monitor_bucket_name" {
  description = "GCS bucket (globally unique) holding scheduled-monitor configs and run data (accession history, reports)."
  type        = string
  default     = "sabeti-adapt-primercast-monitor"
}

variable "monitor_email_sender" {
  description = "From address of monitor reports. Must be a verified sender in Brevo (Senders, Domains & Dedicated IPs)."
  type        = string
  default     = ""
}

variable "monitor_smtp_host" {
  description = "SMTP server for monitor reports."
  type        = string
  default     = "smtp-relay.brevo.com"
}

variable "monitor_smtp_port" {
  description = "SMTP port: 465 = implicit TLS, anything else = STARTTLS."
  type        = number
  default     = 587
}

variable "monitor_smtp_user" {
  description = "SMTP login (Brevo: the login shown under SMTP & API -> SMTP, e.g. 1a2b3c001@smtp-brevo.com). Empty = log in as monitor_email_sender. The SMTP key goes in the primercast-monitor-smtp-key secret (see monitor.tf)."
  type        = string
  default     = ""
}

variable "monitor_email_enabled" {
  description = "Wire the SMTP-key secret into the service and jobs. Set true only after adding a secret version (Cloud Run rejects a secret reference with no versions)."
  type        = bool
  default     = false
}

variable "monitor_time_zone" {
  description = "Time zone for the daily dispatcher trigger and scheduled run dates."
  type        = string
  default     = "America/New_York"
}

variable "monitor_job_cpu" {
  description = "CPU for the monitor dispatcher job (runs fetch + evaluate per due schedule)."
  type        = string
  default     = "4"
}

variable "monitor_job_memory" {
  description = "Memory for the monitor dispatcher job. Scratch files live in memory on Cloud Run, so leave headroom."
  type        = string
  default     = "8Gi"
}

variable "monitor_job_timeout_seconds" {
  description = "Max runtime of one dispatcher execution (all due schedules run sequentially; a first run is its own execution). Cloud Run Jobs allow up to 24h."
  type        = number
  default     = 43200
}
