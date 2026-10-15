"""Compare the loaded public package with this fixed checkout, never authentication data."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import hoyo_roster

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    installed = Path(hoyo_roster.__file__).resolve().parent
    source = ROOT / "src/hoyo_roster"
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
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, timeout=5
    )
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True, timeout=5
    )
    clean = status.returncode == 0 and not status.stdout
    matched = bool(required) and mismatches == 0 and not unexpected
    result = {
        "source_sha": revision.stdout.strip() if revision.returncode == 0 else None,
        "source_worktree_clean": clean,
        "loaded_package": str(installed),
        "python_executable": sys.executable,
        "checked_package_files": len(required),
        "mismatched_files": mismatches,
        "unexpected_installed_files": len(unexpected),
        "installation_matches_source": matched,
    }
    print(json.dumps(result, indent=2))
    return 0 if clean and matched and result["source_sha"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
