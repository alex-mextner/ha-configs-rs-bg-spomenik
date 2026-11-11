"""Explicit opt-in for the EKF physical remote receiver."""
from homeassistant.components.switch import SwitchEntity
from homeassistant.helpers.entity import EntityCategory

async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([EkfRemoteReceiverSwitch(entry)])

class EkfRemoteReceiverSwitch(SwitchEntity):
    _attr_should_poll = False
    _attr_name = "EKF — приём универсального пульта"
    _attr_icon = "mdi:remote"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry):
        self.receiver = entry.runtime_data.receiver
        self._attr_unique_id = f"{entry.entry_id}_ir_receiver_enabled"
        self._attr_suggested_object_id = "ekf_universal_remote_receiver"

    @property
    def is_on(self):
        return self.receiver.enabled

    @property
    def extra_state_attributes(self):
        return {"validated": self.receiver.validated, "last_error": self.receiver.last_error,
                "remote_entity_id": self.receiver.remote_id}

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self.async_on_remove(self.receiver.add_callback(lambda _: self.async_write_ha_state()))

    async def async_turn_on(self, **kwargs):
        await self.receiver.async_set_enabled(True)

    async def async_turn_off(self, **kwargs):
        await self.receiver.async_set_enabled(False)
