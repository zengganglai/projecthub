"""从用户RAM当前状态检查身份与权限，并维护账号授权配置。"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from projecthub.contracts import HubError

from .catalog import (
    PROJECT_PERMISSIONS,
    READ_PERMISSIONS,
    normalize_project_permissions,
    parse_project_permissions,
)

if TYPE_CHECKING:
    from projecthub.database import Database
    from projecthub.users import UserStore


def _valid_id(value) -> bool:
    return type(value) is int and 1 <= value <= 2**63 - 1


class Permissions:
    """展示身份不作为授权依据；授权检查均在共享锁内读取当前用户RAM，不信任调用方权限快照。"""

    def __init__(self, db: Database, user_store: UserStore) -> None:
        self.db = db
        self.user_store = user_store

    def validate(self) -> None:
        """启动时核对管理员标记与全部存储授权。"""
        with self.db.lock:
            models = self.model_names()
            for row in self.user_store.all():
                self.is_admin(row)
                self.assigned_permissions(row, models)

    def model_names(self) -> set[str]:
        return {row["name"] for row in self.db.query("SELECT name FROM project_models")}

    @staticmethod
    def is_admin(row: dict) -> bool:
        if row["is_admin"] not in (0, 1):
            raise HubError("USER_STORE_ERROR", "管理员标识损坏。")
        return bool(row["is_admin"])

    def assigned_permissions(self, row: dict, models: set[str] | None = None) -> dict:
        if models is None:
            models = self.model_names()
        try:
            # permissions 列不再参与授权；保持当前数据库结构，不自动迁移运行数据。
            return parse_project_permissions(row["project_permissions"], models)
        except (ValueError, TypeError) as error:
            raise HubError("USER_STORE_ERROR", "用户型号授权记录损坏，无法读取。") from error

    def visible_models(self, actor: dict) -> frozenset[str]:
        """任意一项型号权限即可发现该型号；具体资源操作仍逐项鉴权。"""
        with self.db.lock:
            row = self.current(actor)
            models = self.model_names()
            if self.is_admin(row):
                return frozenset(models)
            grants = self.assigned_permissions(row, models)
            return frozenset(name for names in grants.values() for name in names)

    def identity(self, row: dict, models: set[str] | None = None) -> dict:
        if models is None:
            models = self.model_names()
        is_admin = self.is_admin(row)
        grants = self.assigned_permissions(row, models)
        by_project = {}
        if is_admin:
            by_project = {name: PROJECT_PERMISSIONS for name in models}
        else:
            for permission, names in grants.items():
                for name in names:
                    by_project.setdefault(name, set()).add(permission)
        return {
            "user_id": row["id"],
            "name": row["name"],
            "is_admin": is_admin,
            "global_permissions": [],
            "projects": [
                {"project_name": name, "permissions": sorted(permissions)}
                for name, permissions in sorted(by_project.items())
            ],
        }

    def user(self, user_id: int) -> dict:
        if not _valid_id(user_id):
            raise HubError("INVALID_USER", "用户 ID 无效。")
        row = self.user_store.get(user_id)
        if row is None:
            raise HubError("USER_NOT_FOUND", "用户不存在。")
        return row

    def current(self, actor: dict) -> dict:
        user_id = actor.get("user_id") if isinstance(actor, dict) else None
        if _valid_id(user_id):
            row = self.user_store.get(user_id)
            if row is not None:
                return row
        raise HubError("PERMISSION_DENIED", "当前身份无权执行此操作。")

    def require_admin(self, actor: dict) -> None:
        with self.db.lock:
            if not self.is_admin(self.current(actor)):
                raise HubError("PERMISSION_DENIED", "此操作只允许管理员执行。")

    def _project(self, project_id: int) -> dict:
        project = (
            self.db.one("SELECT * FROM projects WHERE id=?", (project_id,)) if _valid_id(project_id) else None
        )
        if project is None:
            raise HubError("PROJECT_NOT_FOUND", "项目版本不存在。")
        return project

    def _effective_permissions(self, user: dict, project: dict | None) -> frozenset[str]:
        if self.is_admin(user):
            return PROJECT_PERMISSIONS
        grants = self.assigned_permissions(user)
        if project is None:
            return frozenset()
        return frozenset(permission for permission, names in grants.items() if project["name"] in names)

    def permissions_for(self, actor: dict, project_id: int) -> frozenset[str]:
        with self.db.lock:
            return self._effective_permissions(self.current(actor), self._project(project_id))

    def require(self, actor: dict, permission: str, project_id: int) -> dict:
        with self.db.lock:
            project = self._project(project_id)
            if permission not in self._effective_permissions(self.current(actor), project):
                raise HubError("PERMISSION_DENIED", "当前身份没有此项目的操作权限。")
            return project

    def require_file_download(self, actor: dict, category: str, project_id: int | None = None) -> None:
        with self.db.lock:
            row = self.current(actor)
            permission = READ_PERMISSIONS.get(category) if isinstance(category, str) else None
            if permission is None:
                raise HubError("INVALID_DATA", "文件分类无效。")
            project = self._project(project_id) if project_id is not None else None
            if permission not in self._effective_permissions(row, project):
                raise HubError("PERMISSION_DENIED", "当前身份无权下载此类文件。")

    def get_user_permissions(self, actor: dict, user_id: int) -> dict:
        with self.db.lock:
            self.require_admin(actor)
            grants = self.assigned_permissions(self.user(user_id))
            return {
                "user_id": user_id,
                "global_permissions": [],
                "project_permissions": grants,
            }

    def set_user_permissions(
        self, actor: dict, user_id: int, global_permissions, project_permissions
    ) -> dict:
        with self.user_store.write():
            self.require_admin(actor)
            if not _valid_id(user_id):
                raise HubError("INVALID_USER", "用户 ID 无效。")
            try:
                if not isinstance(global_permissions, list) or global_permissions:
                    raise ValueError("权限只允许按型号授予，global_permissions 必须为空数组。")
                projects_to_write = normalize_project_permissions(project_permissions)
            except (ValueError, TypeError) as error:
                raise HubError("INVALID_DATA", str(error)) from error
            self.user(user_id)
            models = self.model_names()
            if any(name not in models for names in projects_to_write.values() for name in names):
                raise HubError("PROJECT_NOT_FOUND", "授权包含不存在的项目。")
            self.user_store.update(
                user_id, {"permissions": "[]", "project_permissions": json.dumps(projects_to_write)}
            )
            return {
                "user_id": user_id,
                "global_permissions": [],
                "project_permissions": projects_to_write,
            }
