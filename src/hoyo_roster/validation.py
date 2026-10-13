"""Bound, privacy-safe invocation of the standalone repository validator."""

from __future__ import annotations
import hashlib
import subprocess
import sys
from pathlib import Path
from .jsonio import StrictJsonError, require_real_path_chain


class ValidationFailure(ValueError):
    def __init__(self, code: str, diagnostic: str) -> None:
        super().__init__(code)
        self.code = code
        self.diagnostic = diagnostic


def run_validator(
    root: Path, *, scope_game: str | None = None, validator_root: Path | None = None
) -> None:
    try:
        validator = require_real_path_chain(
            validator_root or root, Path("tools/validate_repository.py"), final_kind="regular_file"
        )
        for directory in ("schemas", "data"):
            require_real_path_chain(root, Path(directory), final_kind="directory")
        command = [sys.executable, str(validator), "--root", str(root)]
        if scope_game is not None:
            command.extend(("--scope-game", scope_game))
        result = subprocess.run(command, cwd=root, capture_output=True, check=False, timeout=90)
    except subprocess.TimeoutExpired:
        raise ValidationFailure(
            "VALIDATOR_TIMEOUT", hashlib.sha256(b"validator-timeout").hexdigest()
        ) from None
    except (OSError, StrictJsonError):
        raise ValidationFailure(
            "VALIDATOR_UNAVAILABLE", hashlib.sha256(b"unsafe-validator").hexdigest()
        ) from None
    if result.returncode:
        raise ValidationFailure(
            "VALIDATOR_REJECTED", hashlib.sha256(result.stdout + b"\0" + result.stderr).hexdigest()
        )
