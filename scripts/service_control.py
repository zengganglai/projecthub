"""Windows 项目服务启停与状态查询命令。"""

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

from projecthub.application import AppConfig
from projecthub.contracts import HubError
from projecthub.runtime import service_status, start_service, stop_service


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ProjectHub 项目专属服务控制")
    parser.add_argument("command", choices=("start", "stop", "status"))
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args(argv)
    try:
        settings = AppConfig() if args.project_root is None else AppConfig(project_root=args.project_root)
        http = settings.http
        settings = replace(
            settings,
            http=replace(
                http,
                bind_host=http.bind_host if args.host is None else args.host,
                port=http.port if args.port is None else args.port,
            ),
        )
        if args.command == "start":
            status = start_service(settings, timeout=args.timeout)
        elif args.command == "stop":
            status = stop_service(settings, timeout=args.timeout)
        else:
            status = service_status(settings)
        print(json.dumps(status, ensure_ascii=False))
        return 0
    except (HubError, OSError) as error:
        code = error.code if isinstance(error, HubError) else "SERVICE_CONTROL_FAILED"
        message = error.message if isinstance(error, HubError) else "无法访问项目运行记录。"
        print(f"{code}: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
