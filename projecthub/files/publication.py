"""文件与 SQL 的联合发布；清单持久化后才改文件，重启按数据库结果恢复。"""

import json
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from projecthub.contracts import HubError, decode_json
from projecthub.storage import durable_copy, file_identity, hash_file, require_hash, safe_path, write_json

from .rules import TABLES, utc_text

_LOG = logging.getLogger(__name__)


class Publisher:
    def __init__(self, settings, database):
        self.settings = settings
        self.db = database
        self.directory = safe_path(settings.storage.backups_dir, ".transactions")

    def backup(self, record, path, operation, review_id=None, *, review=False):
        stamp = datetime.now(timezone(timedelta(hours=8)))
        if review:
            folder = f"reviews/{record['project_name']}/{record['path'].split('/')[1]}/{stamp:%Y-%m}"
        else:
            folder = f"files/{record['project']}/{record['project_version']}"
        root = self.settings.storage.backups_dir
        target_dir = safe_path(root, folder)
        target_dir.mkdir(parents=True, exist_ok=True)
        label = f"_{operation}_{stamp:%Y%m%d%H%M%S}"
        if operation == "mod" and record.get("category") == "firmware":
            label = f"_{stamp:%Y%m%d%H%M%S}"
        suffix = path.suffix
        stem = path.stem
        while len((stem + label + "_999999" + suffix + ".metadata.json.tmp").encode("utf-16-le")) > 510:
            stem = stem[:-1]
            if not stem:
                raise HubError("FILE_BACKUP_FAILED", "文件名扩展部分过长，无法生成备份。")
        index = 0
        while True:
            target = target_dir / (stem + label + (f"_{index}" if index else "") + suffix)
            if not target.exists() and not target.with_name(target.name + ".metadata.json").exists():
                break
            index += 1
        try:
            digest = hash_file(path)
            if not review and digest != record["sha256"]:
                raise HubError("REVIEW_CONFLICT", "正式文件已变化。")
            durable_copy(path, target)
            require_hash(target, digest, "FILE_BACKUP_FAILED")
            metadata = dict(
                path=target.relative_to(root).as_posix(),
                sha256=digest,
                record=record,
                review_id=review_id,
                operation=operation,
            )
            write_json(target.with_name(target.name + ".metadata.json"), metadata)
            return metadata["path"]
        except OSError as error:
            raise HubError("FILE_BACKUP_FAILED", "文件备份失败。") from error

    def _new_folder(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        folder = safe_path(self.directory, secrets.token_hex(16))
        folder.mkdir()
        return folder

    def prepare_rejection(self, review, backup):
        """删除待审 SQL 记录前保存清理责任，恢复时以该行是否存在判断提交结果。"""
        stage = safe_path(self.settings.storage.audit_dir, review["path"])
        record = dict(
            table="pending_reviews",
            identifier=review["id"],
            old=self.db.one("SELECT * FROM pending_reviews WHERE id=?", (review["id"],)),
            new=None,
            stage=review["path"],
            stage_identity=file_identity(stage),
            backup=backup,
            backup_sha256=hash_file(safe_path(self.settings.storage.backups_dir, backup)) if backup else None,
        )
        folder = self._new_folder()
        write_json(folder / "operation.json", record)
        return folder

    def apply(self, table, identifier, old, new, *, target, stage=None, backup=None, review_id=None):
        """调用方持有 SQL 写事务；old/new 是数据库原始行字典，均可为 None。"""
        folder = self._new_folder()
        target_path = safe_path(self.settings.storage.object_dir, target)
        record = dict(
            table=table,
            identifier=identifier,
            old=old,
            new=new,
            target=target,
            stage=stage,
            stage_identity=file_identity(safe_path(self.settings.storage.audit_dir, stage))
            if stage
            else None,
            backup=backup,
            review_id=review_id,
            source_identity=None,
        )
        source = safe_path(self.settings.storage.object_dir, old["path"]) if old else None
        if source:
            require_hash(source, old["sha256"], "REVIEW_CONFLICT")
            record["source_identity"] = file_identity(source)
        if stage and new is not None:
            staged = safe_path(self.settings.storage.audit_dir, stage)
            require_hash(staged, new["sha256"], "UPLOAD_HASH_MISMATCH")
            durable_copy(staged, folder / "content")
        write_json(folder / "operation.json", record)
        if new is not None:
            if old is None or source != target_path:
                try:
                    os.link(folder / "content", target_path)
                except FileExistsError as error:
                    raise HubError("FILE_ALREADY_EXISTS", "目标位置已被占用。") from error
            else:
                os.link(folder / "content", folder / "replacement")
                os.replace(folder / "replacement", target_path)
            require_hash(target_path, new["sha256"], "UPLOAD_HASH_MISMATCH")
        if source and (new is None or source != target_path):
            # 原文件身份随 tombstone 保留；回滚只能独占恢复，不能覆盖外部新文件。
            os.rename(source, folder / "removed")
        return folder

    def _clean_stage(self, record):
        if not record["stage"]:
            return
        stage = safe_path(self.settings.storage.audit_dir, record["stage"])
        if stage.exists():
            if file_identity(stage) != record["stage_identity"]:
                raise HubError("REVIEW_RECOVERY_FAILED", "待审暂存已被外部替换，保留清单。")
            stage.unlink()

    def recover_one(self, folder):
        manifest = folder / "operation.json"
        record = decode_json(manifest.read_bytes())
        if (
            record.get("table") not in (*TABLES.values(), "pending_reviews")
            or type(record.get("identifier")) is not int
        ):
            raise HubError("REVIEW_RECOVERY_FAILED", "恢复记录格式无效。")
        if record.get("resolved") is True:
            self._cleanup(folder)
            return record["committed"]
        old, new = record["old"], record["new"]
        current = self.db.one(f"SELECT * FROM {record['table']} WHERE id=?", (record["identifier"],))
        committed = current == new
        if not committed and current != old:
            raise HubError("REVIEW_RECOVERY_FAILED", "数据库与文件恢复记录不一致。")
        if record["table"] == "pending_reviews":
            if committed:
                if record["backup"]:
                    require_hash(
                        safe_path(self.settings.storage.backups_dir, record["backup"]),
                        record["backup_sha256"],
                        "REVIEW_RECOVERY_FAILED",
                    )
                self._clean_stage(record)
            write_json(manifest, dict(record, resolved=True, committed=committed))
            self._cleanup(folder)
            return committed
        target = safe_path(self.settings.storage.object_dir, record["target"])
        source = safe_path(self.settings.storage.object_dir, old["path"]) if old else None
        witness = folder / "content"
        removed = folder / "removed"
        restored = folder / "restore"
        if committed:
            if new:
                require_hash(target, new["sha256"], "REVIEW_RECOVERY_FAILED")
            elif target.exists():
                raise HubError("REVIEW_RECOVERY_FAILED", "已删除文件的位置出现其他文件。")
            if record["backup"]:
                require_hash(
                    safe_path(self.settings.storage.backups_dir, record["backup"]),
                    old["sha256"],
                    "REVIEW_RECOVERY_FAILED",
                )
            self._clean_stage(record)
        else:
            if new and target.exists() and witness.exists() and target.samefile(witness):
                if source == target and old:
                    backup = safe_path(self.settings.storage.backups_dir, record["backup"])
                    require_hash(backup, old["sha256"], "REVIEW_RECOVERY_FAILED")
                    restored.unlink(missing_ok=True)
                    durable_copy(backup, restored)
                    replacement = folder / "replacement"
                    replacement.unlink(missing_ok=True)
                    os.link(restored, replacement)
                    os.replace(replacement, target)
                else:
                    target.unlink()
            elif (
                new
                and target.exists()
                and not (
                    old
                    and source == target
                    and (
                        file_identity(target) == record["source_identity"]
                        or restored.exists()
                        and target.samefile(restored)
                    )
                )
            ):
                raise HubError("REVIEW_RECOVERY_FAILED", "发布目标已被外部替换，保留清单。")
            if old:
                if removed.exists():
                    if source.exists():
                        if not source.samefile(removed):
                            raise HubError("REVIEW_RECOVERY_FAILED", "旧位置被其他文件占用。")
                    else:
                        os.link(removed, source)
                require_hash(source, old["sha256"], "REVIEW_RECOVERY_FAILED")
        # 先持久记录恢复结果；清理重试不再依赖可能已变化的正式行或文件身份。
        write_json(manifest, dict(record, resolved=True, committed=committed))
        self._cleanup(folder)
        return committed

    def _cleanup(self, folder):
        allowed = {"content", "removed", "restore", "replacement", "operation.json.tmp"}
        # 最后删除清单。即使 rmdir 失败，空目录也可由下次恢复直接清理。
        for item in folder.iterdir():
            checked = safe_path(self.directory, item.relative_to(self.directory).as_posix())
            if checked.name == "operation.json":
                continue
            if checked.name not in allowed or not checked.is_file():
                raise HubError("REVIEW_RECOVERY_FAILED", "恢复目录包含未知产物。")
            checked.unlink()
        (folder / "operation.json").unlink()
        folder.rmdir()

    def recover(self):
        if not self.directory.exists():
            return
        for folder in sorted(self.directory.iterdir()):
            folder = safe_path(self.directory, folder.name)
            if not folder.is_dir():
                raise HubError("REVIEW_RECOVERY_FAILED", "恢复目录包含未知文件。")
            if (folder / "operation.json").exists():
                self.recover_one(folder)
            else:
                # 清单落盘前尚未接触正式文件，只清理本模块有限的准备产物。
                for item in folder.iterdir():
                    if item.name not in ("content", "operation.json.tmp") or item.is_dir():
                        raise HubError("REVIEW_RECOVERY_FAILED", "未完成发布目录内容无效。")
                    safe_path(folder, item.name).unlink()
                folder.rmdir()


def finish_publication(files, folder):
    """SQL 已提交；清理失败保留成功结果和清单，同时阻断后续文件操作。"""
    try:
        files.publisher.recover_one(folder)
    except BaseException as error:
        files._file_recovery_required = True
        if not isinstance(error, (OSError, HubError)):
            raise
        _LOG.warning("文件操作已提交，清理待重试；暂停文件操作直至恢复完成。", exc_info=True)


def publish_file(
    files, project_id, category, name, fields, digest, stage, *, original=None, review=None, delete=False
):
    table = files._table(category)
    project = files._project(project_id)
    relative = original["path"] if original else f"{project['name']}/{project['version']}/{category}/{name}"
    if original and category == "firmware" and not delete:
        relative = Path(relative).with_name(name).as_posix()
    target = safe_path(files.settings.storage.object_dir, relative)
    if not target.parent.is_dir():
        raise HubError("REVIEW_CONFLICT", "正式目录不存在。")
    backup = None
    if original:
        backup = files.publisher.backup(
            original,
            files._formal_path(original),
            "del" if delete else "mod",
            review["id"] if review else None,
        )
    folder = None
    result = None
    try:
        with files.db.transaction() as connection:
            old = files.db.one(f"SELECT * FROM {table} WHERE id=?", (original["id"],)) if original else None
            if delete:
                identifier = original["id"]
                connection.execute(f"DELETE FROM {table} WHERE id=?", (identifier,))
                if category == "curve":
                    remaining = files.db.query(
                        "SELECT id FROM curve_configs WHERE project_id=? ORDER BY sort_order,id",
                        (project_id,),
                    )
                    connection.executemany(
                        "UPDATE curve_configs SET sort_order=? WHERE id=?",
                        [(i, r["id"]) for i, r in enumerate(remaining, 1)],
                    )
                new = None
            else:
                values = dict(project_id=project_id, file_name=name, sha256=digest, path=relative, **fields)
                if category == "curve":
                    values["modified_at"] = utc_text(
                        safe_path(files.settings.storage.audit_dir, stage).stat().st_mtime_ns
                    )
                    if not original:
                        values["sort_order"] = files.db.one(
                            "SELECT COALESCE(MAX(sort_order),0)+1 AS n FROM curve_configs WHERE project_id=?",
                            (project_id,),
                        )["n"]
                elif category == "firmware":
                    records = files.catalog.list_files(project_id=project_id, category=category)["files"]
                    files._validate_ranges(
                        [r for r in records if original is None or r["id"] != original["id"]]
                        + [dict(fields, project_id=project_id)]
                    )
                    values["supported_models"] = json.dumps(
                        fields.get("supported_models", []), ensure_ascii=False
                    )
                    values["uploaded_at"] = review["uploaded_at"]
                    uploader = files.db.one(
                        "SELECT name FROM users WHERE id=?", (review["field_info"]["uploaded_by"],)
                    )
                    values["uploaded_by"] = (
                        uploader["name"] if uploader else str(review["field_info"]["uploaded_by"])
                    )
                else:
                    values["file_type"] = Path(name).suffix.lower()
                if original:
                    identifier = original["id"]
                    connection.execute(
                        f"UPDATE {table} SET {','.join(key + '=?' for key in values)} WHERE id=?",
                        (*values.values(), identifier),
                    )
                else:
                    cursor = connection.execute(
                        f"INSERT INTO {table} ({','.join(values)}) VALUES ({','.join('?' for _ in values)})",
                        tuple(values.values()),
                    )
                    identifier = cursor.lastrowid
                new = files.db.one(f"SELECT * FROM {table} WHERE id=?", (identifier,))
                result = files._decorate(category, new)
            if review:
                connection.execute("DELETE FROM pending_reviews WHERE id=?", (review["id"],))
            folder = files.publisher.apply(
                table,
                identifier,
                old,
                new,
                target=relative,
                stage=stage,
                backup=backup,
                review_id=review["id"] if review else None,
            )
    except BaseException as error:
        try:
            files.publisher.recover()
        except BaseException as cleanup:
            files._file_recovery_required = True
            raise HubError("REVIEW_RECOVERY_FAILED", "发布失败且恢复未完成。") from cleanup
        if isinstance(error, OSError):
            raise HubError(
                "FILE_DELETE_FAILED" if delete else "FILE_WRITE_FAILED", "文件操作失败，已恢复。"
            ) from error
        raise
    if folder:
        finish_publication(files, folder)
    if delete:
        result = dict(
            status="deleted",
            project_id=project_id,
            file_id=identifier,
            file_name=name,
            backup_path=backup,
        )
        if review:
            result["review_id"] = review["id"]
        else:
            result["category"] = category
    return result
