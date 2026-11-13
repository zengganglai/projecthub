"""不可变启动配置；校验成功后统一返回绝对路径，不创建运行资源。"""

import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

from projecthub.contracts import HubError
from projecthub.remote import RemoteControlConfig, validate_remote_config


@dataclass(frozen=True, slots=True)
class HttpConfig:
    bind_host: str = "0.0.0.0"
    port: int = 8001
    max_concurrent_requests: int = 16
    host_restriction_enabled: bool = False
    allowed_host: str = "localhost"
    max_json_body_bytes: int = 1048576


@dataclass(frozen=True, slots=True)
class DatabaseConfig:
    path: Path = Path("data/sql/device_bridge.sqlite3")
    busy_timeout_ms: int = 2000


@dataclass(frozen=True, slots=True)
class StorageConfig:
    object_dir: Path = Path("data/files")
    audit_dir: Path = Path("data/audit")
    cache_dir: Path = Path("data/cache")
    logs_dir: Path = Path("data/logs")
    backups_dir: Path = Path("data/backups")


@dataclass(frozen=True, slots=True)
class MigrationConfig:
    lock_path: Path = Path("data/sql/migration.lock")


@dataclass(frozen=True, slots=True)
class UserConfig:
    max_users: int = 100


@dataclass(frozen=True, slots=True)
class UploadConfig:
    max_curve_bytes: int = 16 * 1024 * 1024
    max_firmware_bytes: int = 256 * 1024 * 1024
    max_internal_bytes: int = 256 * 1024 * 1024
    max_public_bytes: int = 256 * 1024 * 1024


def _source_root():
    root = Path(__file__).resolve().parents[2]
    try:
        metadata = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8-sig"))
        if metadata["project"]["name"] == "projecthub":
            return root
    except OSError, ValueError, KeyError:
        pass
    raise HubError("CONFIGURATION_ERROR", "安装运行时须显式指定 project_root。")


@dataclass(frozen=True, slots=True)
class AppConfig:
    project_root: Path = field(default_factory=_source_root)
    http: HttpConfig = field(default_factory=HttpConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    migrations: MigrationConfig = field(default_factory=MigrationConfig)
    remote_control: RemoteControlConfig = field(default_factory=RemoteControlConfig)
    users: UserConfig = field(default_factory=UserConfig)
    uploads: UploadConfig = field(default_factory=UploadConfig)

    @property
    def data_root(self):
        return self.project_root / "data"


def _integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"整数必须位于 {low}～{high}。")


def validate_http(settings: HttpConfig):
    if type(settings) is not HttpConfig:
        raise HubError("HTTP_CONFIGURATION_ERROR", "HTTP 配置类型无效。")
    try:
        _integer(settings.port, 0, 65535)
        _integer(settings.max_concurrent_requests, 1, 100000)
        _integer(settings.max_json_body_bytes, 1, 2147483647)
        if type(settings.host_restriction_enabled) is not bool:
            raise ValueError("Host 检查开关必须是布尔值。")
        for host in (settings.bind_host, settings.allowed_host):
            if (
                not isinstance(host, str)
                or not host.strip()
                or any(c.isspace() for c in host)
                or any(c in host for c in "/\\@")
            ):
                raise ValueError("监听或允许主机名称无效。")
    except ValueError as error:
        raise HubError("HTTP_CONFIGURATION_ERROR", str(error)) from error


def resolve_settings(settings: AppConfig) -> AppConfig:
    from projecthub.storage import reject_links

    if type(settings) is not AppConfig:
        raise HubError("CONFIGURATION_ERROR", "配置必须为 AppConfig。")
    try:
        validate_http(settings.http)
        validate_remote_config(settings.remote_control)
        root = Path(settings.project_root).absolute()
        reject_links(root)
        root = root.resolve()
        data_root = root / "data"

        def normalize(value):
            candidate = Path(value)
            if not candidate.is_absolute():
                candidate = root / candidate
            reject_links(candidate)
            candidate = candidate.resolve()
            if candidate == data_root or not candidate.is_relative_to(data_root):
                raise ValueError("运行资源必须位于项目 data 子目录。")
            return candidate

        storage = replace(
            settings.storage,
            **{
                name: normalize(getattr(settings.storage, name))
                for name in StorageConfig.__dataclass_fields__
            },
        )
        directories = [getattr(storage, name) for name in StorageConfig.__dataclass_fields__]
        if any(
            a == b or a.is_relative_to(b) or b.is_relative_to(a)
            for i, a in enumerate(directories)
            for b in directories[i + 1 :]
        ):
            raise ValueError("不同用途的运行目录不能重叠。")
        database = replace(settings.database, path=normalize(settings.database.path))
        migrations = replace(settings.migrations, lock_path=normalize(settings.migrations.lock_path))
        if database.path == migrations.lock_path or any(
            database.path.is_relative_to(p) or migrations.lock_path.is_relative_to(p) for p in directories
        ):
            raise ValueError("数据库和维护锁必须独立于文件目录。")
        _integer(database.busy_timeout_ms, 0, 2147483647)
        _integer(settings.users.max_users, 1, 100)
        for name in UploadConfig.__dataclass_fields__:
            _integer(getattr(settings.uploads, name), 1, 2**63 - 1)
        return replace(settings, project_root=root, storage=storage, database=database, migrations=migrations)
    except (ValueError, TypeError, OSError, RuntimeError) as error:
        raise HubError("CONFIGURATION_ERROR", str(error)) from error
