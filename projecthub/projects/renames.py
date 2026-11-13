"""项目改名、关联记录更新与持久清单恢复。"""

import json
import logging
import re
import shutil
import sqlite3
from time import monotonic, sleep, time_ns
from uuid import uuid4

from projecthub.contracts import HubError, decode_json
from projecthub.files import FILE_TABLES
from projecthub.permissions import parse_project_permissions
from projecthub.storage import (
    copy_tree,
    durable_copy,
    file_identity,
    project_name,
    reject_links,
    safe_path,
    write_json,
)

from .paths import directory, project_path

_LOGGER = logging.getLogger(__name__)
_JOURNALS = ".project-renames"
_STORAGE_KEYS = ("object_dir", "audit_dir")
_PATH_TABLES = (*FILE_TABLES.values(), "download_authorizations", "pending_reviews")


def _move_directory(source, target):
    """Windows 共享占用短暂出现时重试，始终检查目标未被占用。"""
    deadline = monotonic() + 1
    while True:
        reject_links(source)
        reject_links(target)
        if target.exists():
            raise FileExistsError(target)
        try:
            source.rename(target)
            return
        except OSError as error:
            remaining = deadline - monotonic()
            if getattr(error, "winerror", None) not in (5, 32) or remaining <= 0:
                raise
            sleep(min(0.02, remaining))


def _journal_file(settings, kind, identifier):
    return safe_path(settings.storage.backups_dir, f"{_JOURNALS}/{kind}-{identifier}.json")


def _remove_journal(path):
    safe_path(path.parent, path.name + ".tmp").unlink(missing_ok=True)
    safe_path(path.parent, path.name).unlink(missing_ok=True)


def _same_directory(path, identity):
    return path.is_dir() and file_identity(path) == identity


def _manifest_paths(settings, manifest, entry):
    """只允许本型号或版本的同级改名，且必须核对暂存和原件的目录身份。"""
    storage = entry["storage"]
    if storage not in _STORAGE_KEYS:
        raise ValueError("项目改名目录类型无效。")
    source_rel = "/".join(manifest["before"])
    target_rel = "/".join(manifest["after"])
    root = getattr(settings.storage, storage)
    source = project_path(root, source_rel)
    target = project_path(root, target_rel)
    archive_rel, stage_rel = entry["archive"], entry["stage"]
    archive = project_path(root, archive_rel)
    stage = project_path(root, stage_rel)
    if (
        source.parent != target.parent
        or source.parent != archive.parent
        or source.parent != stage.parent
        or re.fullmatch(re.escape(source.name) + r"-mod_[0-9]+", archive.name) is None
        or re.fullmatch(r"\.project-mod_[0-9]+-[0-9a-f]{32}", stage.name) is None
        or len({source, target, archive, stage}) != 4
    ):
        raise ValueError("项目改名恢复的目录层级或命名无效。")
    for name in ("source_identity", "stage_identity"):
        identity = entry[name]
        if (
            not isinstance(identity, list)
            or len(identity) != 2
            or any(type(value) is not int for value in identity)
        ):
            raise ValueError("项目改名恢复的目录身份无效。")
    return source, target, archive, stage


def repair_renamed_directory(paths, entry, *, committed):
    """恢复已验证路径的目录，依据 SQLite 状态和持久记录的身份。"""
    source, target, archive, stage = paths
    source_identity = entry["source_identity"]
    stage_identity = entry["stage_identity"]
    if committed:
        if not archive.exists() and _same_directory(source, source_identity):
            _move_directory(source, archive)
        if not _same_directory(archive, source_identity):
            raise OSError("已提交改名的归档目录缺失或身份不符。")
        if not target.exists() and _same_directory(stage, stage_identity):
            _move_directory(stage, target)
        if not _same_directory(target, stage_identity) or stage.exists():
            raise OSError("已提交改名的新目录缺失或身份不符。")
        return
    if _same_directory(target, stage_identity):
        _move_directory(target, stage)
    if not source.exists() and _same_directory(archive, source_identity):
        _move_directory(archive, source)
    if not _same_directory(source, source_identity) or archive.exists():
        raise OSError("无法恢复原项目目录，目录缺失或身份不符。")
    if stage.exists():
        if not _same_directory(stage, stage_identity):
            raise OSError("暂存目录身份不符，拒绝清理。")
        reject_links(stage)
        shutil.rmtree(stage)


def _recover_manifest(settings, db, path, manifest):
    kind, identifier = manifest["kind"], manifest["identifier"]
    if kind not in ("model", "version") or type(identifier) is not int or not 1 <= identifier < 2**63:
        raise ValueError("项目改名恢复标识无效。")
    if _journal_file(settings, kind, identifier) != path:
        raise ValueError("项目改名恢复文件与标识不匹配。")
    before, after = manifest["before"], manifest["after"]
    size = 1 if kind == "model" else 2
    if (
        not isinstance(before, list)
        or not isinstance(after, list)
        or len(before) != size
        or len(after) != size
        or any(project_name(name) != name for name in (*before, *after))
        or before[:-1] != after[:-1]
        or before[-1] == after[-1]
    ):
        raise ValueError("项目改名恢复身份无效。")
    entries = manifest["directories"]
    if (
        not isinstance(entries, list)
        or not entries
        or [e["storage"] for e in entries] not in (["object_dir"], ["object_dir", "audit_dir"])
    ):
        raise ValueError("项目改名恢复目录无效。")
    query = (
        "SELECT name FROM project_models WHERE id=?"
        if kind == "model"
        else "SELECT name,version FROM projects WHERE id=?"
    )
    current = db.one(query, (identifier,))
    fields = ("name",) if kind == "model" else ("name", "version")
    identity = [current[field] for field in fields] if current is not None else None
    if identity not in (before, after):
        raise ValueError("数据库项目与改名恢复记录不一致。")
    committed = identity == after
    paths = [(_manifest_paths(settings, manifest, entry), entry) for entry in entries]
    for directories, entry in paths if committed else reversed(paths):
        repair_renamed_directory(directories, entry, committed=committed)
    _remove_journal(path)


def recover_renames(settings, db):
    """启动时按 SQLite 的实际名称恢复未提交目录或完成已提交改名。"""
    with db.lock:
        root = settings.storage.backups_dir
        try:
            directory = safe_path(root, _JOURNALS)
            if directory.exists() and not directory.is_dir():
                raise ValueError("项目改名清单路径已被占用。")
            for item in sorted(directory.glob("*.json")):
                path = safe_path(directory, item.name)
                _recover_manifest(settings, db, path, decode_json(path.read_bytes()))
        except (HubError, OSError, ValueError, TypeError, KeyError, RecursionError) as error:
            raise HubError("REVIEW_RECOVERY_FAILED", "项目改名恢复未完成，已保留清单和原件。") from error


def _plan_directories(settings, before, after):
    source_rel, target_rel = "/".join(before), "/".join(after)
    directories = []
    for storage in _STORAGE_KEYS:
        root = getattr(settings.storage, storage)
        source = safe_path(root, source_rel)
        if storage == "audit_dir" and not source.exists():
            continue
        if not directory(source):
            raise HubError("PROJECT_PATH_CONFLICT", "原项目目录不存在或大小写不一致。")
        target = safe_path(root, target_rel)
        if target.exists():
            raise HubError("PROJECT_PATH_EXISTS", "目标型号或版本对应的路径已存在。")
        stamp = time_ns() // 1_000_000
        while True:
            archive = project_path(
                root, source.with_name(f"{source.name}-mod_{stamp}").relative_to(root).as_posix()
            )
            if not archive.exists():
                break
            stamp += 1
        directories.append((storage, root, source, target, archive))
    return directories


def _copy_contents(source, stage):
    reject_links(source)
    for item in source.iterdir():
        try:
            reject_links(item)
            if item.is_dir():
                copy_tree(item, stage / item.name)
            elif item.is_file():
                durable_copy(item, stage / item.name)
            else:
                raise ValueError("项目包含非普通文件。")
        except ValueError as error:
            raise HubError("PROJECT_PATH_CONFLICT", "项目包含链接或非普通文件。") from error


def _publish_rename(path, manifest, directories):
    safe_path(path.parent, path.name + ".tmp")
    for storage, root, source, target, archive in directories:
        stage = project_path(
            root,
            source.with_name(f".project-mod_{time_ns() // 1_000_000}-{uuid4().hex}")
            .relative_to(root)
            .as_posix(),
        )
        source_identity = file_identity(source)
        stage.mkdir()
        stage_identity = file_identity(stage)
        manifest["directories"].append(
            {
                "storage": storage,
                "archive": archive.relative_to(root).as_posix(),
                "stage": stage.relative_to(root).as_posix(),
                "source_identity": source_identity,
                "stage_identity": stage_identity,
            }
        )
        write_json(path, manifest)
        _copy_contents(source, stage)
        if not _same_directory(source, source_identity):
            raise HubError("PROJECT_PATH_CONFLICT", "复制时原目录已被替换。")
        _move_directory(source, archive)
        _move_directory(stage, target)


def _update_paths(db, connection, project_id, before, after):
    old_prefix = "/".join(before) + "/"
    new_prefix = "/".join(after) + "/"
    for table in _PATH_TABLES:
        for row in db.query(f"SELECT id,path FROM {table} WHERE project_id=?", (project_id,)):
            path = row["path"]
            if not path.startswith(old_prefix):
                raise HubError("PROJECT_PATH_CONFLICT", "记录路径与原型号版本不一致。")
            connection.execute(
                f"UPDATE {table} SET path=? WHERE id=?", (new_prefix + path[len(old_prefix) :], row["id"])
            )


def _update_model_grants(db, connection, previous, updated):
    model_names = {row["name"] for row in db.query("SELECT name FROM project_models")}
    for row in db.query("SELECT id,project_permissions FROM users"):
        try:
            grants = parse_project_permissions(row["project_permissions"], model_names)
        except (TypeError, ValueError) as error:
            raise HubError("USER_STORE_ERROR", "用户项目授权记录损坏。") from error
        if any(previous in names for names in grants.values()):
            for permission, names in grants.items():
                grants[permission] = sorted(updated if name == previous else name for name in names)
            connection.execute(
                "UPDATE users SET project_permissions=? WHERE id=?",
                (json.dumps(grants, ensure_ascii=False), row["id"]),
            )


def _rename_sql(db, files, connection, original, before, after, *, model):
    if model:
        if db.one("SELECT id FROM project_models WHERE name=?", (after[0],)) is not None:
            raise HubError("PROJECT_EXISTS", "目标型号已存在，不能合并型号。")
        projects = db.query("SELECT id,version FROM projects WHERE name=?", (before[0],))
    else:
        if db.one("SELECT id FROM projects WHERE name=? AND version=?", tuple(after)) is not None:
            raise HubError("PROJECT_EXISTS", "该型号的目标版本已存在。")
        projects = [original]
    project_ids = {row["id"] for row in projects}
    files.require_rename_idle(project_ids)
    for row in projects:
        old_location = [before[0], row["version"]]
        new_location = [after[0], row["version"]] if model else after
        _update_paths(db, connection, row["id"], old_location, new_location)
    if model:
        _update_model_grants(db, connection, before[0], after[0])


def update_project(settings, db, files, identifier, changes, *, model):
    """修改型号或版本资料。涉及改名时，目录及关联路径同时发布。"""
    if model:
        original = db.one("SELECT * FROM project_models WHERE name=?", (identifier,))
        if original is None:
            raise HubError("PROJECT_NOT_FOUND", "型号不存在。")
    else:
        original = (
            db.one("SELECT * FROM projects WHERE id=?", (identifier,))
            if type(identifier) is int and 1 <= identifier < 2**63
            else None
        )
        if original is None:
            raise HubError("PROJECT_NOT_FOUND", "项目版本不存在。")
    table, field, kind = ("project_models", "name", "model") if model else ("projects", "version", "version")
    if (
        not isinstance(changes, dict)
        or not changes
        or not changes.keys() <= {field, "description"}
        or any(not isinstance(value, str) for value in changes.values())
    ):
        raise HubError("INVALID_DATA", "请提供此型号或版本支持的修改字段。")
    old_value = original[field]
    new_value = project_name(changes[field]) if field in changes else old_value
    description = changes.get("description", original["description"])
    path = manifest = None
    try:
        with db.transaction() as connection:
            if new_value != old_value:
                before = [old_value] if model else [original["name"], old_value]
                after = [new_value] if model else [original["name"], new_value]
                _rename_sql(db, files, connection, original, before, after, model=model)
                directories = _plan_directories(settings, before, after)
                path = _journal_file(settings, kind, original["id"])
                manifest = {
                    "kind": kind,
                    "identifier": original["id"],
                    "before": before,
                    "after": after,
                    "directories": [],
                }
                _publish_rename(path, manifest, directories)
            connection.execute(
                f"UPDATE {table} SET {field}=?,description=? WHERE id=?",
                (new_value, description, original["id"]),
            )
            result = db.one(f"SELECT * FROM {table} WHERE id=?", (original["id"],))
    except BaseException as error:
        if manifest is not None and manifest["directories"]:
            try:
                _recover_manifest(settings, db, path, manifest)
            except Exception as restore_error:
                files.mark_recovery_required()
                if not isinstance(error, Exception):
                    error.add_note("项目改名未能恢复，已保留清单和原件。")
                    raise error from restore_error
                raise HubError(
                    "PROJECT_RESTORE_FAILED", "项目目录恢复未完成，已保留清单和原件。"
                ) from restore_error
        if isinstance(error, sqlite3.IntegrityError):
            raise HubError("PROJECT_CONFLICT", "项目名称或文件路径与已有记录冲突。") from error
        if isinstance(error, OSError):
            raise HubError("PROJECT_DIRECTORY_ERROR", "项目目录复制或改名失败。") from error
        raise
    if path is not None:
        try:
            _remove_journal(path)
        except OSError, HubError:
            files.mark_recovery_required()
            _LOGGER.warning("项目改名已提交，清单将在下次恢复时清理。")
    return result
