"""Strict enough JSON and filesystem helpers for private state."""

from __future__ import annotations

import json
import os
from ... import platform_io as io
import stat
from pathlib import Path
from typing import Any


class StrictJsonError(ValueError):
    pass


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise StrictJsonError("duplicate JSON key")
        result[key] = value
    return result


def require_regular_file(path: Path) -> os.stat_result:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise StrictJsonError("expected a regular file")
    return metadata


def require_real_directory(path: Path) -> os.stat_result:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise StrictJsonError("expected a real directory")
    return metadata


def load_json(path: Path) -> Any:
    require_regular_file(path)
    try:
        return json.loads(
            path.read_bytes().decode("utf-8"),
            object_pairs_hook=_reject_duplicates,
            parse_constant=lambda value: (_ for _ in ()).throw(
                StrictJsonError(f"non-finite number: {value}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise StrictJsonError("invalid UTF-8 JSON") from error


def json_bytes(value: Any, *, sort_keys: bool = False) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                indent=2,
                sort_keys=sort_keys,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise StrictJsonError("value is not strict JSON") from error


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = io.temporary_file(path, f".{path.name}.")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(json_bytes(value))
            stream.flush()
            io.sync_file(stream.fileno())
        io.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
