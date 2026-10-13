"""Check exact wheel/sdist resources without launching a browser or logging in."""

from __future__ import annotations
import hashlib
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
package = ROOT / "src/hoyo_roster"
required = {
    p.relative_to(ROOT / "src").as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
    for p in package.rglob("*")
    if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
}
for wheel in (ROOT / "dist").glob("*.whl"):
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        for name, digest in required.items():
            assert name in names, name
            assert hashlib.sha256(archive.read(name)).hexdigest() == digest, name
        assert any(name.endswith(".dist-info/licenses/LICENSE") for name in names)
        for source in (ROOT / "third_party").iterdir():
            name = next(n for n in names if n.endswith("/licenses/third_party/" + source.name))
            assert archive.read(name) == source.read_bytes()
        assert not any(
            "chromium" in n.lower() or "hoyo_browser_session" in n or "state.sqlite3" in n
            for n in names
        )
        assert not any(name.startswith("data/") for name in names)
    print("WHEEL_RESOURCES=PASS")
for sdist in (ROOT / "dist").glob("*.tar.gz"):
    with tarfile.open(sdist) as archive:
        members = {name.partition("/")[2]: name for name in archive.getnames()}
        for name, digest in required.items():
            path = "src/" + name
            assert path in members, path
            assert (
                hashlib.sha256(archive.extractfile(members[path]).read()).hexdigest() == digest
            ), path
        for path in [
            "LICENSE",
            "THIRD_PARTY_NOTICES.md",
            "AGENTS.md",
            "CLAUDE.md",
            ".agents/skills/local-roster/SKILL.md",
            "data/.gitkeep",
        ]:
            assert path in members, path
        assert {path for path in members if path.startswith("data/")} == {"data/.gitkeep"}
    print("SDIST_RESOURCES=PASS")
print(f"PACKAGE_RESOURCE_COUNT={len(required)}")
