# Least-privilege IAM

The agent runs as one Google service account, `oncall-agent@YOUR_PROJECT_ID.iam.gserviceaccount.com`.
It authenticates with Application Default Credentials only: the attached service account on Cloud
Run, Workload Identity Federation for GKE on GKE, and `gcloud auth application-default login` on a
laptop. There is no service account key anywhere in this project, and there should not be one in
your deployment either (enforce `iam.disableServiceAccountKeyCreation` at the org level).

## Read role: `oncallAgentReader`

Defined in [`deploy/cloudrun/iam/oncall-reader-role.yaml`](../deploy/cloudrun/iam/oncall-reader-role.yaml).

| Permission | Used by | Why not a predefined role |
|---|---|---|
| `logging.logEntries.list` | `query_logs` | `roles/logging.viewer` also grants sinks, metrics, buckets listing |
| `monitoring.timeSeries.list` | `get_metric_timeseries` | `roles/monitoring.viewer` grants dashboards, uptime checks, groups... |
| `monitoring.alertPolicies.list`, `.get` | `get_active_alerts` | same |
| `run.services.get`, `.list` | `list_cloud_run_revisions` | `roles/run.viewer` is close; custom role keeps everything in one place |
| `run.revisions.get`, `.list` | `list_cloud_run_revisions` | |
| `container.clusters.get` | `get_gke_pod_status` (endpoint + CA) | `roles/container.viewer` grants read on all Kubernetes objects incl. ConfigMaps |
| `container.pods.list`, `.get` | `get_gke_pod_status` | pods only; Secrets/ConfigMaps stay unreadable |
| `cloudbuild.builds.list`, `.get` | `list_recent_builds` | `roles/cloudbuild.builds.viewer` is equivalent; kept custom for a single review surface |

Notes:

* Log entries can contain personal data. If some logs must stay out of reach, route them to a
  separate log bucket and grant `logging.views.access` only on the views the agent may read,
  instead of project-wide `logging.logEntries.list`.
* The agent never reads Secret Manager payloads of workloads. It only reads *its own* secrets
  (`roles/secretmanager.secretAccessor` bound on `oncall-webhook-token` and
  `oncall-approval-secret` individually, not on the project).

## Write role: `oncallAgentRollback` (optional)

Defined in [`deploy/cloudrun/iam/oncall-rollback-role.yaml`](../deploy/cloudrun/iam/oncall-rollback-role.yaml):
`run.services.get`, `run.services.update`, `run.operations.get`.

* Only grant it if `ONCALL_ALLOW_WRITES=true`. Without it, the rollback tool fails closed even if a
  token is presented (defence in depth: config flag + IAM + approval token).
* Bind it on individual Cloud Run services (`gcloud run services add-iam-policy-binding`) rather
  than at project level.
* Updating a service needs `iam.serviceAccounts.actAs` on the service's runtime SA. Grant
  `roles/iam.serviceAccountUser` on that specific SA, never project-wide.
* Consider an IAM Condition with an expiry (`request.time < timestamp(...)`) for break-glass use.

## Other runtime permissions

| Need | Role | Scope |
|---|---|---|
| Gemini on Vertex AI | `roles/aiplatform.user` | project (or a dedicated AI project) |
| Export traces to Cloud Trace (via OTel Collector) | `roles/cloudtrace.agent` | project |
| Read own secrets | `roles/secretmanager.secretAccessor` | the two secrets only |

## Auditing

All calls are made with the agent's own identity, so Cloud Audit Logs show exactly which API calls
it made. The approval decision (approver identity from IAP, comment, timestamp) is part of the
investigation record and the `agent.approval` trace span.
