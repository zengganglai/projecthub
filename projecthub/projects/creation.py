"""创建型号及版本，并补偿未提交操作创建的空目录。"""

import logging
import sqlite3

from projecthub.contracts import HubError
from projecthub.files import FILE_TABLES
from projecthub.storage import file_identity, project_name

from .paths import directory, project_path

_LOGGER = logging.getLogger(__name__)


def _remove_created(root, directories):
    for relative, identity in reversed(directories):
        try:
            path = project_path(root, relative)
            if path.is_dir() and file_identity(path) == identity:
                path.rmdir()
        except OSError, HubError:
            _LOGGER.warning("项目创建失败，保留未能清理的目录 %s", relative)


def create_project(settings, db, name, version_or_none, description):
    """创建独立型号或其版本，失败仅移除本次创建且仍为空的目录。"""
    name = project_name(name)
    version = None if version_or_none is None else project_name(version_or_none)
    if not isinstance(description, str):
        raise HubError("INVALID_PROJECT_DESCRIPTION", "项目描述必须是字符串，可以为空。")
    root = settings.storage.object_dir
    table = "project_models" if version is None else "projects"
    created = []
    identifier = None
    try:
        with db.transaction() as connection:
            if version is None:
                query = (
                    "INSERT INTO project_models(name,description) VALUES(?,?) ON CONFLICT(name) DO NOTHING"
                )
                result = connection.execute(query, (name, description))
                paths = [name]
            else:
                if db.one("SELECT id FROM project_models WHERE name=?", (name,)) is None:
                    raise HubError("PROJECT_NOT_FOUND", "型号不存在。")
                query = (
                    "INSERT INTO projects(name,version,description) VALUES(?,?,?)"
                    " ON CONFLICT(name,version) DO NOTHING"
                )
                result = connection.execute(query, (name, version, description))
                base = f"{name}/{version}"
                paths = [name, base, *(f"{base}/{category}" for category in FILE_TABLES)]
            if not result.rowcount:
                raise HubError("PROJECT_EXISTS", "该型号或版本已存在。")
            identifier = result.lastrowid
            for relative in paths:
                path = project_path(root, relative, code="PROJECT_PATH_EXISTS")
                try:
                    path.mkdir()
                except FileExistsError:
                    if not directory(path):
                        raise HubError(
                            "PROJECT_PATH_EXISTS", "项目或分类路径已被占用，或名称大小写不一致。"
                        ) from None
                else:
                    created.append((relative, file_identity(path)))
    except BaseException as error:
        try:
            # 提交返回异常时，重新读取已结束事务的 SQLite 状态再决定补偿。
            saved = db.one(f"SELECT * FROM {table} WHERE id=?", (identifier,)) if identifier else None
        except Exception as lookup_error:
            error.add_note("数据库提交结果无法确认，已保留本次创建的目录。")
            raise error from lookup_error
        if saved is None:
            _remove_created(root, created)
        elif saved["name"] != name or (version is not None and saved["version"] != version):
            raise HubError("PROJECT_RESTORE_FAILED", "数据库项目状态无法确认，已保留新目录。") from error
        if isinstance(error, sqlite3.IntegrityError):
            raise HubError("PROJECT_CONFLICT", "项目记录与已有数据冲突。") from error
        if isinstance(error, OSError):
            raise HubError("PROJECT_DIRECTORY_ERROR", "项目或分类目录创建失败。") from error
        raise
    response = {"id": identifier, "name": name, "description": description}
    if version is not None:
        response["version"] = version
    return response
