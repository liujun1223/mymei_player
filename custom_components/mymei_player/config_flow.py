"""MYMEI Player 配置流程."""

import asyncio
import contextlib
import logging
from pathlib import Path
import time
from typing import Any

import serial
from serial.tools import list_ports
import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    CONF_BAUDRATE,
    CONF_HOST,
    CONF_PORT,
    DEFAULT_BAUDRATE,
    DEFAULT_TCP_PORT,
    DOMAIN,
    PROBE_COMMAND,
    TCP_CONNECT_TIMEOUT,
    build_frame,
    read_serial_frame,
)

_LOGGER = logging.getLogger(__name__)

SERIAL_MANUAL = "manual"  # 串口列表中的“不使用串口”占位项

# pyserial 在部分 HA OS 环境不会枚举板载 DB9 串口，这里补充常见 ttyS 节点
EXTRA_SERIAL_DEVICES = tuple(f"/dev/ttyS{i}" for i in range(8))


class MymeiConfigFlow(ConfigFlow, domain=DOMAIN):
    """MYMEI 播放器配置流程."""

    VERSION = 1

    def __init__(self) -> None:
        """初始化."""
        self._selected: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # 串口 / 网络探测
    # ------------------------------------------------------------------

    def _sync_list_serial_ports(self) -> list[str]:
        """列出本机串口（含 pyserial 未枚举到的板载 ttyS 口）."""
        devices = {p.device for p in list_ports.comports()}
        devices.update(
            device for device in EXTRA_SERIAL_DEVICES if Path(device).exists()
        )
        return sorted(devices)

    def _sync_probe_candidates(self) -> list[str]:
        """自动识别候选串口：USB 转串口优先，其次板载 ttyS 口."""
        ports = list_ports.comports()
        _LOGGER.warning(
            "串口枚举: %s（回执为 7F 57 53 02 … 0D 即识别为 MYMEI 设备）",
            [(p.device, p.description, p.vid, p.pid) for p in ports],
        )
        usb = sorted(p.device for p in ports if p.vid is not None)
        return usb + [
            device for device in self._sync_list_serial_ports() if device not in usb
        ]

    def _sync_probe_port(self, port: str, baudrate: int) -> dict[str, Any] | None:
        """发送不存在的键码 0x02/0xFF，能收到合法帧即识别为 MYMEI 设备."""
        received = ""
        try:
            with serial.Serial(port, baudrate, timeout=0.2) as ser:
                for attempt in range(2):
                    time.sleep(0.2 if attempt == 0 else 0.4)
                    ser.reset_input_buffer()
                    ser.write(build_frame(*PROBE_COMMAND))
                    ser.flush()
                    reply = read_serial_frame(ser, 1.2)
                    if reply is not None:
                        _LOGGER.warning(
                            "串口 %s 识别为 MYMEI 播放器（回执 0x%02X/0x%02X）",
                            port,
                            reply[0],
                            reply[1],
                        )
                        return {"port": port, "baudrate": baudrate}
                    received = "无回执"
        except (serial.SerialException, OSError) as err:
            _LOGGER.warning("串口 %s 不可用，已跳过：%s", port, err)
            return None
        _LOGGER.warning("串口 %s 未返回 MYMEI 协议回执：%s", port, received)
        return None

    def _sync_serial_openable(self, port: str, baudrate: int) -> bool:
        """兜底校验：串口能打开即可."""
        try:
            with serial.Serial(port, baudrate, timeout=1):
                return True
        except (serial.SerialException, OSError):
            return False

    async def _async_tcp_probe(self, host: str) -> bool:
        """连接 TCP 33080 验证网络通道可用."""
        try:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, DEFAULT_TCP_PORT),
                timeout=TCP_CONNECT_TIMEOUT,
            )
        except (TimeoutError, OSError) as err:
            _LOGGER.warning("TCP %s:%s 连接失败：%s", host, DEFAULT_TCP_PORT, err)
            return False
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()
        return True

    # ------------------------------------------------------------------
    # 配置步骤
    # ------------------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """入口：自动识别串口或手动配置."""
        if user_input is not None:
            if user_input.get("auto_detect"):
                return await self.async_step_auto_detect()
            return await self.async_step_manual()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Optional("auto_detect", default=True): bool}),
        )

    async def async_step_auto_detect(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """扫描串口，自动识别 MYMEI 播放器（并行探测）."""
        ports = await self.hass.async_add_executor_job(self._sync_probe_candidates)
        if ports:
            _LOGGER.warning("开始并行扫描串口，候选: %s", ports)
        else:
            _LOGGER.warning(
                "未发现任何串口设备；请确认串口已接入并映射到 Home Assistant"
                "（Docker 需 --device=/dev/ttyS0 或 privileged），或改用 IP 控制"
            )
        probe_tasks = [
            self.hass.async_add_executor_job(
                self._sync_probe_port, port, DEFAULT_BAUDRATE
            )
            for port in ports
        ]
        results = await asyncio.gather(*probe_tasks)
        for port, result in zip(ports, results, strict=False):
            if result:
                _LOGGER.info("在 %s 上发现 MYMEI 播放器", port)
                self._selected = result
                return await self.async_step_host()
        _LOGGER.warning("未在串口上发现 MYMEI 播放器，转为手动配置（请保持播放器开机时重试）")
        return await self.async_step_manual()

    async def async_step_host(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """识别到串口设备后，选填 IP."""
        if user_input is not None:
            data: dict[str, Any] = {
                CONF_PORT: self._selected["port"],
                CONF_BAUDRATE: self._selected["baudrate"],
            }
            host = (user_input.get(CONF_HOST) or "").strip()
            if host:
                data[CONF_HOST] = host
            return await self._async_create(data)

        return self.async_show_form(
            step_id="host",
            data_schema=vol.Schema({vol.Optional(CONF_HOST): str}),
            description_placeholders={"name": "MYMEI 播放器"},
        )

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """手动配置：串口与 IP 至少填一个."""
        errors: dict[str, str] = {}
        ports = await self.hass.async_add_executor_job(self._sync_list_serial_ports)

        if user_input is not None:
            port = user_input.get(CONF_PORT) or ""
            if port == SERIAL_MANUAL:
                port = ""
            host = (user_input.get(CONF_HOST) or "").strip()

            if not port and not host:
                errors["base"] = "no_channels"
            else:
                if host and not await self._async_tcp_probe(host):
                    errors[CONF_HOST] = "cannot_connect_tcp"
                if port and not errors:
                    probed = await self.hass.async_add_executor_job(
                        self._sync_probe_port, port, DEFAULT_BAUDRATE
                    )
                    if not probed:
                        openable = await self.hass.async_add_executor_job(
                            self._sync_serial_openable, port, DEFAULT_BAUDRATE
                        )
                        if not openable:
                            errors[CONF_PORT] = "cannot_connect_serial"
                        else:
                            _LOGGER.warning(
                                "串口 %s 探测无回执，但可打开，允许保存", port
                            )
                if not errors:
                    data: dict[str, Any] = {CONF_BAUDRATE: DEFAULT_BAUDRATE}
                    if port:
                        data[CONF_PORT] = port
                    if host:
                        data[CONF_HOST] = host
                    return await self._async_create(data)

        port_options = [
            {"value": SERIAL_MANUAL, "label": "不使用串口（仅 IP 控制）"}
        ] + [{"value": p, "label": p} for p in ports]
        return self.async_show_form(
            step_id="manual",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_PORT, default=SERIAL_MANUAL): SelectSelector(
                        SelectSelectorConfig(
                            options=port_options, mode=SelectSelectorMode.DROPDOWN
                        )
                    ),
                    vol.Optional(CONF_HOST, default=""): str,
                }
            ),
            errors=errors,
        )

    # ------------------------------------------------------------------
    # 创建配置项
    # ------------------------------------------------------------------

    async def _async_create(self, data: dict[str, Any]) -> ConfigFlowResult:
        """取唯一 ID 并创建配置项."""
        host = data.get(CONF_HOST)
        unique_id = host or data.get(CONF_PORT)
        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title="MYMEI Player", data=data)
