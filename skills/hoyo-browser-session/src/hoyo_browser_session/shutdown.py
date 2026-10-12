"""Owner shutdown marker and protocol-lock primitives."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from .config import Settings
from .errors import ProtocolStateError
from .locks import FileLock, ensure_private_file

_MARKER_KEYS = frozenset({"owner_id", "schema_version"})


@dataclass(frozen=True, slots=True)
class ShutdownMarker:
    owner_id: str


def protocol_lock(settings: Settings) -> FileLock:
    ensure_private_file(settings.protocol_lock_file)
    return FileLock(settings.protocol_lock_file)


@contextmanager
def locked_protocol(settings: Settings) -> Iterator[None]:
    lock = protocol_lock(settings)
    lock.acquire(blocking=True)
    try:
        yield
    finally:
        lock.release()


def read_shutdown_marker(settings: Settings) -> ShutdownMarker | None:
    path = settings.owner_shutdown_file
    if not os.path.lexists(path):
        return None
    safe = ensure_private_file(path)
    try:
        payload = json.loads(safe.read_text(encoding="ascii"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProtocolStateError("Owner shutdown marker is invalid") from error
    if not isinstance(payload, dict) or set(payload) != _MARKER_KEYS:
        raise ProtocolStateError("Owner shutdown marker fields are invalid")
    if payload.get("schema_version") != 1:
        raise ProtocolStateError("Owner shutdown marker version is invalid")
    owner_id = payload.get("owner_id")
    if not isinstance(owner_id, str):
        raise ProtocolStateError("Owner shutdown marker identity is invalid")
    try:
        canonical = str(uuid.UUID(owner_id))
    except ValueError as error:
        raise ProtocolStateError("Owner shutdown marker identity is invalid") from error
    if canonical != owner_id:
        raise ProtocolStateError("Owner shutdown marker identity is invalid")
    return ShutdownMarker(owner_id=owner_id)


def write_shutdown_marker(settings: Settings, *, owner_id: str) -> None:
    if read_shutdown_marker(settings) is not None:
        raise ProtocolStateError("Owner shutdown marker already exists")
    try:
        canonical = str(uuid.UUID(owner_id))
    except ValueError as error:
        raise ProtocolStateError("Owner shutdown identity is invalid") from error
    if canonical != owner_id:
        raise ProtocolStateError("Owner shutdown identity is invalid")
    path = settings.owner_shutdown_file
    temporary = path.with_name(f".{path.name}.tmp")
    if os.path.lexists(temporary):
        raise ProtocolStateError("Owner shutdown marker temporary residue exists")
    payload = {"owner_id": owner_id, "schema_version": 1}
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        ensure_private_file(path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        if os.path.lexists(temporary):
            os.unlink(temporary)
        raise


def remove_shutdown_marker(settings: Settings, *, owner_id: str) -> None:
    marker = read_shutdown_marker(settings)
    if marker is None or marker.owner_id != owner_id:
        raise ProtocolStateError("Owner shutdown marker authority is invalid")
    try:
        settings.owner_shutdown_file.unlink()
        directory = os.open(settings.broker_root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as error:
        raise ProtocolStateError("Owner shutdown marker cleanup failed") from error
