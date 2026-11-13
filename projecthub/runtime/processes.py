"""Windows 受管服务。使用项目记录和带运行标识的文件通知进行优雅关闭。"""

from __future__ import annotations

import ctypes
import json
import math
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from threading import Event, Thread

from projecthub.application import AppConfig, HttpConfig, resolve_settings
from projecthub.contracts import HubError, decode_json
from projecthub.storage import FileLock, reject_links

_CODE_ROOT = Path(__file__).resolve().parents[2]
_ENTRY = _CODE_ROOT / "main.py"
_NONCE_ENV = "_PROJECTHUB_LAUNCH_ID"
_NONCE = re.compile(r"[0-9a-f]{32}")
_IDENTITY_KEYS = ("pid", "start_time", "interpreter", "command_line")
_NOTICE_KEYS = ("pid", "start_time", "nonce", "project_root")
_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PROCESS_COMMAND_LINE_INFORMATION = 60
_MAX_RECORD_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class _Locations:
    logs: Path

    @property
    def record(self) -> Path:
        return self.logs / "service.json"

    @property
    def stop(self) -> Path:
        return self.logs / "service-stop.json"

    def lock(self) -> FileLock:
        return FileLock(
            self.logs / "service.lock", "SERVICE_ALREADY_RUNNING", "已有受管服务持有本项目运行锁。"
        )

    def log_path(self, stream: str) -> Path:
        return self.logs / f"service.{stream}.log"


@dataclass(frozen=True, slots=True)
class _Identity:
    pid: int
    start_time: int
    interpreter: str
    command_line: str


class _UnicodeString(ctypes.Structure):
    _fields_ = [("length", wintypes.USHORT), ("capacity", wintypes.USHORT), ("buffer", ctypes.c_void_p)]


def _windows_only() -> None:
    if os.name != "nt":
        raise HubError("SERVICE_PLATFORM_UNSUPPORTED", "受管后台服务支持 Windows。")


def _api(library, name, returns, *arguments):
    function = getattr(library, name)
    function.argtypes = arguments
    function.restype = returns
    return function


@lru_cache(maxsize=1)
def _windows_calls():
    _windows_only()
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll")
    shell = ctypes.WinDLL("shell32")
    filetime_pointer = ctypes.POINTER(wintypes.FILETIME)
    dword_pointer = ctypes.POINTER(wintypes.DWORD)
    handle = wintypes.HANDLE
    return {
        "open": _api(kernel, "OpenProcess", handle, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD),
        "close": _api(kernel, "CloseHandle", wintypes.BOOL, handle),
        "state": _api(kernel, "GetExitCodeProcess", wintypes.BOOL, handle, dword_pointer),
        "times": _api(kernel, "GetProcessTimes", wintypes.BOOL, handle, *([filetime_pointer] * 4)),
        "image": _api(
            kernel,
            "QueryFullProcessImageNameW",
            wintypes.BOOL,
            handle,
            wintypes.DWORD,
            wintypes.LPWSTR,
            dword_pointer,
        ),
        "command": _api(
            ntdll,
            "NtQueryInformationProcess",
            ctypes.c_long,
            handle,
            wintypes.ULONG,
            ctypes.c_void_p,
            wintypes.ULONG,
            ctypes.POINTER(wintypes.ULONG),
        ),
        "argv": _api(
            shell,
            "CommandLineToArgvW",
            ctypes.POINTER(wintypes.LPWSTR),
            wintypes.LPCWSTR,
            ctypes.POINTER(ctypes.c_int),
        ),
        "free": _api(kernel, "LocalFree", ctypes.c_void_p, ctypes.c_void_p),
    }


def _read_command_line(query, handle) -> str:
    required = wintypes.ULONG()
    query(handle, _PROCESS_COMMAND_LINE_INFORMATION, None, 0, ctypes.byref(required))
    if not ctypes.sizeof(_UnicodeString) <= required.value <= _MAX_RECORD_BYTES:
        raise OSError("无效的进程命令行缓冲区长度。")
    buffer = ctypes.create_string_buffer(required.value)
    status = query(handle, _PROCESS_COMMAND_LINE_INFORMATION, buffer, len(buffer), ctypes.byref(required))
    if status < 0:
        raise OSError("无法读取进程命令行。")
    contents = _UnicodeString.from_buffer(buffer)
    start = ctypes.addressof(buffer)
    if (
        not contents.buffer
        or contents.length % 2
        or contents.length > contents.capacity
        or not start <= contents.buffer <= contents.buffer + contents.length <= start + len(buffer)
    ):
        raise OSError("进程命令行超出缓冲区。")
    return ctypes.wstring_at(contents.buffer, contents.length // 2)


def _identity(pid: int) -> _Identity | None:
    calls = _windows_calls()
    handle = calls["open"](_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        if ctypes.get_last_error() == 87:
            return None
        raise HubError("SERVICE_IDENTITY_UNAVAILABLE", "无法查询运行记录中的进程。")
    try:
        exit_code = wintypes.DWORD()
        if not calls["state"](handle, ctypes.byref(exit_code)):
            raise OSError("无法查询进程状态。")
        if exit_code.value != _STILL_ACTIVE:
            return None
        created, exited, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
        if not calls["times"](
            handle, *(ctypes.byref(value) for value in (created, exited, kernel_time, user_time))
        ):
            raise OSError("无法查询进程创建时间。")
        image = ctypes.create_unicode_buffer(32768)
        image_length = wintypes.DWORD(len(image))
        if not calls["image"](handle, 0, image, ctypes.byref(image_length)):
            raise OSError("无法查询进程映像。")
        stamp = (created.dwHighDateTime << 32) + created.dwLowDateTime
        return _Identity(pid, stamp, image.value, _read_command_line(calls["command"], handle))
    except OSError as error:
        raise HubError("SERVICE_IDENTITY_UNAVAILABLE", "无法完整验证运行记录中的进程。") from error
    finally:
        calls["close"](handle)


def _arguments(command: str) -> list[str]:
    calls = _windows_calls()
    count = ctypes.c_int()
    vector = calls["argv"](command, ctypes.byref(count))
    if not vector:
        raise HubError("SERVICE_IDENTITY_UNAVAILABLE", "无法解析进程的 Windows 命令行。")
    try:
        return vector[: count.value]
    finally:
        calls["free"](vector)


def _entry_arguments(command: str) -> list[str]:
    arguments = _arguments(command)
    index = 1
    while index < len(arguments):
        argument = arguments[index]
        if argument in ("-X", "-W"):
            index += 2
        elif argument in ("-B", "-E", "-I", "-O", "-OO", "-P", "-s", "-S", "-u") or argument.startswith(
            ("-X", "-W")
        ):
            index += 1
        else:
            break
    if index < len(arguments) and Path(arguments[index]) == _ENTRY:
        return arguments[index + 1 :]
    return []


def _verify_entry(identity: _Identity, project: Path) -> None:
    arguments = _entry_arguments(identity.command_line)
    if arguments.count("--managed") == 1 and arguments.count("--project-root") == 1:
        position = arguments.index("--project-root") + 1
        if position < len(arguments):
            root = Path(arguments[position])
            if root.is_absolute() and root.resolve() == project:
                return
    raise HubError("SERVICE_IDENTITY_MISMATCH", "进程入口或项目根目录与受管服务不符。")


def _location(settings: AppConfig) -> tuple[AppConfig, _Locations]:
    settings = resolve_settings(settings)
    files = _Locations(settings.storage.logs_dir)
    try:
        for path in (
            files.record,
            files.stop,
            files.logs / "service.lock",
            files.log_path("stdout"),
            files.log_path("stderr"),
        ):
            reject_links(path)
    except (OSError, ValueError) as error:
        raise HubError("SERVICE_PATH_CONFLICT", "运行记录或日志路径不安全。") from error
    return settings, files


def _read(path: Path) -> dict | None:
    try:
        with path.open("rb") as stream:
            raw = stream.read(_MAX_RECORD_BYTES + 1)
        if len(raw) > _MAX_RECORD_BYTES:
            raise ValueError("运行记录过大。")
        record = decode_json(raw)
        if not isinstance(record, dict):
            raise ValueError("运行记录须为对象。")
        return record
    except FileNotFoundError:
        return None
    except (OSError, ValueError, RecursionError) as error:
        raise HubError("SERVICE_RECORD_INVALID", "运行记录无法读取或格式无效。") from error


def _write(path: Path, record: dict) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            delete=False,
            dir=path.parent,
            prefix=f".{path.name}.",
        ) as stream:
            temporary = Path(stream.name)
            json.dump(record, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        deadline = time.monotonic() + 1
        while True:
            try:
                os.replace(temporary, path)
                return
            except PermissionError as error:
                if error.winerror not in (5, 32) or time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _verify(record: dict, project: Path) -> _Identity | None:
    pid = record.get("pid")
    nonce = record.get("nonce")
    if (
        record.get("project_root") != str(project)
        or record.get("code_root") != str(_CODE_ROOT)
        or type(pid) is not int
        or not 0 < pid < 2**32
        or type(record.get("start_time")) is not int
        or not isinstance(nonce, str)
        or _NONCE.fullmatch(nonce) is None
    ):
        raise HubError("SERVICE_IDENTITY_MISMATCH", "运行记录缺少当前项目的完整身份。")
    live = _identity(pid)
    if live is not None:
        if any(record.get(key) != getattr(live, key) for key in _IDENTITY_KEYS):
            raise HubError("SERVICE_IDENTITY_MISMATCH", "运行记录与当前 PID 的实际身份不符。")
        _verify_entry(live, project)
    return live


def _remove_stale(files: _Locations, record: dict) -> None:
    try:
        with files.lock():
            if _read(files.record) == record:
                files.record.unlink(missing_ok=True)
    except HubError as error:
        if error.code != "SERVICE_ALREADY_RUNNING":
            raise


def _timeout(seconds: float) -> None:
    try:
        valid = type(seconds) in (int, float) and seconds > 0 and math.isfinite(seconds)
    except OverflowError:
        valid = False
    if not valid:
        raise HubError("SERVICE_CONFIGURATION_ERROR", "等待时间必须为有限正数。")


def _status(record: dict | None = None) -> dict:
    if record is None:
        return {"running": False, "ready": False}
    return {
        "running": True,
        "ready": record.get("ready") is True,
        "pid": record["pid"],
        "port": record.get("port"),
    }


def service_status(settings: AppConfig) -> dict:
    """查询项目受管服务的状态，校验记录对应的实际进程。"""
    _windows_only()
    settings, files = _location(settings)
    record = _read(files.record)
    return _status(record if record is not None and _verify(record, settings.project_root) else None)


def _stop_record(settings: AppConfig, files: _Locations, record: dict, timeout: float) -> dict:
    identity = _verify(record, settings.project_root)
    if identity is None:
        _remove_stale(files, record)
        return _status()
    _write(files.stop, {key: record[key] for key in _NOTICE_KEYS})
    deadline = time.monotonic() + timeout
    while True:
        current = _identity(identity.pid)
        if current is None or current.start_time != identity.start_time:
            _remove_stale(files, record)
            return _status()
        if time.monotonic() >= deadline:
            raise HubError("SERVICE_STOP_TIMEOUT", "优雅停止尚未完成，请检查项目日志。")
        time.sleep(0.05)


def stop_service(settings: AppConfig, *, timeout: float = 30) -> dict:
    """仅向身份吻合的项目运行代发布停止通知，并等待该进程退出。"""
    _windows_only()
    _timeout(timeout)
    settings, files = _location(settings)
    record = _read(files.record)
    return _status() if record is None else _stop_record(settings, files, record, timeout)


def _spawn(settings: AppConfig, files: _Locations, python: str, nonce: str, timeout: float):
    files.logs.mkdir(parents=True, exist_ok=True)
    cache = settings.storage.cache_dir / "process-temp"
    reject_links(cache)
    cache.mkdir(parents=True, exist_ok=True)
    environment = {
        **os.environ,
        _NONCE_ENV: nonce,
        "PYTHONPATH": str(_CODE_ROOT),
        "TEMP": str(cache),
        "TMP": str(cache),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    probe = (
        "import sys; assert (3, 14, 7) <= sys.version_info[:3] < (3, 15, 0); "
        "import fastapi, pydantic, rfc8785, starlette, uvicorn"
    )
    process_options = {"cwd": _CODE_ROOT, "env": environment, "creationflags": subprocess.CREATE_NO_WINDOW}
    try:
        checked = subprocess.run(
            [python, "-B", "-c", probe], capture_output=True, check=False, timeout=timeout, **process_options
        )
        if checked.returncode:
            raise HubError("SERVICE_DEPENDENCY_ERROR", "解释器版本或主服务依赖检查失败。")
        command = [
            python,
            "-X",
            "utf8",
            "-B",
            str(_ENTRY),
            "--managed",
            "--project-root",
            str(settings.project_root),
            "--host",
            settings.http.bind_host,
            "--port",
            str(settings.http.port),
        ]
        with files.log_path("stdout").open("ab") as output, files.log_path("stderr").open("ab") as errors:
            return subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=errors,
                close_fds=True,
                **process_options,
            )
    except (OSError, subprocess.SubprocessError) as error:
        raise HubError("SERVICE_START_FAILED", "无法启动指定解释器或服务。") from error


def start_service(settings: AppConfig, *, python: str = sys.executable, timeout: float = 30) -> dict:
    """隐藏启动项目服务，确认本次启动标识及监听就绪后返回。"""
    _windows_only()
    _timeout(timeout)
    settings, files = _location(settings)
    supported = resolve_settings(
        AppConfig(
            project_root=settings.project_root,
            http=HttpConfig(bind_host=settings.http.bind_host, port=settings.http.port),
        )
    )
    if settings != supported or not settings.http.port:
        raise HubError("SERVICE_CONFIGURATION_ERROR", "后台启动仅支持项目根目录、监听地址及非零端口。")
    if service_status(settings)["running"]:
        raise HubError("SERVICE_ALREADY_RUNNING", "当前项目服务已在运行。")
    nonce = secrets.token_hex(16)
    process = _spawn(settings, files, python, nonce, timeout)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise HubError("SERVICE_START_FAILED", "服务启动失败，请检查项目日志。")
        record = _read(files.record)
        if record is not None and record.get("nonce") == nonce and _verify(record, settings.project_root):
            if record.get("ready") is True:
                return _status(record)
        time.sleep(0.05)
    record = _read(files.record)
    if record is not None and record.get("nonce") == nonce:
        _stop_record(settings, files, record, timeout)
    raise HubError("SERVICE_START_TIMEOUT", "服务未在等待时间内就绪。")


def _read_optional_notice(path: Path) -> dict | None:
    try:
        return _read(path)
    except HubError:
        return None


def _matches_notice(current: dict | None, expected: dict) -> bool:
    return current is not None and all(current.get(key) == expected[key] for key in _NOTICE_KEYS)


def run_managed(settings: AppConfig) -> None:
    """供本工程的绝对 main.py 路径和 --managed --project-root 入口调用。"""
    from projecthub.application import run

    _windows_only()
    settings, files = _location(settings)
    identity = _identity(os.getpid())
    if identity is None:
        raise HubError("SERVICE_IDENTITY_UNAVAILABLE", "无法验证当前受管进程。")
    _verify_entry(identity, settings.project_root)
    nonce = os.environ.pop(_NONCE_ENV, None) or secrets.token_hex(16)
    if _NONCE.fullmatch(nonce) is None:
        raise HubError("SERVICE_IDENTITY_MISMATCH", "受管启动标识无效。")
    record = {
        **asdict(identity),
        "code_root": str(_CODE_ROOT),
        "project_root": str(settings.project_root),
        "nonce": nonce,
        "port": settings.http.port,
        "ready": False,
    }
    with files.lock():
        previous = _read(files.record)
        if previous is not None and _verify(previous, settings.project_root):
            raise HubError("SERVICE_ALREADY_RUNNING", "当前项目已有受管运行记录。")
        _write(files.record, record)
        finished, stop_requested = Event(), Event()

        def monitor() -> None:
            while not finished.wait(0.1):
                if _matches_notice(_read_optional_notice(files.stop), record):
                    stop_requested.set()
                    return

        def ready() -> None:
            _write(files.record, {**record, "ready": True})

        thread = Thread(target=monitor, name="ProjectHub stop monitor")
        try:
            thread.start()
            run(settings, on_started=ready, stop_event=stop_requested)
        finally:
            finished.set()
            if thread.ident is not None:
                thread.join()
            for path in (files.record, files.stop):
                if _matches_notice(_read_optional_notice(path), record):
                    path.unlink(missing_ok=True)
