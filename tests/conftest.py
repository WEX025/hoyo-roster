from pathlib import Path
import pytest
from hoyo_roster import cli

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def private_test_home(tmp_path):
    # Never create or reuse real local authentication state in synthetic tests.
    with pytest.MonkeyPatch.context() as environment:
        environment.setenv("HOME", str(tmp_path / "home"))
        yield


@pytest.fixture
def source():
    return cli.CONTRACT_ROOT


@pytest.fixture
def local_tool():
    return cli
