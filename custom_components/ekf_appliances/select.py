"""Choose the active physical IR receiver without changing the remote layout."""
from homeassistant.components.select import SelectEntity
from homeassistant.helpers.entity import EntityCategory

from .receiver import SOURCES


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([RemoteReceiverSource(entry)])


class RemoteReceiverSource(SelectEntity):
    _attr_should_poll = False
    _attr_name = "Приёмник универсального пульта"
    _attr_icon = "mdi:remote"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = list(SOURCES)

    def __init__(self, entry):
        self.receiver = entry.runtime_data.receiver
        self._attr_unique_id = f"{entry.entry_id}_ir_receiver_source"
        self._attr_suggested_object_id = "universal_remote_receiver"

    @property
    def current_option(self):
        return self.receiver.source

    @property
    def extra_state_attributes(self):
        return self.receiver.diagnostics

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self.async_on_remove(self.receiver.add_callback(lambda _: self.async_write_ha_state()))

    async def async_select_option(self, option):
        await self.receiver.async_set_source(option)
