"""SQLite 连接、数据库维护锁和事务生命周期。"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING

from projecthub.contracts import HubError
from projecthub.storage import FileLock

from .integrity import _matches_schema, _tables, connection_integrity
from .schema import SCHEMA_REVISION, MigrationReport, initialize_schema

if TYPE_CHECKING:
    from projecthub.application import AppConfig


def _migration_error(message: str) -> HubError:
    return HubError("DATABASE_MIGRATION_ERROR", message)


def _check_path(path: Path) -> None:
    if path != path.resolve():
        raise _migration_error("数据库资源路径已被链接替换。")


class Database:
    """拥有一个 SQLite 连接，使用同一把可重入锁保护查询和业务事务。"""

    def __init__(self, settings: AppConfig) -> None:
        self._settings = settings
        self._connection: sqlite3.Connection | None = None
        self.lock = RLock()
        self.migration_report: MigrationReport | None = None

    @property
    def connection(self) -> sqlite3.Connection:
        """只在调用方持有 lock 时借用连接，事务由 transaction() 管理。"""
        if self._connection is None:
            raise HubError("DATABASE_NOT_READY", "数据库未打开。")
        return self._connection

    def open(self, *, initialize: Callable[[], None] | None = None) -> None:
        """打开数据库，仅在空库创建事务中执行 initialize 回调。"""
        with self.lock:
            if self._connection is not None:
                return
            options = self._settings
            path = options.database.path
            try:
                for candidate in (path, options.migrations.lock_path):
                    _check_path(candidate)
                if path == options.migrations.lock_path:
                    raise _migration_error("数据库文件与迁移锁不能使用同一路径。")
                path.parent.mkdir(parents=True, exist_ok=True)
                with FileLock(
                    options.migrations.lock_path,
                    "DATABASE_MIGRATION_LOCKED",
                    "数据库迁移锁已被占用。",
                ):
                    self._connection = sqlite3.connect(
                        path,
                        timeout=options.database.busy_timeout_ms / 1000,
                        autocommit=True,
                        check_same_thread=False,
                    )
                    self._connection.row_factory = sqlite3.Row
                    self._connection.execute("PRAGMA foreign_keys=ON")
                    self._connection.execute("PRAGMA synchronous=FULL")
                    with self.transaction() as connection:
                        report = self._prepare(connection, initialize)
                    if self._connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
                        raise _migration_error("SQLite 无法启用 WAL 日志。")
                    self.migration_report = report
            except BaseException as error:
                if self._connection is not None:
                    self._connection.close()
                    self._connection = None
                if isinstance(error, (OSError, sqlite3.Error)) or (
                    isinstance(error, HubError) and error.code == "SERVICE_LOCK_ERROR"
                ):
                    raise _migration_error("数据库打开、初始化或校验失败。") from error
                raise

    def _prepare(
        self, connection: sqlite3.Connection, initialize: Callable[[], None] | None
    ) -> MigrationReport:
        names = _tables(connection)
        revision = None
        if "alembic_version" in names:
            revisions = list(connection.execute("SELECT version_num FROM alembic_version"))
            if len(revisions) == 1:
                revision = revisions[0][0]
        if not names:
            initialize_schema(connection)
            if initialize is not None:
                initialize()
        elif revision != SCHEMA_REVISION:
            raise _migration_error(f"数据库版本不受支持，当前要求 {SCHEMA_REVISION}。")
        if not _matches_schema(connection) or not connection_integrity(connection)["is_valid"]:
            raise _migration_error("数据库结构或完整性校验失败。")
        return MigrationReport(revision, SCHEMA_REVISION)

    def close(self) -> None:
        with self.lock:
            if self._connection is not None:
                if self._connection.in_transaction:
                    raise HubError("DATABASE_LIFECYCLE_ERROR", "数据库事务内不能关闭连接。")
                self._connection.close()
                self._connection = None
            self.migration_report = None

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """持锁执行写事务。嵌套调用通过保存点独立回滚。"""
        with self.lock:
            connection = self.connection
            nested = connection.in_transaction
            connection.execute("SAVEPOINT hub_nested" if nested else "BEGIN IMMEDIATE")
            try:
                yield connection
                connection.execute("RELEASE hub_nested" if nested else "COMMIT")
            except BaseException:
                if connection.in_transaction:
                    if nested:
                        connection.execute("ROLLBACK TO hub_nested")
                        connection.execute("RELEASE hub_nested")
                    else:
                        connection.execute("ROLLBACK")
                raise

    def query(self, sql: str, params=()) -> list[dict]:
        with self.lock, closing(self.connection.execute(sql, params)) as cursor:
            return [dict(row) for row in cursor]

    def one(self, sql: str, params=()) -> dict | None:
        with self.lock, closing(self.connection.execute(sql, params)) as cursor:
            row = cursor.fetchone()
            return dict(row) if row is not None else None
