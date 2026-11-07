from oncall_agent.agent.llm.providers import flatten_transcript, to_anthropic, to_gemini, to_openai

MESSAGES = [
    {"role": "system", "content": "sys"},
    {"role": "user", "content": "alert"},
    {
        "role": "assistant",
        "content": "checking",
        "tool_calls": [
            {"id": "a", "name": "query_logs", "arguments": {"project_id": "demo-project-123"}},
            {"id": "b", "name": "get_active_alerts", "arguments": {"project_id": "demo-project-123"}},
        ],
    },
    {"role": "tool", "tool_call_id": "a", "name": "query_logs", "content": "<tool_result ...>"},
    {
        "role": "tool",
        "tool_call_id": "b",
        "name": "get_active_alerts",
        "content": "<tool_result ...>",
        "is_error": True,
    },
]


def test_to_anthropic_groups_parallel_results():
    system, msgs = to_anthropic(MESSAGES)
    assert system == "sys"
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assert [b["type"] for b in msgs[1]["content"]] == ["text", "tool_use", "tool_use"]
    assert [b["tool_use_id"] for b in msgs[2]["content"]] == ["a", "b"]
    assert msgs[2]["content"][1]["is_error"] is True


def test_to_anthropic_replays_native_blocks():
    native = [
        {"type": "thinking", "thinking": "", "signature": "sig"},
        {"type": "tool_use", "id": "a", "name": "x", "input": {}},
    ]
    msgs = [*MESSAGES[:2], {**MESSAGES[2], "_anthropic_content": native}]
    _, out = to_anthropic(msgs)
    assert out[1]["content"] == native


def test_to_openai():
    out = to_openai(MESSAGES)
    assert out[2]["tool_calls"][0]["function"]["name"] == "query_logs"
    assert out[3] == {"role": "tool", "tool_call_id": "a", "content": "<tool_result ...>"}


def test_to_gemini_groups_function_responses():
    system, contents = to_gemini(MESSAGES)
    assert system == "sys"
    assert [c["role"] for c in contents] == ["user", "model", "user"]
    assert len(contents[2]["parts"]) == 2


def test_flatten_transcript():
    system, user = flatten_transcript(MESSAGES)
    assert system == "sys" and "query_logs" in user and "Do not invent refs" in user
