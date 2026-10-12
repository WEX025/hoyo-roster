"""Typed parsers for untrusted decimal identifiers."""

from __future__ import annotations

import re

from .errors import ApiResponseError, MappingError, SyncError

MAX_DECIMAL_ID_LENGTH = 32
_CANONICAL_DECIMAL_RE = re.compile(r"^[1-9][0-9]*$")


def _parse_decimal_id(
    value: object,
    *,
    label: str,
    error_type: type[SyncError],
) -> str:
    if isinstance(value, bool):
        raise error_type(f"{label}: expected a canonical decimal ID")
    if isinstance(value, int):
        if value < 1 or value >= 10**MAX_DECIMAL_ID_LENGTH:
            raise error_type(f"{label}: expected a canonical decimal ID")
        return str(value)
    if (
        not isinstance(value, str)
        or len(value) > MAX_DECIMAL_ID_LENGTH
        or _CANONICAL_DECIMAL_RE.fullmatch(value) is None
    ):
        raise error_type(f"{label}: expected a canonical decimal ID")
    return value


def parse_role_uid(
    value: object,
    *,
    error_type: type[SyncError] = MappingError,
) -> str:
    return _parse_decimal_id(value, label="role UID", error_type=error_type)


def parse_character_id(
    value: object,
    *,
    error_type: type[SyncError] = MappingError,
) -> str:
    return _parse_decimal_id(value, label="character ID", error_type=error_type)


def parse_bangboo_id(
    value: object,
    *,
    error_type: type[SyncError] = MappingError,
) -> str:
    return _parse_decimal_id(value, label="Bangboo ID", error_type=error_type)


def parse_w_engine_id(
    value: object,
    *,
    error_type: type[SyncError] = MappingError,
) -> str:
    return _parse_decimal_id(value, label="W-Engine ID", error_type=error_type)


def parse_drive_disc_set_id(
    value: object,
    *,
    error_type: type[SyncError] = MappingError,
) -> str:
    return _parse_decimal_id(value, label="Drive Disc set ID", error_type=error_type)


def parse_api_character_id(value: object) -> str:
    return parse_character_id(value, error_type=ApiResponseError)
