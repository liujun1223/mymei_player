r"""Constants for the MYMEI Player integration.

协议依据：
- 《MYMEI 串口协议 V7.0》RS-232：9600 8N1，HEX 帧
- 《MYMEI IP 控制协议 V7.0》TCP：33080，HEX 帧；事件同时以 UDP 33080 广播

统一帧格式（8 字节）::

    7F 57 53 02 <命令码> <参数> <校验和> 0D

校验和 =（命令码 + 参数）& 0xFF；应答帧第 6 字节换成结果：

    0x00 未知 / 0x01 成功 / 0x02 失败 / 0x03 非法指令

协议没有状态查询命令，电源/播放状态只能靠下发命令后的乐观更新
与设备主动推送的事件帧（视频/音乐 开始、停止、暂停、恢复）维护。
"""

import time
from typing import Any

DOMAIN = "mymei_player"

CONF_HOST = "host"
CONF_PORT = "port"
CONF_BAUDRATE = "baudrate"

DEFAULT_BAUDRATE = 9600
DEFAULT_TCP_PORT = 33080
TCP_CONNECT_TIMEOUT = 4
TCP_COMMAND_TIMEOUT = 3
TCP_READ_TIMEOUT = 75  # 长连接读超时；超时视为断线触发重连
SERIAL_TIMEOUT = 1
# 通道失败后的退避时间（秒），期间降级到另一个通道
CHANNEL_RETRY_INTERVAL = 60
# 所有通道掉线后的恢复探测间隔（秒），设备重新上电后自动恢复
RECOVERY_INTERVAL = 30
# 下发命令时若通道全部掉线，最小重连间隔（秒）
COMMAND_RECONNECT_INTERVAL = 5

# 支持 USB 转串口的芯片 VID
# FTDI: 0x0403, Silicon Labs: 0x10C4, Prolific: 0x067B, CH340: 0x1A86
SUPPORTED_USB_VIDS = [0x0403, 0x10C4, 0x067B, 0x1A86]

# ---------------------------------------------------------------------------
# 帧结构
# ---------------------------------------------------------------------------
FRAME_HEADER = b"\x7fWS"  # 7F 57 53
FRAME_TYPE = 0x02  # 第 4 字节固定 02
FRAME_TAIL = 0x0D
FRAME_LENGTH = 8

# 应答结果
RESULT_UNKNOWN = 0x00
RESULT_SUCCESS = 0x01
RESULT_FAIL = 0x02
RESULT_INVALID = 0x03

# 探测用帧：遥控器键码 0xFF 不存在，设备应回「非法指令」证明协议匹配
PROBE_COMMAND = (0x02, 0xFF)

# ---------------------------------------------------------------------------
# 指令码表：name -> (命令码, 参数)
# 遥控器按键统一用 命令码=0x02；系统设置类用各自的分类码
# ---------------------------------------------------------------------------
COMMAND_CODES: dict[str, tuple[int, int]] = {
    # 开关机
    "power_on": (0x01, 0x01),  # 待机开（开启屏幕）
    "power_off": (0x01, 0x02),  # 待机关（关闭屏幕）
    "reboot": (0x01, 0x03),
    "power_toggle": (0x01, 0x04),  # 待机状态切换
    # 导航与菜单
    "home": (0x02, 0x03),
    "back": (0x02, 0x04),
    "up": (0x02, 0x13),
    "down": (0x02, 0x14),
    "left": (0x02, 0x15),
    "right": (0x02, 0x16),
    "ok": (0x02, 0x17),
    "page_up": (0x02, 0x13),
    "page_down": (0x02, 0x14),
    "delete": (0x02, 0x43),
    "menu": (0x02, 0x52),
    "settings": (0x02, 0x89),
    # 协议文档里该行名称乱码，参数 0x38，按遥控器「信息」键处理
    "info": (0x02, 0x38),
    "refresh_library": (0x02, 0xB2),
    "zoom": (0x02, 0xAA),
    # 功能入口
    "movie": (0x02, 0xD0),
    "music": (0x02, 0xD1),
    "file": (0x02, 0x8B),
    # 音量
    "volume_up": (0x02, 0x18),
    "volume_down": (0x02, 0x19),
    "mute": (0x02, 0xA4),
    # 播放控制
    "play": (0x02, 0x7E),
    "pause": (0x02, 0x7F),
    "play_pause": (0x02, 0x55),
    "stop": (0x02, 0x56),
    "fast_forward": (0x02, 0x5A),
    "rewind": (0x02, 0x59),
    "ff_start": (0x03, 0x08),  # 持续快进
    "rew_start": (0x03, 0x09),  # 持续快退
    "ff_stop": (0x03, 0x0A),  # 停止持续快进快退
    # 音轨字幕
    "audio": (0x02, 0xDE),
    "subtitle": (0x02, 0xAF),
    # 系统设置
    "hdr_to_sdr_on": (0x0B, 0x01),
    "hdr_to_sdr_off": (0x0B, 0x02),
    "audio_downmix_on": (0x08, 0x02),
    "audio_downmix_off": (0x08, 0x03),
    "audio_out_hdmi": (0x0C, 0x01),
    "audio_out_hifi": (0x0C, 0x02),
    "res_720p": (0x07, 0x01),
    "res_1080p60": (0x07, 0x02),
    "res_1080p24": (0x07, 0x03),
    "res_2160p60": (0x07, 0x04),
    "res_2160p30": (0x07, 0x05),
    "res_2160p24": (0x07, 0x06),
    "factory_reset": (0x0A, 0x01),
}

COMMAND_CODES.update({f"num_{i}": (0x02, 0x07 + i) for i in range(10)})

# ---------------------------------------------------------------------------
# 事件推送帧（设备主动上报，无返回值）
# key = (命令码, 参数)，value = (媒体类型, 播放状态)
# 媒体类型沿用 HA 的 media_content_type：1 视频 / 2 音乐
# ---------------------------------------------------------------------------
EVENT_CODES: dict[tuple[int, int], tuple[int, str]] = {
    (0x06, 0x01): (2, "playing"),  # 音乐开始播放
    (0x06, 0x02): (2, "idle"),  # 音乐停止播放
    (0x06, 0x03): (2, "paused"),  # 音乐暂停播放
    (0x06, 0x04): (2, "playing"),  # 音乐恢复播放
    (0x06, 0x05): (2, "paused"),  # 音乐暂停 10 分钟
    (0x04, 0x01): (1, "playing"),  # 视频开始播放
    (0x04, 0x02): (1, "idle"),  # 视频停止播放
    (0x04, 0x03): (1, "paused"),  # 视频暂停播放
    (0x04, 0x04): (1, "playing"),  # 视频恢复播放
    (0x04, 0x05): (1, "paused"),  # 视频暂停 10 分钟
}

# 播放状态（与媒体实体映射一致）
PLAY_STATUS_PLAYING = 1
PLAY_STATUS_PAUSED = 2
PLAY_STATUS_IDLE = -1

PLAY_TYPE_VIDEO = 1
PLAY_TYPE_MUSIC = 2


def checksum(command: int, value: int) -> int:
    """协议校验和：命令码 + 参数（模 256）."""
    return (command + value) & 0xFF


def build_frame(command: int, value: int) -> bytes:
    """构造 8 字节控制帧."""
    return bytes(
        (
            0x7F,
            0x57,
            0x53,
            FRAME_TYPE,
            command,
            value,
            checksum(command, value),
            FRAME_TAIL,
        )
    )


def split_frames(buffer: bytes) -> tuple[list[bytes], bytes]:
    """从字节流中切出完整帧，返回 (完整帧列表, 剩余未成帧字节)."""
    frames: list[bytes] = []
    while True:
        index = buffer.find(FRAME_HEADER)
        if index < 0:
            # 头部可能被拆包，保留最后 2 字节等待下次拼接
            return frames, buffer[-2:] if len(buffer) > 2 else buffer
        buffer = buffer[index:]
        if len(buffer) < FRAME_LENGTH:
            return frames, buffer
        if buffer[FRAME_LENGTH - 1] != FRAME_TAIL:
            buffer = buffer[1:]
            continue
        frames.append(buffer[:FRAME_LENGTH])
        buffer = buffer[FRAME_LENGTH:]


def read_serial_frame(ser: Any, timeout: float) -> tuple[int, int] | None:
    """从串口读取一帧，返回 (命令码, 值)；超时返回 None.

    值字段在应答帧里是结果码（0x00~0x03），在事件帧里是事件参数。
    """
    deadline = time.monotonic() + timeout
    buffer = b""
    prev_timeout = ser.timeout
    ser.timeout = 0.05
    try:
        while time.monotonic() < deadline:
            chunk = ser.read(ser.in_waiting or 1)
            if not chunk:
                continue
            buffer += chunk
            frames, buffer = split_frames(buffer)
            for frame in frames:
                return frame[4], frame[5]
            if len(buffer) > 64:
                buffer = buffer[-64:]
    finally:
        ser.timeout = prev_timeout
    return None
