"""MYMEI Player 媒体播放器实体."""

from datetime import timedelta
import logging

from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SIGNAL_STATE_UPDATED, MymeiConfigEntry, MymeiPlayer
from .const import PLAY_STATUS_IDLE, PLAY_STATUS_PAUSED, PLAY_STATUS_PLAYING

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(seconds=30)

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MymeiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """设置媒体播放器."""
    device = entry.runtime_data
    async_add_entities([MymeiMediaPlayer(device)])


class MymeiMediaPlayer(MediaPlayerEntity):
    """MYMEI 艾美影库播放器."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_icon = "mdi:theater"
    _attr_supported_features = (
        MediaPlayerEntityFeature.TURN_ON
        | MediaPlayerEntityFeature.TURN_OFF
        | MediaPlayerEntityFeature.PLAY
        | MediaPlayerEntityFeature.PAUSE
        | MediaPlayerEntityFeature.STOP
        | MediaPlayerEntityFeature.VOLUME_STEP
        | MediaPlayerEntityFeature.VOLUME_MUTE
    )

    def __init__(self, device: MymeiPlayer) -> None:
        """初始化."""
        self._device = device
        self._attr_unique_id = device.host or device.port
        self._attr_device_info = device.device_info

    @property
    def available(self) -> bool:
        """集成已加载即视为可用.

        设备待机时通道无回执，若返回 False 会让 HA 禁用所有按钮，
        用户就无法再点「开机」唤醒设备，因此这里始终可用，
        由 state 显示 off 来表达设备当前状态。
        """
        return True

    @property
    def state(self) -> MediaPlayerState | None:
        """映射设备状态到 HA 状态."""
        if not self._device.is_available:
            return MediaPlayerState.OFF
        if self._device.power_state == "standby":
            return MediaPlayerState.OFF
        play = self._device.play_status
        if play == PLAY_STATUS_PLAYING:
            return MediaPlayerState.PLAYING
        if play == PLAY_STATUS_PAUSED:
            return MediaPlayerState.PAUSED
        if play == PLAY_STATUS_IDLE:
            return MediaPlayerState.IDLE
        return MediaPlayerState.ON

    @property
    def media_content_type(self) -> MediaType | None:
        """当前媒体类型."""
        if self._device.play_type == 2:
            return MediaType.MUSIC
        if self._device.play_type == 1:
            return MediaType.VIDEO
        return None

    @property
    def media_title(self) -> str | None:
        """当前播放标题（协议不推送片名，仅在设备上报时可用）."""
        return self._device.title

    async def async_added_to_hass(self) -> None:
        """订阅设备状态推送."""

        @callback
        def _state_updated(device: MymeiPlayer) -> None:
            if device is self._device:
                self.async_write_ha_state()

        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_STATE_UPDATED, _state_updated)
        )

    async def async_update(self) -> None:
        """轮询：协议无查询命令，仅在通道掉线后尝试恢复."""
        await self._device.async_poll_state()

    async def _send(self, name: str) -> None:
        """下发命令，失败时抛出可读错误."""
        if await self._device.async_send_command(name):
            return
        if self._device.host and not self._device.port:
            raise HomeAssistantError(
                f"发送命令 {name} 失败：当前仅配置了 IP，且设备 TCP 33080 无响应；"
                "请确认 IP 或补充串口"
            )
        if self._device.port and not self._device.host:
            raise HomeAssistantError(
                f"发送命令 {name} 失败：串口 {self._device.port} 无回执"
            )
        raise HomeAssistantError(f"发送命令 {name} 失败：TCP / 串口均无响应")

    async def async_turn_on(self) -> None:
        """待机开（开启屏幕）."""
        await self._send("power_on")

    async def async_turn_off(self) -> None:
        """待机关（关闭屏幕）."""
        await self._send("power_off")

    async def async_media_play(self) -> None:
        """播放."""
        await self._send("play")

    async def async_media_pause(self) -> None:
        """暂停."""
        await self._send("pause")

    async def async_media_stop(self) -> None:
        """停止."""
        await self._send("stop")

    async def async_volume_up(self) -> None:
        """音量+."""
        await self._send("volume_up")

    async def async_volume_down(self) -> None:
        """音量-."""
        await self._send("volume_down")

    async def async_mute_volume(self, mute: bool) -> None:
        """静音切换（协议为切换键，不区分开关）."""
        await self._send("mute")
