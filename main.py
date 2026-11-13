"""ProjectHub CLI 和 main:app ASGI 入口。"""

import argparse
import sys
from dataclasses import replace
from pathlib import Path

from projecthub.application import AppConfig, run
from projecthub.contracts import HubError
from projecthub.runtime import run_managed
from service import app as app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ProjectHub 文件与设备服务")
    parser.add_argument("--host", help="HTTP 监听地址")
    parser.add_argument("--port", type=int, help="HTTP 监听端口")
    parser.add_argument("--project-root", type=Path, help="运行数据所属的项目根目录")
    parser.add_argument("--managed", action="store_true", help="接受本项目的受管停止通知")
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
        runner = run_managed if args.managed else run
        runner(settings)
    except HubError as error:
        print(f"{error.code}: {error.message}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
