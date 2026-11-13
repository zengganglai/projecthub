"""项目文件服务：实例拥有上传/下载，业务读写在同一数据库门内检查当前权限。"""

from hashlib import sha256

from projecthub.contracts import HubError, json_object
from projecthub.storage import require_hash

from .rules import utc_text, validate_pagination


class DownloadAuthorizations:
    """限时下载授权的管理和校验。"""

    def __init__(self, files):
        self._files = files

    def _authorization(self, row):
        files = self._files
        project = files._project(row["project_id"])
        return dict(project=project["name"], project_version=project["version"], **row)

    @staticmethod
    def _active(record):
        from datetime import datetime, timezone

        try:
            expiry = datetime.fromisoformat(record["expires_at"])
            if expiry.utcoffset() is None:
                raise ValueError("缺少时区。")
        except (ValueError, TypeError) as error:
            raise HubError("AUTHORIZATION_INVALID", "授权到期时间无效。") from error
        if datetime.now(timezone.utc) >= expiry:
            raise HubError("AUTHORIZATION_EXPIRED", "下载授权已到期。")

    def _file_at(self, project_id, path):
        files = self._files
        records = files.catalog.list_files(project_id=project_id, path=path)["files"]
        if len(records) != 1:
            raise HubError("FILE_NOT_FOUND", "授权路径没有对应正式文件。")
        row = records[0]
        return row, files._formal_path(row)

    def create_download_authorization(
        self, actor, project_id, path, expires_at, authorization_code, device_code
    ):
        files = self._files
        import re
        from datetime import date, datetime, timedelta, timezone

        with files.operation(), files.db.transaction() as connection:
            files.permissions.require(actor, "authorization.manage", project_id)
            if (
                not isinstance(authorization_code, str)
                or re.fullmatch("[0-9a-fA-F]{16}", authorization_code) is None
            ):
                raise HubError("INVALID_AUTHORIZATION_CODE", "授权码须为 16 位十六进制字符串。")
            if not isinstance(device_code, str) or not device_code.strip() or len(device_code) > 128:
                raise HubError("INVALID_DATA", "设备编码须为 1～128 个字符的非空文本。")
            if not isinstance(path, str) or not 1 <= len(path) <= 1024:
                raise HubError("INVALID_DATA", "正式文件路径无效。")
            row, source = self._file_at(project_id, path)
            require_hash(source, row["sha256"], "AUTHORIZATION_HASH_MISMATCH")
            try:
                if not isinstance(expires_at, str):
                    raise ValueError("到期时间须为字符串。")
                if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", expires_at):
                    tomorrow = date.fromisoformat(expires_at) + timedelta(days=1)
                    expiry = datetime.combine(tomorrow, datetime.min.time(), timezone(timedelta(hours=8)))
                else:
                    expiry = datetime.fromisoformat(expires_at)
                    if "T" not in expires_at or expiry.utcoffset() is None:
                        raise ValueError("具体时刻必须包含时区。")
                if expiry <= datetime.now(timezone.utc):
                    raise ValueError("到期时间须在未来。")
                expiration = (
                    expiry.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
                )
            except (ValueError, OverflowError) as error:
                raise HubError("INVALID_DATA", "到期时间须为未来日期或带时区时刻。") from error
            code = authorization_code.lower()
            if files.db.one("SELECT id FROM download_authorizations WHERE authorization_code=?", (code,)):
                raise HubError("AUTHORIZATION_CODE_EXISTS", "授权编码已被占用。")
            user = files.db.one("SELECT name FROM users WHERE id=?", (actor["user_id"],))
            identifier = connection.execute(
                "INSERT INTO download_authorizations(created_by,authorization_code,project_id,path,expires_at,sha256,device_code) VALUES(?,?,?,?,?,?,?)",
                (user["name"], code, project_id, path, expiration, row["sha256"], device_code),
            ).lastrowid
            return self._authorization(
                files.db.one("SELECT * FROM download_authorizations WHERE id=?", (identifier,))
            )

    def get_download_authorization(self, authorization_code):
        files = self._files
        import re

        with files.operation():
            if (
                not isinstance(authorization_code, str)
                or re.fullmatch("[0-9a-fA-F]{16}", authorization_code) is None
            ):
                raise HubError("AUTHORIZATION_NOT_FOUND", "下载授权不存在。")
            authorization_code = authorization_code.lower()
            row = files.db.one(
                "SELECT * FROM download_authorizations WHERE authorization_code=?", (authorization_code,)
            )
            if row is None:
                raise HubError("AUTHORIZATION_NOT_FOUND", "下载授权不存在。")
            self._active(row)
            return self._authorization(row)

    def list_download_authorizations(self, actor, project_id, *, offset=0, limit=50):
        files = self._files
        with files.operation():
            files.permissions.require(actor, "authorization.list", project_id)
            validate_pagination(offset, limit)
            rows = files.db.query(
                "SELECT * FROM download_authorizations WHERE project_id=? AND expires_at>? ORDER BY id DESC",
                (project_id, utc_text()),
            )
            selected = [self._authorization(row) for row in rows[offset : offset + limit]]
            return dict(
                authorizations=selected, offset=offset, limit=limit, count=len(selected), total=len(rows)
            )

    def delete_download_authorization(self, actor, project_id, authorization_id):
        files = self._files
        with files.operation(), files.db.transaction() as connection:
            files.permissions.require(actor, "authorization.manage", project_id)
            if type(authorization_id) is not int or not 1 <= authorization_id <= 9007199254740991:
                raise HubError("AUTHORIZATION_NOT_FOUND", "下载授权不存在。")
            cursor = connection.execute(
                "DELETE FROM download_authorizations WHERE id=? AND project_id=?",
                (authorization_id, project_id),
            )
            if cursor.rowcount != 1:
                raise HubError("AUTHORIZATION_NOT_FOUND", "下载授权不存在或不属于项目。")
            return dict(authorization_id=authorization_id, project_id=project_id, status="deleted")

    def _authorized_file(self, code):

        authorization = self.get_download_authorization(code)
        row, source = self._file_at(authorization["project_id"], authorization["path"])
        if row["sha256"] != authorization["sha256"]:
            raise HubError("AUTHORIZATION_HASH_MISMATCH", "原文件已变化。")
        return authorization, row, source

    def read_authorized_json(self, authorization_code):
        files = self._files
        with files.operation():
            record, row, source = self._authorized_file(authorization_code)
            if not row["file_name"].lower().endswith(".json"):
                raise HubError("NOT_JSON_FILE", "此文件不是 JSON，请下载原始文件。")
            try:
                content = source.read_bytes()
            except OSError as error:
                raise HubError("FILE_NOT_FOUND", "授权文件无法读取。") from error
            if sha256(content).hexdigest() != record["sha256"]:
                raise HubError("AUTHORIZATION_HASH_MISMATCH", "原文件已变化。")
            try:
                data = json_object(content)
            except (ValueError, UnicodeError, RecursionError) as error:
                raise HubError("CATALOG_STALE", "JSON 文件无效。") from error
            self._active(record)
            return data

    def prepare_authorized_download(self, authorization_code):
        files = self._files
        from .transfers import Download

        with files.operation():
            record, row, source = self._authorized_file(authorization_code)
            handle = Download(
                files, source, row["file_name"], record["sha256"], verify="AUTHORIZATION_HASH_MISMATCH"
            )
            try:
                self._active(record)
                return handle
            except BaseException:
                handle.close()
                raise
