terraform {
  required_version = ">= 1.5"

  # State lives in GCS (versioned bucket) so more than one person can apply.
  backend "gcs" {
    bucket = "primercast"
    prefix = "tfstate"
  }

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
    google-beta = {
      source  = "hashicorp/google-beta"
      version = "~> 6.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

provider "google-beta" {
  project = var.project_id
  region  = var.region
}
