"""项目文件服务：实例拥有上传/下载，业务读写在同一数据库门内检查当前权限。"""

from hashlib import sha256

from projecthub.contracts import HubError, json_object
from projecthub.storage import safe_path

from .rules import TABLES, version_key, version_range


class FileCatalog:
    """文件查询和读取；资源由所属文件服务持有。"""

    def __init__(self, files):
        self._files = files

    def list_files(
        self,
        *,
        project_id=None,
        category=None,
        file_name=None,
        file_type=None,
        q=None,
        sha256=None,
        path=None,
        actor=None,
    ):
        files = self._files
        with files.operation():
            if actor is not None:
                files.permissions.require_file_download(actor, category, project_id)
            filters = {"file_name": file_name, "sha256": sha256, "path": path}
            if any(
                value is not None and not isinstance(value, str)
                for value in (*filters.values(), file_type, q)
            ):
                raise HubError("INVALID_FILTER", "筛选值须为字符串。")
            kinds = [category] if category is not None else list(TABLES)
            records = []
            for kind in kinds:
                table = files._table(kind)
                clause, params = (
                    (" WHERE project_id=?", (project_id,)) if project_id is not None else ("", ())
                )
                for raw in files.db.query(f"SELECT * FROM {table}{clause}", params):
                    row = files._decorate(kind, raw)
                    type_key = {"curve": "curve_type", "firmware": "firmware_type"}.get(kind, "file_type")
                    if any(value is not None and row[key] != value for key, value in filters.items()):
                        continue
                    if file_type is not None and row[type_key] != file_type:
                        continue
                    searchable = [
                        row["project"],
                        row["project_version"],
                        row["file_name"],
                        row[type_key],
                        row.get("description", row.get("release_notes", "")),
                    ]

                    # SQLite LIKE 的 ASCII 不分大小写语义，非 ASCII 保持精确。
                    def fold_ascii(text):
                        return text.translate(
                            str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
                        )

                    if q and not any(fold_ascii(q) in fold_ascii(text) for text in searchable):
                        continue
                    records.append(row)
            key = (
                (lambda row: (row["project"], row["project_version"], row["sort_order"], row["id"]))
                if category == "curve"
                else (lambda row: (row["path"], row["category"], row["id"]))
            )
            return {"files": sorted(records, key=key), "count": len(records)}

    def check_firmware(self, project_id, current_version, *, actor=None):
        files = self._files
        with files.operation():
            if actor is not None:
                files.permissions.require_file_download(actor, "firmware", project_id)
            current = version_key(current_version)
            result = []
            records = self.list_files(project_id=project_id, category="firmware")["files"]
            files._validate_ranges(records)
            for row in records:
                interval = version_range(row["min_version"], row["max_version"])
                if (
                    interval
                    and interval[0] <= current <= interval[1]
                    and version_key(row["version"]) > current
                ):
                    result.append(row)
            return {
                "update_available": bool(result),
                "firmwares": sorted(result, key=lambda r: (r["firmware_type"], r["id"])),
            }

    def read_json(self, file_id, category, project_id, *, actor=None):
        files = self._files
        with files.operation():
            if actor is not None:
                files.permissions.require_file_download(actor, category, project_id)
            row = files._record(category, file_id, project_id)
            if not row["file_name"].lower().endswith(".json"):
                raise HubError("NOT_JSON_FILE", "此文件不是 JSON，请下载原始文件。")
            try:
                content = files._formal_path(row).read_bytes()
            except OSError as error:
                raise HubError("FILE_NOT_FOUND", "文件无法读取。") from error
            if sha256(content).hexdigest() != row["sha256"]:
                raise HubError("CATALOG_STALE", "文件已发生变化。")
            try:
                return json_object(content)
            except (ValueError, UnicodeError, RecursionError) as error:
                raise HubError("CATALOG_STALE", "JSON 文件无效。") from error

    def _read_fixed(self, relative, code):
        files = self._files
        try:
            path = safe_path(files.settings.storage.object_dir, relative)
            content = path.read_bytes()
        except FileNotFoundError as error:
            raise HubError(code + "_NOT_FOUND", "文件缺失。") from error
        except HubError as error:
            raise HubError(code + "_PATH_INVALID", "文件路径不安全。") from error
        except OSError as error:
            raise HubError(code + "_READ_ERROR", "文件无法读取。") from error
        try:
            return json_object(content)
        except (ValueError, UnicodeError, RecursionError) as error:
            raise HubError(code + "_INVALID_JSON", "文件须为 UTF-8 JSON 对象。") from error

    def read_project_config(self, project_id, *, actor=None):
        """在文件门内重查型号授权，按稳定版本ID定位当前配置路径。"""
        files = self._files
        with files.operation():
            row = files._project(project_id)
            if actor is not None and row["name"] not in files.permissions.visible_models(actor):
                raise HubError("PROJECT_NOT_FOUND", "型号或所属版本不存在。")
            return self._read_fixed(f"{row['name']}/{row['version']}/config.json", "PROJECT_CONFIG")

    def read_curve_template(self, project_id, *, actor=None):
        files = self._files
        with files.operation():
            if actor is not None:
                files.permissions.require_file_download(actor, "curve", project_id)
            row = files._project(project_id)
            return self._read_fixed(f"{row['name']}/{row['version']}/curve/模板.json", "TEMPLATE")

    def prepare_download(self, file_id, category, project_id, *, actor=None):
        files = self._files
        from .transfers import Download

        with files.operation():
            if actor is not None:
                files.permissions.require_file_download(actor, category, project_id)
            row = files._record(category, file_id, project_id)
            return Download(files, files._formal_path(row), row["file_name"], row["sha256"])
