from pathlib import Path
import pytest
from hoyo_roster import cli, paths, platform_io as io

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def private_test_home(tmp_path):
    # Never create or reuse real local authentication state in synthetic tests.
    if io.WINDOWS:
        # mkdir(mode=0700) does not set a Windows DACL. Restrict this newly
        # generated fixture root so manually built synthetic children inherit it.
        io.set_private(tmp_path)
    with pytest.MonkeyPatch.context() as environment:
        environment.setattr(paths, "default_private_root", lambda: tmp_path / "home" / "private")
        yield


@pytest.fixture(autouse=True)
def native_symlink_capability(monkeypatch):
    if not io.WINDOWS:
        return
    original = Path.symlink_to

    def symlink(path, *args, **kwargs):
        try:
            return original(path, *args, **kwargs)
        except OSError as error:
            if error.winerror == 1314:
                pytest.skip("Windows native symlink privilege unavailable")
            raise

    monkeypatch.setattr(Path, "symlink_to", symlink)


@pytest.fixture
def source():
    return cli.CONTRACT_ROOT


@pytest.fixture
def local_tool():
    return cli
