"""项目文件服务：实例拥有上传/下载，业务读写在同一数据库门内检查当前权限。"""

import json
from contextlib import contextmanager
from pathlib import Path

from projecthub.contracts import HubError, decode_json, digest_data
from projecthub.storage import HISTORY, require_hash, safe_path

from .authorizations import DownloadAuthorizations
from .catalog import FileCatalog
from .publication import Publisher, publish_file
from .reviews import FileReviews
from .rules import FIELDS, TABLES, WRITE_PERMISSIONS, upload_fields, utc_text, version_key, version_range
from .synchronization import FileSynchronizer


class FileService:
    def __init__(self, settings, db, permissions):
        self.settings, self.db, self.permissions = settings, db, permissions
        self.publisher = Publisher(settings, db)
        self.catalog = FileCatalog(self)
        self.reviews = FileReviews(self)
        self.authorizations = DownloadAuthorizations(self)
        self.synchronizer = FileSynchronizer(self)
        self._uploads = set()
        self._downloads = set()
        self.initialized = False
        self._closing = False
        self._file_recovery_required = False
        self._external_recovery_required = False

    @property
    def recovery_required(self):
        return self._file_recovery_required or self._external_recovery_required

    def _directories(self):
        for key in ("object_dir", "audit_dir", "cache_dir", "backups_dir"):
            root = getattr(self.settings.storage, key)
            safe_path(root, ".path-check")
            root.mkdir(parents=True, exist_ok=True)

    def scan_initial(self):
        """Database.open 的首次建库事务回调；审核类别不能由磁盘扫描直接发布。"""
        self._directories()
        self.synchronizer.scan({}, initial=True)

    def initialize(self):
        with self.db.lock:
            if self.initialized:
                return
            self._directories()
            self.recover_reviews()
            self._closing = False
            self.initialized = True
            # 回收没有待审记录的平铺接收残留，只处理明确类别层级。
            protected = {row["path"] for row in self.db.query("SELECT path FROM pending_reviews")}
            for item in self.settings.storage.audit_dir.glob("*/*/*/*"):
                relative = item.relative_to(self.settings.storage.audit_dir).as_posix()
                if item.parent.name in TABLES and not any(HISTORY.search(p) for p in Path(relative).parts):
                    checked = safe_path(self.settings.storage.audit_dir, relative)
                    if checked.is_file() and relative not in protected:
                        checked.unlink()

    def close(self):
        with self.db.lock:
            self._closing = True
            resources = tuple(self._uploads | self._downloads)
        errors = []
        for handle in resources:
            try:
                handle.close()
            except Exception as error:
                errors.append(error)
        with self.db.lock:
            if not errors:
                self.initialized = False
            else:
                raise HubError(
                    "FILE_SERVICE_LIFECYCLE_ERROR", "部分文件资源未释放，请重试。"
                ) from ExceptionGroup("close", errors)

    @contextmanager
    def operation(self):
        with self.db.lock:
            if not self.initialized or self._closing:
                raise HubError("FILE_SERVICE_NOT_READY", "文件服务尚未就绪或正在关闭。")
            if self.recovery_required:
                raise HubError("REVIEW_RECOVERY_FAILED", "文件与数据库尚未恢复一致。")
            safe_path(self.settings.storage.object_dir, ".path-check")
            yield self.settings

    def recover_reviews(self):
        with self.db.lock:
            try:
                self.publisher.recover()
            except BaseException:
                self._file_recovery_required = True
                raise
            self._file_recovery_required = False

    def _project(self, identifier):
        if type(identifier) is not int or identifier < 1:
            raise HubError("PROJECT_NOT_FOUND", "项目版本不存在。")
        row = self.db.one("SELECT * FROM projects WHERE id=?", (identifier,))
        if row is None:
            raise HubError("PROJECT_NOT_FOUND", "项目版本不存在。")
        return row

    @staticmethod
    def _table(category):
        if category not in TABLES:
            raise HubError("INVALID_CATEGORY", "文件分类无效。")
        return TABLES[category]

    def _decorate(self, category, row):
        project = self._project(row["project_id"])
        result = dict(category=category, project=project["name"], project_version=project["version"], **row)
        if category == "firmware":
            result["supported_models"] = decode_json(result["supported_models"])
        return result

    def _record(self, category, identifier, project_id):
        table = self._table(category)
        if type(identifier) is not int or identifier < 1:
            raise HubError("FILE_NOT_FOUND", "文件不存在。")
        row = self.db.one(f"SELECT * FROM {table} WHERE id=? AND project_id=?", (identifier, project_id))
        if row is None:
            raise HubError("FILE_NOT_FOUND", "文件不存在或不属于此项目。")
        return self._decorate(category, row)

    def _formal_path(self, row):
        prefix = f"{row['project']}/{row['project_version']}/{row['category']}/"
        if not row["path"].startswith(prefix) or Path(row["path"]).name != row["file_name"]:
            raise HubError("FILE_NOT_FOUND", "文件记录的路径不属于项目。")
        try:
            path = safe_path(self.settings.storage.object_dir, row["path"])
            if not path.is_file() or any(HISTORY.search(part) for part in Path(row["path"]).parts):
                raise ValueError("文件不是活动文件。")
            return path
        except (HubError, OSError, ValueError) as error:
            raise HubError("FILE_NOT_FOUND", "文件路径缺失或不安全。") from error

    @staticmethod
    def _revision(record, category):
        if record is None:
            return None
        values = {key: record[key] for key in FIELDS[category] - {"supported_models"}}
        values.update(id=record["id"], sha256=record["sha256"])
        if category == "firmware" and record.get("supported_models"):
            values["supported_models"] = record["supported_models"]
        return digest_data(values)

    def _pending_available(self, project_id, name, original=None, *, ignore_upload=None, ignore_review=None):
        target_id = original["id"] if original and original["category"] == "firmware" else None
        for row in self.db.query(
            "SELECT id,file_name,field_info FROM pending_reviews WHERE project_id=?", (project_id,)
        ):
            if row["id"] == ignore_review:
                continue
            info = decode_json(row["field_info"])
            if row["file_name"].casefold() == name.casefold():
                raise HubError("REVIEW_ALREADY_PENDING", "该文件已有待审变更。")
            if (
                target_id is not None
                and info.get("category") == "firmware"
                and info.get("file_id") == target_id
            ):
                raise HubError("REVIEW_ALREADY_PENDING", "该文件已有待审变更。")
        for handle in self._uploads:
            if handle is ignore_upload or handle.finished or handle.closed or handle.project_id != project_id:
                continue
            if (
                handle.name.casefold() == name.casefold()
                or target_id is not None
                and handle.original
                and handle.category == "firmware"
                and handle.original["id"] == target_id
            ):
                raise HubError("REVIEW_ALREADY_PENDING", "该文件已有上传正在接收。")

    def _available_name(self, project_id, name, original=None):
        for kind, table in TABLES.items():
            for row in self.db.query(
                f"SELECT id,file_name,path FROM {table} WHERE project_id=?", (project_id,)
            ):
                if row["file_name"].casefold() == name.casefold() and not (
                    original and original["category"] == kind and original["id"] == row["id"]
                ):
                    raise HubError("FILE_ALREADY_EXISTS", "项目内不能存在同名文件。")
        project = self._project(project_id)
        directory = safe_path(self.settings.storage.object_dir, f"{project['name']}/{project['version']}")
        allowed = safe_path(self.settings.storage.object_dir, original["path"]) if original else None

        def inspect(folder):
            if not folder.is_dir():
                raise HubError("REVIEW_CONFLICT", "项目正式目录缺失。")
            for item in folder.iterdir():
                if HISTORY.search(item.name):
                    continue
                checked = safe_path(
                    self.settings.storage.object_dir,
                    item.relative_to(self.settings.storage.object_dir).as_posix(),
                )
                if checked.is_dir():
                    inspect(checked)
                elif item.name.casefold() == name.casefold() and item != allowed:
                    raise HubError("FILE_ALREADY_EXISTS", "项目磁盘存在同名活动文件。")

        inspect(directory)

    def _firmware_target(self, project_id, fields):
        interval = version_range(fields["min_version"], fields["max_version"])
        if interval is None:
            return None
        matches = []
        for row in self.catalog.list_files(project_id=project_id, category="firmware")["files"]:
            if (
                row["firmware_type"] == fields["firmware_type"]
                and interval[0] <= version_key(row["version"]) <= interval[1]
                and version_key(fields["version"]) > version_key(row["version"])
            ):
                matches.append(row)
        if len(matches) > 1:
            raise HubError("REVIEW_CONFLICT", "新固件对应多个升级目标。")
        return matches[0] if matches else None

    @staticmethod
    def _validate_ranges(records):
        groups = {}
        for row in records:
            version_key(row["version"])
            interval = version_range(row["min_version"], row["max_version"])
            if interval:
                groups.setdefault((row.get("project_id"), row["firmware_type"]), []).append(interval)
        for intervals in groups.values():
            intervals.sort()
            if any(second[0] <= first[1] for first, second in zip(intervals, intervals[1:])):
                raise HubError("INVALID_UPLOAD", "同类型固件的支持版本范围重叠。")

    def begin_upload(self, actor, project_id, category, file_name, fields, file_sha256):
        from .transfers import Upload

        fields = upload_fields(category, file_name, fields, file_sha256)
        with self.operation():
            self.permissions.require(actor, WRITE_PERMISSIONS[category], project_id)
            original = self._firmware_target(project_id, fields) if category == "firmware" else None
            self._pending_available(project_id, file_name, original)
            self._available_name(project_id, file_name, original)
            if original:
                require_hash(self._formal_path(original), original["sha256"], "REVIEW_CONFLICT")
                self._pending_available(project_id, original["file_name"], original)
            return Upload(self, actor, project_id, category, file_name, fields, file_sha256, original)

    def begin_curve_update(self, actor, project_id, file_id, fields, file_sha256):
        from .transfers import Upload

        with self.operation():
            self.permissions.require(actor, "curve.manage", project_id)
            original = self._record("curve", file_id, project_id)
            fields = upload_fields("curve", original["file_name"], fields, file_sha256)
            self._pending_available(project_id, original["file_name"], original)
            self._available_name(project_id, original["file_name"], original)
            require_hash(self._formal_path(original), original["sha256"], "REVIEW_CONFLICT")
            return Upload(
                self, actor, project_id, "curve", original["file_name"], fields, file_sha256, original
            )

    def _insert_review(
        self, actor, project_id, category, name, relative, digest, fields, original=None, action=None
    ):
        info = dict(
            category=category,
            uploaded_by=actor["user_id"],
            base_revision=self._revision(original, category),
            fields=fields,
        )
        if action:
            info["action"] = action
        if original:
            info["file_id"] = original["id"]
        user = self.db.one("SELECT name FROM users WHERE id=?", (actor["user_id"],))
        with self.db.transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO pending_reviews(project_id,reviewer_user_id,uploaded_at,file_name,path,sha256,field_info,submitter_name) VALUES(?,NULL,?,?,?,?,?,?)",
                (
                    project_id,
                    utc_text(),
                    name,
                    relative,
                    digest,
                    json.dumps(info, ensure_ascii=False),
                    user["name"],
                ),
            )
            identifier = cursor.lastrowid
        return self.reviews._review(identifier)

    def _finish_upload(self, handle):
        with self.operation():
            self.permissions.require(handle.actor, WRITE_PERMISSIONS[handle.category], handle.project_id)
            original = handle.original
            if handle.category == "firmware":
                current = self._firmware_target(handle.project_id, handle.fields)
            elif original:
                current = self._record(handle.category, original["id"], handle.project_id)
            else:
                current = None
            if self._revision(current, handle.category) != self._revision(original, handle.category):
                raise HubError("REVIEW_CONFLICT", "接收期间正式文件发生变化。")
            self._pending_available(handle.project_id, handle.name, original, ignore_upload=handle)
            self._available_name(handle.project_id, handle.name, original)
            if original:
                require_hash(self._formal_path(original), original["sha256"], "REVIEW_CONFLICT")
            if handle.category in ("internal", "public"):
                return publish_file(
                    self,
                    handle.project_id,
                    handle.category,
                    handle.name,
                    handle.fields,
                    handle.digest,
                    handle.relative,
                )
            fields = dict(handle.fields)
            action = "update" if original else ("create" if handle.category == "firmware" else None)
            if handle.category == "firmware":
                fields.setdefault("supported_models", original["supported_models"] if original else [])
            return self._insert_review(
                handle.actor,
                handle.project_id,
                handle.category,
                handle.name,
                handle.relative,
                handle.digest,
                fields,
                original,
                action,
            )

    def set_curve_position(self, actor, project_id, file_id, position):
        with self.operation(), self.db.transaction() as connection:
            self.permissions.require(actor, "curve.manage", project_id)
            self._record("curve", file_id, project_id)
            rows = self.db.query(
                "SELECT id FROM curve_configs WHERE project_id=? ORDER BY sort_order,id", (project_id,)
            )
            if type(position) is not int or not 1 <= position <= len(rows):
                raise HubError("INVALID_DATA", "位置须为项目曲线数量内从 1 开始的整数。")
            ids = [row["id"] for row in rows if row["id"] != file_id]
            ids.insert(position - 1, file_id)
            connection.executemany(
                "UPDATE curve_configs SET sort_order=? WHERE id=?",
                [(i, value) for i, value in enumerate(ids, 1)],
            )
            return {"project_id": project_id, "file_id": file_id, "sort_order": position}

    def delete_managed_file(self, actor, project_id, category, file_id):
        if category not in ("internal", "public"):
            raise HubError("INVALID_CATEGORY", "直接删除仅用于内部和公共文件。")
        with self.operation():
            self.permissions.require(actor, WRITE_PERMISSIONS[category], project_id)
            original = self._record(category, file_id, project_id)
            self._pending_available(project_id, original["file_name"])
            return publish_file(
                self,
                project_id,
                category,
                original["file_name"],
                {},
                original["sha256"],
                None,
                original=original,
                delete=True,
            )

    def mark_recovery_required(self):
        """项目服务恢复失败时暂停共享文件业务，保留资源归属。"""
        with self.db.lock:
            self._external_recovery_required = True

    def complete_external_recovery(self):
        """外部目录事务恢复完成，仅解除外部门禁，不覆盖文件发布恢复状态。"""
        with self.db.lock:
            self._external_recovery_required = False

    def require_rename_idle(self, project_ids):
        """持有同一数据库门禁检查待审类别上传，项目模块无需读取私有句柄集合。"""
        with self.db.lock:
            if any(
                not item.closed
                and not item.finished
                and item.project_id in project_ids
                and item.category in ("curve", "firmware")
                for item in self._uploads
            ):
                raise HubError("PROJECT_DIRECTORY_ERROR", "项目有文件正在上传，暂不能改名。")
