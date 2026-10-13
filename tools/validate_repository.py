#!/usr/bin/env python3
"""Validate a specified workspace, or the shipped empty public contracts."""

import runpy
import sys
from hoyo_roster.cli import CONTRACT_ROOT

if "--root" not in sys.argv and "--self-test" not in sys.argv:
    sys.argv.extend(["--root", str(CONTRACT_ROOT)])
runpy.run_path(str(CONTRACT_ROOT / "tools/validate_repository.py"), run_name="__main__")
