"""
The eufy_sdk integration — talks to a ha-eufy-sdk bridge over WebSocket.

https://github.com/mega-yfue/ha-eufy-sdk
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.const import Platform
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.loader import async_get_loaded_integration

from .api import EufySdkApiClient
from .arming_sync import apply_arming_mode_event
from .const import (
    CONF_HOST,
    CONF_POLL_INTERVAL,
    CONF_PORT,
    DEFAULT_POLL_INTERVAL_MIN,
    DOMAIN,
    EVENT_TYPE,
    LOGGER,
)
from .coordinator import EufySdkDataUpdateCoordinator
from .data import EufySdkData
from .media_source import async_register_recording_view
from .recordings_ws import async_register_recordings_ws

# The recordings dashboard card, served from the integration and auto-loaded as a
# frontend module so `type: custom:eufy-recordings-card` works with no manual resource.
_CARD_URL = "/eufy_sdk_static/eufy-recordings-card.js"
_CARD_VERSION = "7"  # bump to bust the browser cache when the card changes
_FRONTEND_REGISTERED = f"{DOMAIN}_frontend_card"

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .data import EufySdkConfigEntry

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
    Platform.SELECT,
    Platform.NUMBER,
    Platform.CAMERA,
    Platform.BUTTON,
    Platform.IMAGE,
    Platform.EVENT,
    Platform.LIGHT,
    Platform.LOCK,
    Platform.ALARM_CONTROL_PANEL,
    Platform.SIREN,
]


async def async_setup_entry(hass: HomeAssistant, entry: EufySdkConfigEntry) -> bool:
    """Set up eufy_sdk from a config entry."""
    poll_min = entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL_MIN)
    coordinator = EufySdkDataUpdateCoordinator(
        hass=hass,
        logger=LOGGER,
        name=DOMAIN,
        # HA reads the bridge at the same cadence the bridge polls the cloud.
        update_interval=timedelta(minutes=poll_min),
        config_entry=entry,
    )

    # Forward every bridge event onto the HA event bus for automations — and recover
    # fast when the bridge comes back. A bridge restart drops the WS and fails one
    # coordinator poll, marking every entity `unavailable`; without a nudge they stay
    # that way (and detections don't show) until the next poll, up to `poll_min` minutes
    # later. So refresh the coordinator immediately on the bridge's `ready` broadcast
    # (sent on every boot) and on a WS reconnect.
    def _refresh_now() -> None:
        hass.async_create_task(coordinator.async_request_refresh())

    def _on_event(evt: dict) -> None:
        hass.bus.async_fire(EVENT_TYPE, evt)
        event = evt.get("event")
        if event == "contactState":
            sn = evt.get("deviceSn") or evt.get("sn")
            if sn and sn in coordinator.data and "open" in evt:
                coordinator.data[sn].setdefault("state", {})["contact"] = bool(
                    evt.get("open")
                )
                coordinator.async_update_listeners()
        elif event == "armingModeChanged":
            serial = evt.get("deviceSn") or evt.get("sn")
            if "mode" in evt:
                apply_arming_mode_event(coordinator, evt)
            elif serial and serial in coordinator.data:
                entry.async_create_background_task(
                    hass,
                    coordinator.async_request_refresh(),
                    "arming mode refresh",
                )
        elif event == "ready":
            _refresh_now()

    client = EufySdkApiClient(
        host=entry.data[CONF_HOST],
        port=entry.data[CONF_PORT],
        session=async_get_clientsession(hass),
        on_event=_on_event,
        on_reconnect=_refresh_now,
    )
    entry.runtime_data = EufySdkData(
        client=client,
        integration=async_get_loaded_integration(hass, entry.domain),
        coordinator=coordinator,
    )

    await coordinator.async_config_entry_first_refresh()

    # Push the chosen poll interval to the bridge (the cloud-poll cadence lives there).
    try:
        await client.set_poll_ms(poll_min * 60_000)
    except Exception as err:  # noqa: BLE001 - a failed config push shouldn't block setup
        LOGGER.warning("could not set bridge poll interval: %s", err)

    # Reload when the options change, so a new poll interval is applied.
    entry.async_on_unload(entry.add_update_listener(_async_reload_on_update))

    # Property manifests are static per device — fetch once so the platforms can
    # build switch/select/number/sensor entities. A device that fails is skipped.
    properties: dict[str, list] = {}
    for sn, dev in coordinator.data.items():
        if dev.get("error"):
            continue
        try:
            properties[sn] = await client.get_properties(sn)
        except Exception as err:  # noqa: BLE001 - one bad device must not abort setup
            LOGGER.warning("could not fetch properties for %s: %s", sn, err)
    entry.runtime_data.properties = properties

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Serve the recordings media source's HTTP proxy (once, regardless of entries).
    async_register_recording_view(hass)
    async_register_recordings_ws(hass)
    await _async_register_card(hass)
    return True


async def _async_register_card(hass: HomeAssistant) -> None:
    """Serve the recordings dashboard card and auto-load it as a frontend module."""
    if hass.data.get(_FRONTEND_REGISTERED):
        return
    card = Path(__file__).parent / "www" / "eufy-recordings-card.js"
    try:
        await hass.http.async_register_static_paths(
            [StaticPathConfig(_CARD_URL, str(card), cache_headers=False)]
        )
        add_extra_js_url(hass, f"{_CARD_URL}?v={_CARD_VERSION}")
        hass.data[_FRONTEND_REGISTERED] = True
    except Exception as err:  # noqa: BLE001 - a missing card must not block setup
        LOGGER.warning("could not register the recordings card: %s", err)


async def async_unload_entry(hass: HomeAssistant, entry: EufySdkConfigEntry) -> bool:
    """Unload a config entry and close the bridge connection."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.client.close()
    return unloaded


async def _async_reload_on_update(
    hass: HomeAssistant, entry: EufySdkConfigEntry
) -> None:
    """Reload the entry when its options change (e.g. a new poll interval)."""
    await hass.config_entries.async_reload(entry.entry_id)
