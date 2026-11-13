"""项目文件服务：实例拥有上传/下载，业务读写在同一数据库门内检查当前权限。"""

from projecthub.contracts import HubError, decode_json
from projecthub.storage import require_hash, safe_path

from .publication import finish_publication, publish_file
from .rules import FIELDS, WRITE_PERMISSIONS, upload_fields, validate_pagination


class FileReviews:
    """文件审核状态转换与待审下载。"""

    def __init__(self, files):
        self._files = files

    def _review(self, identifier):
        files = self._files
        if type(identifier) is not int or identifier < 1:
            raise HubError("REVIEW_NOT_FOUND", "待审不存在或已处理。")
        row = files.db.one(
            "SELECT r.*,p.name AS project_name FROM pending_reviews r JOIN projects p ON p.id=r.project_id WHERE r.id=?",
            (identifier,),
        )
        if row is None:
            raise HubError("REVIEW_NOT_FOUND", "待审不存在或已处理。")
        row["field_info"] = decode_json(row["field_info"])
        return row

    def get_review(self, actor, review_id):
        files = self._files
        with files.operation():
            row = self._review(review_id)
            files.permissions.require(actor, row["field_info"]["category"] + ".review", row["project_id"])
            return row

    def list_reviews(self, actor, *, project_id=None, offset=0, limit=50):
        files = self._files
        with files.operation():
            validate_pagination(offset, limit)
            records = []
            for item in files.db.query("SELECT id FROM pending_reviews ORDER BY id"):
                row = self._review(item["id"])
                if project_id is not None and row["project_id"] != project_id:
                    continue
                permission = row["field_info"]["category"] + ".review"
                if permission in files.permissions.permissions_for(actor, row["project_id"]):
                    records.append(row)
            result = records[offset : offset + limit]
            return {"reviews": result, "offset": offset, "count": len(result)}

    def prepare_review_download(self, actor, review_id):
        files = self._files
        from .transfers import Download

        with files.operation():
            row = self.get_review(actor, review_id)
            path = safe_path(files.settings.storage.audit_dir, row["path"])
            return Download(files, path, row["file_name"], row["sha256"], verify="REVIEW_HASH_MISMATCH")

    def request_curve_delete(self, actor, project_id, file_id):

        return self._request_delete(actor, project_id, "curve", file_id)

    def request_firmware_delete(self, actor, project_id, file_id):

        return self._request_delete(actor, project_id, "firmware", file_id)

    def _request_delete(self, actor, project_id, category, file_id):
        files = self._files
        from projecthub.storage import durable_copy

        with files.operation():
            files.permissions.require(actor, WRITE_PERMISSIONS[category], project_id)
            original = files._record(category, file_id, project_id)
            files._pending_available(project_id, original["file_name"], original)
            source = files._formal_path(original)
            require_hash(source, original["sha256"], "REVIEW_CONFLICT")
            relative = (
                f"{original['project']}/{original['project_version']}/{category}/{original['file_name']}"
            )
            stage = safe_path(files.settings.storage.audit_dir, relative)
            stage.parent.mkdir(parents=True, exist_ok=True)
            try:
                durable_copy(source, stage)
            except FileExistsError as error:
                raise HubError("REVIEW_ALREADY_PENDING", "存在同名待审文件。") from error
            try:
                fields = {key: original[key] for key in FIELDS[category]}
                return files._insert_review(
                    actor,
                    project_id,
                    category,
                    original["file_name"],
                    relative,
                    original["sha256"],
                    fields,
                    original,
                    "delete",
                )
            except BaseException:
                if files.db.one("SELECT id FROM pending_reviews WHERE path=?", (relative,)) is None:
                    stage.unlink(missing_ok=True)
                raise

    def approve_review(self, actor, review_id):
        files = self._files
        with files.operation():
            row = self.get_review(actor, review_id)
            info = row["field_info"]
            category, action = info["category"], info.get("action", "create")
            fields = upload_fields(category, row["file_name"], info["fields"], row["sha256"])
            original = None
            if action in ("update", "delete"):
                try:
                    original = files._record(category, info["file_id"], row["project_id"])
                except HubError as error:
                    raise HubError("REVIEW_CONFLICT", "待审目标已变化。") from error
            if files._revision(original, category) != info.get("base_revision"):
                raise HubError("REVIEW_CONFLICT", "正式文件或字段已变化。")
            if category == "firmware" and action != "delete":
                eligible = files._firmware_target(row["project_id"], fields)
                if action == "create" and eligible is not None:
                    raise HubError("REVIEW_CONFLICT", "提交后出现新的固件升级目标。")
                if action == "update" and (eligible is None or eligible["id"] != original["id"]):
                    raise HubError("REVIEW_CONFLICT", "固件已不满足升级条件。")
                fields.setdefault("supported_models", original["supported_models"] if original else [])
            require_hash(
                safe_path(files.settings.storage.audit_dir, row["path"]),
                row["sha256"],
                "REVIEW_HASH_MISMATCH",
            )
            if action != "delete":
                files._available_name(row["project_id"], row["file_name"], original)
            return publish_file(
                files,
                row["project_id"],
                category,
                row["file_name"],
                fields,
                row["sha256"],
                row["path"],
                original=original,
                review=row,
                delete=action == "delete",
            )

    def reject_review(self, actor, review_id):
        files = self._files
        with files.operation():
            row = self.get_review(actor, review_id)
            stage = safe_path(files.settings.storage.audit_dir, row["path"])
            backup = None
            if row["field_info"].get("action") != "delete":
                backup = files.publisher.backup(row, stage, "reject", review_id, review=True)
            try:
                with files.db.transaction() as connection:
                    folder = files.publisher.prepare_rejection(row, backup)
                    connection.execute("DELETE FROM pending_reviews WHERE id=?", (review_id,))
            except BaseException:
                files.recover_reviews()
                raise
            finish_publication(files, folder)
            return {"review_id": review_id, "status": "rejected"}
