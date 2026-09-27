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
  default     = "4"
}

variable "cloud_run_memory" {
  description = "Memory per Cloud Run instance. Validate against a real design run for OOM headroom."
  type        = string
  default     = "4Gi"
}

variable "cloud_run_timeout_seconds" {
  description = "Per-request timeout. Typical design runs ~2 min after recent optimizations; 600s gives headroom."
  type        = number
  default     = 600
}

variable "cloud_run_concurrency" {
  description = "Max concurrent requests per container. Must be >1: Streamlit's persistent websocket plus file-upload XHRs are separate Cloud Run requests, and routing the upload to a fresh sessionless instance returns 400. Kept modest so a single instance runs only a few heavy pipeline jobs; max_instances absorbs additional load. Session affinity pins a browser to one instance."
  type        = number
  default     = 8
}

variable "custom_domain" {
  description = "Custom domain to map to the production Cloud Run service. Leave empty to skip domain mapping."
  type        = string
  default     = "primercast.sabeti.broadinstitute.org"
}
