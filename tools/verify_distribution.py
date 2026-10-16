"""Check real nonempty wheel/sdist resources, also under python -O."""

from __future__ import annotations
import argparse
import hashlib
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class DistributionError(ValueError):
    pass


def require(condition: bool, code: str) -> None:
    if not condition:
        raise DistributionError(code)


def verify(dist: Path) -> int:
    wheels = sorted(dist.glob("*.whl"))
    sdists = sorted(dist.glob("*.tar.gz"))
    require(bool(wheels), "WHEEL_MISSING")
    require(bool(sdists), "SDIST_MISSING")
    required = {
        p.relative_to(ROOT / "src").as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (ROOT / "src/hoyo_roster").rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
    }
    require(bool(required), "PACKAGE_EMPTY")
    for wheel in wheels:
        require(wheel.is_file() and wheel.stat().st_size > 0, "WHEEL_EMPTY")
        with zipfile.ZipFile(wheel) as archive:
            names = set(archive.namelist())
            actual_package = {
                name for name in names if name.startswith("hoyo_roster/") and not name.endswith("/")
            }
            require(actual_package == set(required), "WHEEL_PACKAGE_INVENTORY_DRIFT")
            for name, digest in required.items():
                require(name in names, "WHEEL_RESOURCE_MISSING")
                require(
                    hashlib.sha256(archive.read(name)).hexdigest() == digest, "WHEEL_RESOURCE_DRIFT"
                )
            require(
                any(n.endswith(".dist-info/licenses/LICENSE") for n in names),
                "WHEEL_LICENSE_MISSING",
            )
            for source in (ROOT / "third_party").iterdir():
                matches = [n for n in names if n.endswith("/licenses/third_party/" + source.name)]
                require(len(matches) == 1, "WHEEL_NOTICE_MISSING")
                require(archive.read(matches[0]) == source.read_bytes(), "WHEEL_NOTICE_DRIFT")
            require(
                not any(
                    "chromium" in n.lower() or "hoyo_browser_session" in n or "state.sqlite3" in n
                    for n in names
                ),
                "WHEEL_PRIVATE_RESOURCE",
            )
            require(not any(n.startswith("data/") for n in names), "WHEEL_PERSONAL_DATA")
        print("WHEEL_RESOURCES=PASS")
    for sdist in sdists:
        require(sdist.is_file() and sdist.stat().st_size > 0, "SDIST_EMPTY")
        with tarfile.open(sdist) as archive:
            members = {name.partition("/")[2]: name for name in archive.getnames()}
            actual_package = {
                name.removeprefix("src/")
                for name in members
                if name.startswith("src/hoyo_roster/") and archive.getmember(members[name]).isfile()
            }
            require(actual_package == set(required), "SDIST_PACKAGE_INVENTORY_DRIFT")
            for name, digest in required.items():
                path = "src/" + name
                require(path in members, "SDIST_RESOURCE_MISSING")
                stream = archive.extractfile(members[path])
                require(stream is not None, "SDIST_RESOURCE_NOT_FILE")
                require(hashlib.sha256(stream.read()).hexdigest() == digest, "SDIST_RESOURCE_DRIFT")
            for path in [
                "LICENSE",
                "THIRD_PARTY_NOTICES.md",
                "AGENTS.md",
                "CLAUDE.md",
                ".agents/skills/local-roster/SKILL.md",
                "docs/timed-collection.md",
                "tools/timed_collection.mjs",
                "tools/analyze_collection.py",
                "tools/verify_installation.py",
                "data/.gitkeep",
            ]:
                require(path in members, "SDIST_PUBLIC_RESOURCE_MISSING")
            require(
                {p for p in members if p.startswith("data/")} == {"data/.gitkeep"},
                "SDIST_PERSONAL_DATA",
            )
        print("SDIST_RESOURCES=PASS")
    print(f"PACKAGE_RESOURCE_COUNT={len(required)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist-dir", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    try:
        return verify(args.dist_dir)
    except (DistributionError, OSError, ValueError, tarfile.TarError, zipfile.BadZipFile) as error:
        print("DISTRIBUTION=FAIL")
        print(f"ERROR_CODE={error if isinstance(error, DistributionError) else 'ARCHIVE_INVALID'}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
