"""Reject Git-tracked personal output; inspect path names only, never file contents."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(args.root),
                "ls-files",
                "--full-name",
                "--cached",
                "-z",
                "--",
                ":(top)data",
            ],
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        result = None
    if result is None or result.returncode:
        print("PRIVATE_DATA_CHECK=ERROR\n提示=无法读取 Git 索引，检查仓库路径。")
        return 2
    forbidden = [name for name in result.stdout.split(b"\0") if name and name != b"data/.gitkeep"]
    if forbidden:
        print(
            f"PRIVATE_DATA_CHECK=FAIL\nTRACKED_DATA_COUNT={len(forbidden)}\n"
            "提示=data/ 只能跟踪 .gitkeep；取消跟踪个人结果并保留本机文件后再提交。"
        )
        return 1
    print("PRIVATE_DATA_CHECK=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
