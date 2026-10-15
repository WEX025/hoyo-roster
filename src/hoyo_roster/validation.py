"""Bound, privacy-safe invocation of the standalone repository validator."""

from __future__ import annotations
import hashlib
import subprocess
import sys
from pathlib import Path
from .jsonio import StrictJsonError, require_real_path_chain


CONTRACT_ROOT = Path(__file__).resolve().parent / "contracts"


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
        # Windows CreateProcess rejects a long extended-path working directory.
        # All validator paths are absolute; use the known interpreter directory.
        result = subprocess.run(
            command, cwd=Path(sys.executable).parent, capture_output=True, check=False, timeout=90
        )
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


def copy_game_workspace(source: Path, destination: Path, game: str) -> None:
    """Copy only one game's public contracts/data; never inspect another game."""
    import shutil
    from .storage.common import copy_regular_file, tree_digest

    if game not in {"genshin-impact", "honkai-star-rail", "zenless-zone-zero"}:
        raise ValueError("INVALID_GAME")
    destination.mkdir(parents=True, exist_ok=True)
    for relative in (Path("schemas/common"), Path("schemas") / game, Path("data") / game):
        tree_digest(source / relative)
        (destination / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source / relative, destination / relative)
    for relative in (
        Path("schemas/repository-manifest.schema.json"),
        Path("data/manifest.json"),
        Path("tools/validate_repository.py"),
    ):
        (destination / relative).parent.mkdir(parents=True, exist_ok=True)
        copy_regular_file(source / relative, destination / relative)


def validate_game_source(root: Path, game: str) -> None:
    """Project a full public checkout or own generation into a strict game contract."""
    import tempfile
    from . import platform_io as io

    with tempfile.TemporaryDirectory(prefix="hoyo-contract-", dir=io.temporary_root()) as temporary:
        projected = Path(temporary) / "workspace"
        copy_game_workspace(root, projected, game)
        run_validator(projected, scope_game=game)
