"""操作系统持有的项目独占文件锁。"""

from __future__ import annotations

import errno
import os
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, BinaryIO

from projecthub.contracts import HubError

from .paths import reject_links

if TYPE_CHECKING:
    from projecthub.application import AppConfig


def _lock_handle(handle: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)


class FileLock:
    """持有独占字节区间直到文件句柄关闭。锁文件在释放后保留。"""

    def __init__(self, path: Path, busy_code: str, busy_message: str):
        self._path = path
        self._busy_code = busy_code
        self._busy_message = busy_message
        self._handle: BinaryIO | None = None
        self._gate = Lock()

    def acquire(self) -> None:
        with self._gate:
            if self._handle is not None:
                return
            try:
                reject_links(self._path)
                self._path.parent.mkdir(parents=True, exist_ok=True)
                reject_links(self._path)
                handle = self._path.open("a+b")
            except (OSError, ValueError) as error:
                raise HubError("SERVICE_LOCK_ERROR", "项目锁文件无法安全打开。") from error
            try:
                handle.seek(0)
                _lock_handle(handle)
            except OSError as error:
                handle.close()
                if error.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise HubError(self._busy_code, self._busy_message) from error
                raise HubError("SERVICE_LOCK_ERROR", "无法获取项目文件锁。") from error
            self._handle = handle

    def release(self) -> None:
        with self._gate:
            if self._handle is not None:
                self._handle.close()
                self._handle = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_exc) -> None:
        self.release()


class RuntimeLock(FileLock):
    """应用和离线维护共用的项目互斥，要求使用已归一化的配置。"""

    def __init__(self, settings: AppConfig):
        super().__init__(
            settings.database.path.parent / "service-owner.lock",
            "SERVICE_RUNNING",
            "此项目正在运行服务或维护操作，请先停止。",
        )
