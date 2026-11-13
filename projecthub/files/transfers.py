"""上传和下载句柄独占文件流，由 FileService 登记并在停止时释放。"""

import os
from threading import RLock

from projecthub.contracts import HubError, json_object
from projecthub.storage import file_identity, hash_stream, require_hash, safe_path


class Upload:
    def __init__(self, owner, actor, project_id, category, name, fields, digest, original):
        self.owner, self.actor, self.project_id = owner, dict(actor), project_id
        self.category, self.name, self.fields, self.digest, self.original = (
            category,
            name,
            fields,
            digest,
            original,
        )
        self.closed = self.finished = self.failed = False
        self.size = 0
        project = owner._project(project_id)
        self.relative = f"{project['name']}/{project['version']}/{category}/{name}"
        self.path = safe_path(owner.settings.storage.audit_dir, self.relative)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.stream = self.path.open("xb")
        except FileExistsError as error:
            raise HubError("REVIEW_ALREADY_PENDING", "已有同名待审或上传。") from error
        self.identity = None
        owner._uploads.add(self)
        try:
            self.identity = file_identity(self.path)
        except BaseException as error:
            self.failed = True
            try:
                self.close()
            except BaseException as cleanup:
                error.add_note("上传准备失败且资源未释放，文件服务保留句柄供关闭时重试。")
                raise error from cleanup
            raise

    def _usable(self):
        if self.closed or self.finished or self.failed:
            raise HubError("UPLOAD_CLOSED", "上传已经结束或失败。")

    def write(self, chunk: bytes):
        with self.owner.operation():
            self._usable()
            if not isinstance(chunk, bytes):
                raise HubError("INVALID_UPLOAD", "上传块必须是 bytes。")
            if self.size + len(chunk) > getattr(self.owner.settings.uploads, f"max_{self.category}_bytes"):
                self.failed = True
                raise HubError("UPLOAD_TOO_LARGE", "上传文件超过允许大小。")
            try:
                self.stream.write(chunk)
                self.size += len(chunk)
            except BaseException:
                self.failed = True
                raise

    def finish(self):
        with self.owner.operation():
            self._usable()
            try:
                self.stream.flush()
                os.fsync(self.stream.fileno())
                self.stream.close()
                require_hash(safe_path(self.owner.settings.storage.audit_dir, self.relative), self.digest)
                if (
                    self.category == "curve"
                    or self.category in ("internal", "public")
                    and self.name.lower().endswith(".json")
                ):
                    try:
                        json_object(self.path.read_bytes())
                    except (ValueError, UnicodeError, RecursionError) as error:
                        raise HubError("INVALID_UPLOAD", "JSON 文件须为有效 UTF-8 对象。") from error
                result = self.owner._finish_upload(self)
                self.finished = True
                return result
            except BaseException:
                self.failed = True
                raise

    def close(self):
        with self.owner.db.lock:
            if self.closed:
                return
            self.stream.close()
            if not self.finished:
                registered = self.owner.db.one(
                    "SELECT id FROM pending_reviews WHERE path=?", (self.relative,)
                )
                # 发布清单失败时由恢复负责暂存，不能先删除恢复所需字节。
                if registered is None and not self.owner.recovery_required:
                    path = safe_path(self.owner.settings.storage.audit_dir, self.relative)
                    if self.identity is not None and path.exists() and file_identity(path) == self.identity:
                        path.unlink()
            self.closed = True
            self.owner._uploads.discard(self)


class Download:
    def __init__(self, owner, path, name, digest, *, verify=None):
        self.owner = owner
        self.file_name, self.sha256 = name, digest
        self._lock = RLock()
        self.closed = False
        self._exhausted = False
        try:
            self._stream = path.open("rb")
        except OSError as error:
            raise HubError("FILE_NOT_FOUND", "文件无法读取。") from error
        owner._downloads.add(self)
        try:
            self.size = os.fstat(self._stream.fileno()).st_size
            self._remaining = self.size
            if verify:
                if hash_stream(self._stream) != digest:
                    raise HubError(verify, "文件原字节摘要不符。")
                self._stream.seek(0)
        except BaseException:
            self.close()
            raise

    def read_chunk(self, size=1048576):
        if type(size) is not int or not 1 <= size <= 1048576:
            raise HubError("INVALID_DOWNLOAD_CHUNK_SIZE", "读取块大小须为 1～1048576。")
        with self._lock:
            if self._exhausted:
                return b""
            if self.closed:
                raise HubError("DOWNLOAD_CLOSED", "下载已关闭。")
            if self._remaining == 0:
                self._exhausted = True
                self.close()
                return b""
            try:
                data = self._stream.read(min(size, self._remaining))
                if not data:
                    raise HubError("DOWNLOAD_INTERRUPTED", "下载文件提前结束。")
                self._remaining -= len(data)
                return data
            except BaseException:
                self.close()
                raise

    def close(self):
        with self._lock:
            if self.closed:
                return
            self._stream.close()
            self.closed = True
        with self.owner.db.lock:
            self.owner._downloads.discard(self)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()
