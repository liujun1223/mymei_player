"""MYMEI Player 遥控命令选择器."""

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import MymeiConfigEntry, MymeiPlayer
from .const import COMMAND_CODES

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 1
PLACEHOLDER = "-选择命令-"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MymeiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """设置遥控命令选择器."""
    device = entry.runtime_data
    async_add_entities([MymeiCommandSelect(device)])


class MymeiCommandSelect(SelectEntity):
    """MYMEI 遥控命令选择器，选中即下发."""

    _attr_has_entity_name = True
    _attr_translation_key = "remote_command"
    _attr_icon = "mdi:remote"

    def __init__(self, device: MymeiPlayer) -> None:
        """初始化."""
        self._device = device
        self._attr_unique_id = f"{device.host or device.port}_commands"
        self._attr_device_info = device.device_info
        self._attr_options = [PLACEHOLDER, *COMMAND_CODES]
        self._attr_current_option = PLACEHOLDER

    @property
    def available(self) -> bool:
        """任一通道在线即视为可用."""
        return self._device.is_available

    async def async_select_option(self, option: str) -> None:
        """下发选中的命令，随后恢复为空待选状态."""
        if option == PLACEHOLDER:
            return
        if option not in COMMAND_CODES:
            _LOGGER.warning("未知命令：%s", option)
            return
        await self._device.async_send_command(option)
        self._attr_current_option = PLACEHOLDER
        self.async_write_ha_state()
