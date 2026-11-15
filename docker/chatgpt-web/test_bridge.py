"""Offline tests for the prompt/reply protocol of bridge.py (no browser)."""

import asyncio
import json
from http import HTTPStatus
from unittest.mock import AsyncMock

import bridge
import pytest
from aiohttp.test_utils import TestClient, TestServer

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
    """Keep fenced text unchanged when the request declares no tools."""
    text, calls = bridge.parse_reply('Привет! ```json\n{"name": "ha_call"}\n```', [])
    assert calls == []
    assert text.startswith("Привет")


def test_tool_calls_block_becomes_openai_calls() -> None:
    """Convert known tools into ordered calls while retaining surrounding text."""
    md = (
        "Сейчас включу.\n```tool_calls\n"
        '[{"name": "ha_call", "arguments": {"service": "light.turn_on"}},'
        ' {"name": "web_search", "arguments": {}}]\n```'
    )
    text, calls = bridge.parse_reply(md, TOOLS)
    assert text == "Сейчас включу."
    assert [c["function"]["name"] for c in calls] == ["ha_call", "web_search"]
    assert json.loads(calls[0]["function"]["arguments"]) == {"service": "light.turn_on"}
    assert all(c["id"].startswith("call_") and c["type"] == "function" for c in calls)


def test_json_block_with_unknown_tool_stays_text() -> None:
    """Leave examples of undeclared tools in the ordinary answer text."""
    md = 'Пример:\n```json\n{"name": "rm_rf", "arguments": {}}\n```'
    text, calls = bridge.parse_reply(md, TOOLS)
    assert calls == []
    assert "rm_rf" in text


def test_single_object_and_string_arguments() -> None:
    """Accept a single declared tool and its JSON-encoded arguments."""
    md = '```json\n{"name": "ha_call", "arguments": "{\\"service\\": \\"x\\"}"}\n```'
    _, calls = bridge.parse_reply(md, TOOLS)
    assert json.loads(calls[0]["function"]["arguments"]) == {"service": "x"}


def test_full_prompt_has_system_tools_and_dialogue() -> None:
    """Retain the system prompt, schemas, text, and attachment placeholders."""
    messages = [
        {"role": "system", "content": "Ты Брачо."},
        {
            "role": "user",
            "content": [{"type": "text", "text": "Свет"}, {"type": "image_url"}],
        },
    ]
    prompt = bridge.build_prompt(messages, TOOLS)
    # The Russian system text must survive the prompt rendering exactly.
    assert "=== SYSTEM ===\nТы Брачо." in prompt  # noqa: RUF001
    assert "- ha_call: Call a HA service" in prompt
    assert "```tool_calls" in prompt
    assert prompt.rstrip().endswith(
        "image_url omitted: the web bridge sends text only]"
    )


def test_delta_prompt_renders_tool_results_only() -> None:
    """Send only the new tool-result continuation for an existing chat."""
    delta = [
        {"role": "tool", "tool_call_id": "call_1", "name": "ha_call", "content": "ok"}
    ]
    prompt = bridge.build_prompt([], TOOLS, delta=delta)
    assert prompt.startswith("=== TOOL RESULT (ha_call call_1) ===\nok")
    assert "SYSTEM" not in prompt


def test_conversation_matching_uses_longest_strict_prefix() -> None:
    """Reuse the most specific completed prefix only when new messages exist."""
    a = [bridge.canon({"role": "user", "content": "hi"})]
    b = [*a, bridge.canon({"role": "assistant", "content": "hello"})]
    request = [*b, bridge.canon({"role": "user", "content": "more"})]
    assert bridge.match_conversation([a, b], request) == 1
    assert bridge.match_conversation([b], b) is None  # nothing new to send
    assert bridge.match_conversation([[]], request) is None


def test_canon_ignores_rewritten_tool_call_ids_and_argument_formatting() -> None:
    """Compare tool semantics independently of IDs and JSON whitespace."""
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
    """Preserve indexed tool calls, finish reason, and the usage chunk."""
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
    assert chunks[2]["usage"]["prompt_tokens"] == result["prompt_chars"] // 4


@pytest.mark.parametrize(
    "tail",
    [None, {"name": "unknown", "arguments": {}}],
)
def test_tool_block_is_consumed_only_when_every_item_is_known(tail: object) -> None:
    """Reject an entire mixed block without leaking its earlier valid calls."""
    block = [{"name": "ha_call", "arguments": {"service": "x"}}, tail]
    markdown = "before\n```tool_calls\n" + json.dumps(block) + "\n```\nafter"
    text, calls = bridge.parse_reply(markdown, TOOLS)
    assert text == markdown
    assert calls == []


def test_valid_fence_does_not_consume_adjacent_unknown_example() -> None:
    """Keep ordinary examples while removing only a valid tool-call fence."""
    example = '```json\n{"name":"unknown"}\n```'
    valid = '```tool_call\n{"tool":"ha_call","args":{"service":"x"}}\n```'
    text, calls = bridge.parse_reply(example + "\n" + valid, TOOLS)
    assert text == example
    assert [call["function"]["name"] for call in calls] == ["ha_call"]
    assert json.loads(calls[0]["function"]["arguments"]) == {"service": "x"}


def test_assistant_message_keeps_text_and_tool_call_format() -> None:
    """Keep assistant text and the established JSON tool-call continuation."""
    message = {
        "role": "assistant",
        "content": "Ready",
        "tool_calls": [
            {"function": {"name": "ha_call", "arguments": '{"service":"x"}'}}
        ],
    }
    assert bridge.render_message(message) == (
        "=== ASSISTANT ===\nReady\n```tool_calls\n"
        '[{"name": "ha_call", "arguments": {"service": "x"}}]\n```'
    )
    assert bridge.render_message({"role": "assistant", "content": None}) == (
        "=== ASSISTANT ===\n"
    )


@pytest.mark.parametrize("stream", [False, True])
def test_http_completion_preserves_response_and_disables_unselected_tools(
    monkeypatch: pytest.MonkeyPatch, *, stream: bool
) -> None:
    """Exercise real local HTTP serialization without starting a browser."""
    result = {
        "message": {"role": "assistant", "content": "reply"},
        "prompt_chars": 40,
        "reply_chars": 20,
    }
    browser = _offline_browser()
    complete = AsyncMock(return_value=result)
    monkeypatch.setattr(browser, "complete", complete)
    credential = "offline-test-credential-for-local-app"

    async def check_response() -> None:
        async with TestClient(
            TestServer(bridge.make_app(browser, credential))
        ) as client:
            response = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer " + credential},
                json={
                    "messages": [{"role": "user", "content": "hello"}],
                    "tools": TOOLS,
                    "tool_choice": "none",
                    "stream": stream,
                    "stream_options": {"include_usage": True},
                },
            )
            assert response.status == HTTPStatus.OK
            if stream:
                body = await response.text()
                data = [
                    line[6:] for line in body.splitlines() if line.startswith("data: ")
                ]
                assert data[-1] == "[DONE]"
                chunks = [json.loads(line) for line in data[:-1]]
                assert chunks[0]["choices"][0]["delta"]["content"] == "reply"
                assert chunks[1]["choices"][0]["finish_reason"] == "stop"
                assert chunks[2]["usage"] == bridge.usage(result)
            else:
                body = await response.json()
                assert body["choices"][0]["message"] == result["message"]
                assert body["usage"] == bridge.usage(result)

    asyncio.run(check_response())
    complete.assert_awaited_once_with([{"role": "user", "content": "hello"}], [])


@pytest.mark.parametrize("stream", [False, True])
def test_http_human_handoff_remains_an_error_before_streaming(
    monkeypatch: pytest.MonkeyPatch, *, stream: bool
) -> None:
    """Return a real HTTP error before emitting any SSE for a human handoff."""
    browser = _offline_browser()
    failure = bridge.NeedsHuman("signin_required")
    monkeypatch.setattr(browser, "complete", AsyncMock(side_effect=failure))
    credential = "offline-test-credential-for-local-app"

    async def check_response() -> None:
        async with TestClient(
            TestServer(bridge.make_app(browser, credential))
        ) as client:
            response = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer " + credential},
                json={
                    "messages": [{"role": "user", "content": "hello"}],
                    "stream": stream,
                },
            )
            assert response.status == HTTPStatus.SERVICE_UNAVAILABLE
            assert response.content_type == "application/json"
            assert await response.json() == {
                "error": {"message": str(failure), "type": "signin_required"}
            }

    asyncio.run(check_response())


def _offline_browser() -> bridge.Browser:
    settings = bridge.BrowserSettings(
        profile="/unused-offline-profile",
        max_tabs=1,
        reply_timeout=1.0,
        temporary=True,
        model_slug="",
    )
    return bridge.Browser(settings, bridge.Notifier("", ""))
