"""Offline tests for the prompt/reply protocol of bridge.py (no browser)."""

import json

import bridge

EXPECTED_PROMPT_TOKENS = 100

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "ha_call",
            "description": "Call a HA service",
            "parameters": {
                "type": "object",
                "properties": {"service": {"type": "string"}},
            },
        },
    },
    {
        "type": "function",
        "function": {"name": "web_search", "parameters": {"type": "object"}},
    },
]


def test_plain_reply_without_tools() -> None:
    """Keep an apparent tool block as text when no tools are declared."""
    text, calls = bridge.parse_reply('Привет! ```json\n{"name": "ha_call"}\n```', [])
    assert calls == []
    assert text.startswith("Привет")


def test_tool_calls_block_becomes_openai_calls() -> None:
    """Translate supported tool blocks into complete API tool calls."""
    md = (
        'Сейчас включу.\n```tool_calls\n[{"name": "ha_call", '
        '"arguments": {"service": "light.turn_on"}},'
        ' {"name": "web_search", "arguments": {}}]\n```'
    )
    text, calls = bridge.parse_reply(md, TOOLS)
    assert text == "Сейчас включу."
    assert [c["function"]["name"] for c in calls] == ["ha_call", "web_search"]
    assert json.loads(calls[0]["function"]["arguments"]) == {"service": "light.turn_on"}
    assert all(c["id"].startswith("call_") and c["type"] == "function" for c in calls)


def test_json_block_with_unknown_tool_stays_text() -> None:
    """Preserve a JSON example whose tool name is not declared."""
    md = 'Пример:\n```json\n{"name": "rm_rf", "arguments": {}}\n```'
    text, calls = bridge.parse_reply(md, TOOLS)
    assert calls == []
    assert "rm_rf" in text


def test_single_object_and_string_arguments() -> None:
    """Accept a single call with JSON arguments encoded as a string."""
    md = '```json\n{"name": "ha_call", "arguments": "{\\"service\\": \\"x\\"}"}\n```'
    _, calls = bridge.parse_reply(md, TOOLS)
    assert json.loads(calls[0]["function"]["arguments"]) == {"service": "x"}


def test_full_prompt_has_system_tools_and_dialogue() -> None:
    """Include system instructions, tool schemas, and supported content."""
    messages = [
        {"role": "system", "content": "Ты Брачо."},
        {
            "role": "user",
            "content": [{"type": "text", "text": "Свет"}, {"type": "image_url"}],
        },
    ]
    prompt = bridge.build_prompt(messages, TOOLS)
    assert "=== SYSTEM ===\nТы Брачо." in prompt  # noqa: RUF001 - Cyrillic text.
    assert "- ha_call: Call a HA service" in prompt
    assert "```tool_calls" in prompt
    assert prompt.rstrip().endswith(
        "image_url omitted: the web bridge sends text only]"
    )


def test_delta_prompt_renders_tool_results_only() -> None:
    """Render only new tool results when continuing a conversation."""
    delta = [
        {"role": "tool", "tool_call_id": "call_1", "name": "ha_call", "content": "ok"}
    ]
    prompt = bridge.build_prompt([], TOOLS, delta=delta)
    assert prompt.startswith("=== TOOL RESULT (ha_call call_1) ===\nok")
    assert "SYSTEM" not in prompt


def test_conversation_matching_uses_longest_strict_prefix() -> None:
    """Select the longest stored history strictly extended by the request."""
    a = [bridge.canon({"role": "user", "content": "hi"})]
    b = [*a, bridge.canon({"role": "assistant", "content": "hello"})]
    request = [*b, bridge.canon({"role": "user", "content": "more"})]
    assert bridge.match_conversation([a, b], request) == 1
    assert bridge.match_conversation([b], b) is None  # nothing new to send
    assert bridge.match_conversation([[]], request) is None


def test_canon_ignores_rewritten_tool_call_ids_and_argument_formatting() -> None:
    """Normalize incidental tool identifiers and JSON whitespace."""
    ours = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_abc",
                "type": "function",
                "function": {"name": "ha_call", "arguments": '{"service": "x"}'},
            }
        ],
    }
    echoed = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "toolu_1",
                "type": "function",
                "function": {"name": "ha_call", "arguments": '{"service":"x"}'},
            }
        ],
    }
    assert bridge.canon(ours) == bridge.canon(echoed)


def test_stream_chunks_carry_tool_call_indexes_and_finish_reason() -> None:
    """Include call indexes, completion status, and requested usage."""
    result = {
        "message": {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "ha_call", "arguments": "{}"},
                }
            ],
        },
        "prompt_chars": 400,
        "reply_chars": 40,
    }
    chunks = bridge.stream_chunks(result, "chatgpt-web", include_usage=True)
    assert chunks[0]["choices"][0]["delta"]["tool_calls"][0]["index"] == 0
    assert chunks[1]["choices"][0]["finish_reason"] == "tool_calls"
    assert chunks[2]["usage"]["prompt_tokens"] == EXPECTED_PROMPT_TOKENS


def test_partly_invalid_tool_block_stays_text() -> None:
    """Keep a whole mixed block instead of executing its valid prefix."""
    block = (
        '```tool_calls\n[{"name":"ha_call","arguments":{}},'
        '{"name":"unregistered_tool","arguments":{}}]\n```'
    )
    text, calls = bridge.parse_reply(block, TOOLS)
    assert text == block
    assert calls == []


def test_multiple_tool_blocks_keep_surrounding_text_and_order() -> None:
    """Preserve surrounding prose and the original block order."""
    markdown = (
        'before\n```tool_calls\n[{"name":"ha_call","arguments":{}}]\n```'
        '\nmiddle\n```json\n{"name":"web_search","arguments":{}}\n```\nafter'
    )
    text, calls = bridge.parse_reply(markdown, TOOLS)
    assert text == "before\n\nmiddle\n\nafter"
    assert [call["function"]["name"] for call in calls] == ["ha_call", "web_search"]


def test_assistant_message_keeps_text_and_tool_envelope() -> None:
    """Render canonical assistant text followed by its declared tool calls."""
    rendered = bridge.render_message(
        {
            "role": "assistant",
            "content": "done",
            "tool_calls": [
                {
                    "function": {
                        "name": "ha_call",
                        "arguments": '{"service":"light.turn_on"}',
                    }
                }
            ],
        }
    )
    prefix = "=== ASSISTANT ===\ndone\n```tool_calls\n"
    assert rendered.startswith(prefix)
    payload = rendered.removeprefix(prefix).removesuffix("\n```")
    assert json.loads(payload) == [
        {"name": "ha_call", "arguments": {"service": "light.turn_on"}}
    ]
