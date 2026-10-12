from pathlib import Path
import pytest
from hoyo_roster import cli

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def source():
    return cli.CONTRACT_ROOT


@pytest.fixture
def local_tool():
    return cli
