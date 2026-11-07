"""Safety primitives shared by the MCP server and the agent.

* Untrusted-content handling: everything that comes out of logs, build output or resource
  metadata is attacker-influenced (anyone who can make the app log a string can write into
  the agent's context). We strip control characters, truncate, and flag text that looks
  like instructions aimed at an LLM. Flagged text is still returned (it can be evidence of
  an attack) but it is labelled so the model and the operator can see it.
* Approval tokens: the single write tool only executes with an HMAC token bound to the
  exact action parameters and a short expiry. Tokens are minted by the human-approval step,
  never by the LLM, so a model that "decides" to roll back cannot do so on its own.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
from typing import Any

from oncall_agent.config import Settings

MAX_TEXT_CHARS = 2_000

_CONTROL_CHARS = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Heuristic patterns. This is a tripwire, not a defence on its own: the real defences are
# the tool allowlist, the approval token and the system prompt's data/instruction split.
_INJECTION_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"ignore (all |any )?(previous|prior|above) (instructions|prompts?)",
        r"disregard (the |your )?(system|previous) (prompt|instructions)",
        r"\b(system|assistant)\s*:\s",
        r"you are now\b",
        r"\bcall (the )?(tool|function)\b",
        r"rollback_cloud_run_revision",
        r"approval[_ ]token",
        r"</?tool_result",
        r"do not (tell|inform|alert) (the )?(user|operator|human)",
    )
]


def clean_text(text: str, limit: int = MAX_TEXT_CHARS) -> str:
    text = _CONTROL_CHARS.sub("", text)
    if len(text) > limit:
        text = text[:limit] + f"... [truncated {len(text) - limit} chars]"
    return text


def looks_like_injection(text: str) -> bool:
    return any(p.search(text) for p in _INJECTION_PATTERNS)


def sanitize_untrusted(text: str) -> tuple[str, bool]:
    """Return (clean_text, suspected_injection)."""
    cleaned = clean_text(text)
    return cleaned, looks_like_injection(cleaned)


def wrap_tool_output(ref: str, tool: str, payload: Any) -> str:
    """Render a tool result for the LLM with an explicit untrusted-data boundary."""
    body = json.dumps(payload, indent=None, sort_keys=True, default=str)
    # Neutralise attempts to close our delimiter from inside the data.
    body = body.replace("</tool_result", "<\\/tool_result")
    return f'<tool_result ref="{ref}" tool="{tool}" untrusted="true">\n{body}\n</tool_result>'


# --------------------------------------------------------------------------- approvals

_process_secret: bytes | None = None


def approval_secret(settings: Settings) -> bytes:
    """Configured secret, or a random per-process one (fine for in-process / stdio child)."""
    global _process_secret
    if settings.approval_secret is not None:
        return settings.approval_secret.get_secret_value().encode()
    if _process_secret is None:
        _process_secret = secrets.token_bytes(32)
    return _process_secret


def _canonical(action: dict[str, Any]) -> bytes:
    return json.dumps(action, sort_keys=True, separators=(",", ":")).encode()


def issue_approval_token(secret: bytes, action: dict[str, Any], ttl_seconds: int = 300) -> str:
    expiry = int(time.time()) + ttl_seconds
    mac = hmac.new(secret, _canonical(action) + b"|" + str(expiry).encode(), hashlib.sha256).hexdigest()
    return f"{expiry}.{mac}"


def verify_approval_token(secret: bytes, action: dict[str, Any], token: str) -> tuple[bool, str]:
    try:
        expiry_s, mac = token.split(".", 1)
        expiry = int(expiry_s)
    except ValueError:
        return False, "malformed approval token"
    if expiry < time.time():
        return False, "approval token expired"
    expected = hmac.new(secret, _canonical(action) + b"|" + str(expiry).encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, mac):
        return False, "approval token does not match this action"
    return True, "ok"
