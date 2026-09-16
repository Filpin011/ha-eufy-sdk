"""Select platform — one select per writable enum property, plus the PTZ preset slot."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.helpers.restore_state import RestoreEntity

from .const import PTZ_PRESET_SLOTS
from .entity import (
    EufySdkDeviceEntity,
    EufySdkPropertyEntity,
    classify,
    has_capability,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import EufySdkDataUpdateCoordinator
    from .data import EufySdkConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001
    entry: EufySdkConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create a select per writable enum property, plus the PTZ preset slots."""
    coordinator = entry.runtime_data.coordinator
    entities: list[SelectEntity] = [
        EufySdkSelect(coordinator, sn, spec)
        for sn in coordinator.data
        for spec in entry.runtime_data.properties.get(sn, [])
        if classify(spec) == "select"
    ]
    entities.extend(
        EufySdkPtzPresetSelect(coordinator, sn)
        for sn, dev in coordinator.data.items()
        if has_capability(dev, "ptz")
    )
    async_add_entities(entities)


class EufySdkSelect(EufySdkPropertyEntity, SelectEntity):
    """A writable enum property as a select — options are the enum labels."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: EufySdkDataUpdateCoordinator,
        sn: str,
        spec: dict[str, Any],
    ) -> None:
        """Build the raw<->label maps from the spec's enumValues."""
        super().__init__(coordinator, sn, spec)
        # enumValues is {raw: label}; JSON object keys arrive as strings.
        self._label_by_raw = {str(k): str(v) for k, v in spec["enumValues"].items()}
        self._raw_by_label = {v: k for k, v in self._label_by_raw.items()}
        self._attr_options = list(self._label_by_raw.values())

    @property
    def current_option(self) -> str | None:
        """The label for the property's current raw value."""
        v = self.prop_value
        return None if v is None else self._label_by_raw.get(str(v))

    async def async_select_option(self, option: str) -> None:
        """Write the raw value behind the chosen label."""
        raw = self._raw_by_label.get(option)
        if raw is None:
            return
        # Send an int when the raw code is numeric, else the raw string.
        value: int | str = int(raw) if raw.lstrip("-").isdigit() else raw
        await self.write(value)


class EufySdkPtzPresetSelect(EufySdkDeviceEntity, SelectEntity, RestoreEntity):
    """
    Which stored preset slot this camera's go-to / save buttons act on.

    Not a device reading: nothing on the wire reports where a camera is parked or
    which preset it last used, so this is a local choice. That makes it
    restore-on-restart rather than coordinator-driven — the buttons read it back out
    of the shared runtime data.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:map-marker-multiple"
    _attr_name = "PTZ preset slot"

    def __init__(self, coordinator: EufySdkDataUpdateCoordinator, sn: str) -> None:
        """Offer one option per slot the camera can store."""
        super().__init__(coordinator, sn)
        self._attr_unique_id = f"{sn}_ptz_preset_slot"
        self._attr_options = [str(i) for i in range(1, PTZ_PRESET_SLOTS + 1)]
        self._attr_current_option = "1"

    async def async_added_to_hass(self) -> None:
        """Restore the slot chosen before the restart and publish it."""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in self._attr_options:
            self._attr_current_option = last.state
        self._publish()

    async def async_select_option(self, option: str) -> None:
        """Point this camera's preset buttons at another slot."""
        self._attr_current_option = option
        self._publish()
        self.async_write_ha_state()

    def _publish(self) -> None:
        """Share the slot with the button platform through the entry's runtime data."""
        runtime = self.coordinator.config_entry.runtime_data
        runtime.ptz_slots[self._sn] = int(self._attr_current_option)
