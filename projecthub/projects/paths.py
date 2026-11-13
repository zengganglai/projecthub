"""项目目录边界与大小写一致性检查。"""

from projecthub.contracts import HubError
from projecthub.storage import safe_path


def project_path(root, relative, *, code="PROJECT_PATH_CONFLICT"):
    try:
        return safe_path(root, relative)
    except HubError as error:
        raise HubError(code, "项目目录路径不安全。") from error


def directory(path):
    return path.is_dir() and path.resolve(strict=True).name == path.name
