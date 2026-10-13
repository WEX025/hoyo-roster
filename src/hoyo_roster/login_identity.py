"""Scope-local HMAC identity authority after exact official Binding."""

import hashlib
import hmac
import os
import re
from . import platform_io as io
from dataclasses import dataclass
from .config import Settings
from .errors import ExpectedIdentityConflict, ConfigurationError
from .storage.common import atomic_write_metadata, load_metadata


@dataclass(frozen=True, repr=False)
class VerifiedLoginIdentity:
    fingerprint: str


class LoginIdentityStore:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.path = settings.root / "verified-identity.json"
        self.key_path = settings.root / "identity.key"

    def _key(self) -> bytes:
        try:
            fd = io.open_private(self.key_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "wb") as stream:
                stream.write(os.urandom(32))
                stream.flush()
                io.sync_file(stream.fileno())
        try:
            descriptor = io.open_private(self.key_path, os.O_RDONLY)
            with os.fdopen(descriptor, "rb") as stream:
                key = stream.read()
        except (OSError, io.UnsafePath):
            raise ConfigurationError("Unsafe identity key") from None
        if len(key) != 32:
            raise ConfigurationError("Invalid identity key")
        return key

    def read(self, game: str) -> VerifiedLoginIdentity | None:
        if not self.path.exists():
            return None
        state = load_metadata(self.path, private=True)
        if (
            not isinstance(state, dict)
            or set(state) != {"game", "fingerprint"}
            or state["game"] != game
            or not isinstance(state["fingerprint"], str)
            or re.fullmatch("[0-9a-f]{64}", state["fingerprint"]) is None
        ):
            raise ConfigurationError("Invalid verified identity")
        return VerifiedLoginIdentity(state["fingerprint"])

    def fingerprint(self, game: str, identity_digest: str) -> str:
        return hmac.new(
            self._key(), f"login-identity:v1:{game}:{identity_digest}".encode(), hashlib.sha256
        ).hexdigest()

    def matches(self, game: str, identity_digest: str) -> bool:
        state = self.read(game)
        return state is not None and hmac.compare_digest(
            state.fingerprint, self.fingerprint(game, identity_digest)
        )

    def record_verified(self, game: str, identity_digest: str) -> bool:
        state = self.read(game)
        fingerprint = self.fingerprint(game, identity_digest)
        if state is not None:
            if not hmac.compare_digest(state.fingerprint, fingerprint):
                raise ExpectedIdentityConflict("Exact Binding identity changed")
            return False
        atomic_write_metadata(self.path, {"game": game, "fingerprint": fingerprint})
        return True
