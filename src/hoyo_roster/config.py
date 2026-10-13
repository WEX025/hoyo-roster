"""Identity-scoped settings; no daemon, queue, runtime or database."""

from dataclasses import dataclass, field
from pathlib import Path
from . import paths, platform_io as io


@dataclass(frozen=True)
class Settings:
    external_root: Path = field(default_factory=paths.default_private_root)
    login_probe_interval_seconds: float = 2.0
    login_timeout_seconds: float = 900.0
    test_mode: bool = False

    @property
    def root(self) -> Path:
        return self.external_root.expanduser().absolute()


def production_settings() -> Settings:
    return Settings()


def resolve_private_profile_path(path: Path) -> Path:
    """Only resolve here; game launchers and store enforce path safety."""
    io.check_components(path)
    return path.expanduser().absolute().resolve(strict=False)
