"""Strict JSON I/O for repository and lifecycle metadata."""

from __future__ import annotations

import json
import os
from ... import platform_io as io
import stat
from collections.abc import Callable, Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal


class StrictJsonError(ValueError):
    """Raised for JSON that is unsafe or outside the canonical encoding."""


def atomic_temporary_prefix(target_name: str) -> str:
    return f".{target_name}."


def is_atomic_temporary_name(name: str, target_name: str) -> bool:
    prefix = atomic_temporary_prefix(target_name)
    suffix = name.removeprefix(prefix)
    return (
        name.startswith(prefix)
        and bool(suffix)
        and all(character.isalnum() or character in {"_", "-"} for character in suffix)
    )


def _reject_constant(value: str) -> None:
    raise StrictJsonError(f"non-finite JSON number is forbidden: {value}")


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise StrictJsonError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def require_regular_file(path: Path) -> os.stat_result:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise StrictJsonError(f"expected a regular file: {path.name}")
    return metadata


def require_real_directory(path: Path) -> os.stat_result:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise StrictJsonError(f"expected a real directory: {path.name}")
    return metadata


def require_real_path_chain(
    trusted_root: Path,
    relative_path: Path,
    *,
    final_kind: Literal["directory", "regular_file"],
) -> Path:
    """Validate a trusted relative path component-by-component without following links."""

    try:
        root_metadata = trusted_root.lstat()
    except OSError as error:
        raise StrictJsonError("trusted root inspection failed") from error
    if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
        raise StrictJsonError("trusted root must be a real directory")

    parts = relative_path.parts
    if relative_path.is_absolute() or not parts or any(part in {"", ".", ".."} for part in parts):
        raise StrictJsonError("trusted relative path was invalid")
    if final_kind not in {"directory", "regular_file"}:
        raise StrictJsonError("trusted path final kind was invalid")

    current = trusted_root
    for index, component in enumerate(parts):
        current = current / component
        try:
            metadata = current.lstat()
        except OSError as error:
            raise StrictJsonError("trusted path inspection failed") from error
        is_final = index == len(parts) - 1
        if not is_final:
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise StrictJsonError(
                    "trusted path directory path component was not a real directory"
                )
            continue
        if final_kind == "directory":
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise StrictJsonError("trusted path target was not a real directory")
        elif stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise StrictJsonError("trusted path target was not a regular file")

    try:
        resolved_root = trusted_root.resolve(strict=True)
        resolved_target = current.resolve(strict=True)
    except OSError as error:
        raise StrictJsonError("trusted path resolution failed") from error
    if resolved_target == resolved_root or not resolved_target.is_relative_to(resolved_root):
        raise StrictJsonError("trusted path escaped its root")
    return resolved_target


def load_json(path: Path) -> Any:
    require_regular_file(path)
    try:
        text = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as error:
        raise StrictJsonError(f"JSON is not UTF-8: {path.name}") from error
    try:
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicates,
            parse_constant=_reject_constant,
            parse_float=Decimal,
        )
    except json.JSONDecodeError as error:
        raise StrictJsonError(f"invalid JSON: {path.name}") from error


def _decimal_json_token(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("non-finite Decimal is forbidden")
    sign, digits_tuple, raw_exponent = value.as_tuple()
    if not isinstance(raw_exponent, int):
        raise ValueError("non-finite Decimal exponent is forbidden")
    exponent = raw_exponent
    digits = list(digits_tuple)
    if not any(digits):
        return "0"
    while digits[-1] == 0:
        digits.pop()
        exponent += 1
    coefficient = "".join(str(digit) for digit in digits)
    if exponent >= 0:
        token = coefficient + ("0" * exponent)
    else:
        point = len(coefficient) + exponent
        if point > 0:
            token = f"{coefficient[:point]}.{coefficient[point:]}"
        else:
            token = f"0.{('0' * -point)}{coefficient}"
    return f"-{token}" if sign else token


def _encode_json(
    value: Any,
    *,
    indent: int | None,
    level: int,
    sort_keys: bool,
) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, Decimal):
        return _decimal_json_token(value)
    if isinstance(value, float):
        return json.dumps(value, allow_nan=False)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, Mapping):
        items: list[tuple[str, Any]] = []
        for key, child in value.items():
            if not isinstance(key, str):
                raise TypeError("JSON object keys must be strings")
            items.append((key, child))
        if sort_keys:
            items.sort(key=lambda item: item[0])
        if not items:
            return "{}"
        if indent is None:
            members = (
                f"{json.dumps(key, ensure_ascii=False)}:"
                f"{_encode_json(child, indent=None, level=level + 1, sort_keys=sort_keys)}"
                for key, child in items
            )
            return "{" + ",".join(members) + "}"
        child_prefix = " " * (indent * (level + 1))
        closing_prefix = " " * (indent * level)
        members = (
            f"{child_prefix}{json.dumps(key, ensure_ascii=False)}: "
            f"{_encode_json(child, indent=indent, level=level + 1, sort_keys=sort_keys)}"
            for key, child in items
        )
        return "{\n" + ",\n".join(members) + f"\n{closing_prefix}}}"
    if isinstance(value, list | tuple):
        if not value:
            return "[]"
        if indent is None:
            members = (
                _encode_json(child, indent=None, level=level + 1, sort_keys=sort_keys)
                for child in value
            )
            return "[" + ",".join(members) + "]"
        child_prefix = " " * (indent * (level + 1))
        closing_prefix = " " * (indent * level)
        members = (
            child_prefix + _encode_json(child, indent=indent, level=level + 1, sort_keys=sort_keys)
            for child in value
        )
        return "[\n" + ",\n".join(members) + f"\n{closing_prefix}]"
    raise TypeError(f"unsupported strict JSON value: {type(value).__name__}")


def _strict_json_bytes(
    value: Any,
    *,
    indent: int | None,
    sort_keys: bool,
    trailing_newline: bool,
) -> bytes:
    try:
        text = _encode_json(
            value,
            indent=indent,
            level=0,
            sort_keys=sort_keys,
        )
    except (TypeError, ValueError) as error:
        raise StrictJsonError("value cannot be encoded as strict JSON") from error
    if trailing_newline:
        text += "\n"
    return text.encode("utf-8")


def canonical_json_bytes(value: Any) -> bytes:
    return _strict_json_bytes(
        value,
        indent=2,
        sort_keys=False,
        trailing_newline=True,
    )


def canonical_compact_json_bytes(
    value: Any,
    *,
    sort_keys: bool = False,
) -> bytes:
    return _strict_json_bytes(
        value,
        indent=None,
        sort_keys=sort_keys,
        trailing_newline=False,
    )


def atomic_write_json(
    path: Path,
    value: Any,
    *,
    mode: int = 0o600,
    before_replace: Callable[[Path], None] | None = None,
) -> None:
    if mode != 0o600:
        raise ValueError("private JSON files require owner-only mode")
    data = canonical_json_bytes(value)
    require_real_directory(path.parent)
    descriptor, temporary = io.temporary_file(path, atomic_temporary_prefix(path.name))
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            io.sync_file(output.fileno())
        if before_replace is not None:
            before_replace(temporary)
        io.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        finally:
            raise
