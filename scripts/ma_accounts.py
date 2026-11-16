#!/usr/bin/env python3
"""
Music Assistant accounts per person and guest (aisis#17) - admin side.

Run inside the Home Assistant container with music_assistant_client and
/config mounted. The host wrappers ytm-cookie, yandex-music-token, and
music-accounts hold /home/ultra/.ma-maintenance.lock while this helper changes
Music Assistant.

Commands:
  list                              Show people, accounts, users, and filters.
  add ytmusic|yandex_music WHO       Create or refresh a person's account.
      [--device] [--dry-run]
  assign WHO --instance ID          Adopt an instance added through the MA UI.
  unassign WHO [--provider D]        Forget an account.
      [--delete]                    Also delete the account when requested.
  rename WHO NAME                   Set the instance display name.
  sync [--dry-run]                  Synchronize instances, users, and filters.
  expire [--dry-run]                Remove expired guest instances and users.

WHO selects --person (user-<id>, person.x, HA username, or name),
--guest NAME [--days N], or --default.

The mapping contains no secrets. It lives at
/config/oww-dataset/voices/music_accounts.json, beside the studio's guests.json,
and uses the same user-<HA user id> and guest-<slug> speaker IDs.
custom_components/music_accounts reads it. The host wrapper accepts a secret
with hidden input and supplies it through MA_SECRET. This helper passes it
to MA's setup flow without printing or retaining it; MA stores it encrypted.

MA 2.10 shares a library. Each user's provider_filter steers search, streaming,
and radio refills to that user's instances. Home Assistant's play_media and
search services accept username. Unmapped users, including HA integration,
UI logins, and party guests, follow the household default.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import datetime as dt
import hashlib
import html
import json
import logging
import os
import re
import secrets
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiohttp
from music_assistant_client import MusicAssistantClient

_LOGGER = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import Callable

DEVICE_AUTH_TEXT_FIELDS = 4

MA_URL = "http://172.20.0.1:8095"
PO_TOKEN_SERVER = "http://127.0.0.1:4416"  # noqa: S105 - loopback service URL.
VOICES = Path("/config/oww-dataset/voices")
MAPPING = VOICES / "music_accounts.json"
GUESTS = VOICES / "guests.json"
STORAGE = Path("/config/.storage")
SERVICES = {"ytmusic": "YouTube Music", "yandex_music": "Yandex Music"}
GUEST_LIBRARY_OFF = (
    "library_sync_artists",
    "library_sync_albums",
    "library_sync_tracks",
    "library_sync_playlists",
    "library_sync_podcasts",
    "library_sync_audiobooks",
)


class AccountsError(Exception):
    """A clean, user-facing error."""


Fail = AccountsError  # Compatibility name for existing importers.


def as_dict(obj: Any) -> Any:
    """Convert client model objects and lists into their existing dictionary form."""
    if isinstance(obj, list):
        return [as_dict(o) for o in obj]
    if isinstance(obj, dict) or obj is None:
        return obj
    return obj.to_dict() if hasattr(obj, "to_dict") else vars(obj)


def now() -> dt.datetime:
    """Return the current timezone-aware UTC timestamp."""
    return dt.datetime.now(dt.UTC)


def studio_slug(text: str) -> str:
    """Build the same guest speaker slug as the voice studio."""
    return re.sub(r"[^0-9a-zа-яё]+", "-", text.lower()).strip("-")[:30] or "guest"  # noqa: RUF001 - Preserve Cyrillic names.


def read_json(path: Path, default: Any) -> Any:
    """Read JSON data, falling back to the supplied value on read or parse errors."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path: Path, data: Any) -> None:
    """Replace a JSON file through the existing temporary sibling file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    tmp.replace(path)


def ha_people() -> list[dict[str, Any]]:
    """Read household persons linked to Home Assistant users."""
    persons = read_json(STORAGE / "person", {}).get("data", {}).get("items", [])
    auth = read_json(STORAGE / "auth", {}).get("data", {})
    usernames = {
        c["user_id"]: (c.get("data") or {}).get("username")
        for c in auth.get("credentials", [])
        if c.get("auth_provider_type") == "homeassistant"
    }
    owners = {u["id"] for u in auth.get("users", []) if u.get("is_owner")}
    return [
        {
            "speaker": f"user-{p['user_id']}",
            "entity": f"person.{p['id']}",
            "name": p["name"],
            "username": usernames.get(p["user_id"]),
            "owner": p["user_id"] in owners,
        }
        for p in persons
        if p.get("user_id")
    ]


def is_expired(entry: dict[str, Any]) -> bool:
    """Check whether the account entry's configured expiry is in the past."""
    return (
        bool(entry.get("expires_at"))
        and dt.datetime.fromisoformat(entry["expires_at"]) < now()
    )


class Tool:
    """Manage person-specific Music Assistant accounts and provider filters."""

    def __init__(self, client: MusicAssistantClient, args: argparse.Namespace) -> None:
        """Load the account mapping and retain the selected command settings."""
        self.c = client
        self.args = args
        self.dry = bool(getattr(args, "dry_run", False))
        self.mapping: dict[str, Any] = read_json(MAPPING, {}) or {}

    async def cmd(self, command: str, **kwargs: Any) -> Any:
        """Send a Music Assistant command and normalize its response."""
        return as_dict(await self.c.send_command(command, **kwargs))

    # ------------------------------------------------------------------ state

    async def providers(self) -> list[dict[str, Any]]:
        """List configured music provider instances."""
        return [
            p for p in await self.cmd("config/providers") if p.get("type") == "music"
        ]

    async def users(self) -> list[dict[str, Any]]:
        """List Music Assistant users and their existing settings."""
        return await self.cmd("auth/users")

    @property
    def people(self) -> dict[str, dict[str, Any]]:
        """Return the person mapping, creating its container when absent."""
        return self.mapping.setdefault("people", {})

    def save(self) -> None:
        """Persist the versioned account mapping unless this is a dry run."""
        self.mapping["version"] = 1
        if self.dry:
            print("dry run: mapping not written")
            return
        write_json(MAPPING, self.mapping)

    async def bootstrap(self) -> None:
        """Assign existing accounts to the owner on the first run."""
        if self.mapping.get("default"):
            return
        owner = next((p for p in ha_people() if p["owner"]), None)
        if owner is None:
            error_message = "No HA owner person found to use as the household default"
            raise Fail(error_message)
        me = await self.cmd("auth/me")
        providers: dict[str, str] = {}
        for domain in SERVICES:
            instances = [
                p["instance_id"]
                for p in await self.providers()
                if p["domain"] == domain
            ]
            if len(instances) == 1:
                providers[domain] = instances[0]
            elif len(instances) > 1:
                print(
                    f"note: {len(instances)} {domain} instances, "
                    "assign the default one by hand"
                )
        self.mapping.update({"version": 1, "default": owner["speaker"]})
        self.people.setdefault(
            owner["speaker"],
            {"name": owner["name"], "ma_user": me["username"], "providers": providers},
        )
        print(
            f"household default = {owner['name']} ({owner['speaker']}), "
            f"MA user {me['username']}"
        )

    def who(self) -> tuple[str, dict[str, Any]]:
        """Resolve the selected speaker and its account mapping template."""
        a = self.args
        if getattr(a, "default", False):
            speaker = self.mapping.get("default")
            if not speaker:
                error_message = "No household default yet"
                raise Fail(error_message)
            return speaker, self.people.get(speaker, {})
        if getattr(a, "guest", None):
            return self._guest_selection(a)
        wanted = (getattr(a, "person", None) or "").strip()
        if not wanted:
            error_message = "Say who: --person NAME, --guest NAME or --default"
            raise Fail(error_message)
        if wanted.startswith(("user-", "guest-")):
            return wanted, dict(self.people.get(wanted) or {"name": wanted})
        return self._named_person_selection(wanted)

    def _guest_selection(self, args: argparse.Namespace) -> tuple[str, dict[str, Any]]:
        """Resolve a guest while retaining the studio's existing expiry rules."""
        slug = studio_slug(args.guest)
        speaker = f"guest-{slug}"
        entry = dict(self.people.get(speaker) or {"name": args.guest.strip()[:40]})
        entry["guest"] = True
        # same lifetime rules as the studio's guest voices: 1-7 days, default 3; a
        # guest
        # who already enrolled a voice keeps that expiry unless --days is given
        studio = (read_json(GUESTS, {}) or {}).get(slug) or {}
        if args.days:
            days = max(1, min(7, args.days))
            entry["expires_at"] = (now() + dt.timedelta(days=days)).isoformat()
        elif not entry.get("expires_at"):
            entry["expires_at"] = (
                studio.get("expires_at") or (now() + dt.timedelta(days=3)).isoformat()
            )
        return speaker, entry

    def _named_person_selection(self, wanted: str) -> tuple[str, dict[str, Any]]:
        """Match the requested HA identity or existing mapped display name."""
        key = wanted.lower()
        for p in ha_people():
            if key in (
                p["entity"].lower(),
                (p["username"] or "").lower(),
                p["name"].lower(),
            ):
                return p["speaker"], dict(
                    self.people.get(p["speaker"]) or {"name": p["name"]}
                )
        for speaker, entry in self.people.items():
            if key == str(entry.get("name", "")).lower():
                return speaker, dict(entry)
        error_message = (
            f"Unknown person {wanted!r} "
            f"(HA persons: {', '.join(p['name'] for p in ha_people())})"
        )
        raise Fail(error_message)

    # ------------------------------------------------------------------ flows

    async def _wait(
        self, flow_id: str, step: dict[str, Any], seconds: int
    ) -> dict[str, Any]:
        shown = None
        for _ in range(seconds // 2):
            if step.get("type") != "progress":
                return step
            if step.get("image") and step["image"] != shown:
                shown = step["image"]
                self._show_device_code(step["image"])
            await asyncio.sleep(2)
            step = await self.cmd("config/flows/get", flow_id=flow_id)
        return step

    @staticmethod
    def _show_device_code(image: str) -> None:
        """Display the address and one-time code from the Yandex SVG step."""
        if not image.startswith("data:image/svg+xml;base64,"):
            print("Scan / confirm in the Music Assistant UI (Settings -> Providers).")
            return
        svg = base64.b64decode(image.split(",", 1)[1]).decode("utf-8", "replace")
        texts = [
            html.unescape(t) for t in re.findall(r"<text[^>]*>([^<]*)</text>", svg)
        ]
        if len(texts) >= DEVICE_AUTH_TEXT_FIELDS:
            print(
                f"\n  On the phone or computer of that person open  https://{texts[1]}"
            )
            print(
                f"  sign in to THEIR Yandex account and enter the code:  {texts[3]}\n",
                flush=True,
            )

    async def _abort(self, flow_id: str) -> None:
        with contextlib.suppress(Exception):  # best effort
            await self.cmd("config/flows/abort", flow_id=flow_id)

    async def run_flow(self, domain: str, instance: str | None) -> str | None:
        """Create or reconfigure a provider through its MA setup flow."""
        if instance:
            step = await self.cmd("config/providers/reconfigure", instance_id=instance)
        else:
            step = await self.cmd("config/providers/setup", provider_domain=domain)
        flow_id = step.get("flow_id")
        what = f"refresh {instance}" if instance else f"create a new {domain} instance"
        if step.get("type") != "form" or not flow_id:
            error_message = (
                f"MA refused to {what}: {step.get('type')} {step.get('reason') or ''}"
            )
            raise Fail(error_message)
        if self.dry:
            await self._abort_dry_run_flow(domain, flow_id, what)
            return None
        secret = os.environ.get("MA_SECRET", "")
        try:
            step = await self._submit_provider_flow(domain, flow_id, secret)
        except BaseException:
            await self._abort(flow_id)
            raise
        if step.get("type") == "finish":
            return (step.get("result") or {}).get("instance_id") or instance
        await self._abort(flow_id)
        error_message = (
            "MA did not accept it: "
            f"{step.get('errors') or step.get('reason') or step.get('type')}"
        )
        raise Fail(error_message)

    async def _abort_dry_run_flow(self, domain: str, flow_id: str, what: str) -> None:
        """Abort the opened dry-run flow before reporting existing instances."""
        await self._abort(flow_id)
        existing = [
            p["instance_id"] for p in await self.providers() if p["domain"] == domain
        ]
        print(
            f"dry run: MA opened the flow to {what} "
            f"(existing {domain}: {existing}); aborted"
        )

    async def _submit_provider_flow(
        self, domain: str, flow_id: str, secret: str
    ) -> dict[str, Any]:
        """Submit the existing provider-specific fields and wait for its result."""
        if domain == "ytmusic":
            values = {
                "username": "default",
                "cookie": secret,
                "po_token_server_url": PO_TOKEN_SERVER,
            }
            step = await self.cmd("config/flows/submit", flow_id=flow_id, values=values)
            step = await self._wait(flow_id, step, 60)
        else:
            device = bool(self.args.device)
            step = await self.cmd(
                "config/flows/submit",
                flow_id=flow_id,
                values={
                    "method": "device" if device else "token",
                    "remember_session": True,
                },
            )
            if (
                not device
                and step.get("type") == "form"
                and step.get("step_id") == "token_login"
            ):
                step = await self.cmd(
                    "config/flows/submit", flow_id=flow_id, values={"token": secret}
                )
            step = await self._wait(flow_id, step, 16 * 60 if device else 60)
        return step

    # ------------------------------------------------------------------ commands

    async def add(self) -> int:
        """Create or refresh the selected person's provider account."""
        domain = self.args.service
        await self.bootstrap()
        speaker, entry = self.who()
        existing = {p["instance_id"] for p in await self.providers()}
        instance = (entry.get("providers") or {}).get(domain)
        if instance not in existing:
            instance = None
        if not self.dry and domain == "ytmusic" and not os.environ.get("MA_SECRET"):
            error_message = "No cookie given"
            raise Fail(error_message)
        if (
            not self.dry
            and domain == "yandex_music"
            and not self.args.device
            and not os.environ.get("MA_SECRET")
        ):
            error_message = "No token given (or use --device)"
            raise Fail(error_message)
        new_id = await self.run_flow(domain, instance)
        if new_id is None:
            return 0
        entry.setdefault("providers", {})[domain] = new_id
        self.people[speaker] = entry
        print(
            f"{SERVICES[domain]} of {entry.get('name')}: "
            f"{'refreshed' if instance else 'created'} {new_id}"
        )
        self.save()
        await self.sync()
        return 0

    async def assign(self) -> int:
        """Associate an existing provider instance with the selected person."""
        await self.bootstrap()
        speaker, entry = self.who()
        conf = next(
            (
                p
                for p in await self.providers()
                if p["instance_id"] == self.args.instance
            ),
            None,
        )
        if conf is None or conf["domain"] not in SERVICES:
            error_message = (
                f"No YouTube Music / Yandex Music instance {self.args.instance}"
            )
            raise Fail(error_message)
        for other, e in self.people.items():
            if (
                other != speaker
                and self.args.instance in (e.get("providers") or {}).values()
            ):
                error_message = (
                    f"{self.args.instance} already belongs to {e.get('name')} ({other})"
                )
                raise Fail(error_message)
        entry.setdefault("providers", {})[conf["domain"]] = conf["instance_id"]
        self.people[speaker] = entry
        print(f"{conf['instance_id']} -> {entry.get('name')} ({speaker})")
        self.save()
        await self.sync()
        return 0

    async def unassign(self) -> int:
        """Remove the selected associations and apply requested account deletion."""
        speaker, entry = self.who()
        if speaker not in self.people:
            error_message = f"{speaker} has no accounts"
            raise Fail(error_message)
        if speaker == self.mapping.get("default") and self.args.delete:
            error_message = "Refusing to delete the household default's accounts"
            raise Fail(error_message)
        domains = (
            [self.args.provider]
            if self.args.provider
            else list(entry.get("providers") or {})
        )
        for domain in domains:
            instance = (entry.get("providers") or {}).pop(domain, None)
            if instance and self.args.delete:
                await self._remove_instance(instance)
        if not entry.get("providers") and speaker != self.mapping.get("default"):
            await self._remove_ma_user(entry)
            self.people.pop(speaker, None)
        else:
            self.people[speaker] = entry
        self.save()
        await self.sync()
        return 0

    async def rename(self) -> int:
        """Update the selected person's display name and synchronize providers."""
        await self.bootstrap()
        speaker, entry = self.who()
        entry["name"] = self.args.name
        self.people[speaker] = entry
        self.save()
        await self.sync()
        return 0

    async def expire(self) -> int:
        """Remove expired guest accounts and synchronize remaining settings."""
        expired = [
            s for s, e in self.people.items() if e.get("guest") and is_expired(e)
        ]
        for speaker in expired:
            entry = self.people[speaker]
            print(
                f"guest {entry.get('name')} expired {entry.get('expires_at')}: removing"
            )
            for instance in (entry.get("providers") or {}).values():
                await self._remove_instance(instance)
            await self._remove_ma_user(entry)
            self.people.pop(speaker)
        if expired:
            self.save()
        # hourly from cron: sync also picks up providers added in the MA UI into the
        # filters
        return await self.sync()

    async def _remove_instance(self, instance: str) -> None:
        print(f"{'would remove' if self.dry else 'removing'} MA instance {instance}")
        if not self.dry:
            try:
                await self.cmd("config/providers/remove", instance_id=instance)
            except Exception as err:  # noqa: BLE001 - already gone is fine.
                print(f"  ({err})")

    async def _remove_ma_user(self, entry: dict[str, Any]) -> None:
        name = entry.get("ma_user")
        if not name or not entry.get("ma_user_created"):
            return  # never delete an MA user this tool did not create
        user = next((u for u in await self.users() if u["username"] == name), None)
        if user:
            print(f"{'would delete' if self.dry else 'deleting'} MA user {name}")
            if not self.dry:
                await self.cmd("auth/user/delete", user_id=user["user_id"])

    # ------------------------------------------------------------------ sync

    async def sync(self) -> int:
        """Make MA match the mapping: names, per-instance options, MA users, filters."""
        await self.bootstrap()
        confs = {p["instance_id"]: p for p in await self.providers()}
        default_speaker = self.mapping.get("default")
        default = self.people.get(default_speaker or "", {})
        default_own = dict(default.get("providers") or {})

        changed = await self._sync_provider_settings(confs, default_speaker)

        owned = {
            i for e in self.people.values() for i in (e.get("providers") or {}).values()
        }
        foreign = owned - set(default_own.values())
        shared = [i for i in confs if i not in owned]

        def filter_for(entry: dict[str, Any] | None) -> list[str]:
            """Combine own accounts, missing-service defaults, and shared instances."""
            if not foreign:
                return []  # one account per service: nothing to steer
            if entry is None:
                # everybody else (HA integration, UI logins, party guest) = household
                # default; without a filter MA picks whichever account loaded first
                return sorted({*shared, *default_own.values()})
            own = dict(entry.get("providers") or {})
            fallback = [i for d, i in default_own.items() if d not in own]
            return sorted({*shared, *own.values(), *fallback})

        users, users_changed = await self._sync_person_users(
            default_speaker, filter_for
        )
        filters_changed = await self._sync_provider_filters(
            users, default_speaker, filter_for
        )
        changed = changed or users_changed or filters_changed
        if changed:
            self.save()
        else:
            print("in sync")
        return 0

    async def _sync_provider_settings(
        self, confs: dict[str, Any], default_speaker: str | None
    ) -> bool:
        """Apply the existing per-person names and provider sync settings."""
        changed = False
        # 1. instances: names, sync-back only on the default's Yandex, guests import
        # nothing
        for speaker, entry in self.people.items():
            entry_changed = await self._sync_person_provider_settings(
                speaker, entry, confs, default_speaker
            )
            changed = changed or entry_changed
        return changed

    async def _sync_person_provider_settings(
        self,
        speaker: str,
        entry: dict[str, Any],
        confs: dict[str, Any],
        default_speaker: str | None,
    ) -> bool:
        """Apply each configured provider's settings for one person."""
        changed = False
        for domain, instance in (entry.get("providers") or {}).items():
            conf = confs.get(instance)
            if conf is None:
                print(
                    f"warning: {instance} of {entry.get('name')} does not exist in MA"
                )
                continue
            guest = bool(entry.get("guest"))
            name = (
                f"{SERVICES[domain]} — {entry.get('name')}{' (гость)' if guest else ''}"
            )
            want: dict[str, Any] = {}
            if conf.get("name") != name:
                want["name"] = name
            current = await self._values(instance)
            if domain == "yandex_music" and speaker != default_speaker:
                # MA forwards a library add to EVERY mapped Yandex account with
                # sync-back
                # on; only the household default keeps it, persons like via
                # music_accounts
                want.update(self._diff(current, {"library_sync_back": False}))
            if guest:
                want.update(
                    self._diff(current, dict.fromkeys(GUEST_LIBRARY_OFF, False))
                )
            if want:
                changed = True
                await self._save_provider_settings(domain, instance, want)
        return changed

    async def _save_provider_settings(
        self, domain: str, instance: str, want: dict[str, Any]
    ) -> None:
        """Report a provider update and apply it outside dry runs."""
        print(f"{instance}: set {want}")
        if not self.dry:
            await self.cmd(
                "config/providers/save",
                provider_domain=domain,
                instance_id=instance,
                values=want,
            )

    async def _sync_person_users(
        self,
        default_speaker: str | None,
        filter_for: Callable[[dict[str, Any] | None], list[str]],
    ) -> tuple[dict[str, Any], bool]:
        """Create missing impersonation users and retain existing identities."""
        changed = False
        # 2. MA users for persons with own accounts
        users = {u["username"]: u for u in await self.users()}
        for speaker, entry in self.people.items():
            if (
                speaker == default_speaker
                or not entry.get("providers")
                or is_expired(entry)
            ):
                continue
            users, person_changed = await self._sync_person_user(
                speaker, entry, users, filter_for
            )
            changed = changed or person_changed
        return users, changed

    async def _sync_person_user(
        self,
        speaker: str,
        entry: dict[str, Any],
        users: dict[str, Any],
        filter_for: Callable[[dict[str, Any] | None], list[str]],
    ) -> tuple[dict[str, Any], bool]:
        """Retain or create the impersonation identity for one eligible person."""
        username = entry.get("ma_user") or self._ma_username(speaker, entry)
        if username in users:
            if entry.get("ma_user") != username:
                # an MA user named like the HA user is that person (e.g. an HA
                # login)
                entry["ma_user"] = username
                return users, True
            return users, False
        print(
            f"{'would create' if self.dry else 'creating'} MA user {username} for "
            f"{entry.get('name')}, "
            f"provider filter {filter_for(entry) or 'none (all)'}"
        )
        if not self.dry:
            await self.cmd(
                "auth/user/create",
                username=username,
                # impersonation-only identity: nobody logs in with it, the password
                # is
                # random and deliberately not kept anywhere
                password=secrets.token_urlsafe(32),
                role="guest" if entry.get("guest") else "user",
                display_name=(
                    f"{entry.get('name')}{' (гость)' if entry.get('guest') else ''}"
                ),
            )
            entry["ma_user"] = username
            entry["ma_user_created"] = True
            users = {u["username"]: u for u in await self.users()}
        return users, True

    async def _sync_provider_filters(
        self,
        users: dict[str, Any],
        default_speaker: str | None,
        filter_for: Callable[[dict[str, Any] | None], list[str]],
    ) -> bool:
        """Update managed filters while preserving manually edited filters."""
        changed = False
        # 3. provider filters
        person_users = {
            e["ma_user"]: e
            for s, e in self.people.items()
            if e.get("ma_user") and s != default_speaker
        }
        # filters this tool set last time; a filter someone changed by hand is left
        # alone
        managed: dict[str, list[str]] = self.mapping.setdefault("filters", {})
        for username, user in users.items():
            if not user.get("enabled", True):
                continue
            want_filter = filter_for(person_users.get(username))
            user_changed = await self._sync_user_provider_filter(
                username, user, want_filter, managed
            )
            changed = changed or user_changed
        return changed

    async def _sync_user_provider_filter(
        self,
        username: str,
        user: dict[str, Any],
        want_filter: list[str],
        managed: dict[str, list[str]],
    ) -> bool:
        """Update one managed user filter without overriding a manual change."""
        changed = False
        current = sorted(user.get("provider_filter") or [])
        if current and current != sorted(managed.get(username) or []):
            print(
                f"MA user {username}: filter was set outside music-accounts, left alone"
            )
            return False
        if current != want_filter:
            changed = True
            print(f"MA user {username}: provider filter {want_filter or 'none (all)'}")
            if not self.dry:
                await self.cmd(
                    "auth/user/update",
                    user_id=user["user_id"],
                    provider_filter=want_filter,
                )
        if managed.get(username) != want_filter:
            managed[username] = want_filter
            changed = True
        return changed

    async def _values(self, instance: str) -> dict[str, Any]:
        conf = await self.cmd("config/providers/get", instance_id=instance)
        return {
            k: (v or {}).get("value")
            for k, v in (conf.get("values") or {}).items()
            if isinstance(v, dict)
        }

    @staticmethod
    def _diff(current: dict[str, Any], want: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in want.items() if k in current and current[k] != v}

    @staticmethod
    def _ma_username(speaker: str, entry: dict[str, Any]) -> str:
        if entry.get("guest"):
            return (
                "guest_"
                + hashlib.sha1(speaker.encode(), usedforsecurity=False).hexdigest()[:8]
            )
        person = next((p for p in ha_people() if p["speaker"] == speaker), None)
        username = (person or {}).get("username") or speaker.removeprefix("user-")[:12]
        return re.sub(r"[^a-z0-9_]+", "_", username.lower())

    # ------------------------------------------------------------------ list

    async def list(self) -> int:
        """Print the current person mappings, accounts, and provider filters."""
        confs = {p["instance_id"]: p for p in await self.providers()}
        users = {u["username"]: u for u in await self.users()}
        default = self.mapping.get("default")
        mapped: set[str] = set()
        print(f"mapping: {MAPPING}{'' if MAPPING.exists() else ' (not created yet)'}")
        known = {p["speaker"]: p for p in ha_people()}
        for speaker in [*self.people, *[s for s in known if s not in self.people]]:
            entry = self.people.get(speaker, {"name": known[speaker]["name"]})
            self._print_person_summary(speaker, entry, users, default)
            self._print_person_accounts(entry, confs, mapped)
        self._print_unassigned_accounts(confs, users, mapped)
        return 0

    def _print_unassigned_accounts(
        self, confs: dict[str, Any], users: dict[str, Any], mapped: set[str]
    ) -> None:
        """Print the remaining providers and users after all mapped people."""
        rest = [
            i for i, c in confs.items() if c["domain"] in SERVICES and i not in mapped
        ]
        if rest:
            print(
                f"\nunassigned: {', '.join(rest)}  "
                "(music-accounts assign --person NAME --instance ID)"
            )
        others = [
            u
            for u in users
            if u not in {e.get("ma_user") for e in self.people.values()}
        ]
        print(f"\nother MA users (follow the household default): {', '.join(others)}")

    @staticmethod
    def _print_person_summary(
        speaker: str,
        entry: dict[str, Any],
        users: dict[str, Any],
        default: str | None,
    ) -> None:
        """Print a person's existing tags, MA identity, and provider filter."""
        tags = []
        if speaker == default:
            tags.append("household default")
        if entry.get("guest"):
            tags.append(
                f"guest until {entry.get('expires_at')}"
                f"{' EXPIRED' if is_expired(entry) else ''}"
            )
        print(f"\n{entry.get('name')}  [{speaker}]  {', '.join(tags)}")
        ma_user = entry.get("ma_user")
        if ma_user:
            flt = (users.get(ma_user) or {}).get("provider_filter")
            print(
                f"  MA user: {ma_user}"
                f"{'' if ma_user in users else ' (missing)'}  "
                f"filter: {flt or 'none (all)'}"
            )

    @staticmethod
    def _print_person_accounts(
        entry: dict[str, Any], confs: dict[str, Any], mapped: set[str]
    ) -> None:
        """Print the existing account status rows and record their instances."""
        own = entry.get("providers") or {}
        if not own:
            print(
                "  no own accounts: plays from the household default, likes are refused"
            )
        for domain, instance in own.items():
            mapped.add(instance)
            conf = confs.get(instance) or {}
            state = (
                "OK"
                if conf and not conf.get("last_error")
                else (conf.get("last_error") or "MISSING")
            )
            print(
                f"  {SERVICES.get(domain, domain)}: {instance}  "
                f"{conf.get('name') or ''}  [{state}]"
            )


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse the supported account administration command and its options."""
    parser = argparse.ArgumentParser(
        prog="music-accounts", description=__doc__.split("\n\n")[0]
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def who(p: argparse.ArgumentParser) -> None:
        g = p.add_mutually_exclusive_group()
        g.add_argument("--person", help="user-<id>, person.x, HA username or name")
        g.add_argument(
            "--guest", help="guest name (speaker guest-<slug> as in the voice studio)"
        )
        g.add_argument("--default", action="store_true", help="the household default")
        p.add_argument(
            "--days", type=int, help="guest lifetime in days (1-7, default 3)"
        )

    sub.add_parser("list")
    p = sub.add_parser("add")
    p.add_argument("service", choices=list(SERVICES))
    who(p)
    p.add_argument(
        "--device",
        action="store_true",
        help="Yandex: sign in with a code on ya.ru/device",
    )
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("assign")
    who(p)
    p.add_argument("--instance", required=True)
    p = sub.add_parser("unassign")
    who(p)
    p.add_argument("--provider", choices=list(SERVICES))
    p.add_argument("--delete", action="store_true", help="also delete the MA instance")
    p = sub.add_parser("rename")
    who(p)
    p.add_argument("name")
    p = sub.add_parser("sync")
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("expire")
    p.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


async def main(argv: list[str]) -> int:
    """Connect to Music Assistant and run the selected administration command."""
    args = parse_args(argv)
    async with aiohttp.ClientSession() as session:
        client = MusicAssistantClient(MA_URL, session, token=os.environ["MA_TOKEN"])
        await client.connect()
        try:
            tool = Tool(client, args)
            return await getattr(tool, args.command)()
        except Fail as err:
            print(err, file=sys.stderr)
            return 1
        finally:
            await client.disconnect()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
