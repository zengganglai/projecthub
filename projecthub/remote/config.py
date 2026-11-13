"""远控配置值及不涉及网络的结构校验。"""

import math
import re
from dataclasses import dataclass, field
from ipaddress import ip_address

_HOST_LABEL = re.compile(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?")


@dataclass(frozen=True, slots=True)
class RemoteControlConfig:
    enabled: bool = False
    debug_logging: bool = False
    bind_host: str = "0.0.0.0"
    public_host: str = "127.0.0.1"
    frp_port: int = 8002
    rfb_port: int = 8003
    api_token: str = field(default="", repr=False)
    frp_token: str = field(default="", repr=False)
    capacity: int = 1000
    record_capacity: int = 100000
    snapshot_capacity: int = 100
    snapshot_seconds: float = 60
    lease_seconds: float = 30
    renew_after_seconds: int = 10
    heartbeat_seconds: float = 30
    handshake_seconds: float = 5
    work_seconds: float = 5
    write_seconds: float = 10
    half_close_seconds: float = 2
    sweep_seconds: float = 0.5
    timestamp_window_seconds: int = 0
    pending_capacity: int = 64


def _host_is_valid(value: str) -> bool:
    if not isinstance(value, str) or not 0 < len(value) <= 253:
        return False
    try:
        ip_address(value)
    except ValueError:
        return all(_HOST_LABEL.fullmatch(label) for label in value.split("."))
    return True


def _positive_seconds(value: object) -> bool:
    try:
        return type(value) in (int, float) and value > 0 and math.isfinite(value)
    except OverflowError:
        return False


def validate_remote_config(config: RemoteControlConfig) -> None:
    """验证配置结构并抛出 ValueError。是否具备运行凭据由启动时判断。"""
    if type(config) is not RemoteControlConfig:
        raise ValueError("远控配置必须使用 RemoteControlConfig。")
    for name in ("enabled", "debug_logging"):
        if type(getattr(config, name)) is not bool:
            raise ValueError(f"remote_control.{name} 必须为布尔值。")
    for name in ("bind_host", "public_host"):
        if not _host_is_valid(getattr(config, name)):
            raise ValueError(f"remote_control.{name} 必须为 IP 地址或主机名。")

    integer_bounds = {
        "frp_port": (0, 65535),
        "rfb_port": (0, 65535),
        "capacity": (1, 1000),
        "record_capacity": (1, None),
        "snapshot_capacity": (1, None),
        "pending_capacity": (1, None),
        "renew_after_seconds": (1, 300),
        "timestamp_window_seconds": (0, None),
    }
    for name, (minimum, maximum) in integer_bounds.items():
        value = getattr(config, name)
        if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
            raise ValueError(f"remote_control.{name} 必须为范围内的整数。")
    if config.frp_port and config.frp_port == config.rfb_port:
        raise ValueError("frp 与 RFB 的非零端口不能相同。")
    for name in (
        "lease_seconds",
        "snapshot_seconds",
        "heartbeat_seconds",
        "handshake_seconds",
        "work_seconds",
        "write_seconds",
        "half_close_seconds",
        "sweep_seconds",
    ):
        if not _positive_seconds(getattr(config, name)):
            raise ValueError(f"remote_control.{name} 必须为有限正数。")
    if not config.renew_after_seconds < config.lease_seconds <= 600:
        raise ValueError("租约有效期须长于建议续租间隔且不超过 600 秒。")
    if any(not isinstance(token, str) for token in (config.api_token, config.frp_token)):
        raise ValueError("远控令牌必须为字符串。")
