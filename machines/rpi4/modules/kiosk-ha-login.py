"""
Home Assistant auto-login for the kiosk: tokens straight into Chromium's localStorage.

Runs forever. Every POLL seconds it lists the Chromium tabs over CDP (127.0.0.1) and,
for tabs on the Home Assistant origin, checks the two signs of "not logged in": no
hassTokens in localStorage, or the login page /auth/authorize is open. It then walks
HA's login_flow (POST /auth/login_flow -> /auth/login_flow/<id> -> /auth/token),
stores the tokens in localStorage in the format the HA frontend reads
(home-assistant-js-websocket: hassUrl, clientId, expires, expires_in, access_token,
refresh_token, token_type) and opens the target URL again.

The username and password come from systemd credentials ($CREDENTIALS_DIRECTORY):
they are not in the process's argv or environment and are never logged.

Environment: HA_URL (target HA page), CDP_URL (default http://127.0.0.1:9222).
"""

import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from websockets.exceptions import WebSocketException
from websockets.sync.client import connect

POLL = 15
# Pause after a failed login: a wrong password or 2FA must not get the Pi banned by HA.
LOGIN_RETRY = 120
HTTP_TIMEOUT = 20
CDP_TIMEOUT = 20

HA_URL = os.environ["HA_URL"]
CDP_URL = os.environ.get("CDP_URL", "http://127.0.0.1:9222")
CREDS = Path(os.environ["CREDENTIALS_DIRECTORY"])

_ha = urllib.parse.urlsplit(HA_URL)
ORIGIN = f"{_ha.scheme}://{_ha.netloc}"
CLIENT_ID = ORIGIN + "/"

LOG = logging.getLogger("kiosk-ha-login")

# Transient failures: Chromium not up yet, HA unreachable, an unexpected reply.
TRANSIENT_ERRORS = (
    urllib.error.URLError,
    OSError,
    WebSocketException,
    RuntimeError,
    KeyError,
    ValueError,
)


class LoginError(RuntimeError):
    """HA's login_flow did not end with tokens (unexpected step, bad password, 2FA)."""

    def __init__(self, stage: str, reply: dict[str, Any]) -> None:
        """Describe the reply by type, step and errors only, never the credentials."""
        super().__init__(
            f"{stage}: type={reply.get('type')}, step={reply.get('step_id')}, "
            f"errors={reply.get('errors') or {}}"
        )


class CdpError(RuntimeError):
    """Chromium answered a DevTools call with an error or a page exception."""


def read_credential(name: str) -> str:
    """Read one systemd credential (LoadCredential) without its trailing newline."""
    return (CREDS / name).read_text().rstrip("\n")


def http(
    method: str, url: str, body: dict[str, Any] | None = None, *, form: bool = False
) -> Any:
    """Send a JSON or form request to HA or CDP and decode the JSON reply."""
    if urllib.parse.urlsplit(url).scheme not in {"http", "https"}:
        msg = f"refusing a non-HTTP URL: {url}"
        raise ValueError(msg)
    data = None
    headers = {}
    if body is not None:
        if form:
            data = urllib.parse.urlencode(body).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
    # The scheme is checked above, so S310 (file:// and custom schemes) cannot apply.
    request = urllib.request.Request(  # noqa: S310
        url, data=data, headers=headers, method=method
    )
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:  # noqa: S310
        return json.load(response)


def login() -> dict[str, Any]:
    """Walk HA's login_flow and return tokens in the localStorage.hassTokens format."""
    flow = http(
        "POST",
        f"{ORIGIN}/auth/login_flow",
        {
            "client_id": CLIENT_ID,
            "handler": ["homeassistant", None],
            "redirect_uri": CLIENT_ID,
        },
    )
    if flow.get("type") != "form" or flow.get("step_id") != "init":
        stage = "unexpected first login step"
        raise LoginError(stage, flow)
    step = http(
        "POST",
        f"{ORIGIN}/auth/login_flow/{flow['flow_id']}",
        {
            "username": read_credential("ha-username"),
            "password": read_credential("ha-password"),
            "client_id": CLIENT_ID,
        },
    )
    if step.get("type") != "create_entry":
        # 2FA shows up as step_id=mfa, a wrong password as errors.base=invalid_auth.
        stage = "login not completed"
        raise LoginError(stage, step)
    tokens = http(
        "POST",
        f"{ORIGIN}/auth/token",
        {
            "grant_type": "authorization_code",
            "code": step["result"],
            "client_id": CLIENT_ID,
        },
        form=True,
    )
    tokens["hassUrl"] = ORIGIN
    tokens["clientId"] = CLIENT_ID
    tokens["expires"] = int(time.time() * 1000) + tokens["expires_in"] * 1000
    return tokens


class Page:
    """One Chromium tab over its CDP websocket."""

    def __init__(self, ws_url: str) -> None:
        """Connect to the tab's DevTools websocket."""
        self.ws = connect(ws_url, open_timeout=CDP_TIMEOUT, max_size=16 * 1024 * 1024)
        self.next_id = 0

    def close(self) -> None:
        """Close the websocket."""
        self.ws.close()

    def call(self, method: str, **params: Any) -> dict[str, Any]:
        """Send one CDP command and wait for its reply, skipping unrelated events."""
        self.next_id += 1
        call_id = self.next_id
        self.ws.send(json.dumps({"id": call_id, "method": method, "params": params}))
        while True:
            message = json.loads(self.ws.recv(timeout=CDP_TIMEOUT))
            if message.get("id") == call_id:
                if "error" in message:
                    msg = f"CDP {method}: {message['error'].get('message')}"
                    raise CdpError(msg)
                return message["result"]

    def evaluate(self, expression: str) -> Any:
        """Evaluate a JS expression in the page and return its value."""
        result = self.call(
            "Runtime.evaluate", expression=expression, returnByValue=True
        )
        if "exceptionDetails" in result:
            msg = "exception in the page during Runtime.evaluate"
            raise CdpError(msg)
        return result["result"].get("value")


def needs_login(page: Page) -> bool:
    """Tell whether the tab, still on HA, has no tokens or shows the HA login page."""
    state = json.loads(
        page.evaluate(
            "JSON.stringify({origin: location.origin,"
            " tokens: !!localStorage.getItem('hassTokens'), path: location.pathname})"
        )
    )
    if state["origin"] != ORIGIN:
        return False
    return (not state["tokens"]) or state["path"].startswith("/auth/")


def inject(page: Page, tokens: dict[str, Any]) -> bool:
    """Store the tokens and reload HA, only if the tab is still on the HA origin."""
    # The origin check and the write run in one evaluate: the tab may have navigated
    # away during login(), and the tokens must never land in another site's storage.
    # json.dumps twice: JSON inside a JS string literal (tokens never reach the log).
    payload = json.dumps(json.dumps(tokens))
    stored = page.evaluate(
        f"location.origin === {json.dumps(ORIGIN)}"
        f" ? (localStorage.setItem('hassTokens', {payload}), true)"
        " : false"
    )
    if stored:
        page.call("Page.navigate", url=HA_URL)
    return bool(stored)


class Watcher:
    """Polls the HA tabs and logs them in, pausing after a failed login."""

    def __init__(self) -> None:
        """Start with no failed login: -inf, since time.monotonic() counts from boot."""
        self.last_login_failure = float("-inf")

    def pass_once(self) -> None:
        """Log in every HA tab that needs it."""
        targets = http("GET", f"{CDP_URL}/json/list")
        pages = [
            t
            for t in targets
            if t.get("type") == "page"
            and (
                t.get("url", "") == ORIGIN or t.get("url", "").startswith(ORIGIN + "/")
            )
            and "webSocketDebuggerUrl" in t
        ]
        for target in pages:
            page = Page(target["webSocketDebuggerUrl"])
            try:
                if not needs_login(page):
                    continue
                if time.monotonic() - self.last_login_failure < LOGIN_RETRY:
                    continue
                try:
                    tokens = login()
                except TRANSIENT_ERRORS:
                    self.last_login_failure = time.monotonic()
                    raise
                if inject(page, tokens):
                    LOG.info(
                        "logged in to Home Assistant, tokens stored for %s", ORIGIN
                    )
                else:
                    # Count it as a failure: the next login waits LOGIN_RETRY.
                    self.last_login_failure = time.monotonic()
                    LOG.warning("tab left %s during login; tokens dropped", ORIGIN)
            finally:
                page.close()


def main() -> None:
    """Watch the kiosk tabs forever."""
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    LOG.info("kiosk-ha-login: watching tabs of %s through %s", ORIGIN, CDP_URL)
    watcher = Watcher()
    last_error = None
    while True:
        try:
            watcher.pass_once()
            last_error = None
        except TRANSIENT_ERRORS as err:
            # Chromium not up yet or HA unreachable: retry, log each new error once.
            message = f"{type(err).__name__}: {err}"
            if message != last_error:
                LOG.warning("waiting: %s", message)
                last_error = message
        time.sleep(POLL)


if __name__ == "__main__":
    main()
