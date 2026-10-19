"""Private request-scheduling metadata, with no browser authentication material."""

from dataclasses import dataclass, field
from pathlib import Path
from . import paths


@dataclass(frozen=True)
class Settings:
    external_root: Path = field(default_factory=paths.default_private_root)

    @property
    def root(self) -> Path:
        return self.external_root.expanduser().absolute()
