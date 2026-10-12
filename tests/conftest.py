from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))


@pytest.fixture
def source() -> Path:
    return ROOT


@pytest.fixture
def local_tool():
    spec = importlib.util.spec_from_file_location(
        "run_local_hoyo", ROOT / "tools/run_local_hoyo.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
