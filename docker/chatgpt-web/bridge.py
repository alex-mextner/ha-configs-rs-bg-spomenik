#!/usr/bin/env python3
"""
OpenAI-compatible bridge from OpenClaw to the ChatGPT web UI.

A dedicated Chrome profile runs headed on a private X display. The user signs
in and completes human checks through noVNC. The bridge never reads cookies or
tokens, calls private ChatGPT HTTP APIs, patches or disguises the browser, or
solves challenges. When ChatGPT needs a human, requests fail with 503 and
Home Assistant receives a notification.

Tools are emulated in the prompt. The model answers with one tool_calls JSON
block, which the bridge returns as OpenAI tool_calls. Each conversation stays
in its own temporary browser tab. A request extending a previous answer sends
only the new messages.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import secrets
import signal
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

READY_RELOAD_INTERVAL = 1800
ANSWER_STABLE_INTERVAL = 1.5
ANSWER_START_TIMEOUT = 90
MIN_API_KEY_LENGTH = 24

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from aiohttp import web
    from playwright.async_api import Locator, Page

HOME = "https://chatgpt.com/"
MODEL_ID = "chatgpt-web"

COMPOSER = '#prompt-textarea[contenteditable="true"], textarea#prompt-textarea'
SEND = '[data-testid="send-button"]'
STOP = '[data-testid="stop-button"]'
ASSISTANT = '[data-message-author-role="assistant"]'
COPY = '[data-testid="copy-turn-action-button"]'
CHALLENGE = re.compile(
    r"verify you are human|checking your browser|just a moment|"
    r"подтвердите, что вы человек",
    re.IGNORECASE,
)
LOGIN = re.compile(
    r"^(log in|sign in|sign up for free|войти|зарегистрироваться)$", re.IGNORECASE
)
LIMIT = re.compile(
    r"(?:you.ve (?:hit|reached)|you have reached).{0,80}limit|"
    r"too many requests|limit resets|"
    r"достигнут.{0,40}лимит|лимит.{0,40}достигнут",
    re.IGNORECASE | re.DOTALL,
)

# Captures what ChatGPT's own "copy" button writes (the answer as markdown) without
# touching the
# system clipboard. Only affects pages of this dedicated profile.
COPY_HOOK = (
    "\n"
    "(() => {\n"
    "  if (window.__cgwHooked || !navigator.clipboard) return;\n"
    "  window.__cgwHooked = true;\n"
    "  window.__cgwCopies = [];\n"
    "  const keep = (t) => { window.__cgwCopies.push(String(t)); "
    "window.__cgwCopies.splice(0, window.__cgwCopies.length - 10); };\n"
    "  navigator.clipboard.writeText = async (t) => { keep(t); };\n"
    "  navigator.clipboard.write = async (items) => {\n"
    "    for (const item of items) {\n"
    "      if (item.types.includes('text/plain')) { keep(await (await "
    "item.getType('text/plain')).text()); return; }\n"
    "    }\n"
    "  };\n"
    "})();\n"
)

# Fallback when the copy button is missing: rendered text with code blocks kept
# verbatim.
DOM_MARKDOWN = (
    "\n"
    "(el) => {\n"
    "  const clone = el.cloneNode(true);\n"
    "  clone.querySelectorAll('pre').forEach((pre) => {\n"
    "    const code = pre.querySelector('code');\n"
    "    const lang = code ? ([...code.classList].find((c) => "
    "c.startsWith('language-')) || '').slice(9) : '';\n"
    "    const text = (code || pre).textContent.replace(/\\n$/, '');\n"
    "    pre.replaceWith(document.createTextNode('\\n```' + lang + '\\n' + "
    "text + '\\n```\\n'));\n"
    "  });\n"
    "  clone.style.cssText = 'position:absolute;left:-100000px;top:0;width:90"
    "0px;white-space:normal';\n"
    "  document.body.appendChild(clone);\n"
    "  const text = clone.innerText;\n"
    "  clone.remove();\n"
    "  return text;\n"
    "}\n"
)

log = logging.getLogger("chatgpt-web")


class BridgeError(Exception):
    """Represent an upstream failure with its API status and error category."""

    def __init__(
        self, message: str, status: int = 502, kind: str = "upstream_error"
    ) -> None:
        """Retain the upstream message and its public API metadata."""
        super().__init__(message)
        self.status = status
        self.kind = kind


class NeedsHumanError(BridgeError):
    """Signal that the browser requires the user to complete a manual step."""

    def __init__(self, state: str) -> None:
        """Describe the manual sign-in or challenge required by the browser."""
        what = "a human check" if state == "challenge_required" else "a sign-in"
        super().__init__(
            f"ChatGPT web needs {what}: "
            "open the bridge's noVNC view and complete it yourself.",
            503,
            state,
        )
        self.state = state


NeedsHuman = NeedsHumanError  # Compatibility name for existing importers.


# prompt and reply (pure functions)


def content_text(content: str | list[str | dict] | None) -> str:
    """Extract plain text from a message or its supported content parts."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for part in content:
        if isinstance(part, str):
            parts.append(part)
        elif part.get("type") in ("text", "input_text", "output_text"):
            parts.append(part.get("text", ""))
        else:
            parts.append(
                f"[{part.get('type', 'attachment')} omitted: "
                "the web bridge sends text only]"
            )
    return "\n".join(parts)


def _args(raw: object) -> object:
    if isinstance(raw, str):
        try:
            return json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            return raw
    return raw if raw is not None else {}


def canon(msg: dict) -> str:
    """
    Return a stable identity for a continued conversation message.

    Leave tool-call identifiers out because clients may rewrite them.
    """
    calls = [
        (
            c.get("function", {}).get("name"),
            _args(c.get("function", {}).get("arguments")),
        )
        for c in msg.get("tool_calls") or []
    ]
    return json.dumps(
        [msg.get("role"), content_text(msg.get("content")).strip(), calls],
        ensure_ascii=False,
        sort_keys=True,
    )


def tool_names(tools: list[dict] | None) -> set[str]:
    """Collect the declared function names available to the prompt."""
    return {
        t.get("function", {}).get("name")
        for t in tools or []
        if t.get("type", "function") == "function"
    } - {None}


def render_tools(tools: list[dict] | None) -> str:
    """Render tool names, descriptions, and JSON parameter schemas."""
    lines = []
    for tool in tools or []:
        fn = tool.get("function", {})
        if not fn.get("name"):
            continue
        schema = json.dumps(
            fn.get("parameters") or {"type": "object", "properties": {}},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        lines.append(
            f"- {fn['name']}: {(fn.get('description') or '').strip()}\n"
            f"  parameters: {schema}"
        )
    return "\n".join(lines)


TOOL_RULES = (
    "To call tools, reply with ONLY one fenced code block tagged tool_calls "
    "that holds a JSON array:\n"
    "```tool_calls\n"
    '[{"name": "tool_name", "arguments": {"param": "value"}}]\n'
    "```\n"
    "Several independent calls may go in the same array. Do not write "
    "anything outside that block when calling tools, never invent tool "
    "results, and wait for the TOOL RESULT messages. When no tool is needed, "
    "answer normally in plain text."
)


def _render_assistant(msg: dict, text: str) -> str:
    """Render assistant text and its existing tool-call envelope."""
    body = [text] if text else []
    calls = [
        {
            "name": c.get("function", {}).get("name"),
            "arguments": _args(c.get("function", {}).get("arguments")),
        }
        for c in msg.get("tool_calls") or []
    ]
    if calls:
        body.append("```tool_calls\n" + json.dumps(calls, ensure_ascii=False) + "\n```")
    return "=== ASSISTANT ===\n" + "\n".join(body)


def render_message(msg: dict) -> str:
    """Render one conversation message for the existing prompt protocol."""
    role = msg.get("role")
    text = content_text(msg.get("content")).strip()
    if role == "tool":
        label = " ".join(x for x in (msg.get("name"), msg.get("tool_call_id")) if x)
        return f"=== TOOL RESULT{' (' + label + ')' if label else ''} ===\n{text}"
    if role == "assistant":
        return _render_assistant(msg, text)
    return f"=== {'USER' if role == 'user' else str(role).upper()} ===\n{text}"


def build_prompt(
    messages: list[dict], tools: list[dict] | None, delta: list[dict] | None = None
) -> str:
    """Full prompt for a new chat, or only the new messages when continuing one."""
    if delta is not None:
        body = "\n\n".join(render_message(m) for m in delta)
        hint = " Call tools only with a tool_calls block." if tools else ""
        return (
            f"{body}\n\n(Continue as the assistant: reply to the latest message.{hint})"
        )
    system = "\n\n".join(
        content_text(m.get("content")).strip()
        for m in messages
        if m.get("role") in ("system", "developer")
    )
    rest = [m for m in messages if m.get("role") not in ("system", "developer")]
    out = [
        "You are the language model behind an assistant API. "
        "Follow the SYSTEM instructions below, treat the conversation as "
        "the real dialogue so far and reply to its latest message as the assistant. "
        "Reply in the language the user writes in."
    ]
    if system:
        out.append("=== SYSTEM ===\n" + system)
    if tools:
        out.append(
            "=== TOOLS ===\n"
            + TOOL_RULES
            + "\n\nAvailable tools:\n"
            + render_tools(tools)
        )
    out.extend(render_message(m) for m in rest)
    return "\n\n".join(out)


FENCE = re.compile(r"```[ \t]*([\w-]*)[ \t]*\n(.*?)\n?```", re.DOTALL)


def _tool_block_calls(body: str, names: set[str]) -> list[tuple[str, object]]:
    """Accept a block only when every item names a declared tool."""
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return []
    items = data if isinstance(data, list) else [data]
    found = []
    for item in items:
        if not isinstance(item, dict):
            return []
        name = (
            item.get("name")
            or item.get("tool")
            or (item.get("function") or {}).get("name")
        )
        args = item.get(
            "arguments",
            item.get("args", (item.get("function") or {}).get("arguments", {})),
        )
        if name not in names:
            return []
        found.append((name, _args(args)))
    return found


def parse_reply(markdown: str, tools: list[dict] | None) -> tuple[str, list[dict]]:
    """Separate plain text and calls to declared tools."""
    names = tool_names(tools)
    if not names:
        return markdown.strip(), []
    calls, spans = [], []
    for match in FENCE.finditer(markdown):
        lang, body = match.group(1).lower(), match.group(2).strip()
        if lang not in ("tool_calls", "tool_call", "json", ""):
            continue
        found = _tool_block_calls(body, names)
        if found:
            calls.extend(found)
            spans.append(match.span())
    text = markdown
    for start, end in reversed(spans):
        text = text[:start] + text[end:]
    tool_calls = [
        {
            "id": "call_" + secrets.token_hex(8),
            "type": "function",
            "function": {
                "name": name,
                "arguments": json.dumps(
                    args if isinstance(args, dict) else {"input": args},
                    ensure_ascii=False,
                ),
            },
        }
        for name, args in calls
    ]
    return text.strip(), tool_calls


def _reply_message(markdown: str, tools: list[dict] | None) -> tuple[dict, list[dict]]:
    """Build the same assistant reply stored in conversation history."""
    text, calls = parse_reply(markdown, tools)
    reply = {"role": "assistant", "content": text or None}
    if calls:
        reply["tool_calls"] = calls
    return reply, calls


def match_conversation(histories: list[list[str]], request: list[str]) -> int | None:
    """Find the longest stored history strictly extended by the request."""
    best, best_len = None, -1
    for i, history in enumerate(histories):
        if (
            history
            and len(history) < len(request)
            and request[: len(history)] == history
            and len(history) > best_len
        ):
            best, best_len = i, len(history)
    return best


# ---------------------------------------------------------------- browser side


@dataclass
class Conversation:
    """Keep a browser tab, its canonical history, and its last-use time."""

    page: Page
    history: list[str] = field(default_factory=list)
    used: float = field(default_factory=time.monotonic)


async def visible(locator: Locator) -> bool:
    """Check whether any matching locator is currently visible."""
    try:
        for i in range(await locator.count()):
            if await locator.nth(i).is_visible():
                return True
    except Exception:
        log.exception("Page visibility probe failed")
        return False
    return False


async def readiness(page: Page) -> str:
    """Report the page's observed sign-in, challenge, or composer state."""
    try:
        title = await page.title()
    except Exception:
        log.exception("Page title is not yet readable")
        return "loading"
    if CHALLENGE.search(title) or await visible(page.get_by_text(CHALLENGE)):
        return "challenge_required"
    if not page.url.startswith(HOME):
        return "signin_required"
    if await visible(page.get_by_role("button", name=LOGIN)) or await visible(
        page.get_by_role("link", name=LOGIN)
    ):
        return "signin_required"
    if await visible(page.locator(COMPOSER)):
        return "ready"
    return "loading"


@dataclass(frozen=True)
class BrowserSettings:
    """Group the configured browser session settings."""

    profile: str
    max_tabs: int
    reply_timeout: float
    temporary: bool
    model_slug: str


class Browser:
    """Manage dedicated ChatGPT tabs and serialize requests through them."""

    def __init__(self, settings: BrowserSettings, notifier: Notifier) -> None:
        """Store browser settings and initialize request ownership state."""
        self.profile, self.max_tabs, self.reply_timeout = (
            settings.profile,
            settings.max_tabs,
            settings.reply_timeout,
        )
        self.temporary, self.model_slug = settings.temporary, settings.model_slug
        self.notifier = notifier
        self.lock = asyncio.Lock()
        self.convs: list[Conversation] = []
        self.state = "starting"
        self.status_page = None

    async def start(self) -> None:
        """Open the dedicated persistent browser context and status page."""
        # Pure helpers remain importable without the optional browser runtime.
        from playwright.async_api import async_playwright  # noqa: PLC0415

        self.pw = await async_playwright().start()
        self.ctx = await self.pw.chromium.launch_persistent_context(
            self.profile,
            channel="chrome",
            headless=False,
            viewport=None,
            accept_downloads=False,
            args=["--window-size=1280,900", "--window-position=0,0", "--no-first-run"],
        )
        await self.ctx.add_init_script(COPY_HOOK)
        self.status_page = (
            self.ctx.pages[0] if self.ctx.pages else await self.ctx.new_page()
        )
        await self.status_page.goto(HOME, wait_until="domcontentloaded", timeout=60000)
        await self.refresh_state()

    async def refresh_state(self, *, reload: bool = False) -> str:
        """Read the status page, optionally reloading it first."""
        page = self.status_page
        if reload:
            await page.reload(wait_until="domcontentloaded", timeout=60000)
        state = "loading"
        for _ in range(40):
            state = await readiness(page)
            if state != "loading":
                await self.set_state(state)
                break
            await asyncio.sleep(0.5)
        return state

    async def set_state(self, state: str) -> None:
        """Publish a changed browser state through the configured notifier."""
        if state == self.state:
            return
        log.info("state %s -> %s", self.state, state)
        self.state = state
        await self.notifier.state_changed(state)

    async def monitor(self) -> None:
        """Watch sign-in state and reload ready pages every thirty minutes."""
        last_reload = time.monotonic()
        while True:
            await asyncio.sleep(15 if self.state != "ready" else 60)
            if self.lock.locked():
                continue
            try:
                async with self.lock:
                    reload = (
                        self.state == "ready"
                        and time.monotonic() - last_reload > READY_RELOAD_INTERVAL
                    )
                    if reload:
                        last_reload = time.monotonic()
                    await self.refresh_state(reload=reload)
            except Exception:
                log.exception("Browser state monitor failed")

    def chat_url(self) -> str:
        """Build the chat URL using the configured model and temporary mode."""
        params = []
        if self.temporary:
            params.append("temporary-chat=true")
        if self.model_slug:
            params.append("model=" + self.model_slug)
        return HOME + ("?" + "&".join(params) if params else "")

    async def new_conversation(self) -> Conversation:
        """Open a ready conversation, evicting the oldest tab if needed."""
        live = [c for c in self.convs if not c.page.is_closed()]
        while len(live) >= self.max_tabs:
            oldest = min(live, key=lambda c: c.used)
            live.remove(oldest)
            await oldest.page.close()
        page = await self.ctx.new_page()
        await page.goto(self.chat_url(), wait_until="domcontentloaded", timeout=60000)
        for _ in range(60):
            state = await readiness(page)
            if state == "ready":
                break
            if state in ("challenge_required", "signin_required"):
                await page.close()
                await self.set_state(state)
                raise NeedsHuman(state)
            await asyncio.sleep(0.5)
        else:
            await page.close()
            error_message = "ChatGPT page did not become ready (composer not found)."
            raise BridgeError(error_message)
        conv = Conversation(page)
        self.convs = [*live, conv]
        return conv

    async def _check_reply_state(self, page: Page) -> None:
        """Check sign-in and usage limits before observing an answer."""
        state = await readiness(page)
        if state in ("challenge_required", "signin_required"):
            await self.set_state(state)
            raise NeedsHuman(state)
        alerts = page.get_by_role("alert")
        for i in range(min(await alerts.count(), 4)):
            text = await alerts.nth(i).inner_text()
            if LIMIT.search(text):
                raise BridgeError(
                    "ChatGPT usage limit reached: " + text.strip()[:300],
                    429,
                    "rate_limited",
                )

    async def ask(self, page: Page, prompt: str) -> str:
        """Submit the prompt and wait for a stable completed answer."""
        answers = page.locator(ASSISTANT)
        before = await answers.count()
        composer = page.locator(COMPOSER)
        if await composer.count() != 1:
            error_message = "ChatGPT composer changed; refusing blind input."
            raise BridgeError(error_message)
        await composer.click()
        await composer.fill(prompt)
        send = page.locator(SEND)
        await send.wait_for(state="visible", timeout=15000)
        await send.click()
        deadline = time.monotonic() + self.reply_timeout
        started = time.monotonic()
        last, stable_since = None, 0.0
        while time.monotonic() < deadline:
            await self._check_reply_state(page)
            if await answers.count() > before:
                text = await answers.last.inner_text()
                if await visible(page.locator(STOP)) or not text.strip():
                    last, stable_since = None, 0.0
                elif text != last:
                    last, stable_since = text, time.monotonic()
                elif time.monotonic() - stable_since > ANSWER_STABLE_INTERVAL:
                    break
            elif (
                time.monotonic() - started > ANSWER_START_TIMEOUT
                and not await visible(page.locator(STOP))
            ):
                error_message = (
                    "ChatGPT did not start answering; inspect the chat in noVNC."
                )
                raise BridgeError(error_message)
            await asyncio.sleep(0.5)
        else:
            error_message = (
                f"No complete ChatGPT answer within {self.reply_timeout:.0f} s."
            )
            raise BridgeError(
                error_message,
                504,
                "timeout",
            )
        return await self.answer_markdown(page, answers.last)

    async def answer_markdown(self, page: Page, answer: Locator) -> str:
        """Read the answer through its copy button or rendered text."""
        try:
            await page.evaluate("window.__cgwCopies = []")
            button = answer.locator("xpath=ancestor::article[1]").locator(COPY)
            if await button.count() == 0:
                button = page.locator(COPY)
            if await button.count():
                await answer.hover()
                await button.last.click(timeout=5000)
                for _ in range(30):
                    copies = await page.evaluate("window.__cgwCopies || []")
                    if copies and copies[-1].strip():
                        return copies[-1]
                    await asyncio.sleep(0.1)
        except Exception:
            log.exception("Copy button unavailable; using the rendered text")
        return await answer.evaluate(DOM_MARKDOWN)

    async def complete(self, messages: list[dict], tools: list[dict] | None) -> dict:
        """Continue or create a conversation and return its parsed answer."""
        rest = [m for m in messages if m.get("role") not in ("system", "developer")]
        if not rest:
            error_message = "No user message to answer."
            raise BridgeError(error_message, 400, "invalid_request_error")
        request = [canon(m) for m in rest]
        if self.state in ("signin_required", "challenge_required"):
            # Fail fast without opening tabs over the window the user is signing in
            # with.
            raise NeedsHuman(self.state)
        async with self.lock:
            self.convs = [c for c in self.convs if not c.page.is_closed()]
            index = match_conversation([c.history for c in self.convs], request)
            if index is not None:
                conv = self.convs[index]
                prompt = build_prompt(messages, tools, delta=rest[len(conv.history) :])
            else:
                conv = await self.new_conversation()
                prompt = build_prompt(messages, tools)
            conv.used = time.monotonic()
            started = time.monotonic()
            try:
                markdown = await self.ask(conv.page, prompt)
            except BaseException:
                # The tab's state is unknown now; never continue it.
                conv.history = []
                raise
            reply, calls = _reply_message(markdown, tools)
            conv.history = [*request, canon(reply)]
            await self.set_state("ready")
            log.info(
                "answered in %.1f s (%s, prompt %d chars, "
                "reply %d chars, %d tool calls)",
                time.monotonic() - started,
                "continued" if index is not None else "new chat",
                len(prompt),
                len(markdown),
                len(calls),
            )
            return {
                "message": reply,
                "prompt_chars": len(prompt),
                "reply_chars": len(markdown),
            }


# Home Assistant notification


class Notifier:
    """Send existing browser-state notifications through Home Assistant."""

    NID = "chatgpt_web_bridge"

    def __init__(self, ha_url: str, ha_token: str) -> None:
        """Store the configured Home Assistant endpoint and credentials."""
        self.ha_url, self.ha_token = ha_url.rstrip("/"), ha_token

    async def call(self, service: str, payload: dict) -> None:
        """Send a notification service request when credentials are configured."""
        if not self.ha_url or not self.ha_token:
            return
        import aiohttp  # noqa: PLC0415 - Optional notification runtime; pure helpers import without aiohttp.

        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            ) as session:
                await session.post(
                    f"{self.ha_url}/api/services/{service}",
                    json=payload,
                    headers={"Authorization": f"Bearer {self.ha_token}"},
                )
        except Exception:
            log.exception("HA notification failed")

    async def state_changed(self, state: str) -> None:
        """Create or dismiss notifications for the observed browser state."""
        if state in ("signin_required", "challenge_required"):
            what = (
                "войти в ChatGPT"
                if state == "signin_required"
                else "пройти проверку «я человек»"
            )
            message = (
                f"OpenClaw не может ответить через chatgpt.com: нужно {what}.\n\n"
                "С компьютера: `ssh -N -L 6080:127.0.0.1:6080 root@home`, "  # noqa: RUF001 - Cyrillic text.
                "затем открыть "
                "http://localhost:6080/vnc.html и сделать это в открытом окне Chrome."
            )
            await self.call(
                "persistent_notification/create",
                {
                    "notification_id": self.NID,
                    "title": "ChatGPT web: нужен человек",
                    "message": message,
                },
            )
            await self.call(
                "notify/notify",
                {"title": "ChatGPT web: нужен человек", "message": message},
            )
        elif state == "ready":
            await self.call(
                "persistent_notification/dismiss", {"notification_id": self.NID}
            )


# OpenAI-compatible HTTP API


def completion_payload(result: dict, model: str) -> dict:
    """Wrap an answer in the OpenAI-compatible completion response."""
    message = result["message"]
    return {
        "id": "chatcmpl-" + secrets.token_hex(12),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if message.get("tool_calls") else "stop",
            }
        ],
        "usage": usage(result),
    }


def usage(result: dict) -> dict:
    """Estimate token counts from the recorded prompt and answer lengths."""
    prompt, completion = result["prompt_chars"] // 4, result["reply_chars"] // 4
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }


def stream_chunks(result: dict, model: str, *, include_usage: bool) -> list[dict]:
    """Wrap an answer in the existing completion chunk sequence."""
    base = {
        "id": "chatcmpl-" + secrets.token_hex(12),
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
    }
    message = result["message"]
    delta = {"role": "assistant"}
    if message.get("content"):
        delta["content"] = message["content"]
    if message.get("tool_calls"):
        delta["tool_calls"] = [
            dict(call, index=i) for i, call in enumerate(message["tool_calls"])
        ]
    chunks = [
        dict(base, choices=[{"index": 0, "delta": delta, "finish_reason": None}]),
        dict(
            base,
            choices=[
                {
                    "index": 0,
                    "delta": {},
                    "finish_reason": "tool_calls"
                    if message.get("tool_calls")
                    else "stop",
                }
            ],
        ),
    ]
    if include_usage:
        chunks.append(dict(base, choices=[], usage=usage(result)))
    return chunks


async def run(browser: Browser, messages: list[dict], tools: list[dict] | None) -> dict:
    """Translate unexpected browser failures into the public bridge error."""
    try:
        return await browser.complete(messages, tools)
    except BridgeError:
        raise
    except Exception as exc:
        log.exception("completion failed")
        error_message = f"Browser error: {str(exc)[:300]}"
        raise BridgeError(error_message) from None


def make_app(browser: Browser, api_key: str) -> web.Application:
    """Create the authenticated completion API and its health endpoints."""
    # Load the optional HTTP runtime only when constructing the app.
    from aiohttp import web  # noqa: PLC0415

    def error(status: int, message: str, kind: str) -> web.Response:
        return web.json_response(
            {"error": {"message": message, "type": kind}}, status=status
        )

    @web.middleware
    async def auth(
        request: web.Request,
        handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
    ) -> web.StreamResponse:
        if request.path.startswith("/v1/"):
            given = (
                request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            )
            if not hmac.compare_digest(given.encode(), api_key.encode()):
                return error(401, "Invalid API key.", "invalid_request_error")
        return await handler(request)

    async def health(_request: web.Request) -> web.Response:
        return web.json_response(
            {
                "state": browser.state,
                "tabs": len(browser.convs),
                "busy": browser.lock.locked(),
            }
        )

    async def models(_request: web.Request) -> web.Response:
        return web.json_response(
            {
                "object": "list",
                "data": [
                    {
                        "id": MODEL_ID,
                        "object": "model",
                        "created": 0,
                        "owned_by": "chatgpt-web",
                    }
                ],
            }
        )

    async def stream_response(
        request: web.Request, task: asyncio.Task[dict], model: str, body: dict
    ) -> web.StreamResponse:
        """Write the existing wait, error, result and DONE streaming protocol."""
        response = web.StreamResponse(
            headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"}
        )
        await response.prepare(request)
        while True:
            try:
                result = await asyncio.wait_for(asyncio.shield(task), 10)
                break
            except TimeoutError:
                await response.write(b": waiting for chatgpt.com\n\n")
            except BridgeError as exc:
                payload = {
                    "error": {"message": str(exc), "type": exc.kind, "code": exc.status}
                }
                await response.write(
                    f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()
                )
                await response.write(b"data: [DONE]\n\n")
                return response
        include_usage = bool((body.get("stream_options") or {}).get("include_usage"))
        for chunk in stream_chunks(result, model, include_usage=include_usage):
            await response.write(
                f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()
            )
        await response.write(b"data: [DONE]\n\n")
        return response

    async def chat(request: web.Request) -> web.StreamResponse:
        try:
            body = await request.json()
        except Exception:
            log.exception("Request JSON could not be decoded")
            return error(400, "Body must be JSON.", "invalid_request_error")
        messages, tools = body.get("messages") or [], body.get("tools") or []
        if body.get("tool_choice") == "none":
            tools = []
        model = body.get("model") or MODEL_ID
        task = asyncio.create_task(run(browser, messages, tools))
        # Quick failures (sign-in needed, limits) get a real HTTP status, also for
        # streaming clients.
        done, _ = await asyncio.wait({task}, timeout=8)
        if done and task.exception():
            exc = task.exception()
            return error(exc.status, str(exc), exc.kind)
        if not body.get("stream"):
            try:
                result = await task
            except BridgeError as exc:
                return error(exc.status, str(exc), exc.kind)
            return web.json_response(completion_payload(result, model))
        return await stream_response(request, task, model, body)

    app = web.Application(middlewares=[auth], client_max_size=16 * 1024 * 1024)
    app.add_routes(
        [
            web.get("/health", health),
            web.get("/v1/models", models),
            web.post("/v1/chat/completions", chat),
        ]
    )
    return app


async def main() -> None:
    """Run the configured browser bridge until a termination signal arrives."""
    # Pure helpers remain importable without the optional server runtime.
    from aiohttp import web  # noqa: PLC0415

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    api_key = os.environ.get("CHATGPT_WEB_API_KEY", "")
    if len(api_key) < MIN_API_KEY_LENGTH:
        error_message = "CHATGPT_WEB_API_KEY must be set (24+ characters)."
        raise SystemExit(error_message)
    notifier = Notifier(os.environ.get("HA_URL", ""), os.environ.get("HA_TOKEN", ""))
    settings = BrowserSettings(
        profile=os.environ.get("CHATGPT_WEB_PROFILE", "/data/profile"),
        max_tabs=int(os.environ.get("CHATGPT_WEB_MAX_TABS", "3")),
        reply_timeout=float(os.environ.get("CHATGPT_WEB_REPLY_TIMEOUT", "300")),
        temporary=os.environ.get("CHATGPT_WEB_TEMPORARY", "1") == "1",
        model_slug=os.environ.get("CHATGPT_WEB_MODEL_SLUG", ""),
    )
    browser = Browser(settings, notifier)
    await browser.start()
    monitor_task = asyncio.create_task(browser.monitor())
    runner = web.AppRunner(make_app(browser, api_key))
    await runner.setup()
    await web.TCPSite(
        runner,
        "0.0.0.0",  # noqa: S104 - Dedicated Compose network; API port is not published.
        int(os.environ.get("CHATGPT_WEB_PORT", "8787")),
    ).start()
    log.info("listening; state=%s", browser.state)
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    await stop.wait()
    monitor_task.cancel()
    await asyncio.gather(monitor_task, return_exceptions=True)
    # Close Chrome cleanly so the profile (session cookies) is flushed to disk.
    await runner.cleanup()
    await browser.ctx.close()
    await browser.pw.stop()
    log.info("stopped")


if __name__ == "__main__":
    asyncio.run(main())
