"""Compare the loaded public package with this fixed checkout, never authentication data."""

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import hoyo_roster

ROOT = Path(__file__).resolve().parents[1]


def verify(root: Path, installed: Path) -> dict:
    source = root / "src/hoyo_roster"
    required = [
        path
        for path in source.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    ]
    expected_inventory = {path.relative_to(source).as_posix() for path in required}
    actual_inventory = {
        path.relative_to(installed).as_posix()
        for path in installed.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    }
    unexpected = actual_inventory - expected_inventory
    mismatches = 0
    for path in required:
        target = installed / path.relative_to(source)
        if (
            not target.is_file()
            or hashlib.sha256(target.read_bytes()).digest()
            != hashlib.sha256(path.read_bytes()).digest()
        ):
            mismatches += 1
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, timeout=5
    )
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True, timeout=5
    )
    clean = status.returncode == 0 and not status.stdout
    matched = bool(required) and mismatches == 0 and not unexpected
    return {
        "source_sha": revision.stdout.strip() if revision.returncode == 0 else None,
        "source_worktree_clean": clean,
        "loaded_package": str(installed),
        "python_executable": sys.executable,
        "checked_package_files": len(required),
        "mismatched_files": mismatches,
        "unexpected_installed_files": len(unexpected),
        "installation_matches_source": matched,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--prepare-run", type=Path, help="New private log directory outside checkout"
    )
    args = parser.parse_args()
    result = verify(ROOT, Path(hoyo_roster.__file__).resolve().parent)
    passed = bool(
        result["source_worktree_clean"]
        and result["installation_matches_source"]
        and result["source_sha"]
    )
    if passed and args.prepare_run is not None:
        from hoyo_roster.user_workspace import private_directory

        path = args.prepare_run
        try:
            if (
                not path.is_absolute()
                or path.resolve().is_relative_to(ROOT.resolve())
                or path.exists()
                or path.is_symlink()
            ):
                raise ValueError()
            private_directory(path)
        except Exception:
            result["error_code"] = "UNSAFE_OR_EXISTING_RUN_DIRECTORY"
            passed = False
    print(json.dumps(result, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
