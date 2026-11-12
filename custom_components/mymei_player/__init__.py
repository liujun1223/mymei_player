"""MYMEI Player 集成（TCP 33080 / RS-232 双通道）.

- TCP：33080 长连接，HEX 帧；设备主动推送视频/音乐播放事件
- RS-232：9600 8N1，HEX 帧
- 两通道优先 TCP，失败自动降级串口，60 秒后自动重试 TCP
- 协议没有状态查询命令：电源与播放状态由命令回执 + 事件推送维护
"""

import asyncio
import contextlib
import logging
import time

import serial

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.entity import DeviceInfo

from .const import (
    CHANNEL_RETRY_INTERVAL,
    COMMAND_CODES,
    COMMAND_RECONNECT_INTERVAL,
    CONF_BAUDRATE,
    CONF_HOST,
    CONF_PORT,
    DEFAULT_BAUDRATE,
    DEFAULT_TCP_PORT,
    DOMAIN,
    EVENT_CODES,
    PLAY_STATUS_IDLE,
    PLAY_STATUS_PAUSED,
    PLAY_STATUS_PLAYING,
    PROBE_COMMAND,
    RECOVERY_INTERVAL,
    RESULT_FAIL,
    RESULT_INVALID,
    RESULT_SUCCESS,
    SERIAL_TIMEOUT,
    TCP_COMMAND_TIMEOUT,
    TCP_CONNECT_TIMEOUT,
    TCP_READ_TIMEOUT,
    build_frame,
    read_serial_frame,
    split_frames,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.MEDIA_PLAYER, Platform.SELECT]

# 状态推送信号
SIGNAL_STATE_UPDATED = f"{DOMAIN}_state_updated"


class MymeiPlayer:
    """MYMEI 播放器 API，封装 TCP 长连接与 RS-232 串口."""

    def __init__(
        self,
        hass: HomeAssistant,
        host: str | None,
        port: str | None,
        baudrate: int = DEFAULT_BAUDRATE,
    ) -> None:
        """初始化."""
        self.hass = hass
        self._host = host
        self._port = port
        self._baudrate = baudrate
        self.name = "MYMEI 艾美影库播放器"
        self.model = "MYMEI Player"
        self.version: str | None = None
        self._available_tcp = False
        self._available_serial = False
        self._serial: serial.Serial | None = None
        self._serial_lock = asyncio.Lock()
        self._tcp_lock = asyncio.Lock()
        self._tcp_reader: asyncio.StreamReader | None = None
        self._tcp_writer: asyncio.StreamWriter | None = None
        self._tcp_replies: asyncio.Queue[tuple[int, int]] = asyncio.Queue(maxsize=100)
        self._reader_task: asyncio.Task[None] | None = None
        self._closed = False
        self._last_recovery = 0.0
        # 状态（协议无查询命令，靠回执与事件维护）
        self.power_state: str | None = None  # on / standby
        self.play_status: int | None = None  # 1 播放 / 2 暂停 / -1 停止
        self.play_type: int | None = None  # 1 视频 / 2 音乐
        self.title: str | None = None

    @property
    def host(self) -> str | None:
        """返回 IP."""
        return self._host

    @property
    def port(self) -> str | None:
        """返回串口设备路径."""
        return self._port

    @property
    def is_available(self) -> bool:
        """任一通道可用."""
        return self._available_tcp or self._available_serial

    # ------------------------------------------------------------------
    # TCP 传输（33080 长连接）
    # ------------------------------------------------------------------

    async def _async_tcp_open(self) -> bool:
        """建立 TCP 连接；连接成功即视为通道可用."""
        try:
            self._tcp_reader, self._tcp_writer = await asyncio.wait_for(
                asyncio.open_connection(self._host, DEFAULT_TCP_PORT),
                timeout=TCP_CONNECT_TIMEOUT,
            )
        except (TimeoutError, OSError) as err:
            _LOGGER.warning(
                "TCP 连接失败（%s:%s）：%s（稍后重试，串口可用时自动降级）",
                self._host,
                DEFAULT_TCP_PORT,
                err,
            )
            await self._async_tcp_close()
            return False
        return True

    async def _async_tcp_close(self) -> None:
        """关闭 TCP 连接."""
        writer, self._tcp_writer = self._tcp_writer, None
        self._tcp_reader = None
        if writer is not None:
            with contextlib.suppress(OSError):
                writer.close()
                await writer.wait_closed()

    async def _async_tcp_reader_loop(self) -> None:
        """后台守护任务：解析事件推送与命令回执，断线后按退避重连."""
        buffer = b""
        while not self._closed:
            if self._tcp_reader is None:
                if not self._host:
                    break
                if not await self._async_tcp_open():
                    self._available_tcp = False
                    await asyncio.sleep(CHANNEL_RETRY_INTERVAL)
                    continue
                buffer = b""
                self._available_tcp = True
                _LOGGER.info("TCP 控制已就绪（%s:%s）", self._host, DEFAULT_TCP_PORT)
                async_dispatcher_send(self.hass, SIGNAL_STATE_UPDATED, self)
            try:
                chunk = await asyncio.wait_for(
                    self._tcp_reader.read(256), TCP_READ_TIMEOUT
                )
            except TimeoutError:
                # 协议无心跳命令，长时间无事件属正常，连接保持不动
                continue
            except OSError as err:
                _LOGGER.warning(
                    "TCP 读取异常（%s:%s）：%s", self._host, DEFAULT_TCP_PORT, err
                )
                chunk = b""
            if chunk:
                buffer += chunk
                frames, buffer = split_frames(buffer)
                for frame in frames:
                    self._handle_frame(frame[4], frame[5])
                continue
            was_available = self._available_tcp
            self._available_tcp = False
            await self._async_tcp_close()
            if was_available:
                _LOGGER.warning(
                    "TCP 控制断开（%s:%s），降级到串口，%s 秒后重连",
                    self._host,
                    DEFAULT_TCP_PORT,
                    CHANNEL_RETRY_INTERVAL,
                )
                async_dispatcher_send(self.hass, SIGNAL_STATE_UPDATED, self)
            await asyncio.sleep(CHANNEL_RETRY_INTERVAL)

    async def _async_tcp_command(self, command: int, value: int) -> int | None:
        """发送控制帧并等待回执.

        返回结果码；返回 -1 表示已发出但设备无回执（通道仍可用）；
        返回 None 表示写入失败或连接已断开。
        """
        async with self._tcp_lock:
            writer = self._tcp_writer
            if writer is None:
                _LOGGER.warning("TCP 连接未建立（%s:%s）", self._host, DEFAULT_TCP_PORT)
                return None
            try:
                writer.write(build_frame(command, value))
                await writer.drain()
            except OSError as err:
                _LOGGER.warning(
                    "TCP 写入失败（%s:%s）：%s", self._host, DEFAULT_TCP_PORT, err
                )
                return None
            deadline = time.monotonic() + TCP_COMMAND_TIMEOUT
            while time.monotonic() < deadline:
                try:
                    reply_cmd, reply_value = await asyncio.wait_for(
                        self._tcp_replies.get(),
                        timeout=max(0.1, deadline - time.monotonic()),
                    )
                except TimeoutError:
                    break
                if reply_cmd == command:
                    return reply_value
        _LOGGER.warning(
            "TCP 命令 0x%02X/0x%02X 已发出但 %s 秒内无回执（通道保持可用）",
            command,
            value,
            TCP_COMMAND_TIMEOUT,
        )
        return -1

    @callback
    def _handle_frame(self, command: int, value: int) -> None:
        """分发设备帧：事件帧更新状态，其余作为命令回执入队."""
        event = EVENT_CODES.get((command, value))
        if event is not None:
            self._apply_event(event)
            return
        if self._tcp_replies.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._tcp_replies.get_nowait()
        self._tcp_replies.put_nowait((command, value))

    @callback
    def _apply_event(self, event: tuple[int, str]) -> None:
        """应用视频/音乐事件推送."""
        play_type, state = event
        self.play_type = play_type
        self.play_status = {
            "playing": PLAY_STATUS_PLAYING,
            "paused": PLAY_STATUS_PAUSED,
            "idle": PLAY_STATUS_IDLE,
        }[state]
        if state == "playing":
            self.power_state = "on"
        async_dispatcher_send(self.hass, SIGNAL_STATE_UPDATED, self)

    # ------------------------------------------------------------------
    # 串口传输（9600 8N1）
    # ------------------------------------------------------------------

    def _sync_serial_open(self) -> serial.Serial:
        """打开串口."""
        return serial.Serial(
            self._port, self._baudrate, timeout=SERIAL_TIMEOUT, write_timeout=1
        )

    def _sync_serial_exchange(
        self, command: int, value: int, attempts: int = 2
    ) -> int | None:
        """发送控制帧并读取回执，无应答时重发.

        返回 None 表示写入失败（串口不可用）；返回 -1 表示重发后仍无回执。
        """
        ser = self._serial
        if ser is None or not ser.is_open:
            return None
        for _ in range(attempts):
            try:
                ser.reset_input_buffer()
                ser.write(build_frame(command, value))
                ser.flush()
                reply = read_serial_frame(ser, SERIAL_TIMEOUT)
            except (serial.SerialException, OSError) as err:
                _LOGGER.debug("串口命令失败（0x%02X）：%s", command, err)
                return None
            if reply is not None and reply[0] == command:
                return reply[1]
        return -1

    def _sync_serial_close(self) -> None:
        """关闭串口."""
        ser, self._serial = self._serial, None
        if ser is not None:
            with contextlib.suppress(serial.SerialException, OSError):
                ser.close()

    def _sync_serial_probe(self) -> bool:
        """探测串口上是否接有 MYMEI 设备.

        协议没有查询命令，这里发送一个不存在的键码（0x02/0xFF），
        设备应回「非法指令」；能收到合法帧即视为识别成功。
        收不到回执时退化为「串口能打开即可用」，避免误判为不兼容。
        """
        try:
            ser = self._sync_serial_open()
        except (serial.SerialException, OSError):
            return False
        try:
            time.sleep(0.2)
            for _ in range(2):
                ser.reset_input_buffer()
                ser.write(build_frame(*PROBE_COMMAND))
                ser.flush()
                if read_serial_frame(ser, 1.2) is not None:
                    _LOGGER.warning("串口上收到 MYMEI 协议回执，识别成功")
                    return True
                time.sleep(0.3)
        except (serial.SerialException, OSError):
            return False
        finally:
            with contextlib.suppress(serial.SerialException, OSError):
                ser.close()
        return False

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------

    async def async_connect(self) -> bool:
        """连接设备：TCP 优先，失败降级串口."""
        if self._host:
            if await self._async_tcp_open():
                self._available_tcp = True
                _LOGGER.info("TCP 控制已就绪（%s:%s）", self._host, DEFAULT_TCP_PORT)
                self._reader_task = self.hass.async_create_task(
                    self._async_tcp_reader_loop()
                )
                return True
        if self._port:
            _LOGGER.warning(
                "网络通道不可达（%s，TCP %s），尝试串口降级", self._host, DEFAULT_TCP_PORT
            )
            probed = await self.hass.async_add_executor_job(self._sync_serial_probe)
            if probed:
                self._serial = await self.hass.async_add_executor_job(
                    self._sync_serial_open
                )
                self._available_serial = True
                _LOGGER.info("RS-232 控制已就绪（%s）", self._port)
                return True
            # 兜底：串口能打开即视为可用（设备待机时可能不回执）
            try:
                self._serial = await self.hass.async_add_executor_job(
                    self._sync_serial_open
                )
            except (serial.SerialException, OSError):
                _LOGGER.debug("串口 %s 打开失败", self._port)
            else:
                self._available_serial = True
                _LOGGER.info("RS-232 控制已就绪（%s，探测无回执）", self._port)
                return True
        return False

    async def async_send_command(self, name: str) -> bool:
        """按名称下发命令：TCP → 串口."""
        command = COMMAND_CODES.get(name)
        if command is None:
            _LOGGER.debug("未知命令：%s", name)
            return False
        if not self.is_available:
            # 设备可能刚重新上电，先尝试恢复通道（5 秒内只尝试一次）
            now = time.monotonic()
            if now - self._last_recovery >= COMMAND_RECONNECT_INTERVAL:
                self._last_recovery = now
                await self.async_reconnect()
        result: int | None = None
        if self._available_tcp:
            result = await self._async_tcp_command(*command)
            if result is None:
                self._available_tcp = False
                await self._async_tcp_close()
                _LOGGER.warning("TCP 通道断开，降级到串口")
        if result is None and self._port:
            async with self._serial_lock:
                if self._serial is None or not self._serial.is_open:
                    with contextlib.suppress(serial.SerialException, OSError):
                        self._serial = await self.hass.async_add_executor_job(
                            self._sync_serial_open
                        )
                if self._serial is None:
                    return False
                self._available_serial = True
                result = await self.hass.async_add_executor_job(
                    self._sync_serial_exchange, *command
                )
            if result is None:
                self._available_serial = False
                await self.hass.async_add_executor_job(self._sync_serial_close)
                _LOGGER.warning("串口 %s 写入失败，命令 %s 未能下发", self._port, name)
                return False
        if result is None:
            return False
        if result == RESULT_SUCCESS:
            self._apply_optimistic(name)
            return True
        if result == RESULT_FAIL:
            _LOGGER.warning("命令 %s 被设备拒绝（结果码 0x02 失败）", name)
        elif result == RESULT_INVALID:
            _LOGGER.warning("命令 %s 不受当前固件支持（结果码 0x03 非法指令）", name)
        elif result == -1:
            _LOGGER.warning("命令 %s 已发出，设备未回执", name)
        else:
            _LOGGER.warning("命令 %s 返回未知结果码：%s", name, result)
        return False

    @callback
    def _apply_optimistic(self, name: str) -> None:
        """协议无状态查询，按命令语义乐观更新，避免 UI 滞后."""
        if name == "power_on":
            self.power_state = "on"
        elif name == "power_off":
            self.power_state = "standby"
            self.play_status = PLAY_STATUS_IDLE
        elif name == "power_toggle":
            self.power_state = "standby" if self.power_state == "on" else "on"
            if self.power_state == "standby":
                self.play_status = PLAY_STATUS_IDLE
        elif name == "play":
            self.play_status = PLAY_STATUS_PLAYING
            self.power_state = "on"
        elif name == "pause":
            self.play_status = PLAY_STATUS_PAUSED
        elif name == "play_pause":
            self.play_status = (
                PLAY_STATUS_PAUSED
                if self.play_status == PLAY_STATUS_PLAYING
                else PLAY_STATUS_PLAYING
            )
        elif name == "stop":
            self.play_status = PLAY_STATUS_IDLE
        else:
            return
        async_dispatcher_send(self.hass, SIGNAL_STATE_UPDATED, self)

    async def async_reconnect(self) -> bool:
        """尝试重建任一控制通道（设备重新上电后自动恢复控制）."""
        if self._host and (self._reader_task is None or self._reader_task.done()):
            if await self._async_tcp_open():
                self._available_tcp = True
                self._reader_task = self.hass.async_create_task(
                    self._async_tcp_reader_loop()
                )
        if self._port and (self._serial is None or not self._serial.is_open):
            try:
                self._serial = await self.hass.async_add_executor_job(
                    self._sync_serial_open
                )
            except (serial.SerialException, OSError) as err:
                _LOGGER.warning("串口 %s 打开失败：%s", self._port, err)
            else:
                self._available_serial = True
        if self.is_available:
            _LOGGER.info(
                "控制通道已恢复（TCP=%s 串口=%s）",
                self._available_tcp,
                self._available_serial,
            )
        return self.is_available

    async def async_poll_state(self) -> None:
        """状态轮询：协议无查询命令，只在通道全掉线时尝试恢复."""
        if self.is_available:
            return
        if not self._host and not self._port:
            return
        now = time.monotonic()
        if now - self._last_recovery < RECOVERY_INTERVAL:
            return
        self._last_recovery = now
        if await self.async_reconnect():
            async_dispatcher_send(self.hass, SIGNAL_STATE_UPDATED, self)

    async def async_close(self) -> None:
        """关闭全部通道."""
        self._closed = True
        task, self._reader_task = self._reader_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self._async_tcp_close()
        await self.hass.async_add_executor_job(self._sync_serial_close)

    @property
    def device_info(self) -> DeviceInfo:
        """设备信息."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._host or self._port or "unknown")},
            name=self.name,
            manufacturer="MYMEI",
            model=self.model,
            sw_version=self.version,
        )


type MymeiConfigEntry = ConfigEntry[MymeiPlayer]


async def async_setup_entry(hass: HomeAssistant, entry: MymeiConfigEntry) -> bool:
    """设置集成."""
    device = MymeiPlayer(
        hass,
        entry.data.get(CONF_HOST),
        entry.data.get(CONF_PORT),
        entry.data.get(CONF_BAUDRATE, DEFAULT_BAUDRATE),
    )
    if not await device.async_connect():
        await device.async_close()
        raise ConfigEntryNotReady(
            "无法连接 MYMEI 播放器：TCP 33080 / 串口均不可用（设备可能已待机，稍后自动重试）"
        )
    entry.runtime_data = device
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(
    hass: HomeAssistant, entry: MymeiConfigEntry
) -> None:
    """配置更新时重载."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: MymeiConfigEntry) -> bool:
    """卸载集成."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.async_close()
    return unload_ok
