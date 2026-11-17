"""离线建库、结构检查和文件同步。"""

import argparse
import json
import sys
from contextlib import ExitStack
from dataclasses import asdict
from pathlib import Path

from projecthub.application import AppConfig, resolve_settings
from projecthub.contracts import HubError, decode_json
from projecthub.database import Database
from projecthub.files import FileService
from projecthub.permissions import Permissions
from projecthub.projects import ProjectService
from projecthub.storage import RuntimeLock
from projecthub.users import UserStore


def _metadata(path: Path | None) -> dict:
    if path is None:
        return {}
    metadata = decode_json(path.read_text(encoding="utf-8-sig"))
    if not isinstance(metadata, dict):
        raise ValueError("metadata 必须是对象。")
    return metadata


def run_maintenance(*, synchronize: bool, argv: list[str] | None = None) -> int:
    description = "ProjectHub 手动同步文件目录" if synchronize else "ProjectHub 数据库初始化与检查"
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--project-root", type=Path, help="运行数据所属的项目根目录")
    if synchronize:
        parser.add_argument("--metadata", type=Path, help="以相对文件路径为键的 JSON 对象")
    args = parser.parse_args(argv)

    try:
        metadata = _metadata(args.metadata) if synchronize else None
        supplied = AppConfig() if args.project_root is None else AppConfig(project_root=args.project_root)
        settings = resolve_settings(supplied)
        with RuntimeLock(settings), ExitStack() as cleanup:
            db = Database(settings)
            user_store = UserStore(db, settings.users.max_users)
            permissions = Permissions(db, user_store)
            files = FileService(settings, db, permissions)
            projects = ProjectService(settings, db, permissions, files)
            cleanup.callback(db.close)
            cleanup.callback(user_store.close)
            cleanup.callback(files.close)
            db.open(initialize=files.scan_initial)
            user_store.initialize()
            permissions.validate()
            projects.initialize()
            files.initialize()
            report = files.synchronizer.synchronize(metadata) if synchronize else asdict(db.migration_report)
        print(json.dumps({"status": "ok", **report}, ensure_ascii=False, default=str))
        return 0
    except (HubError, OSError, ValueError, RecursionError) as error:
        code = error.code if isinstance(error, HubError) else "MAINTENANCE_FAILED"
        print(f"{code}: 命令未完成，请检查配置、输入及资源状态。", file=sys.stderr)
        return 1
