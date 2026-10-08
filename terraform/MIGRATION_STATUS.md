# primercast migration — status (2026-09-26)

Renaming the web deployment `qprimer-designer` → `primercast`. The full runbook is in
[README.md → "Migration from qprimer-designer"](README.md#migration-from-qprimer-designer-legacy-redirect).
Delete this file once the migration is finished.

## Done
- Code changes (uncommitted, on `gui_fixes`): terraform renames, `docker.yml` names/registry,
  docs, `gui/redirect_server.py` + Dockerfile `PRIMERCAST_REDIRECT_URL` switch,
  `tests/test_redirect_server.py`.
- `terraform apply` (as khsu@broadinstitute.org) created: APIs, `primercast-run` SA,
  `primercast` GAR repo, `primercast` + `primercast-staging` services (placeholder image).
- Legacy `qprimer-designer` site is untouched and still live.

## Blocked — needs an admin
1. **Public IAM** (`allUsers` → `run.invoker` on both services) failed:
   khsu lacks `run.services.setIamPolicy`. Ask a `sabeti-adapt` owner to grant
   `roles/run.admin` or run `terraform apply` themselves.
2. **Domain mapping** for `primercast.sabeti.broadinstitute.org` failed: khsu is not a
   verified owner of `sabeti.broadinstitute.org` in Google Search Console. The domain owner
   must add khsu as an owner there (or apply themselves).
3. **Error-report resources** (added later, not yet applied): bucket, bucket IAM, email
   notification channels and log-based alert in `error_reports.tf`, plus the report env
   vars on prod. The bucket IAM grant needs the same admin permissions as item 1.

## Caution
- `terraform/terraform.tfstate` exists **only on Bryan's laptop** (gitignored). Share it
  with whoever applies next, or move state to a GCS backend first — otherwise their apply
  will try to recreate everything.

## Next steps
1. Commit the code changes (ideally on a separate branch from the unrelated `gui_fixes`
   work) and push → CI builds into the new registry and deploys a staging preview
   (CI's `--allow-unauthenticated` should make staging public on its own).
2. Resolve the two blockers above, then re-run `terraform apply`.
3. Push a `v*` tag → prod gets the real image.
4. Add the `custom_domain_dns_records` output to DNS; wait for the cert.
5. Switch legacy `qprimer-designer` service to redirect mode (runbook step 4).
6. Verify the staging URL hash (`terraform output staging_base_url`) matches the docs
   (`primercast-staging-soitfyremq-uc.a.run.app`).
7. In a few months: delete the legacy resources (runbook step 5).
