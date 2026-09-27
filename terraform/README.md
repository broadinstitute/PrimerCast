# Terraform — primercast serving infra

Provisions the public web entry point for the primercast (qprimer-designer) Streamlit app on Cloud Run:

```
Internet
   │ HTTPS (443) — Google-managed cert on the *.run.app URL
   ▼
Cloud Run service  (primercast, ingress=ALL, allUsers run.invoker)
   └─ CPU-only container image from Artifact Registry
      us-central1-docker.pkg.dev/sabeti-adapt/primercast/primercast
```

The site is **public** — no authentication. The team accepts a shared-tenant model;
results are not sensitive and (in this iteration) are **not retained durably** — each
Cloud Run instance keeps results on local disk only, which is lost on redeploy / scale
events. See "Deferred" below for adding GCS persistence later.

This stack creates:

- The **Artifact Registry** repo the CI `build-gar` job pushes the CPU image to (with
  cleanup policies: keep 20 recent versions; delete untagged after 7 days).
- A **runtime service account** (`primercast-run`) the Cloud Run services run as.
- The **production** service (`primercast`) and a **staging** service
  (`primercast-staging`), both public, with `cpu_idle=false`, session affinity,
  startup CPU boost, and scale-to-zero (`min_instances=0`).
- A **custom domain mapping** (`primercast.sabeti.broadinstitute.org` → production), when
  `custom_domain` is non-empty. GCP provisions the TLS cert once DNS is in place: the
  identity running terraform must be a verified owner of `sabeti.broadinstitute.org`, and
  the records from the `custom_domain_dns_records` output (a CNAME to
  `ghs.googlehosted.com`) must be added to the `sabeti.broadinstitute.org` Cloud DNS zone.

- **Error reports:** a private GCS bucket (`sabeti-adapt-primercast-error-reports`, objects
  deleted after 90 days) that the runtime SA can only *create* objects in, plus a Cloud
  Logging alert that emails the team whenever the app logs `error_report_submitted`
  (see `error_reports.tf` and `gui/error_report.py`). Prod gets `PRIMERCAST_REPORT_BUCKET` /
  `PRIMERCAST_REPORT_PREFIX` from terraform; staging gets them from CI's `staging-deploy` flags.
  Reports submitted within the same 5 minutes are folded into one email (alert rate limit).

It does **not** create the GPU image (that lives in GHCR) or any GCS bucket (see Deferred).

## Image split (why GAR is CPU-only)

Two images are built from one Dockerfile via the `TORCH_VARIANT` build arg:

- **GHCR** — multi-arch (amd64+arm64), **GPU**-enabled. Used by training and the
  Terra/batch CLI.
- **GAR** — amd64-only, **CPU**-only (slim, ~1.5–2 GB vs ~4.5 GB). This is what Cloud Run
  runs — Cloud Run has no GPU, so CUDA libraries would only bloat the image and slow cold
  starts.

## Prerequisites

1. **Deploy identity (already exists, shared).** CI authenticates via Workload Identity
   Federation as `gha-deployer@sabeti-adapt.iam.gserviceaccount.com`, which already holds
   `roles/run.admin`, `roles/artifactregistry.writer`, and `roles/iam.serviceAccountUser`
   at the project level — so it can deploy these services and act as the runtime SA with
   **no extra IAM**.
2. **GitHub repo secrets** on `broadinstitute/PrimerCast` (same values carmen uses):
   - `GCP_WIF_PROVIDER` — the Workload Identity provider resource name.
   - `GCP_DEPLOY_SA` — `gha-deployer@sabeti-adapt.iam.gserviceaccount.com`.
3. **gcloud auth** for running terraform locally: `gcloud auth application-default login`.

## Apply

`terraform.tfvars` can be empty — all variables have sensible defaults.

```bash
cd terraform
terraform init
terraform plan
terraform apply
```

State is local and gitignored (see `.gitignore`); consider a GCS backend if more than one
person manages this. **Bootstrap order:** apply terraform first (creates the GAR repo +
service shells), *then* push a branch so CI can publish the CPU image and deploy revisions.
The services are created with Google's public placeholder image (`var.image`), because the
new GAR repo is empty at that point; the first CI deploy replaces it (prod needs a `v*` tag).

## Migration from `qprimer-designer` (legacy redirect)

The stack was originally deployed under the name `qprimer-designer` (services
`qprimer-designer` / `qprimer-designer-staging`, SA `qprimer-designer-run`, GAR repo
`qprimer-designer`, domain `qprimer-designer.sabeti.broadinstitute.org`). The rename to
`primercast` creates a **new, parallel** stack rather than renaming in place (Cloud Run
services, service accounts and GAR repos can't be renamed). The legacy resources are not
in this terraform state; they are kept temporarily as a redirect and then deleted by hand.

1. `terraform apply` (creates the `primercast*` resources alongside the legacy ones).
2. Push a branch, then a `v*` tag, so CI publishes the image and deploys staging + prod.
3. Add the `custom_domain_dns_records` output to the `sabeti.broadinstitute.org` DNS zone
   and wait for the cert (`gcloud beta run domain-mappings describe --domain=primercast.sabeti.broadinstitute.org --region=us-central1`).
4. Once `https://primercast.sabeti.broadinstitute.org` works, switch the legacy prod service
   to redirect mode. The image starts `gui/redirect_server.py` (a stdlib 301 server that
   preserves path + query) instead of Streamlit when `PRIMERCAST_REDIRECT_URL` is set:
   ```bash
   gcloud run services update qprimer-designer --region=us-central1 --project=sabeti-adapt \
     --image=us-central1-docker.pkg.dev/sabeti-adapt/primercast/primercast:<vX.Y.Z> \
     --update-env-vars=PRIMERCAST_REDIRECT_URL=https://primercast.sabeti.broadinstitute.org \
     --cpu=1 --memory=512Mi --min-instances=0 --max-instances=2
   ```
   The legacy domain mapping still points at this service, so both
   `qprimer-designer.sabeti.broadinstitute.org` and its `*.run.app` URL now redirect.
5. After the redirect period (a few months), delete the legacy resources:
   ```bash
   gcloud beta run domain-mappings delete --domain=qprimer-designer.sabeti.broadinstitute.org --region=us-central1 --project=sabeti-adapt
   gcloud run services delete qprimer-designer qprimer-designer-staging --region=us-central1 --project=sabeti-adapt
   gcloud iam service-accounts delete qprimer-designer-run@sabeti-adapt.iam.gserviceaccount.com --project=sabeti-adapt
   gcloud artifacts repositories delete qprimer-designer --location=us-central1 --project=sabeti-adapt
   ```
   and remove the legacy CNAME from the DNS zone. Any old copy of the original terraform
   state should be discarded (applying it would recreate the legacy resources).

## Deferred (future changes)

- **GCS result persistence:** create a dedicated bucket with a 1-day lifecycle rule, mount
  it as a gen2 GCS volume on both services, grant the runtime SA `roles/storage.objectAdmin`,
  and set `PRIMERCAST_DATA_DIR` to the mount. The app already roots its data dirs at
  `PRIMERCAST_DATA_DIR`, so this is config-only on the app side. Makes "Past Results" survive
  redeploys and be shared across users.
