"""Google ADK variant (experimental, not exercised in CI).

The LangGraph agent is the reference implementation (budgets, grounding, interrupt-based
approval, evals). This module shows that the MCP server is framework-agnostic: an ADK
``LlmAgent`` consumes the same tools over stdio, restricted to the read-only allowlist, so it
can investigate but never roll back.

    pip install google-adk          # in a separate venv: ADK pins its own `mcp` version
    adk web src/oncall_agent        # or: adk run src/oncall_agent/adk_agent

Requires GOOGLE_API_KEY (AI Studio) or GOOGLE_GENAI_USE_VERTEXAI=true + ADC.
"""

from __future__ import annotations

import sys

from mcp import StdioServerParameters

from oncall_agent.agent.prompts import SYSTEM_PROMPT
from oncall_agent.mcp_server.server import READ_ONLY_TOOLS

try:  # ADK >= 1.10
    from google.adk.agents import LlmAgent
    from google.adk.tools.mcp_tool import McpToolset as _Toolset
    from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
except ImportError:  # older ADK naming
    from google.adk.agents import LlmAgent
    from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
    from google.adk.tools.mcp_tool.mcp_toolset import MCPToolset as _Toolset

toolset = _Toolset(
    connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(command=sys.executable, args=["-m", "oncall_agent.mcp_server", "stdio"]),
        timeout=30,
    ),
    tool_filter=list(READ_ONLY_TOOLS),  # the write tool is never exposed to this agent
)

root_agent = LlmAgent(
    model="gemini-2.5-flash",
    name="gcp_oncall_adk",
    description="Investigates Google Cloud alerts with read-only MCP tools.",
    instruction=SYSTEM_PROMPT.format(max_steps=10, max_tool_calls=16)
    + "\nFinish with: summary, timeline, root cause, evidence (tool + finding), confidence, recommended action.",
    tools=[toolset],
)
