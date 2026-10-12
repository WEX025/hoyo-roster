"""Machine-local HMAC authority for non-reversible Job parameter fingerprints."""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import os
import secrets
import stat
import uuid
from pathlib import Path

from .config import Settings
from .errors import ProtocolStateError
from .locks import ensure_private_directory
from .models import JobParameters

PARAMETER_HMAC_KEY_BYTES = 32
_EMPTY_PARAMETER_DOCUMENT = b"{}"


def _read_key(path: Path) -> bytes:
    try:
        metadata = path.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise ProtocolStateError("parameter HMAC key is unsafe")
        value = path.read_bytes()
    except ProtocolStateError:
        raise
    except OSError as error:
        raise ProtocolStateError("parameter HMAC key is unavailable") from error
    if len(value) != PARAMETER_HMAC_KEY_BYTES:
        raise ProtocolStateError("parameter HMAC key length is invalid")
    return value


def ensure_parameter_hmac_key(settings: Settings) -> bytes:
    """Create the key once without replacing an existing filesystem object."""

    ensure_private_directory(settings.broker_root)
    path = settings.parameter_hmac_key_file
    if os.path.lexists(path):
        return _read_key(path)

    value = secrets.token_bytes(PARAMETER_HMAC_KEY_BYTES)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            return _read_key(path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        persisted = _read_key(path)
        if not hmac.compare_digest(persisted, value):
            raise ProtocolStateError("parameter HMAC key creation lost its authority")
        return persisted
    except ProtocolStateError:
        raise
    except OSError as error:
        raise ProtocolStateError("parameter HMAC key could not be created") from error
    finally:
        if os.path.lexists(temporary):
            with contextlib.suppress(OSError):
                temporary.unlink()


def _fingerprint(settings: Settings, document: bytes) -> str:
    key = ensure_parameter_hmac_key(settings)
    return hmac.new(key, document, hashlib.sha256).hexdigest()


def parameter_fingerprint(settings: Settings, parameters: JobParameters) -> str:
    return _fingerprint(settings, parameters.canonical_bytes())


def empty_parameter_fingerprint(settings: Settings) -> str:
    return _fingerprint(settings, _EMPTY_PARAMETER_DOCUMENT)
