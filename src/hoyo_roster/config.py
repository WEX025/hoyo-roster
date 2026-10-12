"""Identity-scoped settings; no daemon, queue, runtime or database."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    external_root: Path = Path("~/.local/share/hoyo-roster")
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
    return path.expanduser().absolute().resolve(strict=False)
