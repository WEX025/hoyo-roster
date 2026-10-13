from pathlib import Path
import pytest
from hoyo_roster import cli, paths

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def private_test_home(tmp_path):
    # Never create or reuse real local authentication state in synthetic tests.
    with pytest.MonkeyPatch.context() as environment:
        environment.setattr(paths, "default_private_root", lambda: tmp_path / "home" / "private")
        yield


@pytest.fixture
def source():
    return cli.CONTRACT_ROOT


@pytest.fixture
def local_tool():
    return cli
