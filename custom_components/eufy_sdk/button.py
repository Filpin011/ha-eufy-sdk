"""Button platform — device-level actions the bridge exposes (reboot, PTZ)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.const import EntityCategory

from .entity import EufySdkDeviceEntity, has_capability

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import EufySdkDataUpdateCoordinator
    from .data import EufySdkConfigEntry

# Every PTZ control is keyed by the bridge action it sends, and carries how it presents:
#   {action: {"label": …, "icon": …, "category": under Configuration; default primary}}
#
# The d-pad, in the order a d-pad reads. Each verb is a no-arg method on the SDK's `ptz`
# surface, so the action name IS the verb.
_PTZ_STEPS: dict[str, dict[str, Any]] = {
    "up": {"label": "Tilt up", "icon": "mdi:arrow-up"},
    "down": {"label": "Tilt down", "icon": "mdi:arrow-down"},
    "left": {"label": "Pan left", "icon": "mdi:arrow-left"},
    "right": {"label": "Pan right", "icon": "mdi:arrow-right"},
}

# The stored-position actions, whose dotted names the bridge routes into the preset
# sub-API. Both act on the slot the PTZ preset select holds, so neither carries one.
_PTZ_PRESETS: dict[str, dict[str, Any]] = {
    # Recalling a stored view is everyday use, so it stays a primary control.
    "preset.goto": {"label": "Go to preset", "icon": "mdi:target"},
    # Saving one is a setup step, so it sits under Configuration instead.
    "preset.save": {
        "label": "Save preset",
        "icon": "mdi:content-save-move",
        "category": EntityCategory.CONFIG,
    },
}


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001
    entry: EufySdkConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create Reboot, Refresh-Last-Event and the PTZ controls (pan-tilt cameras)."""
    coordinator = entry.runtime_data.coordinator
    entities: list[ButtonEntity] = [
        EufySdkRebootButton(coordinator, sn)
        for sn, dev in coordinator.data.items()
        if dev.get("canReboot")
    ]
    # A "Refresh Last Event" button per camera/doorbell (same set as the event Image
    # entity, gated on `stream`): forces the bridge to pull the newest event cover now —
    # a manual override for when the auto-refresh raced the HomeBase writing the crop.
    entities.extend(
        EufyRefreshEventButton(coordinator, sn)
        for sn, dev in coordinator.data.items()
        if dev.get("stream")
    )
    # The d-pad and the preset actions, gated on the `ptz` capability the bridge
    # reports: a fixed camera has no `ptz` surface, so the bridge would refuse every
    # verb and the buttons would sit there as dead controls.
    for sn, dev in coordinator.data.items():
        if not has_capability(dev, "ptz"):
            continue
        entities.extend(
            EufySdkPtzButton(coordinator, sn, action, meta)
            for action, meta in _PTZ_STEPS.items()
        )
        entities.extend(
            EufySdkPtzPresetButton(coordinator, sn, action, meta)
            for action, meta in _PTZ_PRESETS.items()
        )
    async_add_entities(entities)


class EufySdkRebootButton(EufySdkDeviceEntity, ButtonEntity):
    """Reboot a HomeBase — a device-level action, not a writable property."""

    _attr_device_class = ButtonDeviceClass.RESTART
    _attr_entity_category = EntityCategory.CONFIG
    _attr_name = "Reboot"

    def __init__(self, coordinator: EufySdkDataUpdateCoordinator, sn: str) -> None:
        """Bind to a HomeBase serial."""
        super().__init__(coordinator, sn)
        self._attr_unique_id = f"{sn}_reboot"

    async def async_press(self) -> None:
        """Reboot the HomeBase (it drops offline for a minute or two)."""
        client = self.coordinator.config_entry.runtime_data.client
        await client.reboot(self._sn)


class EufyRefreshEventButton(EufySdkDeviceEntity, ButtonEntity):
    """Force a 'Last event' image refresh — pull the newest event cover now."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:image-refresh"
    _attr_name = "Refresh Last Event"

    def __init__(self, coordinator: EufySdkDataUpdateCoordinator, sn: str) -> None:
        """Bind to a camera/doorbell serial."""
        super().__init__(coordinator, sn)
        self._attr_unique_id = f"{sn}_refresh_last_event"

    async def async_press(self) -> None:
        """Ask the bridge to re-pull the newest event cover (nudges the Image)."""
        client = self.coordinator.config_entry.runtime_data.client
        await client.refresh_event_image(self._sn)


class EufySdkPtzButton(EufySdkDeviceEntity, ButtonEntity):
    """
    One pan-tilt step, in the direction this button carries.

    Fire-and-forget: P2P carries no acknowledgement, so a press that returns without
    raising means the frame left for the camera, not that the camera finished moving.
    Where it ended up arrives separately as a `ptzNotify` event, which the Image entity
    already watches.
    """

    def __init__(
        self,
        coordinator: EufySdkDataUpdateCoordinator,
        sn: str,
        action: str,
        meta: dict[str, Any],
    ) -> None:
        """Bind to a camera serial and the bridge action this button sends."""
        super().__init__(coordinator, sn)
        self._action = action
        self._attr_unique_id = f"{sn}_ptz_{action.replace('.', '_')}"
        self._attr_name = meta["label"]
        self._attr_icon = meta["icon"]
        self._attr_entity_category = meta.get("category")

    @property
    def _args(self) -> tuple:
        """Positional arguments for the action — a movement step takes none."""
        return ()

    async def async_press(self) -> None:
        """Send this button's PTZ action to the bridge."""
        client = self.coordinator.config_entry.runtime_data.client
        await client.action(self._sn, self._action, *self._args)


class EufySdkPtzPresetButton(EufySdkPtzButton):
    """
    Go to, or save, the stored position the PTZ preset select points at.

    A slot the camera has nothing stored in is a silent no-op on the wire: the camera
    ignores the frame and no error comes back, so a go-to against an unsaved slot looks
    like a button that did nothing. Save the position into the slot first.
    """

    @property
    def _args(self) -> tuple[int]:
        """The slot chosen for this camera — slot 1 until the select says otherwise."""
        return (self.coordinator.config_entry.runtime_data.ptz_slots.get(self._sn, 1),)
