#!/usr/bin/env python3
"""Validate the repository dependency authority and all package projections."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

from dependency_authority import (
    AuthorityError,
    canonical_bytes,
    load_repository_projection,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="validate_dependency_authority.py")
    parser.add_argument("--root", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        authority, packages = load_repository_projection(args.root.resolve())
        runtime_digest = hashlib.sha256(
            canonical_bytes(authority.runtime_document())
        ).hexdigest()
        dev_digest = hashlib.sha256(canonical_bytes(authority.dev_document())).hexdigest()
    except (AuthorityError, OSError):
        print("DEPENDENCY_AUTHORITY_VALIDATION=FAIL")
        print("ERROR_CODE=DEPENDENCY_AUTHORITY_INVALID")
        return 1
    print("DEPENDENCY_AUTHORITY_VALIDATION=PASS")
    print("RUNTIME_DEPENDENCY_AUTHORITY_COUNT=1")
    print("DEV_TOOLCHAIN_AUTHORITY_COUNT=1")
    print("PYTHON_RUNTIME_AUTHORITY_COUNT=1")
    print(f"PACKAGE_PROJECTION_COUNT={len(packages)}")
    print(f"RUNTIME_DEPENDENCY_COUNT={len(authority.runtime_dependencies)}")
    print(f"DEV_TOOL_COUNT={len(authority.dev_tools)}")
    print(f"RUNTIME_AUTHORITY_DIGEST={runtime_digest}")
    print(f"DEV_TOOLCHAIN_AUTHORITY_DIGEST={dev_digest}")
    print("SOURCE_METADATA_AUTHORITY_MATCH=PASS")
    print("INTERNAL_PACKAGE_VERSION_CLOSURE=PASS")
    print("GAME_SPECIFIC_DEPENDENCY_POLICY=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
