"""Private owned-game identity, verified by the corresponding game module."""

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True, repr=False)
class OwnedRole:
    game: str
    uid: str = field(repr=False)
    region: str
