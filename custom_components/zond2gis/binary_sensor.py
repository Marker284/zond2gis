"""Бинарный сенсор: зарядка."""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import DOMAIN
from .coordinator import FriendState, ZondCoordinator, SIGNAL_NEW_FRIEND, SIGNAL_UPDATE_FRIEND


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: ZondCoordinator = hass.data[DOMAIN][entry.entry_id]
    added: set[str] = set()

    @callback
    def _on_new_friends(uids: list[str]) -> None:
        new = [
            ZondChargingSensor(coordinator, uid)
            for uid in uids if uid not in added
        ]
        if new:
            added.update(uid for uid in uids if uid not in added)
            async_add_entities(new)

    entry.async_on_unload(
        async_dispatcher_connect(
            hass, SIGNAL_NEW_FRIEND.format(entry.entry_id), _on_new_friends
        )
    )


class ZondChargingSensor(BinarySensorEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "charging"
    _attr_device_class    = BinarySensorDeviceClass.BATTERY_CHARGING

    def __init__(self, coordinator: ZondCoordinator, uid: str) -> None:
        self._coordinator = coordinator
        self._uid = uid

    @property
    def unique_id(self) -> str:
        return f"zond_{self._uid}_charging"

    @property
    def device_info(self):
        from homeassistant.helpers.device_registry import DeviceInfo
        profile = self._coordinator.profiles.get(self._uid)
        return DeviceInfo(
            identifiers={(DOMAIN, self._uid)},
            name=profile.name if profile else self._uid[:8],
            manufacturer="2GIS",
            model="Friends on Map",
        )

    @property
    def _state(self) -> FriendState | None:
        return self._coordinator.states.get(self._uid)

    @property
    def available(self) -> bool:
        s = self._state
        return s.is_available() if s else False

    @property
    def is_on(self) -> bool | None:
        s = self._state
        return s.is_charging if s else None

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_UPDATE_FRIEND.format(self._coordinator.entry_id, self._uid),
                self._handle_update,
            )
        )
