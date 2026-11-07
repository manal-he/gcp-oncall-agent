# Deploying to Cloud Run

Placeholders: `YOUR_PROJECT_ID`, `YOUR_REGION`. Nothing in this folder contains real values.

1. **Identity.** One runtime service account, `oncall-agent@YOUR_PROJECT_ID.iam.gserviceaccount.com`,
   attached to the Cloud Run service. No key files: locally use
   `gcloud auth application-default login`; on Cloud Run the attached service account is used
   automatically. On GKE, use Workload Identity Federation for GKE with a Kubernetes service
   account bound to the same Google service account.
2. **Permissions.** `iam/oncall-reader-role.yaml` lists exactly the permissions the read tools use.
   `iam/oncall-rollback-role.yaml` is optional and only needed with `ONCALL_ALLOW_WRITES=true`;
   bind it per service rather than per project. Rationale per permission: `docs/iam.md`.
3. **Secrets.** `ONCALL_WEBHOOK_TOKEN` and `ONCALL_APPROVAL_SECRET` come from Secret Manager
   (`secretKeyRef` in `service.yaml`). `deploy.sh` reads the values from stdin.
4. **Ingress.** `internal-and-cloud-load-balancing` plus an external HTTPS load balancer with
   Identity-Aware Proxy for the UI/API. The decision endpoint records the IAP identity
   (`X-Goog-Authenticated-User-Email`) as the approver; with the `gcp` backend a decision
   without that header is rejected (403).
5. **Alerts in.** Either
   - a webhook notification channel pointing at `/webhooks/cloud-monitoring` (token auth), or
   - a Pub/Sub notification channel with a push subscription to `/webhooks/pubsub` using an OIDC
     token whose audience equals `ONCALL_PUBSUB_AUDIENCE` (preferred: no shared secret in a URL).

`maxScale: 1` is deliberate: investigation state and pending approvals live in memory. See the
main README roadmap for moving to a durable LangGraph checkpointer.
