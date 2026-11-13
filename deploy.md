# CI/CD using GitHub Actions

The app runs on **Cloud Run**. GitHub Actions tests, builds and deploys it on
every push:

- `staging` → `zero-sales-agent-staging`
- `main` → `zero-sales-agent`

The periodic Slack summary runs from **Cloud Scheduler**, which calls
`POST /jobs/periodic-summary` with an OIDC token.

## Setting it up (once)

1. As a GCP project Owner, run `scripts/gcp_setup.sh` (APIs, image repository,
   service accounts, permissions).
2. In GitHub → Settings → Environments, give both `staging` and `production`
   the variables `GCP_PROJECT`, `GCP_SA_KEY`, `DB_NAME` and `DASHBOARD_URL`.
3. Make sure the database accepts connections from Cloud Run.
4. Push to `staging`.

Everything else - the workflow steps, optional variables, the move off the
VMs, monitoring, rollback and debugging - is in [DEPLOYMENT.md](DEPLOYMENT.md).
