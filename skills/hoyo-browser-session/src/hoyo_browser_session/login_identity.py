"""Machine-local verified login identity fingerprints."""

from __future__ import annotations

import hashlib
import hmac
import sqlite3
import string
import time
from dataclasses import dataclass, field

from .config import SUPPORTED_GAMES, Settings
from .db import Database
from .errors import BrokerDatabaseError, ExpectedIdentityConflict
from .parameter_fingerprint import ensure_parameter_hmac_key

_SOURCE = "VERIFIED_BINDING"
_DOMAIN = "login-identity:v1"


def _validate_game(game: str) -> None:
    if game not in SUPPORTED_GAMES:
        raise ValueError("login identity game was not registered")


def _validate_digest(value: str, *, label: str) -> None:
    if (
        len(value) != 64
        or value != value.lower()
        or any(character not in string.hexdigits for character in value)
    ):
        raise ValueError(f"{label} was invalid")


def login_identity_fingerprint(
    settings: Settings,
    *,
    game: str,
    identity_digest: str,
) -> str:
    """HMAC an in-memory identity digest with fixed domain separation."""

    _validate_game(game)
    _validate_digest(identity_digest, label="login identity digest")
    key = ensure_parameter_hmac_key(settings)
    document = f"{_DOMAIN}:{game}:{identity_digest}".encode()
    return hmac.new(key, document, hashlib.sha256).hexdigest()


@dataclass(frozen=True, slots=True)
class VerifiedLoginIdentity:
    game: str
    fingerprint: str = field(repr=False)
    source: str
    verified_at: float
    updated_at: float

    def __post_init__(self) -> None:
        _validate_game(self.game)
        _validate_digest(self.fingerprint, label="login identity fingerprint")
        if self.source != _SOURCE:
            raise ValueError("login identity source was invalid")
        if self.verified_at <= 0 or self.updated_at <= 0:
            raise ValueError("login identity timestamp was invalid")


class LoginIdentityStore:
    """Persist only one verified HMAC fingerprint per fixed game."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.database = Database(settings)
        self.database.initialize()

    def read(self, game: str) -> VerifiedLoginIdentity | None:
        _validate_game(game)
        try:
            with self.database.connection() as connection:
                row = connection.execute(
                    """
                    SELECT game, identity_fingerprint, source, verified_at, updated_at
                    FROM login_identity_state
                    WHERE game = ?
                    """,
                    (game,),
                ).fetchone()
        except sqlite3.Error as error:
            raise BrokerDatabaseError(
                "verified login identity could not be read"
            ) from error
        if row is None:
            return None
        try:
            return VerifiedLoginIdentity(
                game=str(row["game"]),
                fingerprint=str(row["identity_fingerprint"]),
                source=str(row["source"]),
                verified_at=float(row["verified_at"]),
                updated_at=float(row["updated_at"]),
            )
        except (TypeError, ValueError) as error:
            raise BrokerDatabaseError(
                "verified login identity state was invalid"
            ) from error

    def fingerprint(self, game: str, identity_digest: str) -> str:
        return login_identity_fingerprint(
            self.settings,
            game=game,
            identity_digest=identity_digest,
        )

    def matches(self, game: str, identity_digest: str) -> bool:
        state = self.read(game)
        if state is None:
            return False
        candidate = self.fingerprint(game, identity_digest)
        return hmac.compare_digest(state.fingerprint, candidate)

    def record_verified(self, game: str, identity_digest: str) -> bool:
        """Insert after exact Binding or prove the existing authority identical."""

        fingerprint = self.fingerprint(game, identity_digest)
        now = time.time()
        try:
            with self.database.immediate() as connection:
                row = connection.execute(
                    """
                    SELECT identity_fingerprint, source
                    FROM login_identity_state
                    WHERE game = ?
                    """,
                    (game,),
                ).fetchone()
                if row is None:
                    connection.execute(
                        """
                        INSERT INTO login_identity_state(
                            game, identity_fingerprint, source,
                            verified_at, updated_at
                        ) VALUES(?, ?, ?, ?, ?)
                        """,
                        (game, fingerprint, _SOURCE, now, now),
                    )
                    return True
                persisted = str(row["identity_fingerprint"])
                source = str(row["source"])
                if source != _SOURCE or not hmac.compare_digest(
                    persisted,
                    fingerprint,
                ):
                    raise ExpectedIdentityConflict(
                        "verified login identity conflicts with exact Binding"
                    )
                connection.execute(
                    """
                    UPDATE login_identity_state
                    SET verified_at = ?, updated_at = ?
                    WHERE game = ?
                    """,
                    (now, now, game),
                )
                return False
        except ExpectedIdentityConflict:
            raise
        except sqlite3.Error as error:
            raise BrokerDatabaseError(
                "verified login identity could not be recorded"
            ) from error
