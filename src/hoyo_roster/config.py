"""Account request settings and retained HSR diagnostic path checks."""

from dataclasses import dataclass, field
from pathlib import Path
from . import paths, platform_io as io


@dataclass(frozen=True)
class Settings:
    external_root: Path = field(default_factory=paths.default_private_root)

    @property
    def root(self) -> Path:
        return self.external_root.expanduser().absolute()


def resolve_private_profile_path(path: Path) -> Path:
    """Only resolve here; retained HSR diagnostics enforce profile path safety."""
    io.check_components(path)
    return path.expanduser().absolute().resolve(strict=False)
