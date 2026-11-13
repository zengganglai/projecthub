"""文件摘要、持久写入和独占复制。"""

import hashlib
import json
import os
import shutil

from projecthub.contracts import HubError

from .paths import reject_links


def hash_stream(stream):
    result = hashlib.sha256()
    while chunk := stream.read(1024 * 1024):
        result.update(chunk)
    return result.hexdigest()


def hash_file(path):
    with path.open("rb") as stream:
        return hash_stream(stream)


def require_hash(path, digest, code="UPLOAD_HASH_MISMATCH"):
    try:
        actual = hash_file(path)
    except OSError as error:
        raise HubError(code, "文件无法读取或已发生变化。") from error
    if actual != digest:
        raise HubError(code, "文件 SHA-256 与记录不符。")


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as output:
        json.dump(value, output, ensure_ascii=False, sort_keys=True, allow_nan=False)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def durable_copy(source, destination):
    """独占创建副本；复制失败只移除本次创建且身份未变化的目标。"""
    owned = None
    try:
        with source.open("rb") as incoming, destination.open("xb") as outgoing:
            stat = os.fstat(outgoing.fileno())
            owned = [stat.st_dev, stat.st_ino]
            shutil.copyfileobj(incoming, outgoing, 1024 * 1024)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        shutil.copystat(source, destination)
    except BaseException:
        if owned is not None and destination.exists() and file_identity(destination) == owned:
            destination.unlink()
        raise


def file_identity(path):
    info = path.stat()
    return [info.st_dev, info.st_ino]


def copy_tree(source, target):
    """目标必须尚不存在；复制过程逐条拒绝链接，包含历史及未登记文件。"""
    reject_links(source)
    target.mkdir()
    for item in source.iterdir():
        reject_links(item)
        destination = target / item.name
        if item.is_dir():
            copy_tree(item, destination)
        elif item.is_file():
            durable_copy(item, destination)
        else:
            raise HubError("PROJECT_PATH_CONFLICT", "项目存在非普通文件。")
