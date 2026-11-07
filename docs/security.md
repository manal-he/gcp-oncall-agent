# Security model

An on-call agent reads data written by the systems it investigates and can, in the worst case,
change production. The design assumes the model can be wrong or manipulated, and puts the
controls outside the model.

## Threats and controls

| Threat | Control | Where |
|---|---|---|
| Prompt injection through log lines, build output, revision messages | Tool output is sanitized (control chars stripped, truncated), wrapped in `<tool_result untrusted="true">` delimiters that cannot be closed from inside, and heuristically flagged (`suspected_prompt_injection`). The system prompt states that tool content is data. | `safety.py`, `mcp_server/server.py`, `agent/prompts.py` |
| Model calls a write tool on its own | The investigation phase is offered read-only tools only, and the `tools` node re-checks an allowlist (models can emit calls to tools they were not offered). Blocked attempts are recorded, traced and surfaced in `security_notes`. | `agent/graph.py` |
| Model talks a human into a bad rollback | Rollback parameters are validated (name regex, project allowlist) and shown verbatim in the approval UI with the evidence and confidence. Rejection is the default on timeout. | `agent/graph.py`, `api/app.py`, UI |
| Something calls the MCP write tool directly (another client, a compromised agent) | The tool requires an HMAC token bound to the exact `(project, region, service, revision)` and expiring in 5 minutes. Tokens are minted only by the `execute` node after a human decision, or by a human running `oncall issue-approval-token`. Writes are also disabled on the real backend unless `ONCALL_ALLOW_WRITES=true`, and IAM must grant the rollback role. | `safety.py`, `mcp_server/server.py` |
| Query/filter injection into Cloud Logging / Monitoring | The LLM never writes filter syntax. Tool arguments are typed and regex-validated (no quotes, parentheses or operators in free text); metrics come from an allowlisted catalogue. | `mcp_server/server.py`, `backends/gcp.py` |
| Cross-project access | `ONCALL_ALLOWED_PROJECTS` allowlist enforced in every tool. | `mcp_server/server.py` |
| Runaway cost or loops | `max_steps`, `max_tool_calls`, `max_total_tokens`, `max_cost_usd`; when hit, the agent writes a report with what it has and says it stopped early. | `agent/graph.py` |
| Hallucinated evidence | Evidence refs must match executed tool calls; others are dropped and confidence is reduced. | `agent/graph.py` |
| Secret leakage | No credentials in the repo; ADC only; secrets from Secret Manager; env var *names* (not values) are returned for Cloud Run revisions; the approval token is excluded from recorded traces. | everywhere |
| Unauthenticated webhooks | Bearer/query token (constant-time compare) for Cloud Monitoring webhooks; OIDC verification for Pub/Sub push. Both are mandatory with the `gcp` backend. | `api/app.py` |

## What the injection heuristics do not do

Pattern matching catches the obvious attempts ("ignore previous instructions", tool names in log
text). It will miss paraphrases and encodings. It is a tripwire for visibility, not the defence:
the defence is that the model cannot act on an injection, because it has no write tool and no way
to mint an approval token. The `prompt-injection` eval scenario and the `mock-injectable` provider
(a scripted model that deliberately obeys injected instructions) exercise exactly that path.

## Residual risks

* A human can still approve a wrong rollback. Rollback is the lowest-risk write (traffic shift to
  an existing, previously serving revision) and is reversible, which is why it is the only one.
* Log content is sent to the configured LLM provider. Use Vertex AI in the same project/region
  if logs may contain regulated data, and consider redaction (Sensitive Data Protection) upstream.
* In-memory state: pending approvals are lost on restart (they fail safe: nothing executes).
