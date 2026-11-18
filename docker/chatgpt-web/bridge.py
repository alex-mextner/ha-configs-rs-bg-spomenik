#!/usr/bin/env python3
# noqa: EXE001 - The Docker entrypoint runs this unchanged 0644 module with Python.
"""
OpenAI-compatible bridge from OpenClaw to the ChatGPT web UI.

A dedicated Chrome profile runs headed on a private X display. The user signs in
and completes any human check themselves through noVNC. The bridge never reads
cookies or tokens, never calls private ChatGPT HTTP APIs, never patches or
disguises the browser and never solves challenges: when ChatGPT wants a human,
requests fail with 503 and Home Assistant gets a notification.

Tools are emulated in the prompt. The model answers with one ``tool_calls`` JSON
block, which the bridge returns as OpenAI ``tool_calls``. A conversation stays in
its own browser tab (temporary chat), so follow-up requests whose history extends
a previous answer only send the new messages.
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
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from aiohttp import web
    from playwright.async_api import Locator, Page

HOME = "https://chatgpt.com/"
MODEL_ID = "chatgpt-web"
STATE_RELOAD_SECONDS = 1800
ANSWER_STABLE_SECONDS = 1.5
ANSWER_START_TIMEOUT_SECONDS = 90
MIN_API_KEY_LENGTH = 24

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

# Capture the visible copy-button markdown without touching the system clipboard.
# This only affects pages of the dedicated profile.
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

# Fallback for a missing copy button: rendered text with verbatim code blocks.
DOM_MARKDOWN = (
    "\n"
    "(el) => {\n"
    "  const clone = el.cloneNode(true);\n"
    "  clone.querySelectorAll('pre').forEach((pre) => {\n"
    "    const code = pre.querySelector('code');\n"
    "    const lang = code ? ([...code.classList].find((c) => "
    "c.startsWith('language-')) || '').slice(9) : '';\n"
    "    const text = (code || pre).textContent.replace(/\\n$/, '');\n"
    "    pre.replaceWith(document.createTextNode('\\n```' + lang + "
    "'\\n' + text + '\\n```\\n'));\n"
    "  });\n"
    "  clone.style.cssText = "
    "'position:absolute;left:-100000px;top:0;width:900px;white-space:normal';\n"
    "  document.body.appendChild(clone);\n"
    "  const text = clone.innerText;\n"
    "  clone.remove();\n"
    "  return text;\n"
    "}\n"
)

log = logging.getLogger("chatgpt-web")


class BridgeError(Exception):
    """Carry the HTTP status and public error kind for a failed completion."""

    def __init__(
        self, message: str, status: int = 502, kind: str = "upstream_error"
    ) -> None:
        """Retain the existing public HTTP error fields."""
        super().__init__(message)
        self.status = status
        self.kind = kind


# This existing public control exception names the required human handoff.
class NeedsHuman(BridgeError):  # noqa: N818
    """Signal that the owner must finish sign-in or a human check."""

    def __init__(self, state: str) -> None:
        """Format the handoff instruction for the current browser state."""
        what = "a human check" if state == "challenge_required" else "a sign-in"
        super().__init__(
            f"ChatGPT web needs {what}: open the bridge's noVNC view "
            "and complete it yourself.",
            503,
            state,
        )
        self.state = state


# prompt and reply (pure functions)


def content_text(content: Any) -> str:
    """Flatten text content, marking unsupported attachments without sending them."""
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


def _args(raw: Any) -> Any:
    if isinstance(raw, str):
        try:
            return json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            return raw
    return raw if raw is not None else {}


def canon(msg: dict) -> str:
    """
    Identify one non-system message when recognising a continued conversation.

    Tool-call ids are left out: clients may rewrite them.
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
    """Return function names that the request explicitly makes available."""
    return {
        t.get("function", {}).get("name")
        for t in tools or []
        if t.get("type", "function") == "function"
    } - {None}


def render_tools(tools: list[dict] | None) -> str:
    """Describe the available tool schemas in the established prompt format."""
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
    "To call tools, reply with ONLY one fenced code block tagged "
    "tool_calls that holds a JSON array:\n"
    "```tool_calls\n"
    '[{"name": "tool_name", "arguments": {"param": "value"}}]\n'
    "```\n"
    "Several independent calls may go in the same array. Do not write "
    "anything outside that block when calling tools, never invent "
    "tool results, and wait for the TOOL RESULT messages. When no "
    "tool is needed, answer normally in plain text."
)


def _render_assistant_message(msg: dict, text: str) -> str:
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
    """Render one message with its role and optional tool-call information."""
    role = msg.get("role")
    text = content_text(msg.get("content")).strip()
    if role == "tool":
        label = " ".join(x for x in (msg.get("name"), msg.get("tool_call_id")) if x)
        return f"=== TOOL RESULT{' (' + label + ')' if label else ''} ===\n{text}"
    if role == "assistant":
        return _render_assistant_message(msg, text)
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
        "Follow the SYSTEM instructions below, "
        "treat the conversation as the real dialogue so far "
        "and reply to its latest message as the assistant. "
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


def _parse_tool_block(body: str, names: set[str]) -> list[tuple[str, Any]]:
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
    """
    Split the answer into plain text and calls to explicitly available tools.

    Only known tool names count as calls.
    """
    names = tool_names(tools)
    if not names:
        return markdown.strip(), []
    calls, spans = [], []
    for match in FENCE.finditer(markdown):
        lang, body = match.group(1).lower(), match.group(2).strip()
        if lang not in ("tool_calls", "tool_call", "json", ""):
            continue
        found = _parse_tool_block(body, names)
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


def _assistant_reply(text: str, calls: list[dict]) -> dict:
    reply = {"role": "assistant", "content": text or None}
    if calls:
        reply["tool_calls"] = calls
    return reply


def match_conversation(histories: list[list[str]], request: list[str]) -> int | None:
    """Find the longest stored history that the request strictly extends."""
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


# browser side


@dataclass
class Conversation:
    """Keep a browser tab and the completed conversation prefix it represents."""

    page: Page
    history: list[str] = field(default_factory=list)
    used: float = field(default_factory=time.monotonic)


async def visible(locator: Locator) -> bool:
    """Check visibility while tolerating navigation and disappearing locators."""
    try:
        for i in range(await locator.count()):
            if await locator.nth(i).is_visible():
                return True
    # Browser navigation can invalidate any locator operation; probe failure is hidden.
    except Exception:  # noqa: BLE001
        return False
    return False


async def readiness(page: Page) -> str:
    """Describe visible readiness without attempting authentication or challenges."""
    try:
        title = await page.title()
    # A title probe during navigation is inconclusive, regardless of browser error type.
    except Exception:  # noqa: BLE001
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


@dataclass(frozen=True, kw_only=True)
class BrowserSettings:
    """Collect the owner-configured limits and profile options for one browser."""

    profile: str
    max_tabs: int
    reply_timeout: float
    temporary: bool
    model_slug: str


class Browser:
    """Serialize chats through the owner's ordinary, visible browser profile."""

    def __init__(self, settings: BrowserSettings, notifier: Notifier) -> None:
        """Retain configured options and initialize unloaded browser state."""
        self.profile = settings.profile
        self.max_tabs = settings.max_tabs
        self.reply_timeout = settings.reply_timeout
        self.temporary = settings.temporary
        self.model_slug = settings.model_slug
        self.notifier = notifier
        self.lock = asyncio.Lock()
        self.convs: list[Conversation] = []
        self.state = "starting"
        self.status_page = None
        self.monitor_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Open the configured visible profile and inspect its initial readiness."""
        # The offline prompt protocol does not require browser dependencies.
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
        """Observe the status page, optionally reloading it before bounded probes."""
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
        """Publish an actual browser-state transition to the configured notifier."""
        if state == self.state:
            return
        log.info("state %s -> %s", self.state, state)
        self.state = state
        await self.notifier.state_changed(state)

    async def monitor(self) -> None:
        """Watch sign-in state and lightly reload an idle ready page every 30 min."""
        last_reload = time.monotonic()
        while True:
            await asyncio.sleep(15 if self.state != "ready" else 60)
            if self.lock.locked():
                continue
            try:
                async with self.lock:
                    reload = (
                        self.state == "ready"
                        and time.monotonic() - last_reload > STATE_RELOAD_SECONDS
                    )
                    if reload:
                        last_reload = time.monotonic()
                    await self.refresh_state(reload=reload)
            # Keep the existing monitor alive after any transient browser failure.
            except Exception as exc:  # noqa: BLE001
                log.warning("monitor: %s", exc)

    def chat_url(self) -> str:
        """Build the normal chat URL from the configured visible UI options."""
        params = []
        if self.temporary:
            params.append("temporary-chat=true")
        if self.model_slug:
            params.append("model=" + self.model_slug)
        return HOME + ("?" + "&".join(params) if params else "")

    async def new_conversation(self) -> Conversation:
        """Create one ready tab, evicting the least recently used tab if needed."""
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

    async def _check_answer_state(self, page: Page) -> None:
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
        """Send text through the visible composer and await a stable answer."""
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
            await self._check_answer_state(page)
            if await answers.count() > before:
                text = await answers.last.inner_text()
                if await visible(page.locator(STOP)) or not text.strip():
                    last, stable_since = None, 0.0
                elif text != last:
                    last, stable_since = text, time.monotonic()
                elif time.monotonic() - stable_since > ANSWER_STABLE_SECONDS:
                    break
            elif (
                time.monotonic() - started > ANSWER_START_TIMEOUT_SECONDS
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
        """Read the visible answer using the copy action or rendered-text fallback."""
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
        # Any unavailable copy operation must preserve the rendered-text fallback.
        except Exception as exc:  # noqa: BLE001
            log.info("copy button unavailable (%s); using the rendered text", exc)
        return await answer.evaluate(DOM_MARKDOWN)

    async def complete(self, messages: list[dict], tools: list[dict] | None) -> dict:
        """Answer a request, reusing only a known strict conversation prefix."""
        rest = [m for m in messages if m.get("role") not in ("system", "developer")]
        if not rest:
            error_message = "No user message to answer."
            raise BridgeError(error_message, 400, "invalid_request_error")
        request = [canon(m) for m in rest]
        if self.state in ("signin_required", "challenge_required"):
            # Leave the sign-in window undisturbed while the user completes it.
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
            text, calls = parse_reply(markdown, tools)
            reply = _assistant_reply(text, calls)
            conv.history = [*request, canon(reply)]
            await self.set_state("ready")
            log.info(
                "answered in %.1f s "
                "(%s, prompt %d chars, reply %d chars, %d tool calls)",
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
    """Report human handoffs through the owner's optional Home Assistant URL."""

    NID = "chatgpt_web_bridge"

    def __init__(self, ha_url: str, ha_token: str) -> None:
        """Retain the configured notification destination without making a request."""
        self.ha_url, self.ha_token = ha_url.rstrip("/"), ha_token

    async def call(self, service: str, payload: dict) -> None:
        """Attempt one notification without disrupting browser state on failure."""
        if not self.ha_url or not self.ha_token:
            return
        # Disabled notifications and offline protocol imports require no HTTP client.
        import aiohttp  # noqa: PLC0415

        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            ) as session:
                await session.post(
                    f"{self.ha_url}/api/services/{service}",
                    json=payload,
                    headers={"Authorization": f"Bearer {self.ha_token}"},
                )
        # Notification failures are isolated from successful browser operations.
        except Exception as exc:  # noqa: BLE001
            log.warning("HA notify failed: %s", exc)

    async def state_changed(self, state: str) -> None:
        """Create a human-action notice or dismiss it when the browser is ready."""
        if state in ("signin_required", "challenge_required"):
            what = (
                "войти в ChatGPT"
                if state == "signin_required"
                else "пройти проверку «я человек»"
            )
            message = (
                f"OpenClaw не может ответить через chatgpt.com: нужно {what}.\n\n"
                # Preserve the existing Russian handoff text and Cyrillic initial.
                "С компьютера: `ssh -N -L 6080:127.0.0.1:6080 root@home`, "  # noqa: RUF001
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
    """Wrap a completed reply in the established non-streaming response shape."""
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
    """Keep the bridge's existing four-characters-per-token usage estimate."""
    prompt, completion = result["prompt_chars"] // 4, result["reply_chars"] // 4
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }


def stream_chunks(result: dict, model: str, *, include_usage: bool) -> list[dict]:
    """Build completion chunks, preserving indexed tool calls and usage order."""
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
    """Translate unexpected browser errors into the existing public error shape."""
    try:
        return await browser.complete(messages, tools)
    except BridgeError:
        raise
    except Exception as exc:
        log.exception("completion failed")
        error_message = f"Browser error: {str(exc)[:300]}"
        raise BridgeError(error_message) from None


def make_app(browser: Browser, api_key: str) -> web.Application:
    """Construct authenticated completion endpoints and the public health probe."""
    # Keep optional HTTP dependencies out of offline prompt-protocol imports.
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

    async def stream_completion(
        request: web.Request, task: asyncio.Task[dict], body: dict, model: str
    ) -> web.StreamResponse:
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
        # Preserve the public bad-body response for decoding and body-read failures.
        except Exception:  # noqa: BLE001
            return error(400, "Body must be JSON.", "invalid_request_error")
        messages, tools = body.get("messages") or [], body.get("tools") or []
        if body.get("tool_choice") == "none":
            tools = []
        model = body.get("model") or MODEL_ID
        task = asyncio.create_task(run(browser, messages, tools))
        # Quick failures retain a real HTTP status for streaming clients too.
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
        return await stream_completion(request, task, body, model)

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
    """Run the configured bridge until a normal termination signal arrives."""
    # Keep optional HTTP dependencies out of offline prompt-protocol imports.
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
    browser.monitor_task = asyncio.create_task(browser.monitor())
    runner = web.AppRunner(make_app(browser, api_key))
    await runner.setup()
    await web.TCPSite(
        # Keep the existing container port binding; /v1 endpoints require the API key.
        runner,
        "0.0.0.0",  # noqa: S104
        int(os.environ.get("CHATGPT_WEB_PORT", "8787")),
    ).start()
    log.info("listening; state=%s", browser.state)
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    await stop.wait()
    # Close Chrome cleanly so the profile (session cookies) is flushed to disk.
    await runner.cleanup()
    await browser.ctx.close()
    await browser.pw.stop()
    log.info("stopped")


if __name__ == "__main__":
    asyncio.run(main())
