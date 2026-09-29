"""
WebSocket API for the SD-card recordings dashboard card.

The card (www/eufy-recordings-card.js) drives everything from the browser: it
lists cameras, lists a day's clips, and loads each clip's snapshot through the
existing /api/eufy_sdk/recording proxy. These two commands are the only bridge
round-trips it needs; the heavy download stays on the proxy view, hit only when
a clip is opened.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN

if TYPE_CHECKING:
    from .data import EufySdkConfigEntry


def _first_entry(hass: HomeAssistant) -> EufySdkConfigEntry | None:
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    return entries[0] if entries else None


@websocket_api.websocket_command({vol.Required("type"): "eufy_sdk/recordings/cameras"})
@callback
def ws_cameras(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """List the cameras the card can browse (sn + friendly name)."""
    entry = _first_entry(hass)
    cameras: list[dict[str, str]] = []
    if entry:
        for sn, dev in (entry.runtime_data.coordinator.data or {}).items():
            if dev.get("error"):
                continue
            cameras.append({"sn": sn, "name": dev.get("name") or sn})
    connection.send_result(msg["id"], {"cameras": cameras})


@websocket_api.websocket_command(
    {
        vol.Required("type"): "eufy_sdk/recordings/list",
        vol.Required("sn"): str,
        vol.Required("date"): str,
    }
)
@websocket_api.async_response
async def ws_list(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """List one day's recordings for a camera (YYYYMMDD)."""
    entry = _first_entry(hass)
    if not entry:
        connection.send_error(msg["id"], "not_loaded", "integration not loaded")
        return
    try:
        rows = await entry.runtime_data.client.list_recordings(msg["sn"], msg["date"])
    except Exception as err:  # noqa: BLE001 - surface any bridge error to the card
        connection.send_error(msg["id"], "list_failed", str(err))
        return
    connection.send_result(msg["id"], {"recordings": rows})


@callback
def async_register_recordings_ws(hass: HomeAssistant) -> None:
    """Register the recordings WebSocket commands (idempotent per hass)."""
    if hass.data.get(f"{DOMAIN}_recordings_ws"):
        return
    websocket_api.async_register_command(hass, ws_cameras)
    websocket_api.async_register_command(hass, ws_list)
    hass.data[f"{DOMAIN}_recordings_ws"] = True
