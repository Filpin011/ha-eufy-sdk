"""
Browse and play a standalone camera's SD-card recordings.

The bridge does the P2P work (list a day, download and decrypt a clip to MP4);
this exposes it as a Media Source (camera -> recent day -> clip -> play) and
proxies the bridge's HTTP through HA's own origin so it works on an HTTPS
install. Listing a day and fetching a thumbnail are cheap; the full download,
which wakes a battery camera, happens only when a clip is opened.
"""

from __future__ import annotations

import base64
from datetime import timedelta
from typing import TYPE_CHECKING

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.components.media_player import MediaClass, MediaType
from homeassistant.components.media_source import (
    BrowseMediaSource,
    MediaSource,
    MediaSourceItem,
    PlayMedia,
    Unresolvable,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util

from .const import DOMAIN

if TYPE_CHECKING:
    from .data import EufySdkConfigEntry

# How many recent days to offer under a camera. The calendar query is per-day
# and wakes the camera, so a fixed recent window beats enumerating the whole
# card up front.
RECENT_DAYS = 30
# Identifier shapes: "<sn>" (camera), "<sn>|<ymd>" (day), "<sn>|<ymd>|<b64>" (clip).
_PARTS_DAY = 2
_PARTS_CLIP = 3
_VIEW_REGISTERED = f"{DOMAIN}_recording_view"


async def async_get_media_source(hass: HomeAssistant) -> MediaSource:
    """Return this integration's media source (HA calls this to register it)."""
    return EufyRecordingsSource(hass)


def _first_entry(hass: HomeAssistant) -> EufySdkConfigEntry | None:
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    return entries[0] if entries else None


def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode()


def _unb64(s: str) -> str:
    return base64.urlsafe_b64decode(s.encode()).decode()


class EufyRecordingsSource(MediaSource):
    """A three-level tree: cameras, then recent days, then clips."""

    name = "Eufy recordings"

    def __init__(self, hass: HomeAssistant) -> None:
        """Store hass; the config entry (bridge client) is resolved per request."""
        super().__init__(DOMAIN)
        self.hass = hass

    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        """Resolve a clip id (`<sn>|<ymd>|<b64 path>`) to a playable MP4 URL."""
        parts = (item.identifier or "").split("|")
        if len(parts) != _PARTS_CLIP:
            msg = "not a playable recording"
            raise Unresolvable(msg)
        sn, _ymd, b64path = parts
        path = _unb64(b64path)
        # Play through HA's own proxy view so an HTTPS dashboard fetches it
        # same-origin.
        url = f"/api/{DOMAIN}/recording?sn={sn}&path={_b64(path)}"
        return PlayMedia(url, "video/mp4")

    async def async_browse_media(self, item: MediaSourceItem) -> BrowseMediaSource:
        """Browse: root to cameras, camera to days, day to clips."""
        ident = item.identifier or ""
        if not ident:
            return self._browse_root()
        parts = ident.split("|")
        if len(parts) == 1:
            return await self._browse_days(parts[0])
        if len(parts) == _PARTS_DAY:
            return await self._browse_day(parts[0], parts[1])
        msg = "unknown media item"
        raise Unresolvable(msg)

    def _folder(
        self,
        identifier: str,
        title: str,
        children: list[BrowseMediaSource] | None = None,
    ) -> BrowseMediaSource:
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=identifier,
            media_class=MediaClass.DIRECTORY,
            media_content_type="",
            title=title,
            can_play=False,
            can_expand=True,
            children=children or [],
            children_media_class=MediaClass.DIRECTORY,
        )

    @callback
    def _browse_root(self) -> BrowseMediaSource:
        entry = _first_entry(self.hass)
        cams: list[BrowseMediaSource] = []
        if entry:
            for sn, dev in (entry.runtime_data.coordinator.data or {}).items():
                cams.append(self._folder(sn, dev.get("name") or sn))
        return self._folder("", "Eufy recordings", cams)

    async def _browse_days(self, sn: str) -> BrowseMediaSource:
        entry = _first_entry(self.hass)
        name = sn
        if entry:
            name = (entry.runtime_data.coordinator.data.get(sn) or {}).get("name") or sn
        today = dt_util.now().date()
        days = [
            self._folder(
                f"{sn}|{(today - timedelta(days=i)).strftime('%Y%m%d')}",
                (today - timedelta(days=i)).strftime("%Y-%m-%d"),
            )
            for i in range(RECENT_DAYS)
        ]
        return self._folder(sn, name, days)

    async def _browse_day(self, sn: str, ymd: str) -> BrowseMediaSource:
        entry = _first_entry(self.hass)
        if not entry:
            msg = "integration not loaded"
            raise Unresolvable(msg)
        rows = await entry.runtime_data.client.list_recordings(sn, ymd)
        clips: list[BrowseMediaSource] = []
        for r in rows:
            path = r.get("storage_path")
            if not path:
                continue
            start = str(r.get("start_time", "")).split(" ")[-1][:8]
            end = str(r.get("end_time", "")).split(" ")[-1][:8]
            title = f"{start} - {end}" if end else start
            thumb = r.get("thumb_path")
            clips.append(
                BrowseMediaSource(
                    domain=DOMAIN,
                    identifier=f"{sn}|{ymd}|{_b64(path)}",
                    media_class=MediaClass.VIDEO,
                    media_content_type=MediaType.VIDEO,
                    title=title,
                    can_play=True,
                    can_expand=False,
                    thumbnail=(
                        f"/api/{DOMAIN}/recording?sn={sn}&path={_b64(thumb)}&kind=thumb"
                        if thumb
                        else None
                    ),
                )
            )
        folder = self._folder(f"{sn}|{ymd}", f"{ymd[0:4]}-{ymd[4:6]}-{ymd[6:8]}", clips)
        folder.children_media_class = MediaClass.VIDEO
        return folder


class EufyRecordingMediaView(HomeAssistantView):
    """Proxy the bridge's recording MP4 / thumbnail through HA's origin."""

    url = f"/api/{DOMAIN}/recording"
    name = f"api:{DOMAIN}:recording"

    def __init__(self, hass: HomeAssistant) -> None:
        """Keep hass so the bridge client is resolved per request."""
        self.hass = hass

    async def get(self, request: web.Request) -> web.StreamResponse:
        """Stream the requested recording (or thumbnail) from the bridge."""
        sn = request.query.get("sn")
        b64path = request.query.get("path")
        kind = request.query.get("kind", "video")
        if not sn or not b64path:
            return web.Response(status=400, text="sn and path are required")
        try:
            path = _unb64(b64path)
        except (ValueError, TypeError):
            return web.Response(status=400, text="bad path")

        entry = _first_entry(self.hass)
        if not entry:
            return web.Response(status=503, text="integration not loaded")
        client = entry.runtime_data.client
        if kind == "thumb":
            upstream = client.thumb_url(sn, path)
        else:
            upstream = client.recording_url(sn, path)

        src = await client.open_bridge_stream(upstream)
        try:
            if src.status != 200:  # noqa: PLR2004 - HTTP OK
                return web.Response(status=502, text=f"bridge returned {src.status}")
            ctype = src.headers.get("Content-Type", "application/octet-stream")
            cache = "max-age=86400" if kind == "thumb" else "no-store"
            resp = web.StreamResponse(
                status=200,
                headers={"Content-Type": ctype, "Cache-Control": cache},
            )
            await resp.prepare(request)
            async for chunk in src.content.iter_chunked(64 * 1024):
                await resp.write(chunk)
            await resp.write_eof()
        finally:
            src.release()
        return resp


@callback
def async_register_recording_view(hass: HomeAssistant) -> None:
    """Register the proxy view once, however many config entries exist."""
    if hass.data.get(_VIEW_REGISTERED):
        return
    hass.http.register_view(EufyRecordingMediaView(hass))
    hass.data[_VIEW_REGISTERED] = True
