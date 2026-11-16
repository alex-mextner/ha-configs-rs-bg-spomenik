"""RuTracker.org search, login, and Transmission add services."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import math
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import (
    parse_qs,
    quote_from_bytes,
    unquote,
    urlencode,
    urljoin,
    urlparse,
)

import aiohttp
import voluptuous as vol
from defusedxml import ElementTree
from homeassistant.const import CONF_HOST, CONF_PATH, CONF_PORT
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import discovery
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import (
    DEFAULT_COOKIES_FILE,
    DEFAULT_FEED_URL,
    DEFAULT_RUTRACKER_URL,
    DEFAULT_TEST_DOWNLOAD_DIR,
    DEFAULT_TRANSMISSION_HOST,
    DEFAULT_TRANSMISSION_PATH,
    DEFAULT_TRANSMISSION_PORT,
    DEFAULT_USER_AGENT,
    DOMAIN,
    FORUM_NAMES,
    MOVIE_FORUMS,
    SERIES_FORUMS,
    SERVICE_ADD,
    SERVICE_LOGIN,
    SERVICE_SEARCH,
    SIGNAL_RESULTS_UPDATED,
    SIGNAL_SELECTION_UPDATED,
)

TRANSMISSION_REMOVE_ATTEMPTS = 3
NETSCAPE_COOKIE_FIELDS = 7
MIN_QUOTED_VALUE_LENGTH = 2
FAILURE_PREVIEW_LIMIT = 3
MIN_SEARCH_TOKEN_LENGTH = 2

if TYPE_CHECKING:
    from collections.abc import Iterable

    from homeassistant.helpers.typing import ConfigType

_LOGGER = logging.getLogger(__name__)

CONF_COOKIES_FILE = "cookies_file"
CONF_RUTRACKER_URL = "rutracker_url"
CONF_FEED_URL = "feed_url"
CONF_HOST_ACTIONS_TOKEN = "host_actions_token"  # noqa: S105 - config key.
CONF_HOST_LOGIN_URL = "host_login_url"
CONF_TEST_DOWNLOAD_DIR = "test_download_dir"
CONF_USER_AGENT = "user_agent"
CONF_ENV_FILE = "env_file"

VERIFY_NONE = "none"
VERIFY_QUICK = "quick"
VERIFY_TRANSMISSION = "transmission"
TRACKER_ANNOUNCE_TIMEOUT = 4
TRACKER_ANNOUNCE_MAX_TRACKERS = 2
FALLBACK_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Optional(
                    CONF_RUTRACKER_URL, default=DEFAULT_RUTRACKER_URL
                ): cv.string,
                vol.Optional(CONF_FEED_URL, default=DEFAULT_FEED_URL): cv.string,
                vol.Optional(
                    CONF_COOKIES_FILE, default=DEFAULT_COOKIES_FILE
                ): cv.string,
                vol.Optional(CONF_USER_AGENT, default=DEFAULT_USER_AGENT): cv.string,
                vol.Optional(CONF_ENV_FILE, default="/config/.env"): cv.string,
                vol.Optional(CONF_HOST_LOGIN_URL, default=""): cv.string,
                vol.Optional(CONF_HOST_ACTIONS_TOKEN, default=""): cv.string,
                vol.Optional(CONF_HOST, default=DEFAULT_TRANSMISSION_HOST): cv.string,
                vol.Optional(CONF_PORT, default=DEFAULT_TRANSMISSION_PORT): cv.port,
                vol.Optional(CONF_PATH, default=DEFAULT_TRANSMISSION_PATH): cv.string,
                vol.Optional(
                    CONF_TEST_DOWNLOAD_DIR, default=DEFAULT_TEST_DOWNLOAD_DIR
                ): cv.string,
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)

SEARCH_SCHEMA = vol.Schema(
    {
        vol.Required("query"): cv.string,
        vol.Optional("media_type", default="series"): vol.In(
            ["series", "movies", "both"]
        ),
        vol.Optional("max_results", default=8): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=30)
        ),
        vol.Optional("verify_mode", default=VERIFY_TRANSMISSION): vol.In(
            [VERIFY_NONE, VERIFY_QUICK, VERIFY_TRANSMISSION]
        ),
        vol.Optional("verify_limit", default=5): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=20)
        ),
        vol.Optional("verify_timeout", default=25): vol.All(
            vol.Coerce(int), vol.Range(min=5, max=180)
        ),
        vol.Optional("min_seeders", default=1): vol.All(
            vol.Coerce(int), vol.Range(min=0)
        ),
        vol.Optional("forums"): vol.Any([vol.Coerce(int)], cv.string),
    }
)

ADD_SCHEMA = vol.Schema(
    {
        vol.Optional("result_id"): cv.string,
        vol.Optional("topic_id"): cv.string,
        vol.Optional("magnet"): cv.string,
        vol.Optional("download_dir"): cv.string,
        vol.Optional("labels"): vol.Any([cv.string], cv.string),
        vol.Optional("paused", default=False): cv.boolean,
    }
)

LOGIN_SCHEMA = vol.Schema(
    {
        vol.Optional("username", default=""): cv.string,
        vol.Optional("password", default=""): cv.string,
        vol.Optional("method", default="auto"): vol.In(["auto", "http", "browser"]),
    }
)


@dataclass(slots=True)
class Candidate:
    """One RuTracker.org search candidate."""

    topic_id: str
    title: str
    forum_id: int | None = None
    forum_name: str = ""
    url: str = ""
    size: str = ""
    seeders: int | None = None
    leechers: int | None = None
    updated: str = ""
    approved: bool | None = None
    magnet: str = ""
    source: str = ""
    score: float = 0.0
    downloadable: bool = True
    disabled_reason: str = ""

    @property
    def result_id(self) -> str:
        """Return the stable selection identifier for this topic."""
        return f"rt-{self.topic_id}"

    def as_dict(self, *, include_magnet: bool = False) -> dict[str, Any]:
        """Serialize the candidate, optionally including its magnet link."""
        data: dict[str, Any] = {
            "id": self.result_id,
            "topic_id": self.topic_id,
            "title": self.title,
            "forum_id": self.forum_id,
            "forum_name": self.for_name,
            "url": self.url,
            "size": self.size,
            "seeders": self.seeders,
            "leechers": self.leechers,
            "updated": self.updated,
            "approved": self.approved,
            "source": self.source,
            "score": round(self.score, 2),
            "downloadable": self.downloadable,
            "disabled_reason": self.disabled_reason,
        }
        if include_magnet:
            data["magnet"] = self.magnet
        return data

    @property
    def for_name(self) -> str:
        """Resolve the supplied or known forum name for this candidate."""
        if self.forum_name:
            return self.forum_name
        if self.forum_id is None:
            return ""
        return FORUM_NAMES.get(self.forum_id, f"Forum {self.forum_id}")


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up RuTracker.org Transmission services."""
    domain_config = dict(config.get(DOMAIN, {}))
    runtime = hass.data.setdefault(DOMAIN, {})
    runtime.update(
        {
            "config": domain_config,
            "last_response": {
                "query": "",
                "source": "",
                "searched_at": None,
                "status": "idle",
                "message": "",
                "progress_current": 0,
                "progress_total": 0,
                "selected_result_id": "",
                "results": [],
                "notes": [],
            },
            "cache": {},
            "selected_result_id": "",
            "transmission_session_id": "",
        }
    )

    await discovery.async_load_platform(hass, "sensor", DOMAIN, {}, config)
    await discovery.async_load_platform(hass, "select", DOMAIN, {}, config)

    async def handle_search(call: ServiceCall) -> dict[str, Any]:
        response = await _handle_search(hass, call)
        runtime["last_response"] = response
        runtime["cache"] = {item["id"]: item for item in response.get("results", [])}
        runtime["selected_result_id"] = _first_downloadable_id(
            response.get("results", [])
        )
        runtime["last_response"]["selected_result_id"] = runtime["selected_result_id"]
        async_dispatcher_send(hass, SIGNAL_RESULTS_UPDATED)
        async_dispatcher_send(hass, SIGNAL_SELECTION_UPDATED)
        return response

    async def handle_add(call: ServiceCall) -> dict[str, Any]:
        return await _handle_add(hass, call)

    async def handle_login(call: ServiceCall) -> dict[str, Any]:
        return await _handle_login(hass, call)

    hass.services.async_register(
        DOMAIN,
        SERVICE_SEARCH,
        handle_search,
        schema=SEARCH_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_ADD,
        handle_add,
        schema=ADD_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_LOGIN,
        handle_login,
        schema=LOGIN_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    return True


@dataclass
class SearchContext:
    """Share candidate collection state between its existing search stages."""

    hass: HomeAssistant
    session: aiohttp.ClientSession
    cfg: dict[str, Any]
    query: str
    tokens: list[str]
    forums: list[int]
    max_results: int
    cookie_header: str
    notes: list[str]
    candidates: list[Candidate]
    seen: set[str]


async def _search_tracker_forums(context: SearchContext) -> None:
    hass = context.hass
    session = context.session
    cfg = context.cfg
    query = context.query
    forums = context.forums
    cookie_header = context.cookie_header
    notes = context.notes
    candidates = context.candidates
    seen = context.seen
    tracker_errors: list[str] = []
    for index, forum_id in enumerate(forums, start=1):
        _publish_progress(
            hass,
            SearchResponse(
                query=query,
                status="running",
                message=f"Searching forum {FORUM_NAMES.get(forum_id, forum_id)}...",
                progress_current=index,
                progress_total=len(forums),
                notes=notes,
            ),
        )
        try:
            found = await _search_tracker_page(
                session, cfg, query, forum_id, cookie_header
            )
            for item in found:
                if item.topic_id not in seen:
                    seen.add(item.topic_id)
                    candidates.append(item)
        except Exception as err:  # noqa: BLE001 - service should degrade gracefully.
            tracker_errors.append(f"forum {forum_id}: {err}")
    if tracker_errors:
        notes.append(_summarize_failures("tracker.php", tracker_errors))


async def _search_global_tracker(context: SearchContext) -> None:
    hass = context.hass
    session = context.session
    cfg = context.cfg
    query = context.query
    forums = context.forums
    cookie_header = context.cookie_header
    notes = context.notes
    candidates = context.candidates
    seen = context.seen
    _publish_progress(
        hass,
        SearchResponse(
            query=query,
            status="running",
            message="Searching global RuTracker.org tracker page...",
            progress_current=len(forums),
            progress_total=len(forums) + 1,
            notes=notes,
        ),
    )
    try:
        found = await _search_tracker_page(session, cfg, query, None, cookie_header)
        added = 0
        for item in found:
            if item.topic_id not in seen:
                seen.add(item.topic_id)
                candidates.append(item)
                added += 1
        if added:
            _append_note_once(
                notes,
                f"Global RuTracker.org search added {added} fallback candidate(s).",
            )
    except Exception as err:  # noqa: BLE001
        notes.append(f"global tracker.php fallback: {err}")


async def _search_feed_forums(context: SearchContext) -> None:
    hass = context.hass
    session = context.session
    cfg = context.cfg
    query = context.query
    tokens = context.tokens
    forums = context.forums
    notes = context.notes
    candidates = context.candidates
    seen = context.seen
    for index, forum_id in enumerate(forums, start=1):
        _publish_progress(
            hass,
            SearchResponse(
                query=query,
                status="running",
                message=f"Searching Atom feed {FORUM_NAMES.get(forum_id, forum_id)}...",
                progress_current=index,
                progress_total=len(forums),
                notes=notes,
            ),
        )
        try:
            found = await _search_feed(session, cfg, query, tokens, forum_id)
            for item in found:
                if item.topic_id not in seen:
                    seen.add(item.topic_id)
                    candidates.append(item)
        except Exception as err:  # noqa: BLE001
            notes.append(f"feed forum {forum_id}: {err}")


async def _collect_search_candidates(context: SearchContext) -> bool:
    hass, query, forums = context.hass, context.query, context.forums
    _publish_progress(
        hass,
        SearchResponse(
            query=query,
            status="running",
            message="Searching RuTracker.org tracker pages...",
            progress_current=0,
            progress_total=max(len(forums), 1),
        ),
    )
    if context.cookie_header:
        await _search_tracker_forums(context)
    else:
        context.notes.append(
            "RuTracker.org cookies file is missing or empty; "
            "using public Atom feed fallback."
        )
    if context.cookie_header and len(context.candidates) < context.max_results:
        await _search_global_tracker(context)
    searched_feed = len(context.candidates) < context.max_results
    if searched_feed:
        await _search_feed_forums(context)
    return searched_feed


def _candidate_source(candidates: list[Candidate], *, searched_feed: bool) -> str:
    sources = {item.source for item in candidates if item.source}
    if searched_feed and "tracker" in sources and "feed" not in sources:
        source = "tracker"
    elif "tracker" in sources and "feed" in sources:
        source = "tracker+feed"
    elif "tracker" in sources:
        source = "tracker"
    elif searched_feed:
        source = "feed"
    else:
        source = "none"
    return source


async def _handle_search(hass: HomeAssistant, call: ServiceCall) -> dict[str, Any]:
    query = call.data["query"].strip()
    tokens = _tokens(query)
    if not tokens:
        return SearchResponse(
            query, "none", [], ["Empty query"], message="Empty query"
        ).to_dict()

    cfg = hass.data[DOMAIN]["config"]
    session = async_get_clientsession(hass)
    forums = _forum_ids(call.data.get("forums"), call.data["media_type"])
    max_results = call.data["max_results"]
    notes: list[str] = []
    candidates: list[Candidate] = []
    seen: set[str] = set()

    cookie_header = await _async_read_cookie_header(hass, cfg)
    context = SearchContext(
        hass=hass,
        session=session,
        cfg=cfg,
        query=query,
        tokens=tokens,
        forums=forums,
        max_results=max_results,
        cookie_header=cookie_header,
        notes=notes,
        candidates=candidates,
        seen=seen,
    )
    searched_feed = await _collect_search_candidates(context)
    source = _candidate_source(candidates, searched_feed=searched_feed)

    media_type = call.data["media_type"]
    candidates = [
        item for item in candidates if _looks_like_requested_media(item, media_type)
    ]
    for item in candidates:
        item.score = _score(item, tokens)

    candidates.sort(key=lambda item: item.score, reverse=True)
    candidates = candidates[:max_results]

    _publish_progress(
        hass,
        SearchResponse(
            query=query,
            status="running",
            message=f"Fetching topic details for {len(candidates)} candidates...",
            source=source,
            results=[item.as_dict() for item in candidates],
            notes=notes,
            progress_current=0,
            progress_total=len(candidates),
        ),
    )
    await _fetch_topic_details(
        session,
        cfg,
        candidates,
        TopicDetailsOptions(
            min_seeders=int(call.data["min_seeders"]),
            notes=notes,
            cookie_header=cookie_header,
        ),
    )

    verify_mode = call.data["verify_mode"]
    if verify_mode == VERIFY_TRANSMISSION:
        await _verify_candidates_with_transmission(
            hass,
            session,
            candidates,
            VerificationOptions(
                limit=call.data["verify_limit"],
                poll_seconds=call.data["verify_timeout"],
                query=query,
                source=source,
                notes=notes,
            ),
        )
    elif verify_mode == VERIFY_QUICK:
        _publish_progress(
            hass,
            SearchResponse(
                query=query,
                status="running",
                message="Running quick verification...",
                source=source,
                results=[item.as_dict() for item in candidates],
                notes=notes,
                progress_current=0,
                progress_total=len(candidates),
            ),
        )
        _quick_verify(candidates, int(call.data["min_seeders"]))

    results = [item.as_dict() for item in candidates]
    return SearchResponse(
        query,
        source,
        results,
        notes,
        message=f"Found {len(results)} candidates.",
        selected_result_id=_first_downloadable_id(results),
    ).to_dict()


def _add_target(hass: HomeAssistant, call: ServiceCall) -> tuple[str, str, str]:
    result_id = call.data.get("result_id", "").strip()
    topic_id = call.data.get("topic_id", "").strip()
    magnet = call.data.get("magnet", "").strip()

    if not result_id and not topic_id and not magnet:
        result_id = str(hass.data[DOMAIN].get("selected_result_id") or "").strip()

    if (
        not topic_id
        and not result_id
        and magnet.startswith(("http://", "https://"))
        and (parsed_topic_id := _topic_id_from_url(magnet))
    ):
        topic_id = parsed_topic_id
        magnet = ""
    return result_id, topic_id, magnet


def _cached_add_error(
    item: dict[str, Any] | None,
    result_id: str,
    topic_id: str,
) -> dict[str, Any] | None:
    if not item:
        return {"ok": False, "error": f"Unknown result_id: {result_id}"}
    if item.get("downloadable") is False:
        return {
            "ok": False,
            "error": (
                "Selected result is disabled: "
                f"{item.get('disabled_reason') or 'not downloadable'}"
            ),
            "result_id": result_id,
            "topic_id": str(item.get("topic_id") or topic_id),
        }
    return None


async def _handle_add(hass: HomeAssistant, call: ServiceCall) -> dict[str, Any]:
    cfg = hass.data[DOMAIN]["config"]
    session = async_get_clientsession(hass)
    cache = hass.data[DOMAIN].get("cache", {})

    result_id, topic_id, magnet = _add_target(hass, call)
    title = ""

    if result_id:
        item = cache.get(result_id)
        error = _cached_add_error(item, result_id, topic_id)
        if error is not None:
            return error
        topic_id = str(item.get("topic_id") or topic_id)
        magnet = str(item.get("magnet") or magnet)
        title = str(item.get("title") or "")

    if topic_id and not magnet:
        cookie_header = await _async_read_cookie_header(hass, cfg)
        candidate = Candidate(
            topic_id=topic_id,
            title=title or f"RuTracker.org topic {topic_id}",
            url=f"{cfg[CONF_RUTRACKER_URL].rstrip('/')}/viewtopic.php?t={topic_id}",
        )
        await _fetch_topic_details(
            session,
            cfg,
            [candidate],
            TopicDetailsOptions(min_seeders=0, notes=[], cookie_header=cookie_header),
        )
        magnet = candidate.magnet
        title = candidate.title

    if not magnet.startswith("magnet:?xt=urn:btih:"):
        return {
            "ok": False,
            "error": "No magnet link is available for the selected result.",
            "result_id": result_id,
            "topic_id": topic_id,
        }

    params: dict[str, Any] = {"filename": magnet, "paused": bool(call.data["paused"])}
    download_dir = call.data.get("download_dir", "").strip()
    if download_dir in {"unknown", "unavailable", "none", "None"}:
        download_dir = ""
    if download_dir and not download_dir.startswith("/"):
        return {
            "ok": False,
            "error": "download_dir must be an absolute path.",
            "result_id": result_id,
            "topic_id": topic_id,
        }
    if download_dir:
        params["download_dir"] = download_dir
    labels = _labels(call.data.get("labels"))
    if labels:
        params["labels"] = labels

    rpc = await _transmission_rpc(hass, session, "torrent_add", params)
    return {
        "ok": True,
        "result_id": result_id,
        "topic_id": topic_id,
        "title": title,
        "transmission": _rpc_payload(rpc),
    }


async def _handle_login(hass: HomeAssistant, call: ServiceCall) -> dict[str, Any]:
    cfg = hass.data[DOMAIN]["config"]
    username = call.data["username"].strip()
    password = call.data["password"]
    if not username or not password:
        env_values = await hass.async_add_executor_job(
            _read_env_values, cfg.get(CONF_ENV_FILE, "/config/.env")
        )
        username = username or env_values.get("RUTRACKER_USERNAME", "")
        password = password or env_values.get("RUTRACKER_PASSWORD", "")
    if not username or not password:
        return {
            "ok": False,
            "error": (
                "RuTracker.org username/password are missing. "
                "Set RUTRACKER_USERNAME and RUTRACKER_PASSWORD in /config/.env "
                "or fill the login form."
            ),
        }
    method = call.data["method"]
    attempts: list[dict[str, Any]] = []

    if method in ("auto", "http"):
        http_result = await _attempt_login(_login_http(cfg, username, password), "http")
        attempts.append(_sanitize_login_attempt(http_result))
        if http_result.get("ok"):
            return await _store_login_cookies(hass, cfg, http_result, attempts)

    if method in ("auto", "browser"):
        browser_result = await _attempt_login(
            _login_with_host_browser(hass, cfg, username, password), "browser"
        )
        attempts.append(_sanitize_login_attempt(browser_result))
        if browser_result.get("ok"):
            return await _store_login_cookies(hass, cfg, browser_result, attempts)

    return {
        "ok": False,
        "method": method,
        "error": "RuTracker.org login failed",
        "attempts": attempts,
    }


async def _attempt_login(coro: Any, method: str) -> dict[str, Any]:
    try:
        return await coro
    except Exception as err:  # noqa: BLE001 - service response should explain failures.
        return {"ok": False, "method": method, "error": str(err)}


async def _store_login_cookies(
    hass: HomeAssistant,
    cfg: dict[str, Any],
    result: dict[str, Any],
    attempts: list[dict[str, Any]],
) -> dict[str, Any]:
    cookie_header = str(result.get("cookie_header") or "")
    if not cookie_header:
        return {
            "ok": False,
            "method": result.get("method", "unknown"),
            "error": "Login did not return a cookie header",
            "attempts": attempts,
        }

    path = cfg.get(CONF_COOKIES_FILE, DEFAULT_COOKIES_FILE)
    await hass.async_add_executor_job(_write_cookie_header, path, cookie_header)
    return {
        "ok": True,
        "method": result.get("method", "unknown"),
        "message": result.get("message", "RuTracker.org cookies saved"),
        "cookies_count": _count_cookies(cookie_header),
        "stored_path": path,
        "attempts": attempts,
    }


def _sanitize_login_attempt(result: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in result.items()
        if key != "cookie_header" and value not in ("", None)
    }


async def _login_http(
    cfg: dict[str, Any], username: str, password: str
) -> dict[str, Any]:
    base_url = cfg[CONF_RUTRACKER_URL].rstrip("/")
    login_url = f"{base_url}/login.php"
    headers = {"User-Agent": cfg.get(CONF_USER_AGENT, DEFAULT_USER_AGENT)}
    timeout = aiohttp.ClientTimeout(total=45)
    cookie_jar = aiohttp.CookieJar(unsafe=True)

    async with aiohttp.ClientSession(
        cookie_jar=cookie_jar,
        headers=headers,
        timeout=timeout,
    ) as login_session:
        async with login_session.get(login_url) as resp:
            body = await resp.read()
            if resp.status >= HTTPStatus.BAD_REQUEST:
                return {
                    "ok": False,
                    "method": "http",
                    "error": f"login page HTTP {resp.status}",
                }
            login_html = _decode_http_body(body, resp.headers.get("Content-Type", ""))

        form_data = _form_inputs(login_html)
        form_data.update(
            {
                "login_username": username,
                "login_password": password,
            }
        )
        form_data.setdefault("login", "1")
        form_data.setdefault("redirect", "index.php")
        action_url = _login_form_action(login_html, login_url)
        async with login_session.post(
            action_url,
            data=form_data,
            headers={"Referer": login_url, "Origin": _origin(login_url), **headers},
        ) as resp:
            body = await resp.read()
            final_url = str(resp.url)
            status = resp.status
            content_type = resp.headers.get("Content-Type", "")

    response_text = _decode_http_body(body, content_type)
    if status >= HTTPStatus.BAD_REQUEST:
        return {"ok": False, "method": "http", "error": f"login submit HTTP {status}"}

    cookie_header = _cookie_header_from_jar(cookie_jar, base_url)
    if not cookie_header:
        return {"ok": False, "method": "http", "error": "no cookies returned"}
    if _looks_like_login_page(response_text, final_url):
        return {
            "ok": False,
            "method": "http",
            "error": "login form is still visible after submit",
            "cookies_count": _count_cookies(cookie_header),
        }

    return {
        "ok": True,
        "method": "http",
        "cookie_header": cookie_header,
        "cookies_count": _count_cookies(cookie_header),
        "message": "HTTP login saved RuTracker.org cookies",
    }


async def _login_with_host_browser(
    hass: HomeAssistant,
    cfg: dict[str, Any],
    username: str,
    password: str,
) -> dict[str, Any]:
    url = str(cfg.get(CONF_HOST_LOGIN_URL) or "").strip()
    token = str(cfg.get(CONF_HOST_ACTIONS_TOKEN) or "").strip()
    if not url:
        return {
            "ok": False,
            "method": "browser",
            "error": "host_login_url is not configured",
        }
    if not token:
        return {
            "ok": False,
            "method": "browser",
            "error": "host_actions_token is not configured",
        }

    session = async_get_clientsession(hass)
    timeout = aiohttp.ClientTimeout(total=120)
    async with session.post(
        url,
        json={"username": username, "password": password},
        headers={"X-HA-Host-Token": token},
        timeout=timeout,
    ) as resp:
        text = await resp.text()
        if resp.status >= HTTPStatus.BAD_REQUEST:
            return {
                "ok": False,
                "method": "browser",
                "error": f"host browser HTTP {resp.status}: {text[:200]}",
            }
        try:
            data = json.loads(text)
        except json.JSONDecodeError as err:
            return {"ok": False, "method": "browser", "error": f"invalid JSON: {err}"}

    return _browser_login_result(data)


def _browser_login_result(data: dict[str, Any]) -> dict[str, Any]:
    if not data.get("ok"):
        return {
            "ok": False,
            "method": "browser",
            "error": data.get("error", "agent-browser login failed"),
            "url": data.get("url"),
            "title": data.get("title"),
        }
    cookie_header = str(data.get("cookie_header") or "")
    if not cookie_header:
        return {
            "ok": False,
            "method": "browser",
            "error": "agent-browser did not return cookies",
            "url": data.get("url"),
            "title": data.get("title"),
        }

    return {
        "ok": True,
        "method": "browser",
        "cookie_header": cookie_header,
        "cookies_count": _count_cookies(cookie_header),
        "message": data.get(
            "message", "agent-browser login saved RuTracker.org cookies"
        ),
        "url": data.get("url"),
        "title": data.get("title"),
    }


@dataclass
class SearchResponse:
    """Carry a response or progress update using the existing payload fields."""

    query: str
    source: str = ""
    results: list[dict[str, Any]] | None = None
    notes: list[str] | None = None
    status: str = "done"
    message: str = ""
    progress_current: int = 0
    progress_total: int = 0
    selected_result_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Stamp and serialize the public search payload."""
        return {
            "query": self.query,
            "source": self.source,
            "searched_at": datetime.now(UTC).isoformat(),
            "status": self.status,
            "message": self.message,
            "progress_current": self.progress_current,
            "progress_total": self.progress_total,
            "selected_result_id": self.selected_result_id,
            "results": self.results,
            "notes": self.notes,
        }


@dataclass(frozen=True)
class TopicDetailsOptions:
    """Group the topic filtering and cookie-retry inputs."""

    min_seeders: int
    notes: list[str]
    cookie_header: str = ""


@dataclass(frozen=True)
class VerificationOptions:
    """Carry the verification selection, polling budget, and progress context."""

    limit: int
    poll_seconds: int
    query: str = ""
    source: str = ""
    notes: list[str] | None = None


def _publish_progress(hass: HomeAssistant, response: SearchResponse) -> None:
    runtime = hass.data[DOMAIN]
    response.selected_result_id = runtime.get("selected_result_id", "")
    if response.results is None:
        response.results = runtime.get("last_response", {}).get("results", [])
    if response.notes is None:
        response.notes = runtime.get("last_response", {}).get("notes", [])
    runtime["last_response"] = response.to_dict()
    async_dispatcher_send(hass, SIGNAL_RESULTS_UPDATED)


def _first_downloadable_id(results: list[dict[str, Any]]) -> str:
    for item in results:
        if item.get("downloadable"):
            return str(item.get("id") or "")
    return str(results[0].get("id") or "") if results else ""


async def _search_tracker_page(
    session: aiohttp.ClientSession,
    cfg: dict[str, Any],
    query: str,
    forum_id: int | None,
    cookie_header: str,
) -> list[Candidate]:
    params_data: dict[str, Any] = {"nm": query}
    if forum_id is not None:
        params_data["f"] = forum_id
    params = urlencode(params_data)
    url = f"{cfg[CONF_RUTRACKER_URL].rstrip('/')}/tracker.php?{params}"
    text = await _http_get_text(session, cfg, url, cookie_header=cookie_header)
    if "login.php" in text and "password" in text.lower():
        error_message = "search requires RuTracker.org login cookies"
        raise RuntimeError(error_message)
    return _parse_topic_rows(text, forum_id, source="tracker")


async def _search_feed(
    session: aiohttp.ClientSession,
    cfg: dict[str, Any],
    query: str,
    tokens: list[str],
    forum_id: int,
) -> list[Candidate]:
    del query  # Preserve the public callback parameter names.
    url = cfg[CONF_FEED_URL].format(forum_id=forum_id)
    text = await _http_get_text(session, cfg, url)
    root = ElementTree.fromstring(text)
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    candidates: list[Candidate] = []
    for entry in root.findall("atom:entry", ns):
        title = html.unescape(
            (entry.findtext("atom:title", default="", namespaces=ns) or "").strip()
        )
        if not _all_tokens_match(title, tokens):
            continue
        link = ""
        link_el = entry.find("atom:link", ns)
        if link_el is not None:
            link = link_el.attrib.get("href", "")
        topic_id = _topic_id_from_url(link) or _topic_id_from_text(
            entry.findtext("atom:id", default="", namespaces=ns) or ""
        )
        if not topic_id:
            continue
        category = entry.find("atom:category", ns)
        label = category.attrib.get("label", "") if category is not None else ""
        updated = entry.findtext("atom:updated", default="", namespaces=ns) or ""
        candidates.append(
            Candidate(
                topic_id=topic_id,
                title=title,
                forum_id=forum_id,
                forum_name=label,
                url=link,
                updated=updated,
                source="feed",
            )
        )
    return candidates


def _apply_topic_details(item: Candidate, text: str, min_seeders: int) -> None:
    if magnet := _extract_magnet(text):
        item.magnet = magnet
    else:
        item.downloadable = False
        item.disabled_reason = "Magnet link not found"

    if item.seeders is None:
        item.seeders = _extract_count(text, "Seeders")
    if item.leechers is None:
        item.leechers = _extract_count(text, "Leechers")

    if item.seeders is not None and item.seeders < min_seeders:
        item.downloadable = False
        item.disabled_reason = f"Only {item.seeders} seeders"


async def _fetch_topic_details(
    session: aiohttp.ClientSession,
    cfg: dict[str, Any],
    candidates: list[Candidate],
    options: TopicDetailsOptions,
) -> None:
    min_seeders, notes, cookie_header = (
        options.min_seeders,
        options.notes,
        options.cookie_header,
    )
    retried_without_cookies = False
    for item in candidates:
        if item.url == "":
            item.url = (
                f"{cfg[CONF_RUTRACKER_URL].rstrip('/')}/viewtopic.php?t={item.topic_id}"
            )
        try:
            try:
                text = await _http_get_text(
                    session, cfg, item.url, cookie_header=cookie_header
                )
            except RuntimeError as err:
                if not cookie_header or not _should_retry_without_cookies(err):
                    raise
                text = await _http_get_text_clean(cfg, item.url)
                retried_without_cookies = True
        except Exception as err:  # noqa: BLE001
            item.downloadable = False
            item.disabled_reason = f"Topic page unavailable: {err}"
            continue

        _apply_topic_details(item, text, min_seeders)

    if retried_without_cookies:
        _append_note_once(
            notes,
            "Some RuTracker.org topic pages failed with stored cookies "
            "and were retried without cookies.",
        )

    if candidates and not any(item.magnet for item in candidates):
        notes.append(
            "No magnet links could be extracted from the current candidate set."
        )


def _quick_verify(candidates: Iterable[Candidate], min_seeders: int) -> None:
    for item in candidates:
        if not item.downloadable:
            continue
        if not item.magnet:
            item.downloadable = False
            item.disabled_reason = "Magnet link not available"
        elif item.seeders is not None and item.seeders < min_seeders:
            item.downloadable = False
            item.disabled_reason = f"Only {item.seeders} seeders"


async def _prepare_verification_candidate(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    cfg: dict[str, Any],
    item: Candidate,
) -> int | None:
    announce = await _verify_candidate_with_tracker_announce(item)
    if announce["checked"]:
        _apply_announce_result(item, announce)
        return None

    params = {
        "filename": item.magnet,
        "paused": False,
        "download_dir": cfg.get(CONF_TEST_DOWNLOAD_DIR, DEFAULT_TEST_DOWNLOAD_DIR),
        "labels": ["ha-rutracker-check"],
    }
    try:
        payload = _rpc_payload(
            await _transmission_rpc(hass, session, "torrent_add", params)
        )
    except Exception as err:  # noqa: BLE001
        item.downloadable = False
        item.disabled_reason = f"Transmission test add failed: {err}"
        return None

    torrent = (
        payload.get("torrent_added")
        or payload.get("torrent-added")
        or payload.get("torrent_duplicate")
        or payload.get("torrent-duplicate")
    )
    if not torrent:
        item.downloadable = False
        item.disabled_reason = "Transmission did not return a torrent id"
        return None

    torrent_id = int(torrent["id"])
    if "torrent-duplicate" in payload or "torrent_duplicate" in payload:
        item.downloadable = True
        item.disabled_reason = ""
        return None
    try:
        await _transmission_rpc(
            hass,
            session,
            "torrent_set",
            {
                "ids": [torrent_id],
                "downloadLimited": True,
                "downloadLimit": 1,
                "uploadLimited": True,
                "uploadLimit": 1,
            },
        )
    except Exception:
        _LOGGER.exception("Could not limit temporary verification torrent rates")
    return torrent_id


def _apply_announce_result(item: Candidate, announce: dict[str, Any]) -> None:
    if announce["alive"]:
        item.downloadable = True
        item.disabled_reason = ""
        if announce["seeders"] is not None:
            item.seeders = announce["seeders"]
        if announce["leechers"] is not None:
            item.leechers = announce["leechers"]
    else:
        item.downloadable = False
        item.disabled_reason = announce["reason"]


async def _poll_verification(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    torrent_ids: dict[int, Candidate],
    options: VerificationOptions,
    candidates: list[Candidate],
) -> set[int]:
    query, source = options.query, options.source
    notes = options.notes if options.notes is not None else []
    done: set[int] = set()
    deadline = time.monotonic() + options.poll_seconds
    while torrent_ids and len(done) < len(torrent_ids) and time.monotonic() < deadline:
        await asyncio.sleep(1)
        if query:
            _publish_progress(
                hass,
                SearchResponse(
                    query=query,
                    status="running",
                    message=f"Checking {len(torrent_ids) - len(done)} "
                    "candidate(s) in Transmission...",
                    source=source,
                    results=[candidate.as_dict() for candidate in candidates],
                    notes=notes,
                    progress_current=len(done),
                    progress_total=len(torrent_ids),
                ),
            )
        try:
            payload = _rpc_payload(
                await _transmission_rpc(
                    hass,
                    session,
                    "torrent_get",
                    {
                        "ids": list(torrent_ids),
                        "fields": [
                            "id",
                            "name",
                            "hashString",
                            "metadataPercentComplete",
                            "peersConnected",
                            "rateDownload",
                            "errorString",
                            "trackerStats",
                        ],
                    },
                )
            )
        except Exception as err:  # noqa: BLE001
            _mark_verification_poll_failed(torrent_ids, err)
            break

        _apply_torrent_observations(payload.get("torrents", []), torrent_ids, done)
    return done


def _apply_torrent_observations(
    torrents: list[dict[str, Any]],
    torrent_ids: dict[int, Candidate],
    done: set[int],
) -> None:
    for torrent in torrents:
        torrent_id = int(torrent["id"])
        item = torrent_ids.get(torrent_id)
        if item is None or torrent_id in done:
            continue
        if _torrent_looks_alive(torrent):
            item.downloadable = True
            item.disabled_reason = ""
            done.add(torrent_id)
        elif torrent.get("errorString"):
            item.downloadable = False
            item.disabled_reason = str(torrent["errorString"])
            done.add(torrent_id)


def _mark_verification_poll_failed(
    torrent_ids: dict[int, Candidate], error: Exception
) -> None:
    for item in torrent_ids.values():
        if item.downloadable:
            item.downloadable = False
            item.disabled_reason = f"Transmission test poll failed: {error}"


def _expire_unverified_candidates(
    torrent_ids: dict[int, Candidate],
    done: set[int],
) -> None:
    for torrent_id, item in torrent_ids.items():
        if torrent_id not in done:
            item.downloadable = False
            item.disabled_reason = (
                "Transmission test did not fetch metadata or peers in time"
            )


async def _verify_candidates_with_transmission(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    candidates: list[Candidate],
    options: VerificationOptions,
) -> None:
    limit, query, source, notes = (
        options.limit,
        options.query,
        options.source,
        options.notes,
    )
    to_check = [item for item in candidates if item.downloadable and item.magnet][
        :limit
    ]
    if not to_check:
        return

    notes = notes or []
    cfg = hass.data[DOMAIN]["config"]
    torrent_ids: dict[int, Candidate] = {}
    remove_ids: list[int] = []

    for index, item in enumerate(to_check, start=1):
        if query:
            _publish_progress(
                hass,
                SearchResponse(
                    query=query,
                    status="running",
                    message=f"Checking tracker availability {index}/{len(to_check)}...",
                    source=source,
                    results=[candidate.as_dict() for candidate in candidates],
                    notes=notes,
                    progress_current=index,
                    progress_total=len(to_check),
                ),
            )
        torrent_id = await _prepare_verification_candidate(hass, session, cfg, item)
        if torrent_id is not None:
            torrent_ids[torrent_id] = item
            remove_ids.append(torrent_id)

    polling_options = VerificationOptions(
        limit=limit,
        poll_seconds=options.poll_seconds,
        query=query,
        source=source,
        notes=notes,
    )
    done = await _poll_verification(
        hass, session, torrent_ids, polling_options, candidates
    )
    _expire_unverified_candidates(torrent_ids, done)

    if remove_ids:
        await _remove_transmission_test_torrents(hass, session, remove_ids)


async def _verify_candidate_with_tracker_announce(item: Candidate) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(_tracker_announce_sync, item.magnet)
    except Exception as err:  # noqa: BLE001 - fall back to Transmission verification.
        return {
            "checked": False,
            "alive": False,
            "seeders": None,
            "leechers": None,
            "reason": f"Tracker announce failed: {err}",
        }


def _announce_has_peers(peers: Any, seeders: int | None) -> bool:
    peers_count = _peers_count(peers)
    return peers_count > 0 or (seeders is not None and seeders > 0)


def _tracker_announce_sync(magnet: str) -> dict[str, Any]:
    info_hash, trackers = _magnet_info_hash_and_trackers(magnet)
    if not info_hash or not trackers:
        return {
            "checked": False,
            "alive": False,
            "seeders": None,
            "leechers": None,
            "reason": "No HTTP tracker announce URL in magnet",
        }

    last_error = ""
    transport_error = False
    for tracker in trackers[:TRACKER_ANNOUNCE_MAX_TRACKERS]:
        peer_id = b"-HA0001-" + os.urandom(12)
        announce_url = _build_announce_url(tracker, info_hash, peer_id, "started")
        try:
            data = _http_get_bytes(announce_url, timeout=TRACKER_ANNOUNCE_TIMEOUT)
            payload, _offset = _bdecode(data)
            if not isinstance(payload, dict):
                return _announce_result(
                    alive=False, reason="Tracker returned an invalid response"
                )
            if failure := _bvalue_text(payload, b"failure reason"):
                return _announce_result(
                    alive=False, reason=f"Tracker failure: {failure}"
                )

            seeders = _bvalue_int(payload, b"complete")
            leechers = _bvalue_int(payload, b"incomplete")
            peers = payload.get(b"peers")
            alive = _announce_has_peers(peers, seeders)
            if alive:
                _announce_stopped_best_effort(tracker, info_hash, peer_id)
                return {
                    "checked": True,
                    "alive": True,
                    "seeders": seeders,
                    "leechers": leechers,
                    "reason": "",
                }
            last_error = "Tracker announce returned no peers"
        except Exception as err:  # noqa: BLE001 - try the next tracker.
            transport_error = True
            last_error = f"Tracker announce failed: {err}"

    if transport_error and last_error.startswith("Tracker announce failed:"):
        return {
            "checked": False,
            "alive": False,
            "seeders": None,
            "leechers": None,
            "reason": last_error,
        }

    return _announce_result(
        alive=False, reason=last_error or "No tracker announce succeeded"
    )


def _magnet_info_hash_and_trackers(magnet: str) -> tuple[bytes, list[str]]:
    query = parse_qs(urlparse(magnet).query)
    xt_values = query.get("xt") or []
    info_hash = b""
    for xt in xt_values:
        if not xt.lower().startswith("urn:btih:"):
            continue
        value = xt.rsplit(":", 1)[-1].strip()
        if re.fullmatch(r"[0-9a-fA-F]{40}", value):
            info_hash = bytes.fromhex(value)
            break
    trackers = [
        unquote(tracker)
        for tracker in query.get("tr", [])
        if unquote(tracker).startswith(("http://", "https://"))
    ]
    return info_hash, trackers


def _build_announce_url(
    tracker: str, info_hash: bytes, peer_id: bytes, event: str
) -> str:
    separator = "&" if "?" in tracker else "?"
    params = {
        "info_hash": quote_from_bytes(info_hash),
        "peer_id": quote_from_bytes(peer_id),
        "port": "51413",
        "uploaded": "0",
        "downloaded": "0",
        "left": "1",
        "compact": "1",
        "event": event,
        "numwant": "20",
    }
    return (
        tracker
        + separator
        + "&".join(f"{key}={value}" for key, value in params.items())
    )


class _HTTPOnlyHandler(urllib.request.BaseHandler):
    """Reject non-HTTP schemes before proxy or protocol dispatch."""

    def default_open(self, request: urllib.request.Request) -> None:
        """Apply the scheme boundary to initial and redirected requests."""
        if request.type not in {"http", "https"}:
            error_message = "Only HTTP and HTTPS URLs are supported"
            raise urllib.error.URLError(error_message)


def _http_opener() -> urllib.request.OpenerDirector:
    """Retain standard HTTP behavior without file, FTP, or data handlers."""
    opener = urllib.request.OpenerDirector()
    for handler in (
        _HTTPOnlyHandler(),
        urllib.request.ProxyHandler(),
        urllib.request.UnknownHandler(),
        urllib.request.HTTPHandler(),
        urllib.request.HTTPDefaultErrorHandler(),
        urllib.request.HTTPRedirectHandler(),
        urllib.request.HTTPErrorProcessor(),
        urllib.request.HTTPSHandler(),
    ):
        opener.add_handler(handler)
    return opener


def _http_get_bytes(url: str, timeout: int) -> bytes:
    request = urllib.request.Request(  # noqa: S310 - Dispatched only by the HTTP(S)-only opener.
        url,
        headers={"User-Agent": "Transmission/4.1.1"},
    )
    with _http_opener().open(request, timeout=timeout) as response:
        if response.status >= HTTPStatus.BAD_REQUEST:
            error_message = f"HTTP {response.status}"
            raise RuntimeError(error_message)
        return response.read()


def _announce_stopped_best_effort(
    tracker: str, info_hash: bytes, peer_id: bytes
) -> None:
    try:
        _http_get_bytes(
            _build_announce_url(tracker, info_hash, peer_id, "stopped"), timeout=2
        )
    except Exception:
        _LOGGER.exception("Best-effort tracker stop announcement failed")


def _announce_result(*, alive: bool, reason: str) -> dict[str, Any]:
    return {
        "checked": True,
        "alive": alive,
        "seeders": None,
        "leechers": None,
        "reason": reason,
    }


def _bdecode(data: bytes, offset: int = 0) -> tuple[Any, int]:
    token = data[offset : offset + 1]
    if token == b"i":
        end = data.index(b"e", offset)
        return int(data[offset + 1 : end]), end + 1
    if token == b"l":
        offset += 1
        values = []
        while data[offset : offset + 1] != b"e":
            value, offset = _bdecode(data, offset)
            values.append(value)
        return values, offset + 1
    if token == b"d":
        offset += 1
        values = {}
        while data[offset : offset + 1] != b"e":
            key, offset = _bdecode(data, offset)
            value, offset = _bdecode(data, offset)
            values[key] = value
        return values, offset + 1
    if token.isdigit():
        colon = data.index(b":", offset)
        length = int(data[offset:colon])
        start = colon + 1
        end = start + length
        return data[start:end], end
    error_message = "Invalid bencode response"
    raise ValueError(error_message)


def _bvalue_text(payload: dict[Any, Any], key: bytes) -> str:
    value = payload.get(key)
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return ""


def _bvalue_int(payload: dict[Any, Any], key: bytes) -> int | None:
    value = payload.get(key)
    return value if isinstance(value, int) else None


def _peers_count(peers: Any) -> int:
    if isinstance(peers, bytes):
        return len(peers) // 6
    if isinstance(peers, list):
        return len(peers)
    return 0


async def _remove_transmission_test_torrents(
    hass: HomeAssistant, session: aiohttp.ClientSession, remove_ids: list[int]
) -> None:
    for attempt in range(TRANSMISSION_REMOVE_ATTEMPTS):
        try:
            await _transmission_rpc(
                hass,
                session,
                "torrent_remove",
                {"ids": remove_ids, "delete_local_data": True},
            )
        except Exception as err:  # noqa: BLE001
            if attempt == TRANSMISSION_REMOVE_ATTEMPTS - 1:
                _LOGGER.warning("Failed to remove RuTracker.org test torrents: %s", err)
                return
            await asyncio.sleep(2)
        else:
            return


def _tracker_has_live_counts(tracker: dict[str, Any]) -> bool:
    for key in ("seederCount", "leecherCount", "lastAnnouncePeerCount"):
        try:
            if int(tracker.get(key) or 0) > 0:
                return True
        except (TypeError, ValueError):
            pass
    return False


def _torrent_looks_alive(torrent: dict[str, Any]) -> bool:
    if float(torrent.get("metadataPercentComplete") or 0) >= 1:
        return True
    if int(torrent.get("peersConnected") or 0) > 0:
        return True
    if int(torrent.get("rateDownload") or 0) > 0:
        return True
    for tracker in torrent.get("trackerStats") or []:
        if _tracker_has_live_counts(tracker):
            return True
    return False


async def _transmission_rpc(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    method: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = hass.data[DOMAIN]["config"]
    url = f"http://{cfg[CONF_HOST]}:{cfg[CONF_PORT]}{cfg[CONF_PATH]}"
    payload: dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": f"ha-{method}",
        "method": method,
    }
    if params is not None:
        payload["params"] = params

    headers = {"Content-Type": "application/json"}
    if sid := hass.data[DOMAIN].get("transmission_session_id"):
        headers["X-Transmission-Session-Id"] = sid

    for _attempt in range(2):
        async with session.post(url, json=payload, headers=headers) as resp:
            if resp.status == HTTPStatus.CONFLICT:
                sid = resp.headers.get("X-Transmission-Session-Id", "")
                if not sid:
                    error_message = "Transmission RPC did not provide a session id"
                    raise RuntimeError(error_message)
                hass.data[DOMAIN]["transmission_session_id"] = sid
                headers["X-Transmission-Session-Id"] = sid
                await resp.read()
                continue
            text = await resp.text()
            if resp.status >= HTTPStatus.BAD_REQUEST:
                error_message = f"Transmission RPC HTTP {resp.status}: {text[:200]}"
                raise RuntimeError(error_message)
            data = json.loads(text)
            if "error" in data:
                raise RuntimeError(data["error"])
            return data
    error_message = "Transmission RPC session negotiation failed"
    raise RuntimeError(error_message)


def _rpc_payload(data: dict[str, Any]) -> dict[str, Any]:
    result = data.get("result")
    if isinstance(result, dict):
        return result
    if result == "success":
        return data.get("arguments") or {}
    return data.get("arguments") or {}


async def _http_get_text(
    session: aiohttp.ClientSession,
    cfg: dict[str, Any],
    url: str,
    cookie_header: str = "",
) -> str:
    headers = {"User-Agent": cfg.get(CONF_USER_AGENT, DEFAULT_USER_AGENT)}
    if cookie_header:
        headers["Cookie"] = cookie_header
    timeout = aiohttp.ClientTimeout(total=25)
    async with session.get(url, headers=headers, timeout=timeout) as resp:
        body = await resp.read()
        if resp.status >= HTTPStatus.BAD_REQUEST:
            error_message = f"HTTP {resp.status}"
            raise RuntimeError(error_message)
        return _decode_http_body(body, resp.headers.get("Content-Type", ""))


async def _http_get_text_clean(cfg: dict[str, Any], url: str) -> str:
    return await asyncio.to_thread(_http_get_text_urllib, cfg, url)


def _http_get_text_urllib(cfg: dict[str, Any], url: str) -> str:
    del cfg  # Preserve the public callback parameter names.
    request = urllib.request.Request(  # noqa: S310 - Dispatched only by the HTTP(S)-only opener.
        url,
        headers={
            "User-Agent": FALLBACK_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        },
    )
    with _http_opener().open(request, timeout=25) as response:
        body = response.read()
        if response.status >= HTTPStatus.BAD_REQUEST:
            error_message = f"HTTP {response.status}"
            raise RuntimeError(error_message)
        return _decode_http_body(body, response.headers.get("Content-Type", ""))


def _decode_http_body(body: bytes, content_type: str) -> str:
    encoding = "utf-8"
    if "charset=" in content_type:
        encoding = content_type.rsplit("charset=", 1)[1].split(";", 1)[0].strip()
    elif (
        b"windows-1251" in body[:1000].lower()
        or b"charset=cp1251" in body[:1000].lower()
    ):
        encoding = "cp1251"
    try:
        return body.decode(encoding, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def _parse_topic_rows(text: str, forum_id: int | None, source: str) -> list[Candidate]:
    candidates: list[Candidate] = []
    seen: set[str] = set()
    for match in re.finditer(r'data-topic_id=["\'](?P<id>\d+)["\']', text):
        topic_id = match.group("id")
        if topic_id in seen:
            continue
        seen.add(topic_id)
        start = max(text.rfind("<tr", 0, match.start()), 0)
        end = text.find("</tr>", match.end())
        row = text[start : end if end > 0 else match.end() + 3000]
        title = _extract_title_from_row(row, topic_id)
        if not title:
            continue
        row_forum_id = (
            forum_id if forum_id is not None else _extract_forum_id_from_row(row)
        )
        candidates.append(
            Candidate(
                topic_id=topic_id,
                title=title,
                forum_id=row_forum_id,
                forum_name=(
                    FORUM_NAMES.get(row_forum_id, f"Forum {row_forum_id}")
                    if row_forum_id is not None
                    else ""
                ),
                url=f"https://rutracker.org/forum/viewtopic.php?t={topic_id}",
                size=_extract_size(row),
                seeders=_extract_count(row, "Seeders"),
                leechers=_extract_count(row, "Leechers"),
                approved="tor-approved" in row,
                source=source,
            )
        )
    return candidates


def _extract_forum_id_from_row(row: str) -> int | None:
    patterns = [
        r'href=["\'][^"\']*viewforum\.php\?f=(\d+)',
        r"\bf=(\d+)\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, row, flags=re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def _extract_title_from_row(row: str, topic_id: str) -> str:
    patterns = [
        r'<a[^>]+class=["\'][^"\']*(?:topictitle|tLink|torTopic)[^"\']*["\'][^>]*>(.*?)</a>',
        rf'<a[^>]+href=["\'][^"\']*viewtopic\.php\?t={re.escape(topic_id)}[^"\']*["\'][^>]*>(.*?)</a>',
    ]
    for pattern in patterns:
        matches = re.findall(pattern, row, flags=re.IGNORECASE | re.DOTALL)
        if matches:
            title = _strip_tags(matches[-1])
            if title:
                return title
    return ""


def _extract_size(row: str) -> str:
    match = re.search(
        r'<a[^>]+class=["\'][^"\']*dl-stub[^"\']*["\'][^>]*>(.*?)</a>',
        row,
        re.IGNORECASE | re.DOTALL,
    )
    if match:
        return _strip_tags(match.group(1))
    return ""


def _extract_magnet(text: str) -> str:
    match = re.search(r'magnet:\?xt=urn:btih:[^"\'<\s]+', text, re.IGNORECASE)
    if not match:
        return ""
    return html.unescape(match.group(0)).replace("&amp;", "&")


def _extract_count(text: str, label: str) -> int | None:
    patterns = [
        rf'title=["\']{re.escape(label)}["\'][^>]*>\s*([0-9]+)',
        r'class=["\'][^"\']*(?:seed|leech)[^"\']*["\'][^>]*>\s*([0-9]+)',
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def _strip_tags(value: str) -> str:
    value = re.sub(r"<wbr\s*/?>", "", value, flags=re.IGNORECASE)
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value).replace("\xa0", " ")
    return re.sub(r"\s+", " ", value).strip()


async def _async_read_cookie_header(hass: HomeAssistant, cfg: dict[str, Any]) -> str:
    path = cfg.get(CONF_COOKIES_FILE, DEFAULT_COOKIES_FILE)
    return await hass.async_add_executor_job(_read_cookie_header, path)


def _read_cookie_header(path: str) -> str:
    try:
        content = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if not content:
        return ""
    if "\t" not in content and "=" in content:
        return content.replace("\n", "; ")
    parts = []
    for line in content.splitlines():
        if not line or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) >= NETSCAPE_COOKIE_FIELDS:
            parts.append(f"{fields[5]}={fields[6]}")
    return "; ".join(parts)


def _read_env_values(path: str) -> dict[str, str]:
    try:
        content = Path(path).read_text(encoding="utf-8")
    except OSError:
        return {}
    values: dict[str, str] = {}
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if (
            len(value) >= MIN_QUOTED_VALUE_LENGTH
            and value[0] == value[-1]
            and value[0] in {"'", '"'}
        ):
            value = value[1:-1]
        values[key] = value
    return values


def _should_retry_without_cookies(err: RuntimeError) -> bool:
    message = str(err)
    retryable_statuses = {
        403,
        429,
        500,
        502,
        503,
        520,
        521,
        522,
        523,
        524,
        525,
        526,
        530,
    }
    match = re.search(r"HTTP\s+(\d+)", message)
    return bool(match and int(match.group(1)) in retryable_statuses)


def _append_note_once(notes: list[str], note: str) -> None:
    if note not in notes:
        notes.append(note)


def _summarize_failures(label: str, failures: list[str]) -> str:
    preview = "; ".join(failures[:FAILURE_PREVIEW_LIMIT])
    suffix = (
        ""
        if len(failures) <= FAILURE_PREVIEW_LIMIT
        else f"; +{len(failures) - FAILURE_PREVIEW_LIMIT} more"
    )
    return (
        f"{label} failed for {len(failures)} forum(s); using fallback. "
        f"{preview}{suffix}"
    )


def _write_cookie_header(path: str, cookie_header: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(cookie_header.strip() + "\n", encoding="utf-8")
    try:
        target.chmod(0o600)
    except OSError:
        _LOGGER.debug("Could not restrict cookie file permissions", exc_info=True)


def _count_cookies(cookie_header: str) -> int:
    return sum(1 for part in cookie_header.split(";") if part.strip() and "=" in part)


def _html_attr(attrs: str, name: str) -> str:
    match = re.search(
        rf"\b{re.escape(name)}\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)",
        attrs,
        flags=re.IGNORECASE,
    )
    if not match:
        return ""
    value = match.group(1).strip()
    if (
        len(value) >= MIN_QUOTED_VALUE_LENGTH
        and value[0] in {"'", '"'}
        and value[-1] == value[0]
    ):
        value = value[1:-1]
    return html.unescape(value)


def _form_inputs(text: str) -> dict[str, str]:
    data: dict[str, str] = {}
    for match in re.finditer(
        r"<input\b(?P<attrs>[^>]*)>", text, flags=re.IGNORECASE | re.DOTALL
    ):
        attrs = match.group("attrs")
        name = _html_attr(attrs, "name")
        if not name:
            continue
        data[name] = _html_attr(attrs, "value")
    return data


def _login_form_action(text: str, login_url: str) -> str:
    for match in re.finditer(
        r"<form\b(?P<attrs>[^>]*)>(?P<body>.*?)</form>",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        body = match.group("body")
        if "login_password" not in body and 'type="password"' not in body.lower():
            continue
        action = _html_attr(match.group("attrs"), "action")
        if action:
            return urljoin(login_url, html.unescape(action))
    return login_url


def _origin(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _cookie_header_from_jar(cookie_jar: aiohttp.CookieJar, url: str) -> str:
    parts: list[str] = []
    seen: set[str] = set()
    for name, morsel in cookie_jar.filter_cookies(url).items():
        if name in seen:
            continue
        seen.add(name)
        parts.append(f"{name}={morsel.value}")
    if parts:
        return "; ".join(parts)

    for morsel in cookie_jar:
        name = getattr(morsel, "key", "")
        value = getattr(morsel, "value", "")
        if not name or name in seen:
            continue
        seen.add(name)
        parts.append(f"{name}={value}")
    return "; ".join(parts)


def _looks_like_login_page(text: str, final_url: str) -> bool:
    lowered = text.lower()
    if "login.php" in final_url and "password" in lowered:
        return True
    return (
        "login_password" in lowered
        and "login_username" in lowered
        and not any(marker in lowered for marker in ("logout", "login.php?logout"))
    )


def _forum_ids(value: Any, media_type: str) -> list[int]:
    if value:
        if isinstance(value, str):
            return [int(part.strip()) for part in value.split(",") if part.strip()]
        return [int(item) for item in value]
    if media_type == "movies":
        return MOVIE_FORUMS
    if media_type == "both":
        return SERIES_FORUMS + MOVIE_FORUMS
    return SERIES_FORUMS


def _labels(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        if value.strip() in {"unknown", "unavailable", "none", "None"}:
            return []
        return [part.strip() for part in value.split(",") if part.strip()]
    return [str(item).strip() for item in value if str(item).strip()]


def _topic_id_from_url(url: str) -> str:
    match = re.search(r"[?&]t=(\d+)", url)
    return match.group(1) if match else ""


def _topic_id_from_text(value: str) -> str:
    match = re.search(r"/t/(\d+)", value)
    return match.group(1) if match else ""


def _tokens(value: str) -> list[str]:
    normalized = _norm(value)
    return [
        token for token in normalized.split() if len(token) >= MIN_SEARCH_TOKEN_LENGTH
    ]


def _norm(value: str) -> str:
    # Normalize Cyrillic letters using the existing Russian matching rules.
    value = value.lower().replace("ё", "е")  # noqa: RUF001
    value = re.sub(
        r"[^0-9a-zа-я]+",  # noqa: RUF001 - Cyrillic character range.
        " ",
        value,
        flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", value).strip()


def _all_tokens_match(title: str, tokens: list[str]) -> bool:
    normalized = _norm(title)
    return all(token in normalized for token in tokens)


def _looks_like_requested_media(item: Candidate, media_type: str) -> bool:
    if media_type in ("both", "movies"):
        return True
    if item.forum_id in SERIES_FORUMS:
        return True
    normalized = _norm(item.title)
    return any(marker in normalized for marker in ["сезон", "серии", "сериал"])


def _score_quality(score: float, title: str) -> float:
    if any(
        word in title for word in ["web dl", "webdl", "web dlrip", "web-dl", "hdtv"]
    ):
        score += 5
    if any(
        word in title for word in ["1080", "2160", "uhd", "hdr", "blu ray", "bdrip"]
    ):
        score += 8
    if any(word in title for word in ["camrip", "ts", "telesync", "sample", "trailer"]):
        score -= 30
    return score


def _score(item: Candidate, tokens: list[str]) -> float:
    title = _norm(item.title)
    score = 0.0
    if all(token in title for token in tokens):
        score += 50
    phrase = " ".join(tokens)
    if phrase and phrase in title:
        score += 40
    if item.forum_id in (842, 1803, 2366):
        score += 10
    if item.approved is True:
        score += 8
    if item.seeders is not None:
        score += min(25, math.log(item.seeders + 1) * 7)
    # Match the Russian words for season collections and the first season.
    if "сезоны" in title or re.search(
        r"\bсезон\s+1\s+\d{1,2}\b",  # noqa: RUF001
        title,
    ):
        score += 25
    if re.search(
        r"\bсерии\s+1\s+\d{3}\b",  # noqa: RUF001 - Russian episode range.
        title,
    ):
        score += 10
    return _score_quality(score, title)
