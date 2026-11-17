"""有界用户 RAM 存储；显式加载，先修改 RAM，再同步提交 SQLite。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from typing import TYPE_CHECKING

from projecthub.contracts import HubError

if TYPE_CHECKING:
    from projecthub.database import Database

MAX_USERS = 200
_DEFAULTS = {
    "employee_number": "",
    "phone": "",
    "password": "",
    "permissions": "[]",
    "project_permissions": "{}",
    "is_admin": 0,
}
_FIELDS = {"name", *_DEFAULTS}


class UserStore:
    """一个应用生命周期拥有一个实例，所有用户写入必须通过本类。

    构造不执行 I/O。initialize 显式读取完整用户集；普通读取仅访问 RAM 并
    返回独立字典。transaction 独占数据库锁，拒绝嵌套或调用方已有事务。
    create/update/delete 仅在此事务中可用，其返回值为含密码的内部原始行。
    ready 不阻塞探针；读取和写入会在锁内重新核对连接及生命周期。
    """

    def __init__(self, db: Database, max_users: int = MAX_USERS) -> None:
        if type(max_users) is not int or not 1 <= max_users <= MAX_USERS:
            raise HubError("USERS_CONFIGURATION_ERROR", f"用户上限须为 1～{MAX_USERS}。")
        self.db = db
        self.max_users = max_users
        self._rows: dict[int, dict] = {}
        self._names: dict[str, int] = {}
        self._next_id = 1
        self._connection: sqlite3.Connection | None = None
        self._transaction_connection: sqlite3.Connection | None = None
        self._transaction_error: BaseException | None = None

    @property
    def ready(self) -> bool:
        """非阻塞地检查加载状态；不能代替持锁的业务读取。"""
        connection = self._connection
        if connection is None or self._transaction_error is not None:
            return False
        try:
            # in_transaction 的属性读取无 SQL，并会拒绝已直接关闭的连接。
            connection.in_transaction
            return self.db.connection is connection
        except HubError, sqlite3.Error:
            return False

    def initialize(self) -> None:
        """每个连接只加载一次；失败时不保留部分缓存，重新打开需显式调用。"""
        with self.db.lock:
            if self._transaction_connection is not None:
                raise HubError("USER_STORE_ERROR", "用户事务内不能重新初始化。")
            if self.ready:
                return
            self._clear()
            connection = self.db.connection
            if connection.in_transaction:
                raise HubError("USER_STORE_ERROR", "用户缓存必须在数据库事务外初始化。")
            try:
                self._load(connection)
            except BaseException:
                self._clear()
                raise

    def close(self) -> None:
        """清除持有的用户资料和凭据；不关闭调用方拥有的数据库。"""
        with self.db.lock:
            if self._transaction_connection is not None:
                raise HubError("USER_STORE_ERROR", "用户事务内不能关闭缓存。")
            self._clear()

    def _clear(self) -> None:
        self._connection = None
        self._rows.clear()
        self._names.clear()
        self._next_id = 1

    def _load(self, connection: sqlite3.Connection) -> None:
        # 先完整读取和校验，再发布状态；仅初始化及异常恢复调用此方法。
        with closing(
            connection.execute("SELECT * FROM users ORDER BY id LIMIT ?", (self.max_users + 1,))
        ) as cursor:
            rows = {row["id"]: dict(row) for row in cursor}
        if len(rows) > self.max_users:
            raise HubError("USER_LIMIT_REACHED", f"用户总数不能超过 {self.max_users}。")
        with closing(connection.execute("SELECT seq FROM sqlite_sequence WHERE name='users'")) as cursor:
            sequence = cursor.fetchone()
        next_id = max(max(rows, default=0), sequence["seq"] if sequence else 0) + 1
        names = {row["name"]: user_id for user_id, row in rows.items()}
        self._rows, self._names, self._next_id = rows, names, next_id
        self._connection = connection

    def _require_ready(self) -> None:
        if not self.ready:
            raise HubError("USERS_NOT_READY", "用户缓存尚未初始化或需重新加载。")

    @staticmethod
    def _validate_id(user_id: int) -> None:
        if type(user_id) is not int or not 1 <= user_id <= 2**63 - 1:
            raise HubError("INVALID_USER", "用户 ID 无效。")

    def get(self, user_id: int) -> dict | None:
        with self.db.lock:
            self._require_ready()
            self._validate_id(user_id)
            row = self._rows.get(user_id)
            return dict(row) if row is not None else None

    def find(self, name: str) -> dict | None:
        with self.db.lock:
            self._require_ready()
            user_id = self._names.get(name)
            return dict(self._rows[user_id]) if user_id is not None else None

    def all(self) -> list[dict]:
        with self.db.lock:
            self._require_ready()
            return [dict(self._rows[user_id]) for user_id in sorted(self._rows)]

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """锁内持有完整快照，提交或恢复结束后才允许其他线程观察用户状态。

        成功执行 ROLLBACK 时恢复快照。事务已结束而提交结果不明（例如
        COMMIT 生效后抛错）时重读实际数据；回滚失败且仍处于事务内，或重读
        失败时清空缓存，调用方必须修复数据库并显式 initialize 后才能继续。
        """
        with self.db.lock:
            self._require_ready()
            connection = self.db.connection
            if self._transaction_connection is not None or connection.in_transaction:
                raise HubError("USER_STORE_ERROR", "用户写入必须独占一个外层事务。")
            snapshot = ({key: dict(row) for key, row in self._rows.items()}, dict(self._names), self._next_id)
            self._transaction_connection = connection
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                if self._transaction_error is not None:
                    raise HubError(
                        "USER_STORE_ERROR", "用户写入失败，事务必须回滚。"
                    ) from self._transaction_error
                connection.execute("COMMIT")
                if connection.in_transaction or self.db.connection is not connection:
                    raise HubError("USER_STORE_ERROR", "无法确认用户事务提交结果。")
            except BaseException:
                self._recover(connection, snapshot)
                raise
            finally:
                self._transaction_connection = None
                self._transaction_error = None

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """账号与授权写入统一转换持久化错误；跨模块事务使用 transaction。"""
        try:
            with self.transaction() as connection:
                yield connection
        except sqlite3.IntegrityError as error:
            raise HubError("USER_CONFLICT", "用户或项目配置与已有记录冲突。") from error
        except sqlite3.Error as error:
            raise HubError("USER_STORE_ERROR", "配置操作失败，请重新查询当前配置。") from error

    def _recover(self, connection: sqlite3.Connection, snapshot: tuple) -> None:
        try:
            if self.db.connection is not connection:
                raise HubError("USER_STORE_ERROR", "用户事务期间数据库连接已更换。")
            if connection.in_transaction:
                try:
                    connection.execute("ROLLBACK")
                except BaseException:
                    # 回滚也可能已经生效后抛错；只有确认事务结束才允许重读。
                    if connection.in_transaction:
                        raise
                else:
                    if connection.in_transaction:
                        raise HubError("USER_STORE_ERROR", "用户事务回滚未完成。")
                    self._rows, self._names, self._next_id = snapshot
                    return
            self._load(connection)
        except BaseException as error:
            self._clear()
            raise HubError("USER_STORE_ERROR", "用户事务结果无法确认，缓存已停用。") from error

    def _writable_connection(self) -> sqlite3.Connection:
        self._require_ready()
        connection = self._transaction_connection
        if connection is None or not connection.in_transaction:
            raise HubError("USER_STORE_ERROR", "用户修改必须在用户存储事务内执行。")
        return connection

    @staticmethod
    def _values(values: dict, *, creating: bool) -> dict:
        if not isinstance(values, dict) or not values.keys() <= _FIELDS:
            raise HubError("INVALID_DATA", "用户字段无效。")
        if creating and "name" not in values:
            raise HubError("INVALID_DATA", "姓名不能为空。")
        result = dict(values)
        for column, value in result.items():
            if column == "is_admin":
                if type(value) not in (bool, int) or value not in (0, 1):
                    raise HubError("INVALID_DATA", "管理员标志须为 0 或 1。")
                result[column] = int(value)
            elif column == "employee_number" and value is None:
                continue
            elif not isinstance(value, str) or (column == "name" and not value.strip()):
                raise HubError("INVALID_DATA", "用户资料字段无效。")
        return result

    def _unique_name(self, name: str, user_id: int | None = None) -> None:
        existing = self._names.get(name)
        if existing is not None and existing != user_id:
            raise HubError("USER_CONFLICT", "用户名已存在。")

    def _execute(self, connection: sqlite3.Connection, sql: str, values: tuple) -> None:
        try:
            with closing(connection.execute(sql, values)) as cursor:
                if cursor.rowcount != 1:
                    raise HubError("USER_STORE_ERROR", "用户缓存与持久记录不一致。")
        except BaseException as error:
            # 即使调用方捕获语句异常，事务也不能提交提前修改的缓存。
            self._transaction_error = error
            raise

    def create(self, values: dict) -> dict:
        with self.db.lock:
            connection = self._writable_connection()
            values = self._values(values, creating=True)
            if len(self._rows) >= self.max_users:
                raise HubError("USER_LIMIT_REACHED", f"用户总数不能超过 {self.max_users}。")
            self._unique_name(values["name"])
            if self._next_id > 2**63 - 1:
                raise HubError("USER_STORE_ERROR", "用户 ID 已耗尽。")
            row = {"id": self._next_id, **_DEFAULTS, **values}
            self._rows[row["id"]] = row
            self._names[row["name"]] = row["id"]
            self._next_id += 1
            columns = ", ".join(row)
            placeholders = ", ".join("?" for _ in row)
            self._execute(
                connection, f"INSERT INTO users ({columns}) VALUES ({placeholders})", tuple(row.values())
            )
            return dict(row)

    def update(self, user_id: int, changes: dict) -> dict:
        with self.db.lock:
            connection = self._writable_connection()
            self._validate_id(user_id)
            values = self._values(changes, creating=False)
            current = self._rows.get(user_id)
            if current is None:
                raise HubError("USER_NOT_FOUND", "用户不存在。")
            row = {**current, **values}
            self._unique_name(row["name"], user_id)
            del self._names[current["name"]]
            self._rows[user_id] = row
            self._names[row["name"]] = user_id
            if values:
                assignments = ", ".join(f"{column}=?" for column in values)
                self._execute(
                    connection, f"UPDATE users SET {assignments} WHERE id=?", (*values.values(), user_id)
                )
            return dict(row)

    def delete(self, user_id: int) -> None:
        with self.db.lock:
            connection = self._writable_connection()
            self._validate_id(user_id)
            row = self._rows.get(user_id)
            if row is None:
                raise HubError("USER_NOT_FOUND", "用户不存在。")
            del self._rows[user_id]
            del self._names[row["name"]]
            self._execute(connection, "DELETE FROM users WHERE id=?", (user_id,))
