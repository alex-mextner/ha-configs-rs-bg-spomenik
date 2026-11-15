"""
Per-person likes: like or unlike a track in exactly one streaming account (aisis#17).

Why this exists (Music Assistant 2.10.4):
- the library is shared: MusicController.match_provider_instances() gives a library
item a
  mapping for EVERY instance of a streaming provider, and music/library/add_item
forwards the
  add to every mapped instance with sync-back on, so a like from MA would land in all
accounts;
- music/favorites/add_item only forwards to providers with FAVORITE_*_EDIT, which
neither
  ytmusic nor yandex_music declares (the favourite stays inside MA);
- ytmusic cannot like a track at all (library_add raises NotImplementedError for
tracks).

This plugin adds two API commands that act on the ONE provider instance the caller picks
from a person's own accounts, with that instance's stored credentials (they never
leave MA):

  person_likes/like    provider_instances, player_id | uri, liked=True, dry_run=False
  person_likes/status  provider_instances, player_id | uri

Home Assistant's music_accounts integration calls them; the mapping person ->
instances lives
there. Mounted read-only into the MA image by ha.docker-compose.yaml; tested on MA
2.10.4.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import ytmusicapi
from music_assistant.controllers.webserver.helpers.auth_middleware import (
    get_current_user,
    has_scope,
)
from music_assistant.models.plugin import PluginProvider
from music_assistant_models.auth import Scope
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import (
    InsufficientPermissions,
    InvalidDataError,
    LoginFailed,
    MediaNotFoundError,
    PlayerUnavailableError,
    ProviderUnavailableError,
    UnsupportedFeaturedException,
)
from ytmusicapi import LikeStatus
from ytmusicapi.helpers import get_authorization, sapisid_from_cookie

if TYPE_CHECKING:
    from collections.abc import Callable

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType
    from music_assistant.models.music_provider import MusicProvider
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.media_items import Track
    from music_assistant_models.provider import ProviderManifest

SUPPORTED_DOMAINS = ("ytmusic", "yandex_music")
_YOUTUBE_BRAND_ID_LENGTH = 21
YTM_ORIGIN = "https://music.youtube.com"


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return PersonLikesProvider(mass, manifest, config)


class PersonLikesProvider(PluginProvider):
    """Registers the person_likes/* API commands."""

    _unregister: list[Callable[[], None]]

    async def loaded_in_mass(self) -> None:
        """Register the API commands once loaded."""
        await super().loaded_in_mass()
        self._unregister = [
            self.mass.register_api_command(
                "person_likes/like", self.like, required_scope=Scope.LIBRARY_WRITE
            ),
            self.mass.register_api_command(
                "person_likes/status", self.status, required_scope=Scope.LIBRARY_READ
            ),
        ]

    async def unload(self, is_removed: bool = False) -> None:  # noqa: FBT001, FBT002 -- Preserve PluginProvider positional override.
        """Unregister the API commands."""
        for unregister in getattr(self, "_unregister", []):
            unregister()
        await super().unload(is_removed)

    async def like(
        self,
        provider_instances: list[str],
        player_id: str | None = None,
        uri: str | None = None,
        liked: bool = True,  # noqa: FBT001, FBT002 -- Registered MA API signature.
        dry_run: bool = False,  # noqa: FBT001, FBT002 -- Registered MA API signature.
    ) -> dict[str, Any]:
        """
        Like (or unlike) a track in exactly one of the given accounts.

        :param provider_instances: One person's own streaming instance ids.
        :param player_id: Take the track playing on this player's active queue...
        :param uri: ...or this track uri (give exactly one of the two).
        :param liked: True to like, False to remove the like.
        :param dry_run: Only resolve account + track id, change nothing.
        """
        track, playing_on = await self._get_track(player_id, uri)
        prov, item_id = await self._resolve(track, provider_instances, playing_on)
        result = self._describe(track, prov, item_id, playing_on)
        if dry_run:
            return {**result, "dry_run": True}
        if prov.domain == "ytmusic":
            ok = await self._ytm_rate(prov, item_id, liked=liked)
        else:
            ok = await self._yandex_set(prov, item_id, liked=liked)
        if not ok:
            msg = f"{prov.name} did not accept the {'like' if liked else 'unlike'}"
            raise ProviderUnavailableError(msg)
        self.logger.info(
            "%s %s in %s", "Liked" if liked else "Unliked", result["track"], prov.name
        )
        return {**result, "liked": liked}

    async def status(
        self,
        provider_instances: list[str],
        player_id: str | None = None,
        uri: str | None = None,
    ) -> dict[str, Any]:
        """Return whether the track is liked in the account `like` would use."""
        track, playing_on = await self._get_track(player_id, uri)
        prov, item_id = await self._resolve(track, provider_instances, playing_on)
        if prov.domain == "ytmusic":
            liked = await self._ytm_status(prov, item_id) == "LIKE"
        else:
            liked = await self._yandex_status(prov, item_id)
        return {**self._describe(track, prov, item_id, playing_on), "liked": liked}

    # ---------------------------------------------------------------- resolution

    async def _get_track(
        self, player_id: str | None, uri: str | None
    ) -> tuple[Track, str | None]:
        """Return the track (and the instance streaming it, if playing)."""
        if bool(player_id) == bool(uri):
            message = "Give exactly one of player_id or uri"
            raise InvalidDataError(message)
        playing_on: str | None = None
        if player_id:
            if not (player := self.mass.players.get_player(player_id)):
                message = f"Player {player_id} is not available"
                raise PlayerUnavailableError(message)
            queue = self.mass.players.get_active_queue(player)
            current = queue.current_item if queue else None
            if not current or not current.media_item:
                message = "Nothing is playing on this player"
                raise MediaNotFoundError(message)
            item: Any = current.media_item
            if current.streamdetails:
                playing_on = current.streamdetails.provider
        else:
            item = await self.mass.music.get_item_by_uri(str(uri))
        if getattr(item, "media_type", None) != MediaType.TRACK:
            message = "Only tracks can be liked"
            raise UnsupportedFeaturedException(message)
        return item, playing_on

    async def _resolve(
        self, track: Track, instances: list[str], playing_on: str | None
    ) -> tuple[MusicProvider, str]:
        """Pick the account and the track id in its catalogue."""
        targets = self._target_accounts(instances)
        playing_domain = None
        if playing_on and (
            p := self.mass.get_provider(playing_on, return_unavailable=True)
        ):
            playing_domain = p.domain
        # Catalogue IDs identify the same track across instances of a service.
        mapped: dict[str, str] = {}
        for mapping in sorted(track.provider_mappings, key=lambda m: not m.available):
            mapped.setdefault(mapping.provider_domain, mapping.item_id)
        # Prefer the playing service, then an account that already knows the track.
        targets.sort(key=lambda p: (p.domain != playing_domain, p.domain not in mapped))
        for prov in targets:
            if item_id := mapped.get(prov.domain):
                return prov, item_id
        return await self._find_track_match(track, targets)

    def _target_accounts(self, instances: list[str]) -> list[MusicProvider]:
        """Validate every selected account before resolving a catalogue track."""
        self._check_caller(instances)
        targets: list[MusicProvider] = []
        for instance_id in instances:
            prov = self.mass.get_provider(instance_id)
            # MA may return another account when the requested one is unavailable.
            if prov is None or prov.instance_id != instance_id or not prov.available:
                message = f"Account {instance_id} is not available"
                raise ProviderUnavailableError(message)
            if prov.domain not in SUPPORTED_DOMAINS:
                message = f"Likes are not supported for {prov.domain}"
                raise UnsupportedFeaturedException(message)
            targets.append(prov)  # type: ignore[arg-type]
        if not targets:
            message = "No account given"
            raise InvalidDataError(message)
        return targets

    async def _find_track_match(
        self, track: Track, targets: list[MusicProvider]
    ) -> tuple[MusicProvider, str]:
        """Try MA's existing strict match, then its permitted relaxed match."""
        # Relaxed matching still requires title/artist plus album or duration.
        for strict in (True, False):
            for prov in targets if track.artists else []:
                for match in await self.mass.music.tracks.match_provider(
                    track, prov, strict=strict
                ):
                    if match.provider_domain == prov.domain:
                        return prov, match.item_id
        names = ", ".join(p.name for p in targets)
        message = f"{track.name} was not found in {names}"
        raise MediaNotFoundError(message)

    @staticmethod
    def _check_caller(instances: list[str]) -> None:
        """Allow admins or users whose provider filter includes every target."""
        user = get_current_user()
        if user is None or has_scope(user, Scope.ALL):
            return
        if not user.provider_filter or any(
            i not in user.provider_filter for i in instances
        ):
            message = f"{user.username} may not use these accounts"
            raise InsufficientPermissions(message)

    @staticmethod
    def _describe(
        track: Track, prov: MusicProvider, item_id: str, playing_on: str | None
    ) -> dict[str, Any]:
        return {
            "track": f"{'/'.join(a.name for a in track.artists)} - {track.name}",
            "uri": track.uri,
            "instance": prov.instance_id,
            "account": prov.name,
            "domain": prov.domain,
            "item_id": item_id,
            "playing_on": playing_on,
        }

    # ---------------------------------------------------------------- YouTube Music

    @staticmethod
    def _ytm_client(prov: MusicProvider) -> ytmusicapi.YTMusic:
        """Build the native YouTube Music client from this instance's cookie."""
        cookie = str(prov.get_setup_value("cookie") or "")
        if "__Secure-3PAPISID" not in cookie:
            message = f"{prov.name}: no signed-in YouTube Music cookie stored"
            raise LoginFailed(message)
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:72.0) "
            "Gecko/20100101 Firefox/72.0",
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.5",
            "Content-Type": "application/json",
            "X-Goog-AuthUser": "0",
            "x-origin": YTM_ORIGIN,
            "Cookie": cookie,
            "Authorization": get_authorization(
                sapisid_from_cookie(cookie) + " " + YTM_ORIGIN
            ),
        }
        username = str(prov.get_setup_value("username") or "")
        brand_user = (
            username
            if len(username) == _YOUTUBE_BRAND_ID_LENGTH and username.isdigit()
            else None
        )
        return ytmusicapi.YTMusic(auth=headers, user=brand_user)

    async def _ytm_rate(
        self, prov: MusicProvider, video_id: str, *, liked: bool
    ) -> bool:
        rating = LikeStatus.LIKE if liked else LikeStatus.INDIFFERENT

        def _rate() -> Any:
            return self._ytm_client(prov).rate_song(video_id, rating)

        result = await asyncio.to_thread(_rate)
        return isinstance(result, dict) and "error" not in result

    async def _ytm_status(self, prov: MusicProvider, video_id: str) -> str | None:
        def _status() -> str | None:
            watch = self._ytm_client(prov).get_watch_playlist(videoId=video_id, limit=1)
            for track in watch.get("tracks") or []:
                if track.get("videoId") == video_id:
                    return track.get("likeStatus")
            return None

        return await asyncio.to_thread(_status)

    # ---------------------------------------------------------------- Yandex Music

    @staticmethod
    def _yandex_client(prov: MusicProvider) -> Any:
        client = getattr(prov, "client", None)
        if client is None:
            message = f"{prov.name}: Yandex Music client is not ready"
            raise ProviderUnavailableError(message)
        return client

    async def _yandex_set(
        self, prov: MusicProvider, item_id: str, *, liked: bool
    ) -> bool:
        # My Wave items carry the station after '@' (see yandex_music
        # _parse_radio_item_id)
        track_id = item_id.split("@", 1)[0]
        client = self._yandex_client(prov)
        if liked:
            return bool(await client.like_track(track_id))
        return bool(await client.unlike_track(track_id))

    async def _yandex_status(self, prov: MusicProvider, item_id: str) -> bool:
        base_id = item_id.split("@", 1)[0].split(":", 1)[0]
        liked = await self._yandex_client(prov).get_liked_tracks()
        return any(str(t.id).split(":", 1)[0] == base_id for t in liked)
