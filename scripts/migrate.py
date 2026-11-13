"""初始化或检查当前项目数据库。"""

from scripts.maintenance import run_maintenance


def main(argv: list[str] | None = None) -> int:
    return run_maintenance(synchronize=False, argv=argv)


if __name__ == "__main__":
    raise SystemExit(main())
