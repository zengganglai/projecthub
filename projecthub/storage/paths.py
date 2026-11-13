"""受管目录与路径边界。"""

import ntpath
import re
from pathlib import Path

from projecthub.contracts import HubError

HISTORY = re.compile(
    r"-(?:del|mod)_[0-9]+|_(?:del|mod|reject)_[0-9]{14}(?:_[0-9]+)?(?:\.|$)|_del[0-9]+(?:_[0-9]+)?$", re.I
)


def reject_links(path):
    for part in (path, *path.parents):
        if part.is_symlink() or part.is_junction():
            raise ValueError("运行路径不能经过链接或目录联接。")


def safe_path(root: Path, relative: str) -> Path:
    if (
        not isinstance(relative, str)
        or not relative
        or relative.startswith("/")
        or any(c in relative for c in "\\:")
    ):
        raise HubError("INVALID_UPLOAD", "文件路径无效。")
    parts = relative.split("/")
    if any(p in ("", ".", "..") or ntpath.isreserved(p) for p in parts):
        raise HubError("INVALID_UPLOAD", "文件路径无效。")
    result = root.joinpath(*parts)
    try:
        reject_links(result)
        if root.resolve() != root or result.resolve() != result or not result.is_relative_to(root):
            raise ValueError("路径越界。")
    except (OSError, ValueError, RuntimeError) as error:
        raise HubError("INVALID_UPLOAD", "文件路径不安全。") from error
    return result


def project_name(value):
    if not isinstance(value, str):
        raise HubError("INVALID_PROJECT_NAME", "型号与版本必须是字符串。")
    name = value.strip()
    if (
        not 1 <= len(name) <= 128
        or name in (".", "..")
        or any(c in name for c in "/\\:")
        or ntpath.isreserved(name)
        or HISTORY.search(name)
    ):
        raise HubError("INVALID_PROJECT_NAME", "型号与版本须为合法的单级目录名。")
    return name
