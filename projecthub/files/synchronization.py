"""项目文件服务：实例拥有上传/下载，业务读写在同一数据库门内检查当前权限。"""

from projecthub.contracts import HubError, json_object
from projecthub.storage import HISTORY, hash_file, safe_path

from .rules import FIELDS, TABLES, utc_text


class FileSynchronizer:
    """磁盘目录扫描和文件登记。"""

    def __init__(self, files):
        self._files = files

    def synchronize(self, metadata=None):
        files = self._files
        with files.operation(), files.db.transaction():
            return self.scan({} if metadata is None else metadata, initial=False)

    def scan(self, metadata, *, initial):
        files = self._files
        from projecthub.storage import project_name

        if not isinstance(metadata, dict) or any(
            not isinstance(k, str) or not isinstance(v, dict) for k, v in metadata.items()
        ):
            raise HubError("INVALID_DATA", "metadata 须为路径到字段对象的映射。")
        root = files.settings.storage.object_dir
        seen, scanned = set(), set()
        connection = files.db.connection
        for model in sorted(root.iterdir()):
            if HISTORY.search(model.name):
                continue
            model = safe_path(root, model.name)
            if model.is_file():
                continue
            name = project_name(model.name)
            if not model.is_dir() or name != model.name:
                raise HubError("INVALID_PROJECT_NAME", "磁盘型号名无效。")
            connection.execute(
                "INSERT INTO project_models(name) VALUES(?) ON CONFLICT(name) DO NOTHING", (name,)
            )
            for version in sorted(model.iterdir()):
                if HISTORY.search(version.name):
                    continue
                version = safe_path(root, version.relative_to(root).as_posix())
                if version.name.casefold() == "模板.json" and version.is_file():
                    continue
                if not version.is_dir() or project_name(version.name) != version.name:
                    raise HubError("INVALID_PROJECT_NAME", "型号目录内须为版本目录。")
                connection.execute(
                    "INSERT INTO projects(name,version) VALUES(?,?) ON CONFLICT(name,version) DO NOTHING",
                    (name, version.name),
                )
                pid = files.db.one(
                    "SELECT id FROM projects WHERE name=? AND version=?", (name, version.name)
                )["id"]

                def walk(folder):
                    for item in sorted(folder.iterdir()):
                        if HISTORY.search(item.name):
                            continue
                        item = safe_path(root, item.relative_to(root).as_posix())
                        if item.is_dir():
                            yield from walk(item)
                        elif item.is_file():
                            yield item
                        else:
                            raise HubError("INVALID_UPLOAD", "扫描遇到非普通文件。")

                for path in walk(version):
                    relative = path.relative_to(root).as_posix()
                    parts = path.relative_to(root).parts
                    if len(parts) == 3 and path.name.casefold() == "config.json":
                        continue
                    if len(parts) < 4 or parts[2] not in TABLES:
                        raise HubError("INVALID_CATEGORY", "文件须位于四种分类目录。")
                    category = parts[2]
                    if initial and category in ("curve", "firmware"):
                        continue
                    key = (pid, path.name.casefold())
                    if key in seen:
                        raise HubError("FILE_ALREADY_EXISTS", "项目内有重复活动文件名。")
                    seen.add(key)
                    scanned.add(relative)
                    if path.suffix.lower() == ".json":
                        try:
                            json_object(path.read_bytes())
                        except (ValueError, UnicodeError, RecursionError) as error:
                            raise HubError("INVALID_UPLOAD", "扫描到无效 JSON。") from error
                    digest = hash_file(path)
                    table = TABLES[category]
                    old = files.db.one(f"SELECT * FROM {table} WHERE path=?", (relative,))
                    fields = metadata.get(relative, {})
                    allowed = FIELDS[category] | (
                        {"uploaded_at", "uploaded_by"} if category == "firmware" else set()
                    )
                    if not fields.keys() <= allowed or any(
                        not isinstance(value, str)
                        for key, value in fields.items()
                        if key != "supported_models"
                    ):
                        raise HubError("INVALID_DATA", "同步字段无效。")
                    if category in ("curve", "firmware"):
                        decorated = files._decorate(category, old) if old else {}
                        if (
                            old is None
                            or old["sha256"] != digest
                            or any(decorated.get(k) != v for k, v in fields.items())
                        ):
                            raise HubError("REVIEW_REQUIRED", "曲线和固件变更必须经过上传审核。")
                        if category == "curve":
                            connection.execute(
                                "UPDATE curve_configs SET modified_at=? WHERE id=?",
                                (utc_text(path.stat().st_mtime_ns), old["id"]),
                            )
                        continue
                    for other_kind, other_table in TABLES.items():
                        for candidate in files.db.query(
                            f"SELECT id,file_name FROM {other_table} WHERE project_id=?", (pid,)
                        ):
                            if candidate["file_name"].casefold() == path.name.casefold() and not (
                                old and other_kind == category and candidate["id"] == old["id"]
                            ):
                                raise HubError("FILE_ALREADY_EXISTS", "磁盘文件名与正式记录冲突。")
                    values = dict(
                        project_id=pid,
                        file_name=path.name,
                        sha256=digest,
                        path=relative,
                        file_type=path.suffix.lower(),
                        description=fields.get("description", old["description"] if old else ""),
                    )
                    if old:
                        connection.execute(
                            f"UPDATE {table} SET {','.join(k + '=?' for k in values)} WHERE id=?",
                            (*values.values(), old["id"]),
                        )
                    else:
                        connection.execute(
                            f"INSERT INTO {table}({','.join(values)}) VALUES({','.join('?' for _ in values)})",
                            tuple(values.values()),
                        )
        if metadata.keys() - scanned:
            raise HubError("INVALID_DATA", "metadata 包含未扫描到的路径。")
        return {"scanned": len(scanned), "written": len(scanned)}
