"""协议常量与帧编解码测试."""

from custom_components.mymei_player.const import (
    COMMAND_CODES,
    EVENT_CODES,
    FRAME_HEADER,
    FRAME_LENGTH,
    FRAME_TAIL,
    build_frame,
    checksum,
    split_frames,
)


def test_command_codes_are_bytes() -> None:
    """所有指令码都是 (命令码, 参数) 且落在 0x00~0xFF."""
    for name, (command, value) in COMMAND_CODES.items():
        assert name
        assert 0x00 <= command <= 0xFF, f"{name} 的命令码 {command:#04x} 越界"
        assert 0x00 <= value <= 0xFF, f"{name} 的参数 {value:#04x} 越界"


def test_core_commands_present() -> None:
    """媒体播放器核心命令必须存在."""
    for name in (
        "power_on",
        "power_off",
        "power_toggle",
        "play",
        "pause",
        "stop",
        "volume_up",
        "volume_down",
        "mute",
    ):
        assert name in COMMAND_CODES


def test_build_frame() -> None:
    """帧结构：7F 57 53 02 命令码 参数 校验和 0D."""
    frame = build_frame(0x02, 0x7E)
    assert frame == bytes((0x7F, 0x57, 0x53, 0x02, 0x02, 0x7E, 0x80, 0x0D))
    assert len(frame) == FRAME_LENGTH
    assert frame.startswith(FRAME_HEADER)
    assert frame[-1] == FRAME_TAIL


def test_checksum_matches_protocol_document() -> None:
    """抽查协议文档里的指令值."""
    assert build_frame(0x01, 0x01) == bytes(
        (0x7F, 0x57, 0x53, 0x02, 0x01, 0x01, 0x02, 0x0D)
    )
    assert build_frame(0x01, 0x02) == bytes(
        (0x7F, 0x57, 0x53, 0x02, 0x01, 0x02, 0x03, 0x0D)
    )
    assert build_frame(0x02, 0x56) == bytes(
        (0x7F, 0x57, 0x53, 0x02, 0x02, 0x56, 0x58, 0x0D)
    )
    assert build_frame(0x03, 0x0A) == bytes(
        (0x7F, 0x57, 0x53, 0x02, 0x03, 0x0A, 0x0D, 0x0D)
    )


def test_checksum_wraps() -> None:
    """校验和按字节回绕."""
    assert checksum(0x02, 0xFF) == 0x01


def test_split_frames() -> None:
    """粘包、半包都能正确切开."""
    one = build_frame(0x02, 0x7E)
    two = build_frame(0x02, 0x7F)
    frames, rest = split_frames(one + two)
    assert frames == [one, two]
    assert rest == b""

    frames, rest = split_frames(one + two[:3])
    assert frames == [one]
    assert rest == two[:3]


def test_event_codes() -> None:
    """事件帧只映射到已知播放状态."""
    for (command, value), (play_type, state) in EVENT_CODES.items():
        assert 0x00 <= command <= 0xFF
        assert 0x00 <= value <= 0xFF
        assert play_type in (1, 2)
        assert state in ("playing", "paused", "idle")
