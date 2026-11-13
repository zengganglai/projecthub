"""离线扫描和同步项目文件。"""

from scripts.maintenance import run_maintenance


def main(argv: list[str] | None = None) -> int:
    return run_maintenance(synchronize=True, argv=argv)


if __name__ == "__main__":
    raise SystemExit(main())
