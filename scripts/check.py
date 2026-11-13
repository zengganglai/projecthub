"""运行 Ruff、字节编译及测试，使用临时字节码目录。"""

import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    sources = ("projecthub", "scripts", "tests", "main.py", "service.py")
    commands = (
        ("ruff", "check", "--no-cache", *sources),
        ("ruff", "format", "--check", "--no-cache", *sources),
        ("compileall", "-q", *sources),
        ("pytest", "-q", "-p", "no:cacheprovider"),
    )
    with TemporaryDirectory(prefix="projecthub-check-") as temporary:
        environment = {**os.environ, "PYTHONPYCACHEPREFIX": temporary}
        for command in commands:
            print(f"Running {' '.join(command)}", flush=True)
            result = subprocess.run([sys.executable, "-m", *command], cwd=root, env=environment, check=False)
            if result.returncode:
                return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
