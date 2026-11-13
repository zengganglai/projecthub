"""账户资料、认证和密码；通过显式权限服务依赖核对操作权限。"""

from __future__ import annotations

import hmac
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

from projecthub.contracts import HubError

if TYPE_CHECKING:
    from projecthub.database import Database
    from projecthub.permissions import Permissions


_USER_FIELDS = {"name", "employee_number", "phone", "password"}


def _valid_id(value) -> bool:
    return type(value) is int and 1 <= value <= 2**63 - 1


def _nonblank(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _authentication_failed() -> HubError:
    return HubError("AUTHENTICATION_FAILED", "身份或凭据无效。")


def _password_matches(row: dict | None, password: str) -> bool:
    if row is None or not _nonblank(password) or not _nonblank(row["password"]):
        return False
    try:
        return hmac.compare_digest(password.encode("utf-8"), row["password"].encode("utf-8"))
    except UnicodeError:
        return False


def _user_fields(data: dict, *, creating: bool) -> dict:
    if not isinstance(data, dict) or not data or not data.keys() <= _USER_FIELDS:
        raise HubError("INVALID_DATA", "用户字段无效。")
    if (creating or "name" in data) and not _nonblank(data.get("name")):
        raise HubError("INVALID_DATA", "姓名必须为非空字符串。")
    for name, value in data.items():
        if name == "employee_number" and value is None:
            continue
        if not isinstance(value, str):
            raise HubError("INVALID_DATA", "用户资料字段须为字符串。")
        try:
            value.encode("utf-8")
        except UnicodeError as error:
            raise HubError("INVALID_DATA", "用户资料包含无效字符。") from error
    return dict(data)


class Users:
    """维护账号生命周期，身份展示和权限检查由 Permissions 提供。"""

    def __init__(self, db: Database, permissions: Permissions, max_users: int = 100) -> None:
        if type(max_users) is not int or not 1 <= max_users <= 100:
            raise HubError("USERS_CONFIGURATION_ERROR", "用户上限须为 1～100。")
        self.db = db
        self.permissions = permissions
        self.max_users = max_users

    def _validate_count(self, count: int) -> None:
        if count > self.max_users:
            raise HubError("USER_LIMIT_REACHED", f"用户总数不能超过 {self.max_users}。")

    def validate(self) -> None:
        """启动时核对账号人数上限。"""
        with self.db.lock:
            self._validate_count(self.db.one("SELECT count(*) AS total FROM users")["total"])

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        try:
            with self.db.transaction() as connection:
                yield connection
        except sqlite3.IntegrityError as error:
            raise HubError("USER_CONFLICT", "用户或项目配置与已有记录冲突。") from error
        except sqlite3.Error as error:
            raise HubError("USER_STORE_ERROR", "配置操作失败，请重新查询当前配置。") from error

    def authenticate(self, name: str, password: str) -> dict:
        if not _nonblank(name) or not _nonblank(password):
            raise _authentication_failed()
        try:
            name.encode("utf-8")
        except UnicodeError:
            raise _authentication_failed() from None
        with self.db.lock:
            row = self.db.one("SELECT * FROM users WHERE name=?", (name,))
            if not _password_matches(row, password):
                raise _authentication_failed()
            return self.permissions.identity(row)

    def list_users(self, actor: dict) -> list[dict]:
        with self.db.lock:
            self.permissions.require_admin(actor)
            models = self.permissions.model_names()
            return [
                self.permissions.identity(row, models)
                for row in self.db.query("SELECT * FROM users ORDER BY id")
            ]

    def create_user(self, actor: dict, data: dict) -> dict:
        with self._write() as connection:
            self.permissions.require_admin(actor)
            values = _user_fields(data, creating=True)
            columns = ", ".join(values)
            placeholders = ", ".join("?" for _ in values)
            user_id = connection.execute(
                f"INSERT INTO users ({columns}) VALUES ({placeholders})", tuple(values.values())
            ).lastrowid
            self._validate_count(self.db.one("SELECT count(*) AS total FROM users")["total"])
            return self.permissions.identity(self.permissions.user(user_id))

    def update_user(self, actor: dict, user_id: int, data: dict) -> dict:
        with self._write() as connection:
            self.permissions.require_admin(actor)
            if not _valid_id(user_id):
                raise HubError("INVALID_USER", "用户 ID 无效。")
            values = _user_fields(data, creating=False)
            assignments = ", ".join(f"{column}=?" for column in values)
            updated = connection.execute(
                f"UPDATE users SET {assignments} WHERE id=?", (*values.values(), user_id)
            )
            if not updated.rowcount:
                raise HubError("USER_NOT_FOUND", "用户不存在。")
            return self.permissions.identity(self.permissions.user(user_id))

    def delete_user(self, actor: dict, user_id: int) -> None:
        with self._write() as connection:
            self.permissions.require_admin(actor)
            row = self.permissions.user(user_id)
            if self.permissions.is_admin(row):
                raise HubError("USER_REFERENCED", "不能删除管理员。")
            grants = self.permissions.assigned_permissions(row)
            if (
                grants
                or self.db.one(
                    "SELECT 1 FROM pending_reviews WHERE json_extract(field_info, '$.uploaded_by')=? LIMIT 1",
                    (user_id,),
                )
                is not None
            ):
                raise HubError("USER_REFERENCED", "请先清除该用户的项目授权并处理其提交的待审任务。")
            connection.execute(
                "UPDATE pending_reviews SET reviewer_user_id=NULL WHERE reviewer_user_id=?", (user_id,)
            )
            connection.execute("DELETE FROM users WHERE id=?", (user_id,))

    def change_password(self, actor: dict, current_password: str, new_password: str) -> None:
        with self._write() as connection:
            row = self.permissions.current(actor)
            if not _password_matches(row, current_password):
                raise _authentication_failed()
            if not _nonblank(new_password):
                raise HubError("INVALID_DATA", "新密码必须为非空且非全空白字符串。")
            try:
                new_password.encode("utf-8")
            except UnicodeError:
                raise HubError("INVALID_DATA", "新密码包含无效字符。") from None
            connection.execute("UPDATE users SET password=? WHERE id=?", (new_password, row["id"]))
