# Using the MCP server from other clients

The server is a standard MCP server (official Python SDK, `MCPServer`), so any MCP client can use
the tools directly, without the LangGraph agent. Paths below are placeholders.

By default it serves the **simulated** backend (pick a scenario with `ONCALL_SCENARIO`). For real
data set `ONCALL_BACKEND=gcp` and `ONCALL_ALLOWED_PROJECTS=YOUR_PROJECT_ID` after
`gcloud auth application-default login`.

## Claude Code

```bash
claude mcp add gcp-oncall \
  --env ONCALL_BACKEND=simulated --env ONCALL_SCENARIO=bad-deploy \
  -- /path/to/gcp-oncall-agent/.venv/bin/python -m oncall_agent.mcp_server stdio
```

Or commit a project-scoped `.mcp.json`:

```json
{
  "mcpServers": {
    "gcp-oncall": {
      "command": "/path/to/gcp-oncall-agent/.venv/bin/python",
      "args": ["-m", "oncall_agent.mcp_server", "stdio"],
      "env": { "ONCALL_BACKEND": "simulated", "ONCALL_SCENARIO": "bad-deploy" }
    }
  }
}
```

## Claude Desktop

`claude_desktop_config.json` (Settings > Developer > Edit Config):

```json
{
  "mcpServers": {
    "gcp-oncall": {
      "command": "C:\\path\\to\\gcp-oncall-agent\\.venv\\Scripts\\python.exe",
      "args": ["-m", "oncall_agent.mcp_server", "stdio"],
      "env": {
        "ONCALL_BACKEND": "gcp",
        "ONCALL_ALLOWED_PROJECTS": "YOUR_PROJECT_ID"
      }
    }
  }
}
```

## Streamable HTTP (remote clients, ADK, other agents)

```bash
oncall mcp --transport http --port 8765        # binds 127.0.0.1 by default
# endpoint: http://127.0.0.1:8765/mcp
```

Do not expose this port publicly. If it must be remote, put it behind an authenticating proxy
(IAP / Cloud Run with `--no-allow-unauthenticated`) and keep `ONCALL_ALLOWED_PROJECTS` tight.

## The write tool from an external client

`rollback_cloud_run_revision` is listed (annotated `destructiveHint: true`) but rejects calls
without a valid approval token. With the `gcp` backend it is also disabled unless
`ONCALL_ALLOW_WRITES=true`. To use it from Claude Desktop, a human mints a token for the exact
action after reviewing it; running the command *is* the approval:

```bash
ONCALL_APPROVAL_SECRET=... oncall issue-approval-token \
  --project-id YOUR_PROJECT_ID --region YOUR_REGION \
  --service checkout --target-revision checkout-00041-xit
```

The server process must be started with the same `ONCALL_APPROVAL_SECRET`. The token expires
after `ONCALL_APPROVAL_TTL_SECONDS` (default 300).

## Inspecting with the MCP Inspector

```bash
npx @modelcontextprotocol/inspector /path/to/.venv/bin/python -m oncall_agent.mcp_server stdio
```
