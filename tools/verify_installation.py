"""Compare the loaded public package with this fixed checkout, never authentication data."""

import argparse
import json
from pathlib import Path

import hoyo_roster
from hoyo_roster.installation import verify as verify

ROOT = Path(__file__).resolve().parents[1]


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
