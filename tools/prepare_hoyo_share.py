#!/usr/bin/env python3
"""Prepare a source-only directory with a generated empty baseline, without Git history."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from hoyo_browser_session.repository_mutation.common import (
    copy_regular_file,
    tree_digest,
)
from hoyo_browser_session.user_workspace import (
    WorkspaceError,
    empty_workspace,
    validate_workspace,
)


def prepare(source: Path, output: Path) -> None:
    source = source.expanduser().resolve()
    output = output.expanduser().absolute()
    resolved = output.resolve()
    if (
        resolved.is_relative_to(source)
        or source.is_relative_to(resolved)
        or output.exists()
    ):
        raise WorkspaceError("SHARE_DESTINATION_MUST_BE_NEW_AND_EXTERNAL")
    # Build the baseline without reading data/** or traversing .git.
    empty_workspace(source, output)
    for name in ("README.md", "AGENTS.md", ".gitignore", "dependency-authority.toml"):
        copy_regular_file(source / name, output / name)
    for name in ("examples", "docs"):
        tree_digest(source / name)
        shutil.copytree(source / name, output / name)
    for skill in sorted((source / "skills").iterdir()):
        if not skill.is_dir() or skill.is_symlink():
            raise WorkspaceError("UNSAFE_SOURCE_TREE")
        target = output / "skills" / skill.name
        target.mkdir(parents=True)
        for name in ("pyproject.toml", "SKILL.md"):
            copy_regular_file(skill / name, target / name)
        tree_digest(skill / "src")
        shutil.copytree(
            skill / "src", target / "src", ignore=shutil.ignore_patterns("__pycache__")
        )
    for tool in (source / "tools").glob("*.py"):
        if tool.name != "validate_repository.py":
            copy_regular_file(tool, output / "tools" / tool.name)
    validate_workspace(output)
    # No license is chosen by this preparer. A failed preparation remains local for inspection.


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        prepare(args.source_root, args.output)
        print("SHARE_SOURCE=PREPARED\nPERSONAL_BASELINE=EMPTY\nGIT_HISTORY=EXCLUDED")
        return 0
    except WorkspaceError as error:
        print(f"SHARE_SOURCE=FAIL\nERROR_CODE={error}")
        return 1
    except Exception:
        print("SHARE_SOURCE=FAIL\nERROR_CODE=SHARE_PREPARATION_FAILED")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
